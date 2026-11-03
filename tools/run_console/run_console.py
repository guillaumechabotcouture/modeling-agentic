#!/usr/bin/env python3
"""The run console: a local page for the owner of a v2 run (docs/design/v2-run-console.md).

    python3 tools/run_console/run_console.py --run-dir runs/<run>                    # one local run
    python3 tools/run_console/run_console.py --roots runs ~/keep                     # every run under these roots
    python3 tools/run_console/run_console.py --mirror <dir> --sync-cmd "<command>" --cloud-config cloud/configs/<x>.py
    python3 tools/run_console/run_console.py --self-test

Serves one page (tools/run_console/run_console.html and its script) on 127.0.0.1 only. It reads run artifacts and
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
import csv
import datetime as _dt
import glob
import hashlib
import hmac
import http.server
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

import yaml

# tools/run_console/, outside scripts/ and toolkit/: compute.code_fingerprint hashes those whole, so a console edit
# would invalidate every stored engine batch (review item 17). The console imports the harness by path.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for _p in (REPO, os.path.join(REPO, "scripts"), os.path.join(REPO, "agents"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# dossier.py (the Spec, Evidence, Engine and Calibration tabs) imports this module by name; when the console runs as a
# script, that name must be this module, not a second copy.
sys.modules.setdefault("run_console", sys.modules[__name__])
from lib.safe_io import LinkRefused, dir_fd_nofollow, read_nofollow  # noqa: E402
from lib.safe_io import write_nofollow as _safe_write  # noqa: E402

HTML = os.path.join(HERE, "run_console.html")
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
# The refusal fallback (fallbacks: "default") runs Claude Opus 5 or Claude Opus 4.8: $5 / $25 per MTok, 5-minute
# cache writes 1.25x (claude-api skill price table). The worst case of a message counts both models.
FALLBACK_PRICE = {"in": 5.0, "out": 25.0, "cache_write": 6.25, "cache_read": 0.50}
HISTORY_TURNS = 12
# Character budgets for the run context (each part says when it was cut).
BUDGET = {"results": 12000, "gate": 3000, "blockers": 6000, "tests": 3000, "timeline": 3000, "owner": 3000}
OPTIONAL_STATES = ("FIX_LOOP", "OWNER_DECISION", "PRESENTATION")   # the stepper hides them when never entered
STALE_S = 1800.0               # a progress record not updated for this long is not live
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


_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_CACHE: dict = {}              # path -> entry; the page polls, files rarely change (see _load_rel)
_CACHE_MAX = 2000
_KEEP_BYTES = 2_000_000        # an entry keeps its bytes up to this size (evidence excerpts, served files)
_MAX_READ = 64_000_000
# Never read, whatever the path: credential files (the run's .env and the like).
_CREDENTIAL = re.compile(r"^(?:\.env(?:\..*)?|\.netrc|id_(?:rsa|ed25519|ecdsa).*|.*\.(?:pem|key|p12)|credentials.*)$", re.I)


def check_rel(rel: str) -> str:
    """`rel` if it is a plain relative path inside a run dir and not a credential file; else ValueError. Run
    artifacts are written by agents, so a path from an artifact is untrusted."""
    rel = str(rel or "")
    parts = rel.replace("\\", "/").split("/")
    if not rel or os.path.isabs(rel) or "\0" in rel or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"{rel[:120]!r}: not a relative path inside the run directory")
    if any(_CREDENTIAL.match(x) for x in parts):
        raise ValueError(f"{rel[:120]}: credential files are not read")
    return rel


def confined(run_dir: str, rel: str) -> str:
    """The path of `rel` under the run dir, after check_rel. Naming only: every read opens through read_nofollow
    (no link is followed at any component, so a check cannot race the open)."""
    return os.path.join(os.path.realpath(run_dir), check_rel(rel))


def _parse(rel: str, data: bytes):
    try:
        raw = data.decode("utf-8")
        if rel.endswith(".json"):
            return json.loads(raw), None
        if rel.endswith((".yaml", ".yml")):
            return yaml.load(raw, Loader=_LOADER), None
        return raw, None
    except (UnicodeDecodeError, ValueError, yaml.YAMLError) as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"


def _load_rel(run_dir: str, rel: str) -> dict:
    """One artifact, read once through an fd opened with no link followed (lib.safe_io.read_nofollow):
    {parsed, err, sha256, data (when small), size}. Parsed value and sha256 come from the same bytes. Cached by
    the file's mtime, size, inode and ctime; a cache hit needs the same inode as the no-follow read, so a path
    swapped for a link (another inode) is read, and refused, again."""
    try:
        check_rel(rel)
    except ValueError as e:
        return {"parsed": None, "err": str(e), "sha256": None}
    root = os.path.realpath(run_dir)
    path = os.path.join(root, rel)
    try:
        st = os.lstat(path)
    except OSError as e:
        return {"parsed": None, "err": f"{type(e).__name__}: {e.strerror}", "sha256": None}
    key = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
    hit = _CACHE.get(("rel", path))
    if hit and hit[0] == key:
        return hit[1]
    try:
        data, fst = read_nofollow(root, rel, _MAX_READ)
    except (LinkRefused, OSError) as e:
        return {"parsed": None, "err": f"not read: {e}", "sha256": None}
    parsed, err = _parse(rel, data)
    entry = {"parsed": parsed, "err": err, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
             "data": data if len(data) <= _KEEP_BYTES else None}
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[("rel", path)] = ((fst.st_mtime_ns, fst.st_size, fst.st_ino, fst.st_ctime_ns), entry)
    return entry


def _rread(run_dir: str, rel: str):
    """(parsed, None) or (None, error) for a run artifact (_load_rel)."""
    e = _load_rel(run_dir, rel)
    return (e["parsed"], None) if e["err"] is None else (None, e["err"])


def _rexists(run_dir: str, rel: str) -> bool:
    """The entry exists (a link too, so that a linked artifact is read, and refused, not skipped)."""
    return os.path.lexists(os.path.join(run_dir, rel))


def _read(path: str):
    """(parsed, None) or (None, error) of a plain path (the repo's own files and the self-test), cached by mtime,
    size, inode and ctime. Run artifacts go through _rread."""
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size, st.st_ino, st.st_ctime_ns)
        hit = _CACHE.get(path)
        if hit and hit[0] == key:
            return hit[1]
        with open(path, "rb") as f:
            out = _parse(path, f.read())
    except OSError as e:
        return None, f"{type(e).__name__}: {str(e)[:200]}"
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[path] = (key, out)
    return out


def read_jsonl_rel(run_dir: str, rel: str) -> tuple[list, list]:
    """(rows, errors) of a JSON-lines artifact read through read_nofollow; a torn or non-object line is an error."""
    if not _rexists(run_dir, rel):
        return [], []
    try:
        data = read_nofollow(os.path.realpath(run_dir), check_rel(rel), _MAX_READ)[0].decode("utf-8")
    except (ValueError, OSError, UnicodeDecodeError) as e:
        return [], [f"{os.path.basename(rel)}: {e}"]
    return _jsonl_rows(data, os.path.basename(rel))


def _jsonl_rows(text: str, name: str) -> tuple[list, list]:
    rows, errs = [], []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            if not isinstance(r, dict):
                raise ValueError("not an object")
            rows.append(r)
        except ValueError as e:
            errs.append(f"{name} line {n}: {e}")
    return rows, errs


def read_jsonl(path: str) -> tuple[list, list]:
    """(rows, errors) of a plain path."""
    if not os.path.exists(path):
        return [], []
    try:
        with open(path, encoding="utf-8") as f:
            return _jsonl_rows(f.read(), os.path.basename(path))
    except (OSError, UnicodeDecodeError) as e:
        return [], [f"{os.path.basename(path)}: {e}"]


def _err_card(run_dir: str, path: str, stage: str, err: str) -> dict:
    rel = _rel(run_dir, path)
    return {"id": f"error:{rel}", "kind": "error", "stage": stage, "at": _mtime(path),
            "title": f"{rel} cannot be read", "body": err, "anchor": {"kind": "file", "id": rel, "file": rel}}


def _mtime(path: str) -> str:
    try:
        return _iso(os.lstat(path).st_mtime)
    except OSError:
        return ""


def read_file_bytes(run_dir: str, rel: str, max_bytes: int = 4_000_000) -> bytes:
    """A run file's bytes for /api/file, through read_nofollow; ValueError when it cannot be served."""
    try:
        return read_nofollow(os.path.realpath(run_dir), check_rel(rel), max_bytes)[0]
    except (LinkRefused, OSError) as e:
        raise ValueError(f"{rel}: not a plain file in the run directory ({e})") from None


def safe_path(run_dir: str, rel: str) -> str:
    """The run file's path after the checks of read_file_bytes (a read through read_nofollow); kept for callers
    that name a file. Reading from the returned path is not safe: use read_file_bytes."""
    read_file_bytes(run_dir, rel, 0)
    return confined(run_dir, rel)


# ------------------------------------------------------------------ writes: never through a link (lib.safe_io)
def write_nofollow(base: str, rel: str, data: bytes, append: bool = False, exclusive: bool = False) -> str:
    try:
        return _safe_write(base, rel, data, append=append, exclusive=exclusive)
    except LinkRefused as e:
        raise ValueError(str(e)) from None


def _dir_fd(base: str, parts) -> int:
    try:
        return dir_fd_nofollow(base, parts, create=True)
    except LinkRefused as e:
        raise ValueError(str(e)) from None


def states() -> tuple:
    from agents import stage_machine
    return tuple(stage_machine.STATES)


# ------------------------------------------------------------------ state, stepper, cards
def load_run_state(run_dir: str):
    """(state dict, None) or (None, error): run_state.json read through _load_rel and checked as stage_machine's
    load_state checks it (a known state), without the harness opening the file."""
    if not _rexists(run_dir, "run_state.json"):
        return None, "no run_state.json"
    doc, err = _rread(run_dir, "run_state.json")
    if err or not isinstance(doc, dict):
        return None, f"run_state.json unreadable: {err or 'not a mapping'}"
    try:
        from agents import stage_machine
        st = stage_machine.RunState(**{k: v for k, v in doc.items() if k in stage_machine.RunState.__dataclass_fields__})
        if st.state not in stage_machine.STATES:
            raise ValueError(f"unknown state {st.state!r}")
        return st.to_dict(), None
    except Exception as e:  # noqa: BLE001 -- fail closed: shown on the page
        return None, f"run_state.json unreadable: {type(e).__name__}: {str(e)[:200]}"


def stepper(st: dict) -> dict:
    order = states()
    hist = [h for h in st.get("history") or [] if isinstance(h, dict) and "to" in h]
    entered = {h["to"] for h in hist} | {"SPEC"}
    left = {h.get("from") for h in hist}
    cur = st.get("state")
    ci = order.index(cur) if cur in order else -1
    failed_from = hist[-1].get("from") if cur == "FINAL" and st.get("status") in FAIL_STATUSES and hist else None
    out = []
    for i, s in enumerate(order):
        why = None
        if s == cur:
            status = "waiting" if st.get("paused") else "current"
        elif s == failed_from:
            status, why = "failed", hist[-1].get("why")
        elif s in left or s in entered:
            status = "done"
        elif i < ci:
            status = "skipped"
        else:
            status = "todo"
        if s in OPTIONAL_STATES and status in ("todo", "skipped"):
            continue
        out.append({"state": s, "status": status, **({"why": why} if why else {})})
    return {"stages": out, "round": st.get("round"), "max_rounds": st.get("max_rounds"),
            "fix_rounds": sum(1 for h in hist if h["to"] == "FIX_LOOP"), "paused": st.get("paused"),
            "status": st.get("status")}


def _words(v) -> str:
    """A mapping or list in plain words ("fixed: T-001, T-002 · rejected: none"), never JSON."""
    if isinstance(v, dict):
        return " · ".join(f"{str(k).replace('_', ' ')}: {_words(x) if x not in (None, [], {}) else 'none'}"
                          for k, x in v.items())
    if isinstance(v, (list, tuple)):
        return ", ".join(_words(x) for x in v)
    return str(v)


def timeline_cards(st: dict) -> list:
    out = []
    for i, h in enumerate(st.get("history") or []):
        if not isinstance(h, dict):
            continue
        if "to" in h:
            out.append({"id": f"stage:{i}", "kind": "stage", "stage": h["to"], "at": h.get("at", ""),
                        "title": f"{h.get('from')} → {h['to']}", "body": h.get("why", ""),
                        "round": h.get("round"), "spent_usd": h.get("spent_usd"),
                        "anchor": {"kind": "stage", "id": h["to"], "file": "run_state.json"}})
        elif "fix_loop" in h:
            out.append({"id": f"fix:{i}", "kind": "fix_loop", "stage": "FIX_LOOP", "at": h.get("at", ""),
                        "title": "fix loop", "body": _words(h["fix_loop"])[:800],
                        "anchor": {"kind": "stage", "id": "FIX_LOOP", "file": "run_state.json"}})
    return out


def critique_cards(run_dir: str) -> list:
    out = []
    for p in sorted(glob.glob(os.path.join(run_dir, "critique_*.yaml"))):
        rel = _rel(run_dir, p)
        ent = _load_rel(run_dir, rel)
        doc, err = ent["parsed"], ent["err"]
        if err or not isinstance(doc, dict) or not isinstance(doc.get("blockers", []), list):
            out.append(_err_card(run_dir, p, "CRITIQUE", err or "not a mapping with a `blockers` list"))
            continue
        for b in doc.get("blockers") or []:
            if not isinstance(b, dict):
                out.append(_err_card(run_dir, p, "CRITIQUE", f"a blocker is not a mapping: {str(b)[:80]}"))
                continue
            bid = str(b.get("id") or "?")
            out.append({"id": f"blocker:{rel}#{bid}", "kind": "blocker", "stage": "CRITIQUE", "at": _mtime(p),
                        "title": f"{doc.get('reviewer') or rel} raised {bid}",
                        "reviewer": doc.get("reviewer"), "round": doc.get("round"), "blocker_id": bid,
                        **{k: b.get(k) for k in ("severity", "category", "target_stage", "claim", "fix_requires",
                                                 "done_when", "resolved", "first_seen_round", "evidence")},
                        "anchor": {"kind": "blocker", "id": bid, "file": rel},
                        "snapshot": sealed({"run": os.path.realpath(run_dir), "file": rel, "anchor_id": bid,
                                            "sha256": ent["sha256"], "round": doc.get("round"),
                                            "excerpt": _blocker_excerpt(b), "at": _now(), "captured": "render"})})
    return out


def gate_cards(run_dir: str) -> list:
    out = []
    for p in sorted(glob.glob(os.path.join(run_dir, "gate_r*.json"))):
        rel = _rel(run_dir, p)
        ent = _load_rel(run_dir, rel)
        doc, err = ent["parsed"], ent["err"]
        if err or not isinstance(doc, dict) or "action" not in doc:
            out.append(_err_card(run_dir, p, "GATE", err or "no `action` in the gate record"))
            continue
        m = re.match(r"gate_r(\d+)", os.path.basename(p))
        uh = doc.get("unresolved_high") or []
        out.append({"id": f"gate:{rel}", "kind": "gate", "stage": "GATE", "at": _mtime(p),
                    "title": f"gate round {m.group(1) if m else '?'}"
                             f"{' (post-write)' if doc.get('post_write') else ''}: {doc.get('action')}",
                    "action": doc.get("action"), "rule": doc.get("rule_matched"), "body": doc.get("rationale", ""),
                    "unresolved_high": [str(u.get("claim") if isinstance(u, dict) else u)[:300] for u in uh],
                    "done_when_status": doc.get("done_when_status") or {},
                    "done_when": done_when_rows(run_dir, doc.get("done_when_status") or {}),
                    "anchor": {"kind": "gate", "id": "action", "file": rel},
                    "snapshot": sealed({"run": os.path.realpath(run_dir), "file": rel, "anchor_id": "action",
                                        "sha256": ent["sha256"], "round": int(m.group(1)) if m else None,
                                        "excerpt": f"{doc.get('action')} (rule {doc.get('rule_matched')}): "
                                                   f"{str(doc.get('rationale') or '')[:400]}", "at": _now(),
                                        "captured": "render"})})
    return out


def _alternatives(pattern: str) -> list:
    """A regex's top-level alternatives as plain phrases: "(?i)status[- ]quo|business[- ]as[- ]usual" ->
    ["status quo", "business as usual"]. Groups and classes are kept whole, then stripped of regex syntax."""
    p, parts, depth, cur, cls = str(pattern).replace("(?i)", ""), [], 0, "", False
    for ch in p:
        if ch == "[" and not cls:
            cls = True
        elif ch == "]" and cls:
            cls = False
        elif not cls and ch == "(":
            depth += 1
        elif not cls and ch == ")":
            depth -= 1
        if ch == "|" and depth == 0 and not cls:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    out = []
    for x in parts:
        x = re.sub(r"\[[^\]]*\]", " ", x)                 # [- ] and the like read as a space
        x = re.sub(r"\.\{\d*,?\d*\}|\.\*|\.\+", " … ", x)   # .{0,80} reads as "…"
        x = re.sub(r"\\[bBdswDSW]|[\\^$?*+{}]|\d+,\d*", "", x)
        x = re.sub(r"(?<=[A-Za-z])\.(?=[A-Za-z])", " ", x)       # case.management: "." matches the space
        while re.search(r"\(([^()]*)\)", x):                    # inner groups first: (ly) -> ly, (a|b) -> a/b
            x = re.sub(r"\(([^()]*)\)", lambda m: m.group(1).replace("|", "/"), x)
        x = x.replace("|", "/")
        x = re.sub(r"\s+", " ", x).strip(" …")
        if x:
            out.append(x)
    return out


def _check_words(c) -> str:
    """One done-when check in words: "manuscript.md mentions one of: status quo, business as usual",
    "judged: ..."."""
    if not isinstance(c, dict):
        return str(c)
    if "judgement" in c:
        return f"judged: {c['judgement']}"
    f = c.get("file", "?")
    for k in ("contains", "lacks"):
        if k in c:
            alts = _alternatives(c[k]) or [str(c[k])]
            verb = "mentions" if k == "contains" else "does not mention"
            return (f"{f} {verb} {alts[0]}" if len(alts) == 1 else
                    f"{f} {verb} {'one' if k == 'contains' else 'any'} of: {', '.join(alts)}")
    if c.get("exists"):
        return f"{f} exists"
    if "key" in c:
        return f"{f}: {c['key']} {c.get('op', '=')} {c.get('value')}"
    return _words(c)


def done_when_rows(run_dir: str, status: dict) -> list:
    """The gate's done-when status joined with each blocker's done_when checks (critique_*.yaml):
    [{blocker, reviewer, severity, checks: [words], passes, detail, mechanical}]."""
    specs = {}
    for c in critique_cards(run_dir):
        if c["kind"] == "blocker":
            specs[c["blocker_id"]] = c
    rows = []
    for bid, st in sorted((status or {}).items()):
        st = st if isinstance(st, dict) else {"detail": str(st)}
        b = specs.get(bid) or {}
        dw = b.get("done_when")
        rows.append({"blocker": bid, "reviewer": b.get("reviewer"), "severity": b.get("severity"),
                     "claim": b.get("claim"), "checks": [_check_words(x) for x in (dw if isinstance(dw, list) else
                                                                                [dw] if dw else [])],
                     "raw": [(x.get("contains") or x.get("lacks") or "") if isinstance(x, dict) else ""
                             for x in (dw if isinstance(dw, list) else [dw] if dw else [])],
                     "passes": st.get("passes"), "detail": st.get("detail"), "mechanical": st.get("mechanical")})
    return rows


def figure_cards(run_dir: str) -> list:
    caps, out = {}, []
    fy = os.path.join(run_dir, "analysis", "figures.yaml")
    if _rexists(run_dir, "analysis/figures.yaml"):
        doc, err = _rread(run_dir, "analysis/figures.yaml")
        if err or not isinstance(doc, dict):
            out.append(_err_card(run_dir, fy, "ANALYZE", err or "not a mapping"))
        else:
            for f in ((doc.get("data") or {}).get("figures") or []):
                if isinstance(f, dict) and f.get("path"):
                    caps[f["path"]] = f.get("shows", "")
    for p in sorted(glob.glob(os.path.join(run_dir, "figures", "*.png"))):
        rel = _rel(run_dir, p)
        card = {"id": f"figure:{rel}", "kind": "figure", "stage": "ANALYZE", "at": _mtime(p), "title": rel,
                "body": caps.get(rel, ""), "file": rel, "anchor": {"kind": "figure", "id": rel, "file": rel}}
        card["snapshot"] = sealed({"run": os.path.realpath(run_dir), "file": rel, "anchor_id": rel,
                                   "sha256": _sha_rel(run_dir, rel), "round": None,
                                   "excerpt": caps.get(rel) or "(no caption recorded)", "at": _now(),
                                   "captured": "render"})
        prov = p + ".provenance.json"
        if os.path.lexists(prov):
            doc, err = _rread(run_dir, rel + ".provenance.json")
            if err or not isinstance(doc, dict):
                card["provenance_error"] = err or "not a mapping"
            else:
                card["at"] = doc.get("written_at_utc") or card["at"]
                card["producer"] = doc.get("producer")
                card["sources"] = sorted(os.path.basename(s) for s in (doc.get("source_hashes") or {}))
        out.append(card)
    return out


DT_STATES = {"changed": "changes the plan", "unchanged": "does not change the plan", "not_run": "not run",
             "error": "result cannot be read"}


def decision_test_cards(run_dir: str) -> list:
    """One card per decision test, with one of four states: changed, unchanged, not_run, error. A NOT_RUN test
    or an unreadable result is never read as "no change"."""
    p = os.path.join(run_dir, "analysis", "decision_tests.yaml")
    if not _rexists(run_dir, "analysis/decision_tests.yaml"):
        return []
    doc, err = _rread(run_dir, "analysis/decision_tests.yaml")
    tests = ((doc or {}).get("data") or {}).get("tests") if isinstance(doc, dict) else None
    if err or not isinstance(tests, list):
        return [_err_card(run_dir, p, "ANALYZE", err or "no data.tests list")]
    units = {str(x.get("name")): x.get("units") for x in (_data(run_dir, "analysis/engine.yaml").get("parameters") or [])
             if isinstance(x, dict)}
    spec, _ = _rread(run_dir, "decision_spec.yaml")
    spec_tests = {str(x.get("id")): x for x in (_dig(spec, "analysis.decision_tests") or []) if isinstance(x, dict)}
    out = []
    for t in tests:
        if not isinstance(t, dict):
            continue
        tid, f = str(t.get("test_id") or "?"), t.get("path") or "analysis/decision_tests.yaml"
        q, qerr = (_rread(run_dir, str(t["path"])) if t.get("path") else ({}, None))
        if qerr is None and not isinstance(q, dict):
            qerr = "not a mapping"
        q = q if isinstance(q, dict) else {}
        v = t.get("verdict")
        state = ("error" if qerr else "not_run" if v == "NOT_RUN" or q.get("status") == "not_run"
                 else "changed" if v == "CHANGES_PLAN" or q.get("decision_changes") is True
                 else "unchanged" if v == "NO_CHANGE" or (v is None and q.get("decision_changes") is False)
                 else "error")
        why = qerr or (f"unknown verdict {v!r}" if state == "error" else None)
        changes = state == "changed"
        param = (spec_tests.get(tid) or {}).get("parameter") or q.get("parameter") or (
            tid[len("prior_range_"):] if tid.startswith("prior_range_") else None)
        res = [r for r in q.get("results") or [] if isinstance(r, dict)]
        rel = []
        for r in res:
            a, b = r.get("objective"), r.get("point_plan_objective_under_test")
            rel.append(round(100 * (a - b) / b, 2) if isinstance(a, (int, float)) and isinstance(b, (int, float)) and b
                       else None)
        share = t.get("share_pct")
        if share is None and isinstance(q.get("share_units_changed"), (int, float)):
            share = 100 * q["share_units_changed"]
        thr = t.get("threshold_pct")
        if thr is None and isinstance(q.get("share_threshold"), (int, float)):
            thr = 100 * q["share_threshold"]
        out.append({"id": f"dt:{tid}", "kind": "decision_test", "stage": "ANALYZE", "at": _mtime(p),
                    "title": f"{tid}: {DT_STATES[state]}" + (f" ({why})" if why else ""),
                    "body": (spec_tests.get(tid) or {}).get("text") or q.get("question") or t.get("statement", ""),
                    "verdict": v, "state": state, "why": why, "changes": changes, "test_id": tid,
                    "parameter": param, "unit": units.get(param) if param else None,
                    "values": [r.get("value") for r in res], "share_pct": share, "threshold_pct": thr,
                    "objective_vs_point_pct": rel, "criterion": q.get("criterion"),
                    "recommendation": q.get("recommendation"),
                    "anchor": {"kind": "decision_test", "id": tid, "file": f}})
    return out


def _meta(run_dir: str) -> dict:
    meta, _ = _rread(run_dir, "metadata.json")
    return meta if isinstance(meta, dict) else {}


def _last_at(st: dict | None) -> str:
    return max((h.get("at") or "" for h in (st or {}).get("history") or [] if isinstance(h, dict)), default="")


def _meta_end(run_dir: str, st: dict | None) -> dict:
    """metadata.json's status and finished, unless the run moved on after it (a resumed copy keeps the old
    metadata: its history has transitions after `finished`)."""
    m = _meta(run_dir)
    return m if m.get("finished") and str(m["finished"]) >= _last_at(st) else {}


def run_status(run_dir: str, st: dict | None) -> str | None:
    """run_state's status, else the one main.py recorded in metadata.json when the run ended."""
    return (st or {}).get("status") or _meta_end(run_dir, st).get("status")


def finished(run_dir: str, st: dict | None) -> bool:
    """The run has ended: FINAL, a terminal status, or metadata.json says it finished after its last step."""
    return (st or {}).get("state") == "FINAL" or bool(run_status(run_dir, st)) or bool(_meta_end(run_dir, st))


def compute_status(run_dir: str, cap_core_hours: float | None = None, st: dict | None = None,
                   now: float | None = None) -> dict:
    """Core-hours used vs cap and each call in flight (ledger reservations joined with their progress).
    A call is live only while the run is unfinished and its progress record is fresh; otherwise it was
    interrupted and has no ETA. Used = the ledger's settled core_seconds plus live calls' core_s."""
    import compute as C
    ended, now = finished(run_dir, st), time.time() if now is None else now
    cap = float(cap_core_hours if cap_core_hours is not None else C.RUN_CAP_CORE_HOURS)
    out = {"cap_core_h": cap, "cap_source": "--core-hours-cap" if cap_core_hours is not None
           else "compute.RUN_CAP_CORE_HOURS (default; the run's MA_COMPUTE_MAX_CORE_HOURS is not recorded)",
           "used_core_h": 0.0, "calls": [], "done_calls": 0, "live": False}
    if not _rexists(run_dir, C.LEDGER_REL):
        return out
    led, err = _rread(run_dir, C.LEDGER_REL)
    if err or not isinstance(led, dict):
        out["error"] = f"{C.LEDGER_REL}: {err or 'not a mapping'}"
        return out
    used = float(led.get("core_seconds") or 0)
    out["done_calls"] = len(led.get("calls") or [])
    for rid, r in sorted((led.get("reservations") or {}).items()):
        if not isinstance(r, dict):
            continue
        call = {"fn": r.get("fn"), "purpose": r.get("purpose"), "n": r.get("n"), "done": None, "core_s": None,
                "eta_s": None, "started": r.get("started"), "live": False}
        prog_rel = f"{C.PROGRESS_DIR}/{os.path.basename(str(r.get('progress') or ''))}"
        if r.get("progress") and os.path.lexists(os.path.join(run_dir, prog_rel)):
            rec, perr = _rread(run_dir, prog_rel)
            if perr or not isinstance(rec, dict):
                call["error"] = f"progress record: {perr or 'not a mapping'}"
            else:
                call["done"], call["core_s"] = int(rec.get("done_tasks") or 0), rec.get("core_s")
                call["live"] = not ended and now - float(rec.get("t") or 0) < STALE_S
                el = float(rec.get("t") or 0) - float(r.get("started") or 0)
                if call["live"]:
                    used += float(rec.get("core_s") or 0)
                    if call["done"] and r.get("n") and el > 0:
                        call["eta_s"] = round((int(r["n"]) - call["done"]) * el / call["done"])
        else:
            call["error"] = "no progress record"
        out["calls"].append(call)
    out["used_core_h"], out["live"] = round(used / 3600, 2), any(c["live"] for c in out["calls"])
    return out


SPEC, SPEC_SIGNOFF, BRIEF_SIGNOFF = "decision_spec.yaml", "decision_spec.signoff.json", "decision_brief.signoff.json"
DERIV_RECORD, SHEET = "data/derivations_record.json", "decision_spec.signoff_sheet.md"


def spec_view(run_dir: str) -> dict:
    """The console's own read model of the spec sign-off: it parses JSON and YAML it reads through _load_rel and
    never runs the harness's loaders (toolkit/spec/schema.load can instantiate the engine a spec names; review
    round 2, P1-1 and P1-4). status: absent | awaiting | signed | changed | (None with an error). "awaiting"
    does not say the spec is valid: the sign command validates it and refuses an invalid one. With it, the approval
    snapshot: the sheet's text and sha256, the spec's sha256 and the derivation record's ("none" when absent), each
    from one read, so the card shows exactly what the signer is told to check."""
    spec = _load_rel(run_dir, SPEC) if _rexists(run_dir, SPEC) else None
    if spec is not None and spec["sha256"] is None:
        return {"status": None, "error": f"{SPEC}: {spec['err']}"}
    rec = None
    for name in (BRIEF_SIGNOFF, SPEC_SIGNOFF):
        if _rexists(run_dir, name):
            doc, err = _rread(run_dir, name)
            if err or not isinstance(doc, dict):
                return {"status": None, "error": f"{name}: {err or 'not a mapping'}"}
            if (doc.get("sha256") or {}).get(SPEC) is not None or name == SPEC_SIGNOFF:
                rec = doc
                break
    deriv = _load_rel(run_dir, DERIV_RECORD) if _rexists(run_dir, DERIV_RECORD) else None
    if deriv is not None and deriv["sha256"] is None:
        return {"status": None, "error": f"{DERIV_RECORD}: {deriv['err']}"}
    dsha = deriv["sha256"] if deriv else None
    if spec is None:
        return {"status": "changed" if rec is not None else "absent", "error": None}
    if rec is not None:
        shas = rec.get("sha256") or {}
        same = spec["sha256"] == shas.get(SPEC) and dsha == shas.get(DERIV_RECORD)
        if same and deriv is not None:
            outs = [o for e in ((deriv["parsed"] or {}).get("derivations") or {}).values() if isinstance(e, dict)
                    for o in [(o, h) for o, h in (e.get("outputs") or {}).items()]]
            same = all(_load_rel(run_dir, str(o))["sha256"] == h for o, h in outs)   # outside the run dir: refused
        return {"status": "signed" if same else "changed", "error": None}
    sheet = _load_rel(run_dir, SHEET) if _rexists(run_dir, SHEET) else None
    from toolkit.spec import signoff as _SO        # sheet_sources only parses text: no harness reader runs
    src = _SO.sheet_sources(sheet["parsed"]) if sheet and sheet["err"] is None else None
    stale = None
    if sheet and sheet["err"] is None:
        if src is None:
            stale = "the sheet records no source hashes (it predates this check): it is regenerated by the run"
        elif src.get("spec_sha256") != spec["sha256"] or src.get("derivations_sha256") != (dsha or "none"):
            stale = "the sheet was generated from another spec or derivation record"
    return {"status": "awaiting", "error": None, "spec_sha256": spec["sha256"], "derivations_sha256": dsha or "none",
            "sheet_stale": stale,
            "sheet": sheet["parsed"] if sheet and sheet["err"] is None else None,
            "sheet_error": None if sheet and sheet["err"] is None else (sheet["err"] if sheet else f"no {SHEET} yet"),
            "sheet_sha256": sheet["sha256"] if sheet and sheet["err"] is None else "none"}


def _spec_status(run_dir: str):
    v = spec_view(run_dir)
    return v.get("status"), v.get("error")


def _scope_answers(run_dir: str, name: str) -> tuple[dict, str | None]:
    """{fingerprint: answer} from scope_signoff.json, checked as scope_signoff.load_answers checks it."""
    import scope_signoff as SS
    if not _rexists(run_dir, name):
        return {}, None
    doc, err = _rread(run_dir, name)
    ans = doc.get("answers") if isinstance(doc, dict) else None
    if err or not isinstance(ans, dict) or not all(isinstance(a, dict) and a.get("decision") in SS.DECISIONS
                                                   for a in ans.values()):
        return {}, f"{name}: {err or 'answers must map fingerprints to {decision: accept|decline}'}"
    return ans, None


def inbox(run_dir: str) -> dict:
    """The questions the run waits on: the spec sign-off and pending scope declarations, from the console's own
    read model (spec_view; the scope request and answers parsed here)."""
    import scope_signoff as SS
    v = spec_view(run_dir)
    out = {"spec": dict(v)}
    if _rexists(run_dir, SS.REQUEST):
        req, rerr = _rread(run_dir, SS.REQUEST)
        if rerr or not isinstance(req, dict):
            out["scope"] = {"error": f"{SS.REQUEST}: {rerr or 'not a mapping'}"}
        elif req.get("pending"):
            answers, bad = _scope_answers(run_dir, SS.SIGNOFF)
            items = []
            for it in req.get("items") or []:
                if not isinstance(it, dict):
                    continue
                items.append({"finding": it.get("finding"), "fingerprint": it.get("fingerprint"),
                              "reason": it.get("reason"), "claim": it.get("claim"),
                              "declarations": [{"id": d.get("id"), "test": SS._test_md(d.get("test") or {}),
                                                "text": d.get("text") or {}} for d in it.get("declarations") or []
                                               if isinstance(d, dict)],
                              "answer": (answers.get(it.get("fingerprint")) or {}).get("decision")})
            done = items and all(i["answer"] for i in items)
            out["scope"] = {"pending": True, "round": req.get("round"), "items": items,
                            "fingerprints": sorted(str(i.get("fingerprint")) for i in items),
                            "rationale": req.get("decision_rationale"),
                            "status": "answered" if done and not bad else "awaiting", "error": bad}
    return out


def _inbox_cards(run_dir: str, box: dict, st: dict) -> list:
    out = []
    spec = box.get("spec") or {}
    if spec.get("error"):
        out.append({"id": "inbox:spec", "kind": "error", "stage": "SIGN", "at": "", "title": "spec sign-off status "
                    "cannot be read", "body": spec["error"], "anchor": {"kind": "file", "id": "decision_spec.yaml",
                                                                        "file": "decision_spec.yaml"}})
    elif spec.get("status") == "awaiting":
        out.append({"id": "inbox:spec", "kind": "decision_needed", "sub": "spec", "stage": "SIGN",
                    "at": _mtime(os.path.join(run_dir, "decision_spec.yaml")), "title": "Your decision needed: "
                    "sign the decision spec", "body": spec.get("sheet") or spec.get("sheet_error") or "",
                    "paused": st.get("paused"), "spec_sha256": spec.get("spec_sha256"),
                    "sheet_sha256": spec.get("sheet_sha256"), "derivations_sha256": spec.get("derivations_sha256"),
                    "sheet_stale": spec.get("sheet_stale"),
                    "anchor": {"kind": "spec", "id": "signoff",
                                                           "file": "decision_spec.signoff_sheet.md"}})
    sc = box.get("scope")
    if sc and (sc.get("pending") or sc.get("error")):
        out.append({"id": "inbox:scope", "kind": "decision_needed" if sc.get("pending") else "error",
                    "sub": "scope", "stage": "OWNER_DECISION", "at": _mtime(os.path.join(run_dir,
                                                                                         "scope_signoff_request.json")),
                    "title": "Your decision needed: scope declarations" if sc.get("pending") else
                    "scope sign-off request cannot be read", "body": sc.get("rationale") or sc.get("error") or "",
                    "items": sc.get("items") or [], "status": sc.get("status"),
                    "fingerprints": sc.get("fingerprints") or [],
                    "anchor": {"kind": "scope", "id": "request", "file": "scope_signoff_request.json"}})
    return out


def build_cards(run_dir: str, st: dict | None, box: dict, cap=None) -> list:
    cards = timeline_cards(st) if st else []
    for fn in (critique_cards, gate_cards, figure_cards, decision_test_cards):
        try:
            cards += fn(run_dir)
        except Exception as e:  # noqa: BLE001 -- a reader bug is shown, not hidden
            cards.append({"id": f"error:{fn.__name__}", "kind": "error", "stage": "", "at": "",
                          "title": f"{fn.__name__} failed", "body": f"{type(e).__name__}: {e}", "anchor": None})
    comp = compute_status(run_dir, cap, st)
    for i, c in enumerate(comp["calls"]):
        cards.append({"id": f"batch:{i}", "kind": "batch", "stage": "ANALYZE",
                      "at": _iso(c["started"]) if c.get("started") else "", "title": f"engine batch {c.get('fn')}",
                      **c, "anchor": {"kind": "batch", "id": str(c.get("fn")), "file": "models/compute_ledger.json"}})
    cards += _inbox_cards(run_dir, box, st or {})
    for name, kind in ((CHAT, "chat"), (FEEDBACK, "feedback")):
        rows, errs = read_jsonl_rel(run_dir, name)
        for i, r in enumerate(rows):
            cards.append({"id": f"{kind}:{i}", "kind": kind, "stage": r.get("stage") or "", "at": r.get("ts", ""),
                          **{k: r.get(k) for k in ("role", "text", "usd", "uncited", "anchor", "layer",
                                                   "proposed_change", "verdict", "model", "feedback_id",
                                                   "citations", "fallback", "evidence")},
                          "fb_kind": r.get("kind")})
        cards += [{"id": f"error:{name}:{j}", "kind": "error", "stage": "", "at": "", "title": f"{name} has a bad "
                   "line", "body": e, "anchor": None} for j, e in enumerate(errs)]
    return sorted(cards, key=lambda c: (c.get("at") or "", c["id"]))


def headline(run_dir: str) -> str:
    txt, err = _rread(run_dir, "results.md") if _rexists(run_dir, "results.md") else (None, None)
    if not isinstance(txt, str):
        return ""
    m = re.search(r"^## Recommendation\s*\n+(.+?)(?:\n\s*\n|$)", txt, re.M | re.S)
    return (m.group(1).strip() if m else "")[:300]


def headline_number(text: str) -> str:
    """The first "averts <number> <unit>" in the headline, e.g. "12.34M DALYs"; "" when there is none."""
    m = re.search(r"\bavert(?:s|ed)?\s+([\d.,]+\s?[kKMB]?)\s+([A-Za-z]+)", text or "")
    return f"{m.group(1)} {'DALYs' if m.group(2).lower() == 'dalys' else m.group(2)}" if m else ""


def run_label(run_dir: str, root: str | None) -> tuple[str, str]:
    """(label, group). The label is the path under the root without "runs" folders and without the
    timestamped run folder when a parent names it (F1/runs/2026-..._question -> F1). The group is the
    root's last folder that is not "run"/"runs"."""
    if not root:
        return os.path.basename(run_dir), os.path.basename(os.path.dirname(run_dir))
    parts = [x for x in os.path.relpath(run_dir, root).split(os.sep) if x not in (".", "run", "runs")]
    if len(parts) > 1:
        parts = parts[:-1]
    names = [x for x in os.path.realpath(root).split(os.sep) if x and x not in ("run", "runs")]
    base = os.path.basename(os.path.realpath(root))
    if parts == [os.path.basename(run_dir)] and base not in ("run", "runs"):
        parts = [base]               # a run directly under a named root (a mirror's live/<run>) is named by it
    return "/".join(parts) or os.path.basename(run_dir), (names[-1] if names else root)


def engine_of(run_dir: str, st: dict | None) -> str | None:
    doc, err = _rread(run_dir, "engine_choice.json")
    if isinstance(doc, dict) and isinstance(doc.get("engine"), dict):
        return doc["engine"].get("ref")
    return (st or {}).get("engine")


# ------------------------------------------------------------------ reader support: summary, glossary, context
def _data(run_dir: str, rel: str) -> dict:
    doc, _ = _rread(run_dir, rel)
    d = doc.get("data") if isinstance(doc, dict) else None
    return d if isinstance(d, dict) else {}


def results_summary(run_dir: str) -> list:
    """The run's key numbers, each with its unit and the file it is read from: outcomes averted (the
    headline with its interval and median), envelope, allocatable and allocated cost, units with no package."""
    pt, un = _data(run_dir, "analysis/point.yaml"), _data(run_dir, "analysis/uncertainty.yaml")
    spec, _ = _rread(run_dir, "decision_spec.yaml")
    head = ((un.get("headline") or {}).get("outcome")) if isinstance(un.get("headline"), dict) else None
    out = []
    for k, v in (pt.get("averted") or {}).items():
        if not isinstance(v, (int, float)):
            continue
        item = {"label": f"{k} averted", "value": v, "unit": k, "source": "analysis/point.yaml", "key": f"averted.{k}"}
        fp = (un.get("fixed_plan") or {}).get(k) if isinstance(un.get("fixed_plan"), dict) else None
        if isinstance(fp, dict) and fp.get("ci_low") is not None:
            item.update(ui=[fp.get("ci_low"), fp.get("ci_high")], level=fp.get("level"), median=fp.get("median"),
                        ui_source="analysis/uncertainty.yaml", covers=un.get("covers"))
        out.insert(0, item) if k == head else out.append(item)
    env = _dig(spec, "constraints.envelope_usd")
    for label, v, src, key in (("envelope", env, "decision_spec.yaml", "constraints.envelope_usd"),
                               ("allocatable", pt.get("budget_allocatable"), "analysis/point.yaml", "budget_allocatable"),
                               ("allocated", pt.get("cost_allocated"), "analysis/point.yaml", "cost_allocated")):
        if isinstance(v, (int, float)):
            out.append({"label": label, "value": v, "unit": "usd", "source": src, "key": key})
    mix = pt.get("mix") or {}
    if isinstance(mix.get("no_package"), int):
        out.append({"label": "units with no package", "value": mix["no_package"], "unit": "units",
                    "of": pt.get("n_units"), "source": "analysis/point.yaml", "key": "mix.no_package"})
    return out


def benchmarks(run_dir: str) -> dict:
    """The engine card's benchmarks by id (analysis/engine.yaml): what each measures, its role and status."""
    card = (_data(run_dir, "analysis/engine.yaml").get("card") or {})
    return {str(b["id"]): {k: b.get(k) for k in ("metric", "role", "target", "band", "computed", "status", "source")}
            for b in card.get("benchmarks") or [] if isinstance(b, dict) and b.get("id")}


def batch_info(run_dir: str) -> list:
    """Each stored engine batch's summary: points computed here and reused from an earlier batch."""
    out = []
    for p in sorted(glob.glob(os.path.join(run_dir, "analysis", "**", "*.summary.json"), recursive=True)):
        d, _ = _rread(run_dir, _rel(run_dir, p))
        if isinstance(d, dict) and d.get("n") is not None:
            out.append({"out": _rel(run_dir, p)[:-len(".summary.json")], "n": d.get("n"), "computed": d.get("computed"),
                        "reused": d.get("reused") or 0, "code_fp": d.get("code_fp"), "task_core_s": d.get("task_core_s")})
    return out


def is_active(run_dir: str, st: dict | None) -> bool:
    """Unfinished, and a live progress record or run_state.json written within STALE_S."""
    try:
        fresh = time.time() - os.path.getmtime(os.path.join(run_dir, "run_state.json")) < STALE_S
    except OSError:
        fresh = False
    return not finished(run_dir, st) and (fresh or compute_status(run_dir, None, st)["live"])


def stop_detail(run_dir: str, st: dict | None) -> str:
    """Why an unfinished run is not moving, in words, from run_state.json: the stage, the round, and the
    last task the journal shows as started and never completed. The operator's reason is not recorded there."""
    st = st or {}
    if not st or finished(run_dir, st) or st.get("paused") or is_active(run_dir, st):
        return ""
    j = st.get("journal") if isinstance(st.get("journal"), dict) else {}
    open_ = [(k, v) for k, v in j.items() if isinstance(v, dict) and v.get("status") == "started"]
    where = f"{stage_word(st.get('state'))} (round {st.get('round')} of {st.get('max_rounds')})"
    if open_:
        k, v = open_[-1]
        return f"during {where}: task {v.get('task') or k} started {str(v.get('started') or '')[:16].replace('T', ' ')} and never finished"
    return f"in {where}: no task was open"


def operator_note(run_dir: str, st: dict | None = None) -> str:
    """The last note in progress.md's "## Notes" section, only when it is dated after the run's last
    transition (notes carry a day, "2026-11-02 (operator): ...", so the day must be later): an older note
    is about an earlier attempt, not about why this run stopped."""
    txt, _ = _rread(run_dir, "progress.md")
    if not isinstance(txt, str) or "## Notes" not in txt:
        return ""
    notes = [ln[2:].strip() for ln in txt.split("## Notes", 1)[1].splitlines() if ln.startswith("- ")]
    last = _last_at(st)[:10]
    notes = [n for n in notes if (re.match(r"(\d{4}-\d\d-\d\d)", n) or [None, ""])[1] > last] if last else notes
    return notes[-1] if notes else ""


_GATE = "scripts/validate_critique_yaml.py (_recompute_action)"
_SCHEMA = ".claude/skills/critique-blockers-schema/SKILL.md"
GLOSSARY = {   # term -> (definition, where it is defined). The page marks only terms listed here (RC16).
    "gate": ("Decides from the critiques' blockers, the done-when tests and the required artifacts whether the run "
             "may write its report (ACCEPT), must fix (PATCH_OR_RETHINK), declares scope, waits for the owner, "
             "or fails. It applies fixed rules in order.", _GATE),
    "ACCEPT": ("Rule 4: no unresolved HIGH blocker, no missing required artifact, no structural mismatch. The run "
               "may write its report.", _GATE),
    "PATCH_OR_RETHINK": ("Rule 2: a HIGH blocker or a missing required artifact is open and rounds remain. The run "
                         "goes to the fix loop; ACCEPT is forbidden.", _GATE),
    "DECLARE_SCOPE": ("Rules 3 and 3a: the open items are declared as limits of scope in scope_declaration.yaml and "
                      "the report quotes each declaration in Limitations. DECLARE_SCOPE is not ACCEPT.", _GATE),
    "RETHINK_STRUCTURAL": ("Rule 1 with rounds left: a critic found a structural mismatch (the model does not answer "
                           "the question). Not patchable, not scope-declarable.", _GATE),
    "RUN_FAILED": ("Rule 1 with no rounds left, or rule 5: rounds are exhausted with a finding that cannot be "
                   "scope-declared. The run fails.", _GATE),
    "AWAIT_OWNER": ("A finding that bears on the primary decision would be declared; only the owner may accept it, so "
                    "the run waits at OWNER_DECISION.", "agents/stage_machine.py (route_gate); scripts/scope_signoff.py"),
    "rule 1": ("Structural mismatch → RETHINK_STRUCTURAL, or RUN_FAILED with no rounds left.", _GATE),
    "rule 2": ("HIGH blocker or missing required artifact, rounds left → PATCH_OR_RETHINK.", _GATE),
    "rule 3": ("Rounds exhausted, every open item scope-declarable → DECLARE_SCOPE.", _GATE),
    "rule 3a": ("Every open item declared at this gate and the previous one, none from a critic, all declarable → "
                "DECLARE_SCOPE with rounds left.", _GATE),
    "rule 4": ("Nothing outstanding → ACCEPT.", _GATE),
    "rule 5": ("Rounds exhausted with a finding that cannot be scope-declared → RUN_FAILED.", _GATE),
    "blocker": ("A critic's finding with a stable id, a severity, the stage that must change, and a done-when test. "
                "The gate decides from these, not from the critique's prose.", _SCHEMA),
    "HIGH": ("The report cannot ship over it: a hard blocker, a wrong parameter or citation, a missing required "
             "deliverable, or an unsupported primary number. Blocks ACCEPT.", _SCHEMA),
    "MEDIUM": ("Degrades the report but does not invalidate the core conclusions (most presentation issues).", _SCHEMA),
    "LOW": ("A minor note; it is not carried forward between rounds.", _SCHEMA),
    "target stage": ("The stage the fix belongs to (PLAN, DATA, MODEL, ANALYZE or WRITE).", _SCHEMA),
    "done-when": ("The test that decides a HIGH or MEDIUM blocker is fixed: file checks (exists, contains or lacks a "
                  "pattern, a key compared with a value) or a judgement. Frozen once the gate has seen it, re-run "
                  "every round; passing it resolves the blocker.", _SCHEMA + "; scripts/done_when.py"),
    "mechanical": ("A done-when test made only of file checks: the harness decides it.", "scripts/done_when.py (mechanical)"),
    "judged": ("A done-when test with a judgement: a reviewer decides it.", "scripts/done_when.py (mechanical)"),
    "fix loop": ("One open finding per task: the planner orders the fixes, an agent edits, the harness diffs the "
                 "files, rebuilds and re-runs the finding's checks.", "agents/fix_loop.py"),
    "UI": ("Uncertainty interval: the 2.5th to 97.5th percentile of the outcome over the uncertainty draws, for the "
           "point plan held fixed. Its label lists what the draws cover.", "toolkit/analysis/uncertainty.py"),
    "calibration partly clamped": ("In some draws the calibration hit a bound for some units; those draws do not carry "
                                   "the full calibration uncertainty, and the interval says so.",
                                   "toolkit/analysis/uncertainty.py"),
    "decision test": ("The plan re-solved with one input set to stated values. It changes the plan when the package "
                      "changes in more than the threshold share of units.", "toolkit/analysis/decision_tests.py"),
    "benchmark": ("A published result the engine is run against. Role fit: the calibration used it; held out: it did "
                  "not. Status: inside or outside the target band.", "toolkit/engines/benchmark.py"),
    "DALYs": ("Disability-adjusted life years. Averted = the comparator's outcome minus the plan's.",
              "toolkit/spec/schema.py (objective, sense: avert)"),
    "comparator": ("What happens without the plan: the package every unit gets in the counterfactual (the spec's "
                   "comparator).", "toolkit/spec/schema.py"),
    "allocatable": ("The envelope minus the spec's fixed costs: what the optimiser may assign to packages.",
                    "analysis/point.yaml budget_allocatable; decision_spec.yaml constraints"),
    # the run dossier (tools/run_console/dossier.py)
    "emulator": ("A Gaussian-process surrogate fitted to engine runs at sampled points. The analysis predicts each "
                 "unit's outcomes and costs from it instead of running the engine for every unit and package.",
                 "toolkit/analysis/model.py (EmulatorModel)"),
    "emulator inversion": ("Per unit, the calibrated attribute that reproduces the unit's target on the emulator's "
                           "calibration metric (logit scale), on a grid over calibration.range. A target outside "
                           "the grid's range is clamped.", "toolkit/analysis/calibration.py"),
    "clamped": ("The unit's target lies outside what the calibration range can reach, so its attribute was set to "
                "the range's end.", "toolkit/analysis/calibration.py"),
    "slope": ("The local slope of the calibration metric in the attribute at the fitted point. A slope near zero "
              "means the target does not pin the attribute down.", "toolkit/analysis/calibration.py"),
    "identifiability": ("Whether the targets pin down what is calibrated. For emulator inversion: one attribute per "
                        "target, judged by the slope (flat_slope_threshold).", "toolkit/analysis/calibration.py"),
    "held out": ("A benchmark the calibration and the fitted parameters did not use: a check, not a fit.",
                 "toolkit/engines/benchmark.py"),
    "fitted": ("Fitted once to the named benchmarks, within its fit box; not refitted per run.",
               "toolkit/engines/benchmark.py (fit_rule); the card's fitted block"),
    "prior range": ("The range the engine card records for a parameter (lo to hi), from the source it cites.",
                    "analysis/engine.yaml data.parameters (the engine card)"),
    "price rule": ("How a cost is brought to the base year: as_published (no change), index (the spec's price "
                   "index, base year over price year), local_index (through the local currency).",
                   "toolkit/alloc/costs.py (_factor)"),
    "cost basis": ("What a cost item is charged on: per person, per eligible person, per person reached, per "
                   "commodity (people per commodity), fixed, or per outcome (a model output).",
                   "toolkit/alloc/costs.py (item_cost)"),
    "per person": ("The base-year value times share, times per year and years, divided by people per commodity "
                   "for a commodity: the cost of covering one person over the window.",
                   "toolkit/alloc/costs.py (item_cost)"),
    "re-stamped by proof": ("The benchmark numbers were computed by the previous engine version. A byte-identity "
                            "proof showed the new code gives identical outputs on the same inputs, so the file's "
                            "identity was updated without recomputing.",
                            "toolkit/engines/<engine>/benchmarks/restamp_proof.py"),
    "validate_once": ("A full run of the benchmark pack on Coiled that confirms a re-stamp. Pending until it has "
                      "run.", "toolkit/engines/<engine>/benchmarks/validate_once.sh"),
    "binding": ("How the spec gives the engine a unit attribute: a column of the units table, several columns, or "
                "one value for every unit, with its source.", "decision_spec.yaml decision_units.engine_attributes"),
    "defaults accepted": ("Engine defaults the spec accepts by name, each with a reason, for attributes the staged "
                          "inputs do not give.", "decision_spec.yaml decision_units.defaults_accepted"),
    "code fingerprint": ("A hash of what a stored engine batch depends on besides its parameters: the run's models/ "
                         "and data/ files and the repo's scripts/ and toolkit/. Stored results are reused only under "
                         "the same fingerprint.", "scripts/compute.py (code_fingerprint)"),
}
GATE_ACTIONS = ("ACCEPT", "PATCH_OR_RETHINK", "DECLARE_SCOPE", "RETHINK_STRUCTURAL", "RUN_FAILED", "AWAIT_OWNER")


def read_run(run_dir: str, cap=None) -> dict:
    st, err = load_run_state(run_dir)
    box = inbox(run_dir)
    docs = {}
    for name in ("results.md", "manuscript.md", "report.md"):
        if _rexists(run_dir, name):
            txt, derr = _rread(run_dir, name)
            docs[name] = {"text": txt, "error": derr}
    return {"run_dir": run_dir, "name": os.path.basename(run_dir), "error": err,
            "state": {k: v for k, v in (st or {}).items() if k not in ("history", "journal", "last_gate")},
            "stepper": stepper(st) if st else None, "compute": compute_status(run_dir, cap, st),
            "inbox": box, "cards": build_cards(run_dir, st, box, cap), "docs": docs,
            "headline": headline(run_dir), "engine": engine_of(run_dir, st), "finished": finished(run_dir, st),
            "status": run_status(run_dir, st), "fail_statuses": FAIL_STATUSES, "summary": results_summary(run_dir),
            "benchmarks": benchmarks(run_dir), "batches": batch_info(run_dir), "stop": stop_detail(run_dir, st),
            "operator_note": operator_note(run_dir, st)}


# ------------------------------------------------------------------ feedback and the learning view
# Evidence snapshots (review round 2, P2-9): what a card showed, sealed by this console process when the card was
# rendered, so feedback keeps it even when the file is overwritten before the owner submits. A snapshot the page
# sends back is used only when its seal checks; otherwise the evidence is captured at submit time and says so.
_EVIDENCE_KEY = secrets.token_bytes(32)
_EV_FIELDS = ("run", "file", "anchor_id", "sha256", "round", "excerpt", "at", "captured")


def _ev_mac(ev: dict) -> str:
    return hmac.new(_EVIDENCE_KEY, json.dumps([ev.get(k) for k in _EV_FIELDS], default=str).encode(),
                    hashlib.sha256).hexdigest()


def sealed(ev: dict) -> dict:
    ev = {k: ev.get(k) for k in _EV_FIELDS}
    ev["mac"] = _ev_mac(ev)
    return ev


def evidence_ok(ev, anchor: dict | None, run_dir: str) -> bool:
    """The seal checks, and the snapshot is for this run (canonical path) and this anchor."""
    a = anchor or {}
    return (isinstance(ev, dict) and isinstance(ev.get("mac"), str) and hmac.compare_digest(ev["mac"], _ev_mac(ev))
            and ev.get("run") == os.path.realpath(run_dir) and ev.get("file") == a.get("file")
            and str(ev.get("anchor_id") or "") == str(a.get("id") or ""))


def _blocker_excerpt(b: dict) -> str:
    return " ".join(str(b.get(k) or "") for k in ("severity", "claim", "fix_requires") if b.get(k)).strip()[:600]


def evidence(run_dir: str, anchor: dict | None, captured: str = "submit") -> dict | None:
    """The anchored artifact now: sha256, round and a short excerpt (the blocker's claim, the figure's caption, the
    decision test's question, or the lines around the anchor id), all from one no-follow read of the file."""
    a = anchor or {}
    f = a.get("file")
    if not f:
        return None
    e = _load_rel(run_dir, str(f))
    if e["sha256"] is None:
        return {"file": f, "error": str(e["err"])[:200]}
    doc, aid = e["parsed"], str(a.get("id") or "")
    rnd = doc.get("round") if isinstance(doc, dict) else None
    excerpt = ""
    if a.get("kind") == "blocker" and isinstance(doc, dict):
        excerpt = _blocker_excerpt(next((x for x in doc.get("blockers") or [] if isinstance(x, dict)
                                         and str(x.get("id")) == aid), {}))
    elif a.get("kind") == "figure":
        c = next((x for x in figure_cards(run_dir) if x.get("file") == f), {})
        excerpt = " · ".join(str(x) for x in (c.get("body"), c.get("producer")) if x) or "(no caption recorded)"
    elif a.get("kind") == "decision_test":
        c = next((x for x in decision_test_cards(run_dir) if x.get("test_id") == aid), {})
        excerpt = f"{c.get('title') or ''}. {c.get('body') or ''}".strip(". ")
    if not excerpt and isinstance(e.get("data"), bytes):
        lines = e["data"].decode("utf-8", "replace").splitlines()
        hit = next((i for i, ln in enumerate(lines) if aid and aid in ln), 0)
        excerpt = "\n".join(lines[hit:hit + 6])
    if rnd is None:
        st, _ = load_run_state(run_dir)
        rnd = (st or {}).get("round")
    return sealed({"run": os.path.realpath(run_dir), "file": f, "anchor_id": aid, "sha256": e["sha256"], "round": rnd,
                   "excerpt": excerpt.strip()[:600], "at": _now(), "captured": captured})


def _evidence_for(run_dir: str, anchor: dict | None, shown) -> dict | None:
    """The snapshot the page showed when its seal checks for this run and anchor. A snapshot that does not check is
    refused (ValueError), never replaced with fresh evidence (review round 4, P2-5); with none sent, the evidence is
    captured now and marked `captured: submit`."""
    if shown is None:
        return evidence(run_dir, anchor)
    if evidence_ok(shown, anchor, run_dir):
        return dict(shown)
    raise ValueError("the evidence snapshot does not match this run or anchor (a control left from another run, or "
                     "a stale page): not recorded; reload and try again")


def append_feedback(run_dir: str, rec: dict) -> dict:
    kind, layer = rec.get("kind"), rec.get("layer")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if layer not in LAYERS:
        raise ValueError(f"layer must be one of {', '.join(LAYERS)}")
    if not str(rec.get("text") or "").strip():
        raise ValueError("feedback needs text")
    a = rec.get("anchor") or {}
    out = {"id": "F-" + secrets.token_hex(4), "ts": _now(),
           "anchor": {"kind": a.get("kind"), "id": a.get("id"), "file": a.get("file")},
           "kind": kind, "layer": layer, "text": str(rec["text"]).strip()}
    for k in ("proposed_change", "verdict", "stage"):
        if rec.get(k):
            out[k] = rec[k]
    if out.get("verdict") not in (None, "agree", "disagree"):
        raise ValueError("verdict must be agree or disagree")
    ev = _evidence_for(run_dir, out["anchor"], rec.get("evidence"))
    if ev:
        out["evidence"] = ev
    _append(run_dir, FEEDBACK, out)
    return out


_APPEND_LOCK = threading.Lock()


def _append(run_dir: str, rel: str, row: dict) -> None:
    """One JSON line under owner/, never through a link (write_nofollow)."""
    with _APPEND_LOCK:
        write_nofollow(run_dir, rel, (json.dumps(row, default=str) + "\n").encode(), append=True)


def learning(run_dirs: list) -> dict:
    by_layer, errors = {lay: [] for lay in LAYERS}, []
    for rd in run_dirs:
        rows, errs = read_jsonl_rel(rd, FEEDBACK)
        errors += [f"{os.path.basename(rd)}: {e}" for e in errs]
        for r in rows:
            by_layer.setdefault(r.get("layer") or "unknown", []).append({**r, "run": os.path.basename(rd),
                                                                         "run_dir": rd})
    return {"by_layer": by_layer, "errors": errors, "n": sum(len(v) for v in by_layer.values())}


def export_digest(run_dirs: list, out_dir: str) -> str:
    """The digest under out_dir (<first root>/_owner/), written without following a link."""
    lv = learning(run_dirs)
    lines = ["# Owner feedback digest", "", f"Written {_now()} by the run console from "
             f"{len(run_dirs)} run(s), {lv['n']} item(s). Each layer's items are candidate changes for the "
             "learning loop (finding → layer → change → re-test); the console changed nothing.", ""]
    for lay, items in lv["by_layer"].items():
        if not items:
            continue
        lines += [f"## {lay} ({len(items)})", ""]
        for r in items:
            a = r.get("anchor") or {}
            where = f"{a.get('file') or ''}{'#' + str(a['id']) if a.get('id') else ''}"
            lines.append(f"- **{r.get('kind')}** ({r['run']}, {r.get('ts')}, {r.get('id')}"
                         + (f", on `{where}`" if where else "") + (f", {r['verdict']}" if r.get("verdict") else "")
                         + f"): {r.get('text')}")
            if r.get("proposed_change"):
                lines.append(f"  - proposed change: {r['proposed_change']}")
            ev = r.get("evidence") or {}
            if ev.get("sha256"):
                lines.append(f"  - evidence: round {ev.get('round')}, sha256 {str(ev['sha256'])[:12]}: "
                             + str(ev.get("excerpt") or "").replace("\n", " ")[:300])
        lines.append("")
    if lv["errors"]:
        lines += ["## Unreadable feedback lines", ""] + [f"- {e}" for e in lv["errors"]] + [""]
    name = f"feedback_digest_{time.strftime('%Y%m%dT%H%M%S')}_{secrets.token_hex(2)}.md"
    parent = os.path.realpath(os.path.dirname(os.path.abspath(out_dir)))
    return write_nofollow(parent, f"{os.path.basename(os.path.abspath(out_dir))}/{name}", "\n".join(lines).encode(),
                          exclusive=True)


# ------------------------------------------------------------------ the owner's answers (existing commands)
def _env() -> dict:
    # The owner signs from their own shell: MA_SIGNOFF_MODE marks an agent's environment (main.py).
    return {k: v for k, v in os.environ.items() if k != "MA_SIGNOFF_MODE"}


def _run(cmd: list, runner=None) -> dict:
    runner = runner or subprocess.run
    try:
        r = runner(cmd, cwd=REPO, env=_env(), capture_output=True, text=True, timeout=600)
        out = ((r.stdout or "") + (r.stderr or "")).strip()[-4000:]
        return {"ok": r.returncode == 0, "rc": r.returncode, "out": out, "cmd": cmd}
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "rc": None, "out": f"{type(e).__name__}: {e}", "cmd": cmd}


def _snapshot_flags(snap: dict | None) -> list:
    snap = snap or {}
    out = []
    for flag, k in (("--expect-derivations-sha256", "derivations_sha256"), ("--expect-sheet-sha256", "sheet_sha256")):
        if snap.get(k):
            out += [flag, str(snap[k])]
    return out


def _remote_sign(cfg: str, answers: dict, note: str, remote, runner=None, expect_sha256: str | None = None,
                 action: str = "scope", snapshot: dict | None = None) -> dict:
    """run_cluster.py <config> sign <answers> --label L --run RUN --action spec|scope [--expect-spec-sha256 SHA
    --expect-derivations-sha256 SHA --expect-sheet-sha256 SHA] <note>: the worker refuses another run than the one
    shown, and stage_machine.py refuses a snapshot other than the one shown."""
    if not remote or not all(remote):
        return {"ok": False, "refused": "the remote label and run of this mirrored run are not known"}
    fd, p = tempfile.mkstemp(suffix=".yaml", prefix="console_answers_")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(answers, f, sort_keys=True)
    try:
        return _run([sys.executable, "cloud/run_cluster.py", cfg, "sign", p, "--label", str(remote[0]), "--run",
                     str(remote[1]), "--action", action]
                    + (["--expect-spec-sha256", expect_sha256] + _snapshot_flags(snapshot) if expect_sha256 else [])
                    + [note], runner)
    finally:
        os.remove(p)


_SHA = re.compile(r"[0-9a-f]{64}")


def sign_spec(run_dir: str, note: str, confirm: bool, mirror: bool = False, cloud_config: str | None = None,
              status_fn=None, runner=None, expect_sha256: str | None = None, remote=None,
              snapshot: dict | None = None) -> dict:
    """The owner signs decision_spec.yaml through agents/stage_machine.py sign with the approval snapshot shown:
    --expect-sha256 (the spec), --expect-derivations-sha256 and --expect-sheet-sha256 (remote: run_cluster sign).
    The command, not the console, makes the check that binds the signature."""
    if not confirm:
        return {"ok": False, "refused": "not confirmed: tick the confirmation to sign"}
    if not str(note or "").strip():
        return {"ok": False, "refused": "the owner's note is required"}
    if not _SHA.fullmatch(str(expect_sha256 or "")):
        return {"ok": False, "refused": "the sha256 of the spec you were shown is missing: reload the run"}
    if snapshot is not None and not all(_SHA.fullmatch(str(snapshot.get(k) or "")) or snapshot.get(k) == "none"
                                        for k in ("derivations_sha256", "sheet_sha256")):
        return {"ok": False, "refused": "the approval snapshot is incomplete: reload the run"}
    if mirror:
        if not cloud_config:
            return {"ok": False, "refused": "a mirrored run is signed on the cluster: start the console with "
                                            "--cloud-config"}
        return _remote_sign(cloud_config, {}, note, remote, runner, expect_sha256, "spec", snapshot)
    if status_fn:
        status, view = status_fn(run_dir), None
    else:
        view = spec_view(run_dir)
        status = view.get("status")
    if status != "awaiting":
        return {"ok": False, "refused": f"the spec is not awaiting sign-off (status {status or (view or {}).get('error')})"}
    if view is not None and view.get("sheet_stale"):
        return {"ok": False, "refused": f"the sign-off sheet is stale ({view['sheet_stale']}): approval waits for the "
                                        "regenerated sheet"}
    if view is not None and (view.get("spec_sha256") != expect_sha256 or (snapshot is not None and any(
            view.get(k) != snapshot.get(k) for k in ("derivations_sha256", "sheet_sha256")))):
        return {"ok": False, "refused": "the spec, its sheet or the derivation record changed since they were shown: "
                                        "reload the run and read them again"}
    return _run([sys.executable, "agents/stage_machine.py", "sign", run_dir, "--by", "owner", "--note", note,
                 "--expect-sha256", expect_sha256] + _snapshot_flags(snapshot), runner)


def answer_scope(run_dir: str, answers: dict, note: str, confirm: bool, mirror: bool = False,
                 cloud_config: str | None = None, runner=None, fingerprints=None, remote=None) -> dict:
    """Answers to the pending scope declarations, through scripts/scope_signoff.py sign (remote: run_cluster
    sign). `answers`: fingerprint -> accept|decline, or {decision, note}, one per item shown; `fingerprints`: the
    items the page showed. scope_signoff.sign keys answers by fingerprint, so an item whose declaration or test
    changed after it was shown has no answer and the whole sign-off is refused there, atomically."""
    import scope_signoff as SS
    if not confirm:
        return {"ok": False, "refused": "not confirmed: tick the confirmation to answer"}
    if not str(note or "").strip():
        return {"ok": False, "refused": "the owner's note is required"}
    req, _ = _rread(run_dir, SS.REQUEST) if _rexists(run_dir, SS.REQUEST) else (None, None)
    req = req if isinstance(req, dict) else None
    if not req or not req.get("pending") or not req.get("items"):
        return {"ok": False, "refused": "no scope declaration is waiting for an answer"}
    now_fps = sorted(str(it.get("fingerprint")) for it in req["items"])
    if fingerprints is None or sorted(str(x) for x in fingerprints) != now_fps:
        return {"ok": False, "refused": "the scope request changed since it was shown (or the page sent no "
                                        "fingerprints): reload the run and answer again"}
    clean, missing = {}, []
    for it in req["items"]:
        a = (answers or {}).get(it.get("fingerprint"))
        d = a.get("decision") if isinstance(a, dict) else a
        if d not in SS.DECISIONS:
            missing.append(str(it.get("finding")))
            continue
        clean[it["fingerprint"]] = {"decision": d, "note": str(a.get("note") or note)} if isinstance(a, dict) else d
    if missing:
        return {"ok": False, "refused": "accept or decline each item; missing: " + ", ".join(missing)}
    if mirror:
        if not cloud_config:
            return {"ok": False, "refused": "a mirrored run is answered on the cluster: start the console with "
                                            "--cloud-config"}
        return _remote_sign(cloud_config, clean, note, remote, runner, action="scope")
    fd, p = tempfile.mkstemp(suffix=".yaml", prefix="console_scope_")
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(clean, f, sort_keys=True)
    try:
        return _run([sys.executable, "scripts/scope_signoff.py", "sign", run_dir, "--answers", p, "--note", note],
                    runner)
    finally:
        os.remove(p)


# ------------------------------------------------------------------ chat
def _cut(name: str, text: str, n: int, cut: list) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    cut.append(name)
    return text[:n] + f"\n[truncated: {name}, first {n} of {len(text)} characters]"


def chat_context(run_dir: str) -> dict:
    """The system prompt's two blocks: the fixed rules, then this run's context (each part cut to its
    budget, and the cut stated). `seen`: each file whose content went into the context, with that text, so a
    reply's citations can be checked against what Claude was actually shown."""
    st, err = load_run_state(run_dir)
    cut, parts, seen = [], [f"# Run context: {os.path.basename(run_dir)} (read {_now()})"], {}

    def saw(rel, text):
        seen[rel] = seen.get(rel, "") + "\n" + str(text or "")
    if err:
        parts.append(f"run_state.json: {err}")
    else:
        head = "\n".join(f"{k}: {st.get(k)}" for k in (
            "question", "state", "round", "max_rounds", "status", "paused", "spent_usd", "budget_usd", "engine"))
        parts.append("## run_state.json\n" + head)
        tl = "\n".join(f"- {h.get('at')} {h.get('from')} → {h.get('to')}: {h.get('why')} "
                       f"(spent ${h.get('spent_usd')})"
                       for h in (st.get("history") or [])[-20:] if isinstance(h, dict) and "to" in h)
        tl = _cut("timeline", tl, BUDGET["timeline"], cut)
        parts.append("## Timeline (run_state.json history, last 20)\n" + tl)
        saw("run_state.json", head + "\n" + tl)
    if _rexists(run_dir, "results.md"):
        txt, rerr = _rread(run_dir, "results.md")
        body = _cut("results.md", txt, BUDGET["results"], cut) if isinstance(txt, str) and txt else None
        parts.append("## results.md\n" + (body or f"unreadable: {rerr}"))
        if body:
            saw("results.md", body)
    gates = sorted(glob.glob(os.path.join(run_dir, "gate_r*.json")), key=os.path.getmtime)
    if gates:
        rel = _rel(run_dir, gates[-1])
        g, gerr = _rread(run_dir, rel)
        keep = {k: g.get(k) for k in ("action", "rule_matched", "rationale", "unresolved_high", "done_when_status",
                                      "post_write", "rounds_remaining")} if isinstance(g, dict) else gerr
        body = _cut("gate", json.dumps(keep, indent=1, default=str), BUDGET["gate"], cut)
        parts.append(f"## Latest gate: {rel}\n" + body)
        saw(rel, body)
    bl = [c for c in critique_cards(run_dir) if c["kind"] == "error" or not c.get("resolved")]
    lines = [(f"- [[{c['anchor']['file']}#{c['blocker_id']}]] {c.get('severity')} {c.get('category')} → "
              f"{c.get('target_stage')}: {str(c.get('claim') or '').strip()[:400]}") if c["kind"] == "blocker"
             else f"- {c['title']}: {c['body']}" for c in bl]
    kept = _cut("blockers", "\n".join(lines) or "none", BUDGET["blockers"], cut)
    parts.append("## Unresolved blockers (critique_*.yaml)\n" + kept)
    for c, ln in zip(bl, lines):
        if c["kind"] == "blocker" and ln in kept:          # only what the prompt kept counts as seen
            saw(c["anchor"]["file"], ln)
    dts = decision_test_cards(run_dir)
    lines = [f"- [[{c['anchor']['file']}#{c['anchor']['id']}]] {c['title']}. {c.get('body') or ''} Criterion: "
             f"{c.get('criterion')}" if c["kind"] == "decision_test" else f"- {c['title']}: {c['body']}" for c in dts]
    kept = _cut("decision tests", "\n".join(lines) or "none", BUDGET["tests"], cut)
    parts.append("## Decision tests (each one: changes the plan, does not change the plan, not run, or its result "
                 "cannot be read)\n" + kept)
    for c, ln in zip(dts, lines):
        if c["kind"] == "decision_test" and ln in kept:
            saw(c["anchor"]["file"], ln)
            saw("analysis/decision_tests.yaml", ln)
    box = inbox(run_dir)
    sc = box.get("scope") or {}
    owner = [f"spec sign-off: {box['spec'].get('status') or box['spec'].get('error')}"]
    owner += [f"scope item {i['finding']}: {i.get('reason')}; answer: {i.get('answer') or 'none yet'}"
              for i in sc.get("items") or []]
    parts.append("## Questions for the owner\n" + _cut("owner questions", "\n".join(owner), BUDGET["owner"], cut))
    files = sorted(_rel(run_dir, f) for f in glob.glob(os.path.join(run_dir, "*")) + glob.glob(
        os.path.join(run_dir, "figures", "*.png")) + glob.glob(os.path.join(run_dir, "analysis", "*.yaml"))
        if os.path.isfile(f) and not _CREDENTIAL.match(os.path.basename(f)))
    parts.append("## Files you may cite (named only; their content is above when it was included)\n"
                 + ", ".join(files[:120]))
    return {"stable": SYSTEM, "run": "\n\n".join(parts), "truncated": cut, "seen": seen}


_CITE = re.compile(r"\[\[([^\]#]+)(?:#([^\]]*))?\]\]")


def check_citations(text: str, seen: dict) -> list:
    """Each [[file#anchor]] in a reply: checked (the file's content, and the anchor, were in the context),
    anchor not in context (the file was, the anchor was not), or unchecked (the file's content was not sent)."""
    out = []
    for m in _CITE.finditer(text or ""):
        f, a = m.group(1).strip(), (m.group(2) or "").strip()
        if f not in seen:
            status = "unchecked"
        elif a and a.lower() not in seen[f].lower():
            status = "anchor not in context"
        else:
            status = "checked"
        row = {"file": f, "anchor": a, "status": status}
        if row not in out:
            out.append(row)
    return out


def price(usage, rates: dict | None = None) -> float:
    r = rates or PRICE
    g = (lambda k: float(getattr(usage, k, 0) or 0))
    return round((g("input_tokens") * r["in"] + g("output_tokens") * r["out"]
                  + g("cache_creation_input_tokens") * r["cache_write"]
                  + g("cache_read_input_tokens") * r["cache_read"]) / 1e6, 6)


def worst_case_usd(input_tokens: int) -> float:
    """Before a call: every input token billed as a cache write and a full max_tokens of output, on Opus 5.5 and
    again on the refusal fallback (a mid-stream refusal is billed, then the fallback model runs the request)."""
    return round(sum(input_tokens * p["cache_write"] + MAX_TOKENS * p["out"] for p in (PRICE, FALLBACK_PRICE)) / 1e6,
                 6)


def input_token_bound(client, system: list, msgs: list) -> tuple[int, str]:
    """(tokens, how): the API's own count (messages.count_tokens, free), else the prompt's UTF-8 byte length,
    which bounds the token count from above (a token is at least one byte)."""
    try:
        n = client.messages.count_tokens(model=MODEL, system=system, messages=msgs).input_tokens
        return int(n), "count_tokens"
    except Exception:  # noqa: BLE001 -- no count: the byte bound, which is always above it
        n = sum(len(str(b.get("text") or "").encode()) for b in system) + sum(
            len(str(m.get("content") or "").encode()) for m in msgs)
        return n, "UTF-8 bytes (upper bound)"


_SENT = re.compile(r"(?<=[.!?])\s+|\n+")


def uncited(text: str) -> list:
    """Sentences that state a number and carry no [[file#anchor]] citation."""
    out = []
    for s in _SENT.split(text or ""):
        bare = re.sub(r"\[\[[^\]]*\]\]", "", s)
        bare = re.sub(r"^\s*(?:[-*]|\d+[.)]|#+)\s*", "", bare)
        if re.search(r"\d", bare) and "[[" not in s:
            out.append(s.strip()[:200])
    return out


def api_key() -> str | None:
    """ANTHROPIC_API_KEY, or None when it is unset or an obvious placeholder (empty, `...`, `your…`, `xxx`)."""
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    low = key.lower()
    if (not key or set(key) <= {".", " "} or "your" in low or "xxx" in low
            or (low.startswith("<") and low.endswith(">"))):
        return None
    return key


def default_client():
    key = api_key()
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set (or is a placeholder) in the console's environment; chat is "
                           "off (the rest of the console works). Export the real key in the shell that starts it.")
    import anthropic
    return anthropic.Anthropic(api_key=key)


def _fallback_ran(msg) -> bool:
    its = getattr(getattr(msg, "usage", None), "iterations", None) or []
    return getattr(msg, "model", MODEL) != MODEL or any(getattr(x, "type", "") == "fallback_message" for x in its)


class ChatSession:
    """One console session's chat: its spend, its reservations and its cap across runs. Each message reserves
    its worst case under the lock before its call and settles to its usage after; one run's turns are
    serialized, so each carries the turns before it."""

    def __init__(self, cap_usd: float = 2.0, client_factory=default_client, log=print):
        self.cap_usd, self.spent, self.reserved = float(cap_usd), 0.0, 0.0
        self.client_factory, self.log = client_factory, log
        self._lock = threading.Lock()
        self._run_locks: dict = {}

    def _run_lock(self, run_dir: str) -> threading.Lock:
        with self._lock:
            return self._run_locks.setdefault(os.path.realpath(run_dir), threading.Lock())

    def _settle(self, reserved: float, usd: float) -> None:
        with self._lock:
            self.reserved = round(max(0.0, self.reserved - reserved), 6)
            self.spent = round(self.spent + usd, 6)

    def send(self, run_dir: str, text: str, anchor: dict | None = None, kind: str = "question",
             layer: str | None = None, proposed_change: str | None = None, stage: str | None = None,
             verdict: str | None = None, shown_evidence: dict | None = None) -> dict:
        if not str(text or "").strip():
            return {"ok": False, "error": "empty message"}
        if kind not in KINDS:
            return {"ok": False, "error": f"kind must be one of {', '.join(KINDS)}"}
        with self._run_lock(run_dir):
            return self._send(run_dir, text, anchor, kind, layer, proposed_change, stage, verdict, shown_evidence)

    def _send(self, run_dir, text, anchor, kind, layer, proposed_change, stage, verdict, shown_evidence=None) -> dict:
        fb = None
        if kind in STEERING:
            try:
                fb = append_feedback(run_dir, {"kind": kind, "layer": layer, "text": text, "anchor": anchor,
                                               "proposed_change": proposed_change, "stage": stage,
                                               "verdict": verdict, "evidence": shown_evidence})
            except ValueError as e:
                return {"ok": False, "error": str(e)}
        try:
            shown_ok = _evidence_for(run_dir, anchor, shown_evidence) if (anchor or {}).get("file") else None
        except ValueError as e:
            return {"ok": False, "feedback": fb, "error": str(e)}
        ctx = chat_context(run_dir)
        hist, _ = read_jsonl_rel(run_dir, CHAT)
        msgs = [{"role": r["role"], "content": r["text"]} for r in hist[-HISTORY_TURNS:]
                if r.get("role") in ("user", "assistant") and r.get("text")]
        a = anchor or {}
        prefix = (f"[Anchored to {a.get('kind')} {a.get('id')} in {a.get('file')}]\n" if a.get("kind") else "")
        ev = (fb or {}).get("evidence") or shown_ok
        if ev and ev.get("excerpt") is not None:
            prefix += (f"[Evidence from {ev['file']} (sha256 {str(ev.get('sha256'))[:12]}, round {ev.get('round')}): "
                       f"{ev['excerpt']}]\n")
            ctx["seen"][ev["file"]] = ctx["seen"].get(ev["file"], "") + "\n" + str(a.get("id") or "") + "\n" + ev["excerpt"]
        elif ev and ev.get("error"):
            prefix += f"[The anchored file cannot be read: {ev['error']}]\n"
        if fb:
            prefix += (f"[Owner {kind}, recorded as feedback {fb['id']}, layer {layer}. Acknowledge: what you "
                       "would change and in which layer.]\n")
        msgs.append({"role": "user", "content": prefix + text})
        try:
            client = self.client_factory()
        except Exception as e:  # noqa: BLE001 -- no key or no SDK: said on the page
            return {"ok": False, "feedback": fb, "error": str(e)}
        system = [{"type": "text", "text": ctx["stable"], "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": ctx["run"], "cache_control": {"type": "ephemeral"}}]
        n_in, how = input_token_bound(client, system, msgs)
        worst = worst_case_usd(n_in)
        with self._lock:
            if self.spent + self.reserved + worst > self.cap_usd:
                return {"ok": False, "feedback": fb, "error": f"chat cap: ${self.spent:.3f} spent and "
                        f"${self.reserved:.3f} reserved by messages in flight, of ${self.cap_usd:.2f}; this message "
                        f"could cost up to ${worst:.3f} ({n_in} input tokens by {how}). Raise --chat-cap-usd to "
                        "continue."}
            self.reserved = round(self.reserved + worst, 6)
        user_row = {"ts": _now(), "role": "user", "text": prefix + text, "anchor": anchor, "kind": kind,
                    "stage": stage, "feedback_id": fb and fb["id"], "evidence": ev}
        snap = None
        try:
            with client.beta.messages.stream(
                    model=MODEL, max_tokens=MAX_TOKENS, thinking={"type": "adaptive"},
                    output_config={"effort": EFFORT}, betas=[FALLBACK_BETA], fallbacks="default",
                    system=system, messages=msgs) as stream:
                try:
                    msg = stream.get_final_message()
                except Exception:
                    try:
                        snap = stream.current_message_snapshot
                    except Exception:  # noqa: BLE001 -- no snapshot: the usage is unknown
                        snap = None
                    raise
        except Exception as e:  # noqa: BLE001 -- the SDK's typed errors, shown as text (never the key)
            # No final usage: a partial snapshot is a lower bound (output may have been billed after it), so the whole
            # reservation is kept as spent (review round 2, P2-7). The partial figure is logged, not charged.
            u = getattr(snap, "usage", None)
            usd = worst
            self._settle(worst, usd)
            self.log(f"[console] chat call failed: {type(e).__name__}; charged the reservation ${usd:.4f}"
                     + (f" (partial usage seen: ${price(u):.4f})" if u is not None else ""))
            return {"ok": False, "feedback": fb, "error": f"Claude API error: {type(e).__name__}: {str(e)[:300]}",
                    "charged": usd, "spent": self.spent}
        fell_back = _fallback_ran(msg)
        usd = worst if fell_back else price(msg.usage)   # a fallback's per-model split is not priced here: keep it
        self._settle(worst, usd)
        reply = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        if msg.stop_reason == "refusal":
            reply = reply or "The model declined to answer this message."
        model = getattr(msg, "model", MODEL)
        bot = {"ts": _now(), "role": "assistant", "text": reply, "usd": usd, "model": model, "stage": stage,
               "uncited": uncited(reply), "citations": check_citations(reply, ctx["seen"]), "priced_at": MODEL,
               "fallback": fell_back, "truncated_context": ctx["truncated"],
               "cache_read_tokens": getattr(msg.usage, "cache_read_input_tokens", 0)}
        _append(run_dir, CHAT, user_row)
        _append(run_dir, CHAT, bot)
        self.log(f"[console] chat reply ${usd:.4f} (session ${self.spent:.4f} of ${self.cap_usd:.2f})")
        return {"ok": True, "reply": bot, "feedback": fb, "spent": self.spent, "cap": self.cap_usd}


# ------------------------------------------------------------------ projects, build on a run
def _within(path: str, root: str) -> bool:
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r + os.sep)


def discover(roots: list, mirror_roots: list = (), depth: int = 4) -> list:
    """Every run under the roots. A run is a mirror when its real path is inside a mirror root, whichever root
    found it first (roots may overlap)."""
    out, seen = [], set()
    mroots = [os.path.realpath(os.path.expanduser(r)) for r in mirror_roots]
    for root in list(roots) + list(mirror_roots):
        root = os.path.realpath(os.path.expanduser(root))
        if not os.path.isdir(root):
            continue
        for dp, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")) and d != OWNER_DIR)
            if dp[len(root):].count(os.sep) >= depth:
                dirs[:] = []
            if "run_state.json" in files and dp not in seen:
                seen.add(dp)
                dirs[:] = []
                out.append(run_summary(dp, root, any(_within(dp, m) for m in mroots)))
    return sorted(_short_labels(out), key=lambda r: r.get("started") or "", reverse=True)


def _short_labels(runs: list) -> list:
    """F2_baseline -> F2 when that is unique; else the full label; else the full label and the run folder."""
    def count(key):
        n: dict = {}
        for r in runs:
            n[key(r)] = n.get(key(r), 0) + 1
        return n
    for r in runs:
        r.setdefault("path_label", r["label"])
    full = count(lambda r: r["path_label"])
    for r in runs:
        r["full_label"] = r["path_label"] if full[r["path_label"]] == 1 else f"{r['path_label']}/{r['name']}"
    short = count(lambda r: r["full_label"].split("/")[0].split("_")[0])
    for r in runs:
        k = r["full_label"].split("/")[0].split("_")[0]
        r["label"] = k if short[k] == 1 and k else r["full_label"]
    return runs


def run_summary(run_dir: str, root: str | None = None, mirror: bool = False) -> dict:
    st, err = load_run_state(run_dir)
    meta = _meta(run_dir)
    hist = (st or {}).get("history") or []
    started = meta.get("started") or (hist[0].get("at") if hist and isinstance(hist[0], dict) else "") or _mtime(
        os.path.join(run_dir, "run_state.json"))
    label, group = run_label(run_dir, root)
    head = headline(run_dir)
    resumed = _resumed(hist)
    return {"id": run_dir, "name": os.path.basename(run_dir), "mirror": mirror, "label": label, "group": group,
            "headline_number": headline_number(head), "finished": finished(run_dir, st), "resumed": resumed,
            "question": str((st or {}).get("question") or meta.get("question") or ""), "started": started,
            "state": (st or {}).get("state"), "status": run_status(run_dir, st), "paused": (st or {}).get("paused"),
            "headline": head, "engine": engine_of(run_dir, st), "error": err,
            **identity(run_dir, st, head)}


# ------------------------------------------------------------------ lineage: projects, scenarios, compare
LINEAGE_FIELDS = ("horizon", "objective.outcome", "objective.horizon", "objective.delivery", "decision_units.engine_attributes",
                  "decision_units.defaults_accepted", "constraints", "analysis.decision_tests")


def _dig(doc, dotted: str):
    for k in dotted.split("."):
        doc = doc.get(k) if isinstance(doc, dict) else None
    return doc


def _flat(v, path: str, out: dict) -> dict:
    if isinstance(v, dict):
        for k in sorted(v, key=str):
            _flat(v[k], f"{path}.{k}", out)
    else:
        out[path] = json.dumps(v, sort_keys=True, default=str) if isinstance(v, list) else str(v)
    return out


def spec_fields(spec) -> dict:
    """The lineage fields of a decision spec as flat path -> value strings. defaults_accepted: its keys;
    analysis.decision_tests: by id, with every field (the values tested among them)."""
    out: dict = {}
    for f in LINEAGE_FIELDS:
        v = _dig(spec, f)
        if v is None:
            continue
        if f.endswith("defaults_accepted"):
            v = sorted(v) if isinstance(v, (dict, list)) else v
            v = [x if not isinstance(x, dict) else x.get("id") or x.get("name") for x in v] if isinstance(v, list) else v
        elif f.endswith("decision_tests") and isinstance(v, list):
            v = {str(t.get("id")): t for t in v if isinstance(t, dict)} | {str(t): {"id": t} for t in v
                                                                          if not isinstance(t, dict)}
        _flat(v, f, out)
    return out


def spec_diff(before: dict, after: dict) -> list:
    """[{field, before, after}] over two spec_fields() maps; a field one side lacks is None."""
    return [{"field": k, "before": before.get(k), "after": after.get(k)}
            for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]


def _val(v) -> str:
    """A diff value for a phrase: 2027.0 -> 2027, [2024.0, 2027.0] -> 2024–2027, long text cut."""
    if v is None:
        return "—"
    try:
        x = json.loads(v)
    except ValueError:
        x = v
    num = (lambda n: str(int(n)) if isinstance(n, float) and n.is_integer() else str(n))
    if isinstance(x, list) and len(x) == 2 and all(isinstance(n, (int, float)) for n in x):
        return f"{num(x[0])}–{num(x[1])}"
    t = num(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else str(v)
    return t if len(t) <= 24 else t[:22] + "…"


def diff_phrase(diff: list, n: int = 4) -> str:
    """A short phrase for a spec diff, in LINEAGE_FIELDS order. A block added or removed whole reads as
    "+name value" or "−name" (its `value` child, if any); a changed leaf as "field before → after"."""
    def rank(f):
        return next((i for i, x in enumerate(LINEAGE_FIELDS) if f.startswith(x)), len(LINEAGE_FIELDS))
    groups: dict = {}
    for d in sorted(diff, key=lambda d: (rank(d["field"]), d["field"])):
        groups.setdefault(d["field"].rsplit(".", 1)[0] if "engine_attributes." in d["field"] else d["field"], []).append(d)
    bits = []
    for key, ds in groups.items():
        name = key.split("engine_attributes.")[-1].replace("decision_units.", "")
        val = next((d for d in ds if d["field"].endswith(".value")), ds[0])
        if all(d["before"] is None for d in ds):
            bits.append(f"+{name} {_val(val['after'])}" if len(ds) > 1 or val["after"] else f"+{name}")
        elif all(d["after"] is None for d in ds):
            bits.append(f"−{name}")
        else:
            bits += [f"{d['field'].split('engine_attributes.')[-1].replace('decision_units.', '')} "
                     f"{_val(d['before'])} → {_val(d['after'])}" for d in ds]
    return " · ".join(bits[:n]) + (f" · +{len(bits) - n} more" if len(bits) > n else "")


def package_counts(run_dir: str) -> dict:
    """allocation.csv: how many units each package component (split on "+") is assigned to."""
    if not _rexists(run_dir, "allocation.csv"):
        return {}
    counts: dict = {}
    try:
        e = _load_rel(run_dir, "allocation.csv")
        if e["err"]:
            raise ValueError(e["err"])
        import io
        with io.StringIO(e["parsed"], newline="") as f:
            for row in csv.DictReader(f):
                for c in str(row.get("package") or "").split("+"):
                    if c.strip():
                        counts[c.strip()] = counts.get(c.strip(), 0) + 1
    except (OSError, csv.Error, ValueError) as e:
        return {"_error": f"{type(e).__name__}: {e}"}
    return dict(sorted(counts.items()))


def _sha(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _sha_rel(run_dir: str, rel: str) -> str | None:
    """sha256 of a run artifact, read through read_nofollow (None when it cannot be read)."""
    return _load_rel(run_dir, rel)["sha256"] if _rexists(run_dir, rel) else None


def end_reason(st: dict | None, status: str | None, chip: str) -> str:
    st = st or {}
    hist = [h for h in st.get("history") or [] if isinstance(h, dict) and "to" in h]
    why = str((hist[-1].get("why") if hist else "") or "")
    where = stage_word(hist[-1].get("from") if hist and st.get("state") == "FINAL" else st.get("state"))
    if chip == "failed":
        if "timed out" in why:
            return f"{where} timed out"
        if re.search(r"Traceback|line \d+|Error\b", why):
            return f"{where} crashed"
        return why.split("\n")[0][:60] or str(status)
    return {"stopped": f"stopped in {where}", "waiting": f"waiting: {st.get('paused')}", "running": f"in {where}",
            "completed": str(status or "completed")}.get(chip, "")


def stage_word(s) -> str:
    return str(s or "?").lower().replace("_", " ")


def identity(run_dir: str, st: dict | None, head: str = "") -> dict:
    """What tells runs of one question apart: spec sha and sign-off, engine and version, outcome, package
    counts, decision-test verdicts, how it ended, spend."""
    so, _ = _rread(run_dir, "decision_spec.signoff.json")
    so = so if isinstance(so, dict) else {}
    spec_sha = (so.get("sha256") or {}).get("decision_spec.yaml") if isinstance(so.get("sha256"), dict) else None
    spec_sha = spec_sha or _sha_rel(run_dir, "decision_spec.yaml")
    ec, _ = _rread(run_dir, "engine_choice.json")
    eng = {}
    if isinstance(ec, dict) and isinstance(ec.get("engine"), dict):
        ref = ec["engine"].get("ref")
        cand = next((c for c in ec.get("candidates") or [] if isinstance(c, dict) and c.get("ref") == ref), {})
        eng = {"ref": ref, "name": cand.get("name"), "version": cand.get("version"), "at": ec.get("at")}
    tests = {}
    for c in decision_test_cards(run_dir):
        if c["kind"] == "decision_test":
            tests[c["anchor"]["id"]] = c.get("state")
    status = run_status(run_dir, st)
    active = is_active(run_dir, st)
    chip = ("failed" if status in FAIL_STATUSES else "completed" if status else "waiting" if (st or {}).get("paused")
            else "running" if active else "stopped" if not finished(run_dir, st) else "ended")
    m = re.search(r"95% (?:UI|CI|interval)[^\d$]*([^)]+)\)", head or "")
    top = next((x for x in results_summary(run_dir) if x.get("ui")), None)
    return {"spec_sha": spec_sha, "signed_at": so.get("signed_at"), "signed_by": so.get("signed_by"),
            "signoff_note": so.get("note"), "engine_id": eng, "packages": package_counts(run_dir), "tests": tests,
            "ui": m.group(1).strip() if m else None,
            "ui_vals": top["ui"] + [top.get("level")] if top else None, "chip": chip, "ended": end_reason(st, status, chip),
            "spent_usd": (st or {}).get("spent_usd"), "launch_commit": _meta(run_dir).get("commit"),
            "spec_fields": spec_fields(_rread(run_dir, "decision_spec.yaml")[0]),
            "first_at": min((h.get("at") or "" for h in (st or {}).get("history") or [] if isinstance(h, dict)
                             and h.get("from") == "SIGN"), default=""), "last_at": _last_at(st),
            "has_engine_choice": bool(eng), "state_sha": _sha_rel(run_dir, "run_state.json"),
            "batches": batch_info(run_dir), "stop": stop_detail(run_dir, st), "operator_note": operator_note(run_dir, st)}


def _resumed(st_hist: list) -> bool:
    return any(h.get("from") == "FINAL" for h in st_hist if isinstance(h, dict))


def component_names(question: str, ids) -> dict:
    """A component id spelled out from the question when its letters are the initials of consecutive words
    ("cm" -> "case management", "smc" -> "seasonal malaria chemoprevention"); otherwise the id."""
    words = re.findall(r"[A-Za-z][A-Za-z-]*", question or "")
    out = {}
    for c in ids:
        k = str(c).lower()
        if not k.isalpha() or len(k) < 2:
            continue
        for i in range(len(words) - len(k) + 1):
            w = words[i:i + len(k)]
            if "".join(x[0].lower() for x in w) == k and all(len(x) > 2 for x in w):
                out[c] = " ".join(x.lower() for x in w)
                break
    return out


def short_title(question: str) -> str:
    """The question's first clause, without a leading "Build a ... model of": the project's name."""
    q = re.split(r"(?<=[.?!])\s", (question or "").strip(), maxsplit=1)[0]
    q = re.split(r",|;|\s(?:using|with|that|so that|in order to)\s", q, maxsplit=1)[0]
    q = re.sub(r"^(?:build|develop|create|construct|write|make)\s+(?:an?\s+|the\s+)?[\w\s-]*?\bmodel\s+of\s+", "", q,
               flags=re.I)
    q = re.sub(r"\s*\([^)]*\)", "", q).strip(" .")
    return q[:1].upper() + q[1:]


def projects(runs: list) -> list:
    """Runs grouped by question into projects; inside one, the lineage in order (oldest first in `lineage`),
    each run's relation to the one before it and its scenario (the spec diff against it). A staging copy is
    a draft, not a run: a dir that never got past sign-off (state SPEC or SIGN and no engine_choice.json),
    or a byte-identical copy of another run's run_state.json (the copy whose label sorts first is the run)."""
    by_sha: dict = {}
    for r in runs:
        by_sha.setdefault((r.get("state_sha"), r.get("spec_sha")), []).append(r)
    for r in runs:
        twins = sorted(by_sha.get((r.get("state_sha"), r.get("spec_sha"))) or [], key=lambda x: x["label"])
        r["draft"] = None
        if r.get("state") in ("SPEC", "SIGN") and not r.get("has_engine_choice"):
            r["draft"] = "never got past sign-off"
        elif r.get("state_sha") and len(twins) > 1 and twins[0] is not r:
            r["draft"] = f"a copy of {twins[0]['label']}"
    out: dict = {}
    for r in runs:
        out.setdefault(r.get("question") or "", {"question": r.get("question") or "", "title": short_title(r.get("question")),
                                                 "lineage": [], "drafts": []})
        out[r.get("question") or ""]["drafts" if r["draft"] else "lineage"].append(r)
    for pj in out.values():
        pj["lineage"].sort(key=lambda r: (r.get("signed_at") or "", (r.get("engine_id") or {}).get("at") or "",
                                          r.get("first_at") or "", r.get("last_at") or ""))
        prev = None
        for r in pj["lineage"]:
            r["prev"], r["relation"], r["scenario"], r["diff"], r["shift"] = None, None, "", [], ""
            if prev:
                r["prev"], r["diff"] = prev["id"], spec_diff(prev.get("spec_fields") or {}, r.get("spec_fields") or {})
                # Inferred from the question and the order: the harness records no parent run id (open item).
                same = bool(r.get("spec_sha")) and r.get("spec_sha") == prev.get("spec_sha")
                r["relation"] = (("resumed (inferred)" if r.get("resumed") else "relaunched (inferred)") if same
                                 else "related run (inferred)")
                r["scenario"] = (("same signed spec" if r.get("signed_at") and prev.get("signed_at") else "same spec")
                                 if same else diff_phrase(r["diff"]) or "the spec differs outside the compared fields")
                base = next((x for x in reversed(pj["lineage"][:pj["lineage"].index(r)]) if x.get("packages")), None)
                a, b = (base or {}).get("packages") or {}, r.get("packages") or {}
                if a and b and not a.get("_error") and not b.get("_error"):
                    def weight(c):   # the units moved, times the share of the component's units that moved
                        d = abs(b.get(c, 0) - a.get(c, 0))
                        return d * d / max(a.get(c, 0), b.get(c, 0), 1)
                    top = sorted((c for c in set(a) | set(b) if b.get(c, 0) != a.get(c, 0)),
                                 key=lambda c: (-weight(c), c))[:2]
                    names = component_names(pj["question"], set(a) | set(b))
                    r["shifts"] = [{"component": c, "name": names.get(c, c), "before": a.get(c, 0), "after": b.get(c, 0)}
                                   for c in top]
                    if top:
                        r["shift"] = " · ".join(f"{x['name']}: {x['before']} → {x['after']} units" for x in r["shifts"])
                        r["shift_vs"] = base["label"]
            prev = r
        for i, r in enumerate(pj["lineage"]):        # where reused engine points were computed
            for b in r.get("batches") or []:
                if b.get("reused"):
                    src = next((x for x in reversed(pj["lineage"][:i]) for y in x.get("batches") or []
                                if y.get("code_fp") == b.get("code_fp") and y.get("out") == b.get("out")), None)
                    b["reused_from"] = src["label"] if src else None
    return sorted(out.values(), key=lambda pj: max((r.get("last_at") or "" for r in pj["lineage"] + pj["drafts"]),
                                                   default=""), reverse=True)


def compare(a: dict, b: dict) -> dict:
    """Two run summaries side by side: spec diff, engines, headline, package counts, decision tests."""
    comps = sorted(set(a.get("packages") or {}) | set(b.get("packages") or {}))
    tids = sorted(set(a.get("tests") or {}) | set(b.get("tests") or {}))
    return {"a": a["id"], "b": b["id"], "labels": [a["label"], b["label"]],
            "same_spec": bool(a.get("spec_sha")) and a.get("spec_sha") == b.get("spec_sha"),
            "spec_shas": [a.get("spec_sha"), b.get("spec_sha")],
            "spec_diff": spec_diff(a.get("spec_fields") or {}, b.get("spec_fields") or {}),
            "engines": [a.get("engine_id"), b.get("engine_id")],
            "headline": [[a.get("headline_number"), a.get("ui")], [b.get("headline_number"), b.get("ui")]],
            "ui_vals": [a.get("ui_vals"), b.get("ui_vals")],
            "packages": {c: [(a.get("packages") or {}).get(c, 0), (b.get("packages") or {}).get(c, 0)] for c in comps
                         if not c.startswith("_")},
            "tests": {t: [(a.get("tests") or {}).get(t), (b.get("tests") or {}).get(t)] for t in tids},
            "spent": [a.get("spent_usd"), b.get("spent_usd")], "chips": [[a.get("chip"), a.get("ended")],
                                                                          [b.get("chip"), b.get("ended")]]}


def _fingerprint_refusal(run_dir: str, fn: str) -> str | None:
    """Why compute.code_fingerprint is not run for this summary, or None: only what the confined fingerprint does
    not check itself (a function file that is absent here)."""
    f = fn.partition("::")[0]
    if os.path.isabs(f) and not os.path.isfile(f):
        return "the batch function's file is not at that path on this machine"
    return None


def batch_reuse(run_dir: str) -> list:
    """For each stored engine batch (an --out with its .summary.json): would scripts/compute.py reuse its rows
    with today's code? The same test _reusable makes: the summary's fn and code_fp against code_fingerprint."""
    import compute as C
    out = []
    for s in sorted(glob.glob(os.path.join(run_dir, "**", "*.summary.json"), recursive=True)):
        if C.PROGRESS_DIR in s.split(os.sep):
            continue
        doc, err = _rread(run_dir, _rel(run_dir, s))
        rel = _rel(run_dir, s[:-len(".summary.json")])
        if err or not isinstance(doc, dict) or not doc.get("fn") or not doc.get("code_fp"):
            out.append({"out": rel, "reused": False, "why": err or "the summary names no fn and code_fp"})
            continue
        why = _fingerprint_refusal(run_dir, str(doc["fn"]))
        if why:
            out.append({"out": rel, "fn": doc["fn"], "reused": False, "why": why})
            continue
        o = s[:-len(".summary.json")]
        try:     # confined: no link followed under models/ or data/, nothing outside the run dir read (round 3)
            fp = C.code_fingerprint(run_dir, doc["fn"], exclude=(o, o + ".tmp", o + ".summary.json",
                                                                 o + ".summary.json.tmp", o + C.PARTIAL_EXT,
                                                                 o + C.JOB_EXT, o + ".log"), confined=True)
        except ValueError as e:
            out.append({"out": rel, "fn": doc["fn"], "reused": False, "why": f"not checked: {e}"})
            continue
        out.append({"out": rel, "fn": doc["fn"], "n": doc.get("n"), "reused": fp == doc["code_fp"],
                    "why": "code fingerprint matches" if fp == doc["code_fp"] else
                    "code fingerprint differs (model code, data or the repo's scripts/ or toolkit/ changed)"})
    return out


BUILD_ON_FILES = ("analysis_settings.yaml", "engine_choice.json", "data/derivations.yaml",
                  "data/derivations_record.json", "data/README.md")
BUILD_ON_DIRS = ("data/derive", "data/derived")


def build_on(run_dir: str, drafts_root: str) -> dict:
    """A draft for a new run that reuses this run's assets. Launches nothing; the page shows the commands. Every
    file is read through confined() and written without following a link; links in the source are skipped."""
    st, err = load_run_state(run_dir)
    if err:
        raise ValueError(err)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    name = f"{stamp}_{secrets.token_hex(2)}_from_{os.path.basename(run_dir)[:60]}"
    parent = os.path.realpath(os.path.dirname(os.path.abspath(drafts_root)))
    sub = f"{os.path.basename(os.path.abspath(drafts_root))}/{name}"
    os.close(_dir_fd(parent, sub.split("/")))       # refuses a linked _drafts/ before anything is copied
    dest = os.path.join(parent, sub)
    copied, skipped = [], []

    def put(src_rel, dest_rel):
        try:
            data = read_nofollow(os.path.realpath(run_dir), check_rel(src_rel), _MAX_READ)[0]
        except (ValueError, OSError) as e:
            skipped.append(f"{src_rel}: {e}")
            return None
        write_nofollow(parent, f"{sub}/{dest_rel}", data, exclusive=True)
        return data
    spec_rel = "decision_brief.signed/decision_spec.yaml"
    spec_rel = spec_rel if _rexists(run_dir, spec_rel) else "decision_spec.yaml"
    spec_bytes = put(spec_rel, "decision_spec.yaml") if _rexists(run_dir, spec_rel) else None
    if spec_bytes is not None:
        copied.append(spec_rel)
    for rel in BUILD_ON_FILES:
        if _rexists(run_dir, rel) and put(rel, rel) is not None:
            copied.append(rel)
    for rel in BUILD_ON_DIRS:
        top = os.path.join(run_dir, rel)
        if not os.path.lexists(top):
            continue
        if os.path.islink(top) or not os.path.isdir(top):
            skipped.append(f"{rel}: a link or not a directory")
            continue
        for dp, dirs, fns in os.walk(top):
            for x in [d for d in dirs if os.path.islink(os.path.join(dp, d))]:
                skipped.append(f"{_rel(run_dir, os.path.join(dp, x))}: a link")
                dirs.remove(x)
            for fn in sorted(fns):
                r = _rel(run_dir, os.path.join(dp, fn))
                if os.path.islink(os.path.join(dp, fn)):
                    skipped.append(f"{r}: a link")
                else:
                    put(r, r)
        copied.append(rel + "/")
    spec_status, serr = _spec_status(run_dir)
    q = st.get("question") or ""
    manifest = {
        "source_run": run_dir, "question": q, "written_at": _now(), "producer": "the run console",
        "spec": {"copied_from": spec_rel if spec_bytes is not None else None,
                 "sha256": hashlib.sha256(spec_bytes).hexdigest() if spec_bytes is not None else None,
                 "signoff_status": spec_status or serr,
                 "signed_sha256": ((_rread(run_dir, SPEC_SIGNOFF)[0] or {}).get("sha256") or {}).get(SPEC)
                 if spec_status in ("signed", "changed") and isinstance(_rread(run_dir, SPEC_SIGNOFF)[0], dict)
                 else None},
        "engine": engine_of(run_dir, st), "copied": copied, "skipped": skipped, "batches": batch_reuse(run_dir),
        "commands": {"continue_this_run": f"python3 main.py --v2 --resume {run_dir}",
                     "new_run": f"python3 main.py --v2 {json.dumps(q)}"},
        "harness_support": [
            "A new v2 run starts at SPEC and drafts its own spec (h_SPEC, draft_spec): there is no seed-spec input, so "
            "the copied spec is a reference for the owner, not an input the run reads.",
            "stage_inputs refuses files under runs/, so the copied derivations and derived data cannot be staged "
            "into a new run by naming them in the question.",
            "A stored batch is reused only inside the run dir that holds it, when compute.py's code fingerprint "
            "matches (see `batches`)."],
    }
    write_nofollow(parent, f"{sub}/build_on.json", json.dumps(manifest, indent=1, default=str).encode(),
                   exclusive=True)
    return {"draft_dir": dest, **manifest}


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
        if not self.config:
            return {"available": False, "why": "not available: start the console with --cloud-config <config.py>"}
        if not self.coiled_ok:
            return {"available": False, "why": "not available: coiled is not installed in this Python"}
        with self._lock:
            wait = CLOUD_MIN_S - (self.clock() - self.at)
            if not live or wait > 0:
                return {"available": True, "live": live, "snapshot": self.last,
                        "next_refresh_s": max(0, round(wait)) if self.last else 0}
            self.at = self.clock()
            snap = {"at": _now()}
            for cmd in ("status", "health", "report-cost"):
                snap[cmd] = _run([sys.executable, "cloud/run_cluster.py", self.config, cmd], self.runner)
            m = re.search(r"cluster (\S+): (\w+); keeper: (\w+); overflow: (\w+)", snap["status"]["out"] or "")
            snap["clusters"] = ({"run": {"name": m.group(1), "state": m.group(2)}, "keeper": {"state": m.group(3)},
                                 "overflow": {"state": m.group(4)}} if m else None)
            self.last = snap
            return {"available": True, "live": True, "snapshot": snap, "next_refresh_s": CLOUD_MIN_S}


# ------------------------------------------------------------------ the server
JS = os.path.join(os.path.dirname(HTML), "run_console.js")
DOSSIER_JS = os.path.join(os.path.dirname(HTML), "dossier.js")
# The page loads its one script from this server; artifact text can never run as script. Inline style attributes
# remain (style-src 'unsafe-inline'); nothing else is allowed.
PAGE_CSP = ("default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self'; style-src 'self' "
            "'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
# A served artifact (an SVG can hold a script) runs nothing, even when opened on its own.
FILE_CSP = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"


class Console:
    def __init__(self, roots: list, run_dirs: list = (), mirror: str | None = None, sync_cmd: str | None = None,
                 sync_s: float = 120.0, cloud_config: str | None = None, chat_cap_usd: float = 2.0,
                 core_hours_cap: float | None = None, client_factory=default_client, log=print,
                 remote_label: str | None = None, runner=None):
        self.roots, self.extra, self.mirror = [os.path.realpath(os.path.expanduser(r)) for r in roots], \
            [os.path.realpath(d) for d in run_dirs], mirror and os.path.realpath(mirror)
        self.sync_cmd, self.sync_s, self.cloud_config, self.cap = sync_cmd, sync_s, cloud_config, core_hours_cap
        self.remote_label, self.runner = remote_label, runner
        self.chat = ChatSession(chat_cap_usd, client_factory, log)
        self.cloud = CloudPanel(cloud_config)
        self.token, self.log = secrets.token_urlsafe(24), log
        self._secret = secrets.token_bytes(32)     # binds each decision card to its run and content (ticket)
        self._sync = {"last_ok_t": None, "last_ok_at": None, "last_attempt_at": None, "failures": 0,
                      "last_error": None}
        self._sync_lock = threading.Lock()
        first = self.roots[0] if self.roots else os.path.join(REPO, "runs")
        self.drafts_root, self.digest_dir = os.path.join(first, "_drafts"), os.path.join(first, "_owner")

    # -------------------------------------------------- runs
    def is_mirror(self, path: str) -> bool:
        return bool(self.mirror) and _within(path, self.mirror)

    def runs(self) -> list:
        found = discover(self.roots, [self.mirror] if self.mirror else [])
        ids = {r["id"] for r in found}
        return _short_labels([run_summary(d, os.path.dirname(d), self.is_mirror(d)) for d in self.extra
                              if d not in ids] + found)

    def run_dir(self, rid: str) -> tuple[str, bool]:
        rid = os.path.realpath(rid) if rid else rid
        for r in self.runs():
            if r["id"] == rid:
                return r["id"], r["mirror"]
        raise ValueError("unknown run")

    def remote_target(self, run_dir: str) -> tuple[str, str]:
        """(remote label, run dir name) of a mirrored run: --remote-label, else the mirror's <label>/runs/<run>
        layout (the agent VM's /tmp/ab/<label>/runs/<run>). Anything else is refused, never guessed."""
        if not self.is_mirror(run_dir):
            raise ValueError("not a mirrored run")
        rel = os.path.relpath(os.path.realpath(run_dir), self.mirror).split(os.sep)
        if self.remote_label:
            return self.remote_label, rel[-1]
        if len(rel) == 3 and rel[1] == "runs":
            return rel[0], rel[2]
        raise ValueError("cannot tell which remote label this mirrored run belongs to: mirror it as "
                         "<label>/runs/<run>, or start the console with --remote-label <label>")

    # -------------------------------------------------- the mirror's sync
    def sync_once(self, runner=None, now: float | None = None) -> None:
        runner = runner or subprocess.run
        t = time.time() if now is None else now
        try:
            r = runner(self.sync_cmd, shell=True, capture_output=True, text=True, timeout=max(60.0, self.sync_s * 5))
            rc, out = r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()[-500:]
        except (OSError, subprocess.SubprocessError) as e:
            rc, out = None, f"{type(e).__name__}: {str(e)[:400]}"
        with self._sync_lock:
            s = self._sync
            s["last_attempt_at"] = _iso(t)
            if rc == 0:
                s.update(last_ok_t=t, last_ok_at=_iso(t), failures=0)
            else:
                s["failures"] += 1
                s["last_error"] = f"exit {rc}: {out}"
        _CACHE.clear()          # every file may have been replaced: parse again

    def sync_status(self, now: float | None = None) -> dict | None:
        """None without a mirror. Stale: no successful sync yet, 2 failed syncs in a row, or no success within 3
        sync periods. Without --sync-cmd the console does not know how fresh the mirror is (stale: None)."""
        if not self.mirror:
            return None
        if not self.sync_cmd:
            return {"configured": False, "stale": None,
                    "why": "no --sync-cmd: the console does not know how fresh the mirror is"}
        t = time.time() if now is None else now
        with self._sync_lock:
            s = dict(self._sync)
        age = None if s["last_ok_t"] is None else t - s["last_ok_t"]
        why = ("no successful sync yet" if age is None else f"{s['failures']} failed syncs in a row"
               if s["failures"] >= 2 else f"no successful sync for {round(age)} s" if age > 3 * self.sync_s else "")
        return {"configured": True, "stale": bool(why), "why": why, "sync_s": self.sync_s,
                "age_s": None if age is None else round(age), **{k: v for k, v in s.items() if k != "last_ok_t"}}

    def sync_loop(self) -> None:
        while self.sync_cmd:
            self.sync_once()
            time.sleep(self.sync_s)

    # -------------------------------------------------- decisions: bound to the run and the content shown
    def ticket(self, run_dir: str, sub: str, content) -> str:
        msg = json.dumps([os.path.realpath(run_dir), sub, content], sort_keys=True, default=str).encode()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    SNAPSHOT_KEYS = ("spec_sha256", "derivations_sha256", "sheet_sha256")

    def _content(self, sub: str, body: dict):
        """What a decision card binds: the whole approval snapshot (spec), or the fingerprints shown (scope)."""
        return [str(body.get(k) or "") for k in self.SNAPSHOT_KEYS] if sub == "spec" else sorted(
            str(x) for x in body.get("fingerprints") or [])

    def answer(self, run_dir: str, sub: str, body: dict, now: float | None = None) -> dict:
        """A sign-off or scope answer from the page. Refused when the mirror is stale, when the remote run cannot
        be named, or when the card's ticket does not match this run and the content the request says it showed;
        then the sign command itself checks that content (sha256 / fingerprints) again, atomically."""
        mirror, remote = self.is_mirror(run_dir), None
        if mirror:
            ss = self.sync_status(now) or {}
            if ss.get("stale"):
                return {"ok": False, "refused": f"the mirror is stale ({ss.get('why')}): remote approval is off "
                                                "until a sync succeeds"}
            try:
                remote = self.remote_target(run_dir)
            except ValueError as e:
                return {"ok": False, "refused": str(e)}
        if not hmac.compare_digest(str(body.get("ticket") or ""), self.ticket(run_dir, sub, self._content(sub, body))):
            return {"ok": False, "refused": "this answer was prepared for another run or other content: reload the "
                                            "run and answer again"}
        if sub == "spec":
            return sign_spec(run_dir, body.get("note", ""), body.get("confirm") is True, mirror, self.cloud_config,
                             runner=self.runner, expect_sha256=body.get("spec_sha256"), remote=remote,
                             snapshot={k: body.get(k) for k in ("derivations_sha256", "sheet_sha256")})
        return answer_scope(run_dir, body.get("answers") or {}, body.get("note", ""), body.get("confirm") is True,
                            mirror, self.cloud_config, self.runner, fingerprints=body.get("fingerprints"),
                            remote=remote)

    def _with_tickets(self, run: dict, run_dir: str) -> dict:
        for c in run.get("cards") or []:
            if c.get("kind") == "decision_needed":
                content = self._content(c.get("sub"), c)
                c["ticket"], c["run_id"] = self.ticket(run_dir, c.get("sub"), content), run_dir
        return run

    # -------------------------------------------------- HTTP
    def handle(self, method: str, path: str, query: dict, body: dict):
        """(status, content_type, payload bytes or JSON-able)."""
        q = {k: v[0] for k, v in query.items()}
        if method == "GET" and path == "/":
            with open(HTML, encoding="utf-8") as f:
                return 200, "text/html; charset=utf-8", f.read().replace("__CONSOLE_TOKEN__", self.token).encode()
        if method == "GET" and path in ("/run_console.js", "/dossier.js"):
            with open(JS if path == "/run_console.js" else DOSSIER_JS, "rb") as f:
                return 200, "text/javascript; charset=utf-8", f.read()
        if method == "GET" and path == "/api/runs":
            runs = self.runs()
            return 200, "json", {"runs": runs, "projects": [{"question": p["question"], "title": p["title"],
                                                             "lineage": [r["id"] for r in p["lineage"]],
                                                             "drafts": [r["id"] for r in p["drafts"]]}
                                                            for p in projects(runs)], "sync": self.sync_status(),
                                 "chat": {"spent": self.chat.spent, "reserved": self.chat.reserved,
                                          "cap": self.chat.cap_usd, "key": api_key() is not None},
                                 "kinds": KINDS, "layers": LAYERS, "states": states(),
                                 "glossary": {k: {"text": v[0], "source": v[1]} for k, v in GLOSSARY.items()}}
        if method == "GET" and path == "/api/learning":
            return 200, "json", learning([r["id"] for r in self.runs()])
        if method == "GET" and path == "/api/compare":
            by = {r["id"]: r for r in self.runs()}
            projects(list(by.values()))
            a, b = by.get(os.path.realpath(q.get("a") or "")), by.get(os.path.realpath(q.get("b") or ""))
            if not a or not b:
                raise ValueError("unknown run")
            return 200, "json", compare(a, b)
        if method == "GET" and path == "/api/cloud":
            return 200, "json", self.cloud.snapshot(q.get("live") == "1")
        rd, mirror = self.run_dir(q.get("run") or body.get("run") or "")
        if method == "GET" and path == "/api/run":
            return 200, "json", {**self._with_tickets(read_run(rd, self.cap), rd), "mirror": mirror}
        if method == "GET" and path == "/api/dossier":
            import dossier as _dossier            # the run dossier (docs/design/v2-run-console.md §15)
            return 200, "json", _dossier.dossier(rd)
        if method == "GET" and path == "/api/file":
            rel = q.get("path") or ""
            data = read_file_bytes(rd, rel)
            want = q.get("sha") or ""
            if q.get("bound") and not re.fullmatch(r"[0-9a-f]{64}", want):
                # a snapshot-bound request names the digest it was rendered with; none means do not serve (round 5)
                raise ValueError(f"{rel}: a snapshot-bound request needs the figure's sha256; reload the run")
            if want and hashlib.sha256(data).hexdigest() != want:
                # the page asks for the bytes its snapshot recorded (review round 4, P2-6)
                raise ValueError(f"{rel} changed since the page was rendered: reload the run")
            ext = os.path.splitext(rel)[1].lower()
            ctype = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}.get(
                ext, "text/plain; charset=utf-8")
            return 200, ctype, data
        if method != "POST":
            return 404, "json", {"error": "not found"}
        if path == "/api/feedback":
            return 200, "json", append_feedback(rd, body)
        if path == "/api/chat":
            return 200, "json", self.chat.send(rd, body.get("text", ""), body.get("anchor"),
                                               body.get("kind", "question"), body.get("layer"),
                                               body.get("proposed_change"), body.get("stage"), body.get("verdict"),
                                               body.get("evidence"))
        if path == "/api/sign_spec":
            return 200, "json", self.answer(rd, "spec", body)
        if path == "/api/scope":
            return 200, "json", self.answer(rd, "scope", body)
        if path == "/api/build_on":
            return 200, "json", build_on(rd, self.drafts_root)
        if path == "/api/export":
            return 200, "json", {"path": export_digest([r["id"] for r in self.runs()], self.digest_dir)}
        return 404, "json", {"error": "not found"}


DEFAULT_PORT, PORT_TRIES = 8765, 20


def bind(console: Console, port: int | None = None):
    """The server on 127.0.0.1. No --port: the first free port from DEFAULT_PORT on (PORT_TRIES ports).
    An explicit --port that is busy is an error (ValueError, one line)."""
    ports = [port] if port is not None else range(DEFAULT_PORT, DEFAULT_PORT + PORT_TRIES)
    for p in ports:
        try:
            return make_server(console, "127.0.0.1", p)
        except OSError as e:
            if port is not None:
                raise ValueError(f"port {port} on 127.0.0.1 is in use ({e.strerror}); choose another with --port, "
                                 "or omit --port to use the next free one") from None
    raise ValueError(f"ports {DEFAULT_PORT}-{DEFAULT_PORT + PORT_TRIES - 1} on 127.0.0.1 are all in use; "
                     "choose one with --port")


def make_server(console: Console, host: str = "127.0.0.1", port: int = 8765):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("the run console binds to the loopback interface only (run outputs derive from licensed data)")

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # the default logs to stderr; no query strings (paths only)
            code = args[1] if len(args) > 1 else ""
            console.log(f"[console] {self.command} {urllib.parse.urlsplit(self.path).path} {code}")

        def _go(self, method):
            u = urllib.parse.urlsplit(self.path)
            host_ok = self.headers.get("Host", "").rsplit(":", 1)[0] in ("127.0.0.1", "localhost", "[::1]")
            if not host_ok:
                return self._send(403, "json", {"error": "bad Host header"})
            body = {}
            if method == "POST":
                if self.headers.get("X-Console-Token") != console.token:
                    return self._send(403, "json", {"error": "missing console token"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self._send(400, "json", {"error": "body is not JSON"})
            try:
                self._send(*console.handle(method, u.path, urllib.parse.parse_qs(u.query), body))
            except ValueError as e:
                self._send(400, "json", {"error": str(e)})
            except Exception as e:  # noqa: BLE001 -- shown on the page, the server keeps running
                self._send(500, "json", {"error": f"{type(e).__name__}: {str(e)[:300]}"})

        def _send(self, code, ctype, payload):
            data = json.dumps(payload, default=str).encode() if ctype == "json" else payload
            self.send_response(code)
            self.send_header("Content-Type", "application/json" if ctype == "json" else ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", PAGE_CSP if str(ctype).startswith("text/html") else
                             "default-src 'none'; frame-ancestors 'none'" if ctype == "json" else FILE_CSP)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

    return http.server.ThreadingHTTPServer((host, port), H)


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
        ok([s["state"] for s in sp["stages"]] == [s for s in states() if s != "PRESENTATION"]
           and states()[0] == "SPEC" and states()[-1] == "FINAL",
           "RC1b the stage list is stage_machine.STATES, without optional states never entered")
        rd2 = _toy_run(os.path.join(td, "runs", "proj", "run2"), "WRITE", None,
                       path=("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "CRITIQUE", "GATE",
                             "WRITE"))
        by2 = {s["state"]: s["status"] for s in stepper(load_run_state(rd2)[0])["stages"]}
        ok("FIX_LOOP" not in by2 and "OWNER_DECISION" not in by2 and "PRESENTATION" not in by2
           and by2["WRITE"] == "current" and by2["FINAL"] == "todo",
           f"RC1c FIX_LOOP, OWNER_DECISION and PRESENTATION are collapsed when never entered ({by2})")
        rd3 = _toy_run(os.path.join(td, "runs", "other", "run3"), "FINAL", None, "run_failed",
                       path=("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "FINAL"))
        sp3 = stepper(load_run_state(rd3)[0])["stages"]
        by3 = {s["state"]: s["status"] for s in sp3}
        ok(by3["ANALYZE"] == "failed" and by3["CRITIQUE"] == "skipped"
           and [s.get("why") for s in sp3 if s["state"] == "ANALYZE"] == ["step 6"],
           f"RC1d a failed run marks the state it failed in, with the reason ({by3})")
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
        ok(cs["used_core_h"] == 2.01 and c0.get("done") == 25 and c0.get("n") == 100
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
        # RC5 scope answers through scripts/scope_signoff.py, read back by the harness's reader; keyed by the
        # fingerprints the page showed (RV6)
        import scope_signoff as SS
        FP, FPS = "fp0000000000aaaa", ["fp0000000000aaaa"]
        for kw, why in ((dict(answers={FP: "accept"}, note="n", confirm=False, fingerprints=FPS), "not confirmed"),
                        (dict(answers={FP: "accept"}, note=" ", confirm=True, fingerprints=FPS), "note"),
                        (dict(answers={}, note="n", confirm=True, fingerprints=FPS), "missing"),
                        (dict(answers={FP: "maybe"}, note="n", confirm=True, fingerprints=FPS), "missing"),
                        (dict(answers={"decision_test:DT1": "accept"}, note="n", confirm=True, fingerprints=FPS),
                         "missing"),
                        (dict(answers={FP: "accept"}, note="n", confirm=True), "changed since it was shown")):
            r = answer_scope(rd, **kw)
            ok(not r["ok"] and why in r.get("refused", ""), f"RC5a scope answer refused: {why}")
        r = answer_scope(rd, {FP: "accept"}, "n", True, mirror=True, fingerprints=FPS)
        ok(not r["ok"] and "--cloud-config" in r["refused"], "RC5b a mirrored run is not answered locally")
        seen = []
        r = answer_scope(rd, {FP: "decline"}, "remote note", True, mirror=True, cloud_config="c.py", fingerprints=FPS,
                         remote=("F1", "run1"), runner=lambda cmd, **k: seen.append((cmd, open(cmd[4]).read()))
                         or NS(returncode=0, stdout="ok", stderr=""))
        ok(r["ok"] and seen and seen[0][0][1:4] == ["cloud/run_cluster.py", "c.py", "sign"]
           and f"{FP}: decline" in seen[0][1] and seen[0][0][-1] == "remote note",
           "RC5c a mirrored run is answered through run_cluster.py <config> sign <answers.yaml> ... <note>")
        r = answer_scope(rd, {FP: "accept"}, "the plan stands", True, fingerprints=FPS)
        ans, bad = SS.load_answers(rd)
        a = ans.get(FP) or {}
        ok(r["ok"] and not bad and a.get("decision") == "accept" and a.get("by") == "owner"
           and a.get("note") == "the plan stands",
           f"RC5d the answer round-trips through scope_signoff.load_answers ({r})")
        ok(bool((json.load(open(os.path.join(rd, SS.SIGNOFF))) or {}).get("answers")),
           "RC5e h_OWNER_DECISION's check (scope_signoff.json has answers) now holds")
        ok(inbox(rd)["scope"]["items"][0]["answer"] == "accept", "RC5f the inbox shows the recorded answer")
        # RC6 spec sign-off
        SHA = "ab" * 32
        ok(not sign_spec(rd, "n", False, expect_sha256=SHA)["ok"], "RC6a spec sign-off refused without confirmation")
        ok("note" in sign_spec(rd, "", True, expect_sha256=SHA).get("refused", ""),
           "RC6b spec sign-off refused without a note")
        r = sign_spec(rd, "n", True, expect_sha256=SHA)
        ok(not r["ok"] and "not awaiting" in r["refused"], f"RC6c no spec: refused ({r.get('refused')})")
        r = sign_spec(rd, "n", True, status_fn=lambda d: "signed", expect_sha256=SHA)
        ok(not r["ok"] and "signed" in r["refused"], "RC6d an already signed spec is refused")
        r = sign_spec(rd, "n", True, status_fn=lambda d: "awaiting")
        ok(not r["ok"] and "sha256" in r["refused"], "RC6d2 a sign request without the shown spec's sha256 is refused")
        seen = []
        r = sign_spec(rd, "looks right", True, status_fn=lambda d: "awaiting", expect_sha256=SHA,
                      runner=lambda cmd, **k: seen.append((cmd, k.get("env")))
                      or NS(returncode=0, stdout="{}", stderr=""))
        ok(r["ok"] and seen[0][0][1:] == ["agents/stage_machine.py", "sign", rd, "--by", "owner", "--note",
                                          "looks right", "--expect-sha256", SHA]
           and "MA_SIGNOFF_MODE" not in seen[0][1],
           "RC6e a confirmed sign runs stage_machine.py sign --by owner --expect-sha256 <the sha shown>")
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
           and names["run1"]["engine"] == "toy.engine:Toy" and names["run1"]["headline_number"] == "120 toy"
           and names["run1"]["label"] == "proj/run1" and names["run3"]["label"] == "other"
           and names["run1"]["group"] == names["run3"]["group"] == os.path.basename(td),
           f"RC9a runs are found under the roots with a label, question, headline, group and engine ({sorted(names)})")
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
        # RC12 a busy port: the next free one without --port; a one-line error with an explicit --port
        import socket
        global DEFAULT_PORT
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        busy, old_default = held.getsockname()[1], DEFAULT_PORT
        DEFAULT_PORT = busy
        con = Console([td], log=lambda *a: None)
        try:
            srv = bind(con)
            ok(srv.server_address[0] == "127.0.0.1" and srv.server_address[1] != busy,
               f"RC12a no --port and the default busy: the next free port ({busy} -> {srv.server_address[1]})")
            srv.server_close()
            try:
                bind(con, busy)
                ok(False, "RC12b an explicit busy --port is a clear error")
            except ValueError as e:
                ok(f"port {busy}" in str(e) and "in use" in str(e) and "\n" not in str(e),
                   f"RC12b an explicit busy --port is a one-line error ({e})")
            rc = main(["--roots", td, "--port", str(busy)])
            ok(rc == 1, "RC12c main exits 1 on a busy explicit port (no traceback)")
        finally:
            DEFAULT_PORT = old_default
            held.close()
        # RC13 a placeholder key counts as no key: no client is built and no call is made
        prev = os.environ.get("ANTHROPIC_API_KEY")
        try:
            for ph in ("", "...", "your-key-here", "sk-ant-xxxx", "<YOUR_KEY>"):
                os.environ["ANTHROPIC_API_KEY"] = ph
                made = []
                r = ChatSession(1.0, lambda: made.append(1) or default_client(), log=lambda *a: None).send(rd, "hi")
                ok(api_key() is None and not r["ok"] and "not set" in r["error"],
                   f"RC13 a placeholder key ({ph!r}) means no key and no call")
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-realistic"
            ok(api_key() == "sk-ant-api03-realistic", "RC13b a real-looking key is used")
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev
        # RC14 live vs finished, and compute used
        with tempfile.TemporaryDirectory() as t2:
            live = _toy_run(os.path.join(t2, "live"))
            cl = compute_status(live, None, load_run_state(live)[0])
            done = _toy_run(os.path.join(t2, "done"), "FINAL", None, "run_failed",
                            path=("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "FINAL"))
            cd = compute_status(done, None, load_run_state(done)[0])
            stale = compute_status(live, None, load_run_state(live)[0], now=time.time() + STALE_S + 1)
            ok(cl["live"] and cl["calls"][0]["live"] and cl["calls"][0]["eta_s"] is not None
               and cl["used_core_h"] == 2.01, f"RC14a an unfinished run with a fresh progress record is live: "
               f"an ETA, and used = settled + live core_s ({cl['used_core_h']})")
            ok(not cd["live"] and cd["calls"][0]["done"] == 25 and cd["calls"][0]["eta_s"] is None
               and cd["used_core_h"] == 2.0, f"RC14b a progress record left on a finished run is interrupted: "
               f"no ETA, used = settled only ({cd['used_core_h']})")
            ok(not stale["live"] and stale["calls"][0]["eta_s"] is None and stale["used_core_h"] == 2.0,
               "RC14c a progress record older than STALE_S is not live")
            json.dump({"question": "q", "started": "2026-01-01T00:00:00+00:00", "finished": "2026-01-01T01:00:00"},
                      open(os.path.join(live, "metadata.json"), "w"))
            ok(finished(live, load_run_state(live)[0]) and headline_number("It averts 12.34M dalys against x")
               == "12.34M DALYs" and headline_number("no number") == "",
               "RC14d metadata.json finished means finished; the headline number is read from the recommendation")
        # RC15 lineage: projects, staging copies, relations, the scenario diff, compare
        with tempfile.TemporaryDirectory() as t3:
            ts = "2026-01-01_000000_toy-question"
            fail_path = ("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "CONFIGURE", "ANALYZE", "FINAL")
            spec = {"horizon": {"start": 2016.0, "stop": 2027.0}, "objective": {"horizon": [2024.0, 2027.0]},
                    "decision_units": {"engine_attributes": {"season": {"column": "s"}}},
                    "constraints": {"envelope_usd": 100}, "analysis": {"decision_tests": [{"id": "DT1"}]}}

            def lin(rel, path, spec_doc, signed_at, version, alloc, resumed=False, status="run_failed"):
                d = _toy_run(os.path.join(t3, rel, ts), path[-1], None, status, path=path)
                st = json.load(open(os.path.join(d, "run_state.json")))
                st["question"] = "Toy question: one option per unit."
                for h in st["history"]:                     # each run its own times, as a relaunch would have
                    h["at"] = h["at"].replace("2026-01-01", signed_at[:10])
                st["history"] = [h for h in st["history"] if "to" in h]
                st["history"][-1]["why"] = "analysis failed: analyze: timed out after 3600s"
                if resumed:
                    st["history"] += [{"at": "2026-01-01T01:00:00+00:00", "from": "FINAL", "to": "ANALYZE", "why": "resume"},
                                      {"at": "2026-01-01T01:10:00+00:00", "from": "ANALYZE", "to": "CRITIQUE", "why": "built"}]
                    st["state"], st["status"] = "CRITIQUE", None
                json.dump(st, open(os.path.join(d, "run_state.json"), "w"))
                yaml.safe_dump(spec_doc, open(os.path.join(d, "decision_spec.yaml"), "w"))
                json.dump({"signed_by": "owner", "signed_at": signed_at, "note": f"sign {rel}",
                           "sha256": {"decision_spec.yaml": _sha(os.path.join(d, "decision_spec.yaml"))}},
                          open(os.path.join(d, "decision_spec.signoff.json"), "w"))
                json.dump({"at": signed_at, "engine": {"ref": "toy.engine:Toy"}, "candidates": [
                    {"ref": "toy.engine:Toy", "name": "toy", "version": version}]},
                          open(os.path.join(d, "engine_choice.json"), "w"))
                if alloc:
                    open(os.path.join(d, "allocation.csv"), "w").write(
                        "unit,package\n" + "".join(f"{i},{p}\n" for i, p in enumerate(alloc)))
                return d
            lin("A/runs", fail_path, spec, "2026-01-01T00:00:00+00:00", "0.5", None)
            lin("B_baseline", fail_path, spec, "2026-01-01T00:00:00+00:00", "0.5",
                ["cm+smc", "cm+smc", "cm"], resumed=True)
            spec2 = json.loads(json.dumps(spec))
            spec2["horizon"]["stop"], spec2["objective"]["delivery"] = 2029.25, [2024.0, 2027.0]
            spec2["decision_units"]["engine_attributes"]["routine"] = {"value": 3, "source": "toy"}
            lin("C_amended", fail_path, spec2, "2026-01-02T00:00:00+00:00", "0.6", ["cm", "cm", "cm+net"])
            cp = os.path.join(t3, "x_copy", ts)
            shutil.copytree(os.path.join(t3, "A", "runs", ts), cp)
            stg = _toy_run(os.path.join(t3, "x", ts), "SIGN", "awaiting_signoff", None, path=("SPEC", "SIGN"))
            st = json.load(open(os.path.join(stg, "run_state.json")))
            st["question"] = "Toy question: one option per unit."
            json.dump(st, open(os.path.join(stg, "run_state.json"), "w"))
            runs = discover([t3])
            pj = projects(runs)
            ok(len(pj) == 1 and [r["label"] for r in pj[0]["lineage"]] == ["A", "B", "C"]
               and sorted((r["label"], r["draft"]) for r in pj[0]["drafts"]) == [
                   ("x", "never got past sign-off"), ("x_copy", "a copy of A")],
               f"RC15a one project per question; lineage A, B, C by sign-off; staging copies are drafts "
               f"({[(r['label'], r.get('draft')) for r in runs]})")
            A, B, C = (pj[0]["lineage"] + [{}] * 3)[:3] if pj else ({}, {}, {})
            ok([A["relation"], B["relation"], C["relation"]] == [None, "resumed (inferred)", "related run (inferred)"]
               and B["scenario"] == "same signed spec",
               f"RC15b relations: resumed (FINAL -> ANALYZE, same spec sha), related run (inferred) (another spec sha) "
               f"({[A['relation'], B['relation'], C['relation']]})")
            fields = {d["field"]: (d["before"], d["after"]) for d in C["diff"]}
            ok(fields == {"horizon.stop": ("2027.0", "2029.25"), "objective.delivery": (None, "[2024.0, 2027.0]"),
                          "decision_units.engine_attributes.routine.source": (None, "toy"),
                          "decision_units.engine_attributes.routine.value": (None, "3")}
               and C["scenario"] == "horizon.stop 2027 → 2029.25 · +objective.delivery 2024–2027 · +routine 3",
               f"RC15c the scenario is the structured spec diff and its phrase ({C['scenario']!r}; {fields})")
            ok(C["engine_id"]["version"] == "0.6" and C["chip"] == "failed" and C["ended"] == "analyze timed out"
               and B["chip"] in ("running", "stopped") and B["ended"].endswith("critique"), f"RC15d engine version, chip and why it ended ({C['engine_id']}, "
               f"{C['chip']}, {C['ended']})")
            ok([(x["component"], x["before"], x["after"]) for x in C["shifts"]] == [("smc", 2, 0), ("net", 0, 1)]
               and C["shift_vs"] == "B",
               f"RC15e the package shift is against the nearest earlier run with an allocation ({C['shift']})")
            cmpd = compare(B, C)
            ok(cmpd["packages"] == {"cm": [3, 3], "net": [0, 1], "smc": [2, 0]}
               and [d["field"] for d in cmpd["spec_diff"]] == sorted(fields)
               and cmpd["engines"][0]["version"] == "0.5" and cmpd["engines"][1]["version"] == "0.6",
               f"RC15f compare: spec diff, engine versions, package counts side by side ({cmpd['packages']})")
            live = os.path.join(t3 + "_live", "live")
            shutil.copytree(os.path.join(t3, "C_amended", ts), os.path.join(live, ts))
            sp20 = yaml.safe_load(open(os.path.join(live, ts, "decision_spec.yaml")))
            sp20["horizon"]["stop"] = 2030.0
            yaml.safe_dump(sp20, open(os.path.join(live, ts, "decision_spec.yaml"), "w"))
            so20 = json.load(open(os.path.join(live, ts, "decision_spec.signoff.json")))
            so20.update(signed_at="2026-01-03T00:00:00+00:00",
                        sha256={"decision_spec.yaml": _sha(os.path.join(live, ts, "decision_spec.yaml"))})
            json.dump(so20, open(os.path.join(live, ts, "decision_spec.signoff.json"), "w"))
            pj20 = projects(Console([t3], mirror=live).runs())
            lin20 = pj20[0]["lineage"] if len(pj20) == 1 else []
            ok([(r["label"], r["mirror"]) for r in lin20] == [("A", False), ("B", False), ("C", False), ("live", True)]
               and lin20[-1]["relation"] == "related run (inferred)",
               f"RC20 --mirror combines with --roots: the mirrored live run joins the same project's lineage "
               f"({[(r['label'], r['mirror']) for r in lin20]})")
            shutil.rmtree(t3 + "_live")
            with make_server(Console([t3]), "127.0.0.1", 0) as srv:
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                base = f"http://127.0.0.1:{srv.server_address[1]}"
                j = json.load(urllib.request.urlopen(base + "/api/runs"))
                c2 = json.load(urllib.request.urlopen(base + "/api/compare?" + urllib.parse.urlencode(
                    {"a": B["id"], "b": C["id"]})))
                srv.shutdown()
            ok(len(j["projects"]) == 1 and len(j["projects"][0]["lineage"]) == 3 and c2["labels"] == ["B", "C"],
               "RC15g /api/runs lists projects and /api/compare answers")
        # RC16 reader support: every marked term is defined; no JSON shown; tables from structured data
        page = _page_sources()
        marked = set(re.findall(r'T\("([^"]+)"', page))
        ok(marked and marked <= set(GLOSSARY) and set(GATE_ACTIONS) <= set(GLOSSARY)
           and {f"rule {r}" for r in ("1", "2", "3", "3a", "4", "5")} <= set(GLOSSARY)
           and {"HIGH", "MEDIUM", "LOW"} <= set(GLOSSARY) and all(len(v) == 2 and v[0] and v[1] for v in GLOSSARY.values()),
           f"RC16a every term the page marks, every gate action, rule and severity has a definition and a source "
           f"(undefined: {sorted(marked - set(GLOSSARY))})")
        js = [ln for ln in page.splitlines() if "JSON.stringify" in ln]
        ok(all("anchor" in ln or "body: JSON.stringify" in ln for ln in js),
           "RC16b JSON.stringify is used only for anchors and request bodies, never for text the reader sees")
        g = [c for c in read_run(rd)["cards"] if c["kind"] == "gate"][0]
        ok(g["done_when"] and g["done_when"][0]["blocker"] == "T-001" and g["done_when"][0]["severity"] == "HIGH"
           and g["done_when"][0]["detail"] == "fail" and _words({"fixed": ["T-001", "T-002"], "rejected": []})
           == "fixed: T-001, T-002 · rejected: none", f"RC16c done-when rows join the gate status with the blocker; "
           f"fix-loop records read as words ({g['done_when'][:1]})")
        needs = re.search(r"const PAGE_NEEDS = \[([^\]]*)\]", page)
        keys = re.findall(r'"([a-z_]+)"', needs.group(1)) if needs else []
        _, _, served = Console([os.path.join(td, "runs")]).handle("GET", "/api/runs", {}, {})
        ok(keys and set(keys) <= set(served) and {k for k in re.findall(r"S\.meta\.([a-z_]+)", page)} <= set(keys),
           f"RC17 the keys the page needs from /api/runs (PAGE_NEEDS, and every S.meta.<key> it reads) are served "
           f"(needs {keys}; served {sorted(served)})")
        ok('id="banner"' in page and 'addEventListener("unhandledrejection"' in page and "AbortController" in page,
           "RC17b a failed fetch or render shows a banner; fetches time out instead of hanging")
        # RC19 plain words for patterns, components, project titles; notes newer than the run; units
        q19 = ("Build an agent-based model of toy fever across Examplia's 300 districts (D) using a toy engine. "
               "Options: seasonal mass chemoprevention (SMC) and case management.")
        ok(_check_words({"file": "m.md", "contains": "(?i)status[- ]quo|business[- ]as[- ]usual|current (X )?(plan|alloc)"})
           == "m.md mentions one of: status quo, business as usual, current X plan/alloc"
           and _check_words({"file": "m.md", "lacks": "(?i)DT2"}) == "m.md does not mention DT2"
           and component_names(q19, ["smc", "cm", "net_x"]) == {"smc": "seasonal mass chemoprevention", "cm": "case management"}
           and short_title(q19) == "Toy fever across Examplia's 300 districts",
           f"RC19a a pattern reads as its alternatives; components are spelled out from the question; the project "
           f"title is the first clause ({short_title(q19)!r}, {component_names(q19, ['smc', 'cm'])})")
        with tempfile.TemporaryDirectory() as t6:
            d6 = _toy_run(os.path.join(t6, "r"))
            st6 = load_run_state(d6)[0]
            open(os.path.join(d6, "progress.md"), "w").write("## Notes:\n\n- 2025-12-31 (operator): old\n")
            old = operator_note(d6, st6)
            open(os.path.join(d6, "progress.md"), "w").write("## Notes:\n\n- 2026-01-02 (operator): new\n")
            yaml.safe_dump({"data": {"parameters": [{"name": "p", "units": "days"}]}},
                           open(os.path.join(d6, "analysis", "engine.yaml"), "w"))
            yaml.safe_dump({"data": {"tests": [{"test_id": "prior_range_p", "verdict": "NO_CHANGE"}]}},
                           open(os.path.join(d6, "analysis", "decision_tests.yaml"), "w"))
            dt6 = [c for c in decision_test_cards(d6) if c["kind"] == "decision_test"]
            ok(old == "" and operator_note(d6, st6) == "2026-01-02 (operator): new"
               and dt6 and dt6[0]["parameter"] == "p" and dt6[0]["unit"] == "days",
               "RC19b only a note dated after the run's last transition is quoted; a decision test's value carries the "
               "parameter's unit from the engine card")
        with tempfile.TemporaryDirectory() as t5:
            f5 = os.path.join(t5, "a.yaml")
            open(f5, "w").write("x: 1\n")
            first = _read(f5)[0]
            open(f5, "w").write("x: 22\n")
            ok(first == {"x": 1} and _read(f5)[0] == {"x": 22} and _read(f5) is _read(f5),
               "RC18 parsed files are cached by mtime and size, and a rewritten file is read again")
        with tempfile.TemporaryDirectory() as t4:
            d = _toy_run(os.path.join(t4, "r"))
            os.makedirs(os.path.join(d, "analysis", "batch"))
            for rel, doc in (("analysis/point.yaml", {"data": {"averted": {"dalys": 1.5e6, "cases": 2e6}, "n_units": 10,
                                                                 "budget_allocatable": 80.0, "cost_allocated": 79.0,
                                                                 "mix": {"no_package": 2}}}),
                             ("analysis/uncertainty.yaml", {"data": {"headline": {"outcome": "dalys"}, "covers": ["costs"],
                                                                       "fixed_plan": {"dalys": {"ci_low": 1.2e6, "ci_high": 1.8e6,
                                                                                                "median": 1.4e6, "level": 0.95}}}}),
                             ("decision_spec.yaml", {"constraints": {"envelope_usd": 100.0}})):
                yaml.safe_dump(doc, open(os.path.join(d, rel), "w"))
            json.dump({"n": 4, "computed": 1, "reused": 3, "code_fp": "ab", "task_core_s": 10.0},
                      open(os.path.join(d, "analysis", "batch", "p.jsonl.summary.json"), "w"))
            sm = results_summary(d)
            ok([x["label"] for x in sm] == ["dalys averted", "cases averted", "envelope", "allocatable", "allocated",
                                            "units with no package"] and sm[0]["ui"] == [1.2e6, 1.8e6]
               and sm[0]["source"] == "analysis/point.yaml" and sm[2]["source"] == "decision_spec.yaml"
               and sm[-1]["of"] == 10 and batch_info(d)[0]["reused"] == 3,
               f"RC16d the summary: headline first with its UI, each value with its source; reused batch points ({sm})")
    with tempfile.TemporaryDirectory() as tr:
        _self_test_review(ok, tr)
    with tempfile.TemporaryDirectory() as tr2:
        _self_test_review2(ok, tr2)
    with tempfile.TemporaryDirectory() as tr3:
        _self_test_review3(ok, tr3)
    with tempfile.TemporaryDirectory() as tr4:
        _self_test_review4(ok, tr4)
    with tempfile.TemporaryDirectory() as tr5:
        _self_test_review5(ok, tr5)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _page_sources() -> str:
    """The page and every script it loads, as text (the self-test reads the page's code)."""
    out = [open(HTML, encoding="utf-8").read()]
    for name in ("run_console.js", "dossier.js"):
        js = os.path.join(os.path.dirname(HTML), name)
        if os.path.exists(js):
            out.append(open(js, encoding="utf-8").read())
    return "\n".join(out)


def _attr_interpolations(src: str) -> list:
    """Every ${...} inside a double-quoted HTML attribute of the page's templates: [(attribute, expression)]."""
    out = []
    for m in re.finditer(r'\s([a-zA-Z][\w-]*)="', src):
        i, n = m.end(), len(src)
        while i < n and src[i] not in '"\n':
            if src.startswith("${", i):
                depth, j, quote = 1, i + 2, None
                while j < n and depth:
                    ch = src[j]
                    if quote:
                        if ch == "\\":
                            j += 1
                        elif ch == quote:
                            quote = None
                    elif ch in "\"'":
                        quote = ch
                    elif ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                    j += 1
                out.append((m.group(1), src[i + 2:j - 1]))
                i = j
            else:
                i += 1
    return out


_SAFE_ATTR = re.compile(r"^\s*(?:esc|ref|q)\(")
_LIT_TERNARY = re.compile(r"""^[^?`$]*\?\s*("[^"]*"|'[^']*')\s*:\s*("[^"]*"|'[^']*')\s*$""")


def _self_test_review4(ok, td: str) -> None:
    """RY5-RY6: Codex review round 4 of PR #129 (the console's part)."""
    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok(False, f"{name}: {type(e).__name__}: {str(e)[:300]}")

    # RY5 every feedback control and the composer are bound to the run they were taken in
    def ry5():
        src = _page_sources()
        agree = src[src.index('case "agree"'):][:600]
        rec = src[src.index('$("record").onclick'):][:900]
        snd = src[src.index('$("send").onclick'):][:1200]
        ok("R.run" in agree and "S.anchorRun" in rec and "S.anchorRun" in snd,
           "RY5a agree, record and send post to the run the control or the anchor was taken in, and refuse another")
        a, b = _toy_run(os.path.join(td, "ry5", "a")), _toy_run(os.path.join(td, "ry5", "b"))
        card = next(c for c in read_run(a)["cards"] if c["kind"] == "blocker")
        con = Console([os.path.join(td, "ry5")], log=lambda *a: None)
        try:
            con.handle("POST", "/api/feedback", {}, {"run": b, "kind": "approval", "layer": "critique", "text": "x",
                                                      "anchor": card["anchor"], "evidence": card["snapshot"]})
            r = "accepted"
        except ValueError as e:
            r = str(e)
        ok(r != "accepted" and not read_jsonl_rel(b, FEEDBACK)[0],
           f"RY5b the server refuses a snapshot from another run instead of substituting fresh evidence ({r[:80]})")
    case("RY5", ry5)

    # RY6 a figure is served by the hash its snapshot recorded
    def ry6():
        d = _toy_run(os.path.join(td, "ry6", "a"))
        fig = next(c for c in read_run(d)["cards"] if c["kind"] == "figure")
        open(os.path.join(d, "figures", "plan.png"), "wb").write(b"\x89PNG\r\n\x1a\nCHANGED")
        con = Console([os.path.join(td, "ry6")], log=lambda *a: None)
        try:
            con.handle("GET", "/api/file", {"run": [d], "path": ["figures/plan.png"], "sha": [fig["snapshot"]["sha256"]]}, {})
            r = "served"
        except ValueError as e:
            r = str(e)
        src = _page_sources()
        ok(r != "served" and "figSrc(" in src[src.index("function renderFigures"):][:1200],
           f"RY6 /api/file refuses a figure whose bytes differ from the snapshot's hash; the page asks by hash ({r[:80]})")
    case("RY6", ry6)


def _self_test_review5(ok, td: str) -> None:
    """RZ5: Codex review round 5 of PR #129 (P2-5): a figure with no digest is never requested unbound."""
    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok(False, f"{name}: {type(e).__name__}: {str(e)[:300]}")

    def rz5():
        d = _toy_run(os.path.join(td, "rz5", "a"))
        con = Console([os.path.join(td, "rz5")], log=lambda *a: None)
        res = []
        for sha in ("", "nothex"):
            try:
                con.handle("GET", "/api/file", {"run": [d], "path": ["figures/plan.png"], "bound": ["1"], "sha": [sha]}, {})
                res.append("served")
            except ValueError:
                res.append("refused")
        good = hashlib.sha256(open(os.path.join(d, "figures", "plan.png"), "rb").read()).hexdigest()
        st, _, body = con.handle("GET", "/api/file", {"run": [d], "path": ["figures/plan.png"], "bound": ["1"],
                                                     "sha": [good]}, {})
        ok(res == ["refused", "refused"] and st == 200,
           f"RZ5a a snapshot-bound figure request with an empty or invalid sha is refused ({res})")
        src = _page_sources()
        ok("function figSrc" in src and "bound: 1" in src and 'sha256 || ""' not in src,
           "RZ5b the page builds figure URLs only from a captured digest (figSrc), never with sha=\"\"")
    case("RZ5", rz5)


def _self_test_review3(ok, td: str) -> None:
    """RX3-RX8: Codex review round 3 of PR #129 (the console's part; the harness's part is in the harness tests)."""
    from types import SimpleNamespace as NS
    import builtins

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- a missing function or a crash is a failing case
            ok(False, f"{name}: {type(e).__name__}: {str(e)[:300]}")

    # RX3 a linked models/ (the entry itself, not a descendant) is never fingerprinted
    def rx3():
        d = _toy_run(os.path.join(td, "rx3", "r"))
        out = os.path.join(td, "rx3_out")
        os.makedirs(out)
        open(os.path.join(out, "rx3_secret_mod.py"), "w").write("SECRET = 1\n")
        open(os.path.join(d, "f.py"), "w").write("def f(x):\n    return x\n")
        os.makedirs(os.path.join(d, "analysis", "b"))
        json.dump({"fn": "f.py::f", "code_fp": "x", "n": 1}, open(os.path.join(d, "analysis", "b", "o.jsonl.summary.json"), "w"))
        shutil.rmtree(os.path.join(d, "models"))
        os.symlink(out, os.path.join(d, "models"))
        opened, real = [], builtins.open
        builtins.open = lambda f, *a, **k: (opened.append(str(f)) if str(f).endswith("rx3_secret_mod.py") else None) or \
            real(f, *a, **k)
        try:
            br = batch_reuse(d)
        finally:
            builtins.open = real
        ok(not any(o.endswith("rx3_secret_mod.py") for o in opened) and not any(b.get("reused") for b in br),
           f"RX3 a linked models/ is not read by the batch-reuse fingerprint ({[o for o in opened if "rx3_secret" in o]})")
    case("RX3", rx3)

    # RX4 a sheet generated from another spec does not enable approval
    def rx4():
        from toolkit.spec import schema, signoff as SO
        d = os.path.join(td, "rx4", "r")
        _toy_run(d, "SIGN", "awaiting_signoff", None, path=("SPEC", "SIGN"))
        os.remove(os.path.join(d, "scope_signoff_request.json"))
        p = schema.toy_spec(d)
        SO.write_sheet(d)
        with open(p, "a") as f:
            f.write("\n# changed after the sheet was written\n")
        con = Console([os.path.join(td, "rx4")], log=lambda *a: None)
        card = next((c for c in con.handle("GET", "/api/run", {"run": [d]}, {})[2]["cards"]
                     if c.get("sub") == "spec"), {})
        body = {k: card.get(k) for k in ("spec_sha256", "derivations_sha256", "sheet_sha256", "ticket")}
        r = con.answer(d, "spec", {**body, "note": "n", "confirm": True})
        ok(card.get("sheet_stale") and not r.get("ok") and "sheet" in (r.get("refused") or ""),
           f"RX4 a stale sheet is shown as stale and does not enable approval ({card.get('sheet_stale')}, "
           f"{r.get('refused')})")
    case("RX4", rx4)

    # RX6 an evidence seal is bound to its run; the page clears the composer anchor when the run changes
    def rx6():
        a, b = _toy_run(os.path.join(td, "rx6", "a")), _toy_run(os.path.join(td, "rx6", "b"))
        card = next(c for c in read_run(a)["cards"] if c["kind"] == "blocker" and c["blocker_id"] == "T-001")
        snap = card.get("snapshot") or card.get("evidence") or {}
        n_before = len(read_jsonl_rel(b, FEEDBACK)[0])
        try:
            append_feedback(b, {"kind": "correction", "layer": "critique", "text": "x", "anchor": card["anchor"],
                                "evidence": snap})
            refused = False
        except ValueError:
            refused = True
        ok(refused and snap.get("run") and len(read_jsonl_rel(b, FEEDBACK)[0]) == n_before,
           f"RX6a feedback carrying a snapshot sealed for another run is refused, never re-captured (round 4) "
           f"({refused})")
        src = _page_sources()
        sel = src[src.index("async function selectRun"):][:400]
        ok("setAnchor(null" in sel, "RX6b selecting another run clears the composer's anchor and snapshot")
    case("RX6", rx6)

    # RX7 gate cards and the figure side panel carry the displayed snapshot
    def rx7():
        d = _toy_run(os.path.join(td, "rx7", "a"))
        g = next(c for c in read_run(d)["cards"] if c["kind"] == "gate")
        ok(isinstance(g.get("snapshot"), dict) and g["snapshot"].get("mac"), "RX7a a gate card carries its snapshot")
        src = _page_sources()
        of = src[src.index("async function openFile"):][:1500]
        ok("snapshot" in of, "RX7b the figure side panel's Comment carries the figure card's snapshot")
    case("RX7", rx7)

    # RX8 the critic's own `evidence` field is kept; the snapshot is stored apart
    def rx8():
        d = _toy_run(os.path.join(td, "rx8", "a"))
        yaml.safe_dump({"reviewer": "critique-domain", "round": 1, "blockers": [
            {"id": "T-001", "severity": "HIGH", "claim": "c", "evidence": "results.md line 4 says so", "resolved": False}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        b = next(c for c in read_run(d)["cards"] if c["kind"] == "blocker")
        src = _page_sources()
        ok(b.get("evidence") == "results.md line 4 says so" and isinstance(b.get("snapshot"), dict)
           and "c.snapshot" in src, f"RX8 the blocker's evidence text is kept and the snapshot is separate "
                                    f"({str(b.get('evidence'))[:60]})")
    case("RX8", rx8)


def _self_test_review2(ok, td: str) -> None:
    """RW1-RW11: Codex review round 2 of PR #129. Each case was written failing first."""
    from types import SimpleNamespace as NS

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- a missing function or a crash is a failing case
            ok(False, f"{name}: {type(e).__name__}: {str(e)[:300]}")

    def spec_run(d):
        from toolkit.spec import schema
        _toy_run(d, "SIGN", "awaiting_signoff", None, path=("SPEC", "SIGN"))
        os.remove(os.path.join(d, "scope_signoff_request.json"))
        schema.toy_spec(d)
        from toolkit.spec import signoff as _so
        if hasattr(_so, "write_sheet"):           # round 3: the harness's sheet writer records its source hashes
            _so.write_sheet(d)
        else:
            open(os.path.join(d, "decision_spec.signoff_sheet.md"), "w").write("# Sheet\n\nThe toy spec, shown.\n")
        return d

    def need(run, sub):
        return next((c for c in run["cards"] if c["kind"] == "decision_needed" and c.get("sub") == sub), {})

    # RW1 a spec naming an arbitrary class is never imported by the console (the harness part is SP-RW in schema.py)
    def rw1():
        import importlib
        d = spec_run(os.path.join(td, "rw1", "r"))
        marker = os.path.join(td, "rw1_marker")
        raw = yaml.safe_load(open(os.path.join(d, "decision_spec.yaml")))
        raw["engine"] = {"ref": "subprocess:Popen", "config": {"args": ["touch", marker]}}
        yaml.safe_dump(raw, open(os.path.join(d, "decision_spec.yaml"), "w"))
        seen, real = [], importlib.import_module
        importlib.import_module = lambda name, *a, **k: seen.append(name) or real(name, *a, **k)
        try:
            read_run(d)
        finally:
            importlib.import_module = real
        ok(not os.path.exists(marker) and "subprocess" not in seen,
           f"RW1 loading a run whose spec names subprocess:Popen imports and runs nothing ({seen[:5]})")
    case("RW1", rw1)

    # RW3 reads open through directory fds with no-follow traversal: a check-then-open race cannot follow a link
    def rw3():
        d = _toy_run(os.path.join(td, "rw3", "r"))
        out = os.path.join(td, "rw3_out")
        os.makedirs(out)
        open(os.path.join(out, "results.md"), "w").write("## Recommendation\n\nRACE-SECRET averts 1.\n")
        open(os.path.join(out, "plan.png"), "w").write("RACE-SECRET")
        os.remove(os.path.join(d, "results.md"))
        os.symlink(os.path.join(out, "results.md"), os.path.join(d, "results.md"))
        os.rename(os.path.join(d, "figures"), os.path.join(d, "figures_real"))
        os.symlink(out, os.path.join(d, "figures"))
        real = os.path.realpath
        os.path.realpath = lambda p, *a, **k: os.path.abspath(p)      # the check saw no link (the race)
        _CACHE.clear()
        got = []
        try:
            got.append(str(_rread(d, "results.md")[0]))
            try:
                got.append(open(safe_path(d, "figures/plan.png")).read())
            except ValueError:
                pass
            got.append(json.dumps(evidence(d, {"kind": "figure", "id": "figures/plan.png", "file": "results.md"})))
            got.append(str(_sha_rel(d, "results.md")))
        finally:
            os.path.realpath = real
        leak = [g for g in got if "RACE-SECRET" in g or g == _sha(os.path.join(out, "results.md"))]
        ok(not leak, f"RW3 a read whose path check raced a link swap does not follow the link ({len(leak)} leaked)")
    case("RW3", rw3)

    # RW4 indirect readers: spec status, derivation outputs and batch fn paths stay inside the run dir
    def rw4():
        from toolkit.spec import signoff as SO, schema as SC
        import compute as C
        d = spec_run(os.path.join(td, "rw4", "r"))
        calls = []
        real_status, real_load, real_fp = SO.status, SC.load, C.code_fingerprint
        SO.status = lambda *a, **k: calls.append("signoff.status") or real_status(*a, **k)
        SC.load = lambda *a, **k: calls.append("schema.load") or real_load(*a, **k)
        C.code_fingerprint = lambda *a, **k: calls.append(("code_fingerprint", a[1], k.get("confined"))) or "y"
        try:
            box = inbox(d)
            os.makedirs(os.path.join(d, "models", "b"), exist_ok=True)
            json.dump({"fn": "../../elsewhere/m.py::f", "code_fp": "x", "n": 1},
                      open(os.path.join(d, "models", "b", "o.jsonl.summary.json"), "w"))
            json.dump({"fn": "/etc/m.py::f", "code_fp": "x", "n": 1},
                      open(os.path.join(d, "models", "b", "p.jsonl.summary.json"), "w"))
            br = batch_reuse(d)
        finally:
            SO.status, SC.load, C.code_fingerprint = real_status, real_load, real_fp
        ok(box["spec"].get("status") == "awaiting" and "signoff.status" not in calls and "schema.load" not in calls,
           f"RW4a the console's spec status is its own read model: no harness loader runs ({calls})")
        ok(all(c[2] is True for c in calls if isinstance(c, tuple)) and len(br) == 2 and not any(b["reused"] for b in br),
           f"RW4b a batch fn path from an artifact reaches code_fingerprint only in confined mode (round 3: hashed through "
           f"no-follow fds, which refuse a path out of the run dir) ({calls})")
    case("RW4", rw4)

    # RW5 the approval is one snapshot: the sheet shown, the spec sha and the derivation record sha, all checked
    def rw5():
        d = spec_run(os.path.join(td, "rw5", "r"))
        con = Console([os.path.join(td, "rw5")], log=lambda *a: None)
        card = need(con.handle("GET", "/api/run", {"run": [d]}, {})[2], "spec")
        ok(card.get("body") == open(os.path.join(d, "decision_spec.signoff_sheet.md")).read() and card.get("sheet_sha256")
           and "derivations_sha256" in card and card.get("spec_sha256"),
           f"RW5a the spec card holds the sheet, its sha, the spec sha and the derivation record sha "
           f"({sorted(k for k in card if k.endswith('sha256'))})")
        seen = []
        body = {k: card.get(k) for k in ("spec_sha256", "sheet_sha256", "derivations_sha256", "ticket")}
        con.runner = lambda cmd, **k: seen.append(cmd) or NS(returncode=0, stdout="{}", stderr="")
        r = con.answer(d, "spec", {**body, "note": "n", "confirm": True})
        cmd = seen[0] if seen else []
        ok(r.get("ok") and "--expect-sha256" in cmd and "--expect-derivations-sha256" in cmd
           and "--expect-sheet-sha256" in cmd, f"RW5b the sign command checks every sha of the snapshot ({cmd[-8:]})")
        con.runner = None
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        json.dump({"derivations": {}}, open(os.path.join(d, "data", "derivations_record.json"), "w"))
        r = con.answer(d, "spec", {**body, "note": "n", "confirm": True})
        from toolkit.spec import signoff as SO
        ok(not r.get("ok") and SO.status(d) != "signed",
           f"RW5c a derivation record written after the snapshot is not signed over ({r.get('refused') or r.get('out')})")
    case("RW5", rw5)

    # RW7 an interrupted stream keeps its whole reservation (partial usage is a lower bound)
    def rw7():
        os.environ["ANTHROPIC_API_KEY"], prev = "sk-test-x", os.environ.get("ANTHROPIC_API_KEY")
        try:
            d = _toy_run(os.path.join(td, "rw7", "a"))
            part = NS(input_tokens=2000, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0)

            class Boom:
                current_message_snapshot = NS(usage=part, model=MODEL)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def get_final_message(self):
                    raise RuntimeError("stream broke")
            calls = []
            cl = NS(messages=NS(count_tokens=lambda **k: NS(input_tokens=2000)),
                    beta=NS(messages=NS(stream=lambda **k: calls.append(1) or Boom())))
            cs = ChatSession(0.40, lambda: cl, log=lambda *a: None)
            for _ in range(3):
                cs.send(d, "q")
            ok(len(calls) == 1 and abs(cs.spent - worst_case_usd(2000)) < 1e-9,
               f"RW7 an interrupted stream keeps its reservation: one call under a $0.40 cap ({len(calls)} calls, "
               f"${cs.spent:.3f})")
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev
    case("RW7", rw7)

    # RW9 feedback evidence is the snapshot the card was rendered with, and evidence() parses the bytes it hashed
    def rw9():
        import builtins
        d = _toy_run(os.path.join(td, "rw9", "a"))
        cards = read_run(d)["cards"]
        b = next(c for c in cards if c["kind"] == "blocker" and c["blocker_id"] == "T-001")
        ev = b.get("snapshot") or {}
        before = _sha(os.path.join(d, "critique_domain.yaml"))
        yaml.safe_dump({"reviewer": "critique-domain", "round": 2, "blockers": [
            {"id": "T-001", "severity": "LOW", "claim": "Rewritten later.", "resolved": True}]},
            open(os.path.join(d, "critique_domain.yaml"), "w"))
        row = append_feedback(d, {"kind": "correction", "layer": "critique", "text": "no", "anchor": b["anchor"],
                                  "evidence": ev})
        got = row.get("evidence") or {}
        ok(ev.get("sha256") == before and got.get("sha256") == before and "omits one source" in (got.get("excerpt") or "")
           and got.get("round") == 1, f"RW9a feedback keeps the evidence the card was rendered with ({got})")
        forged = dict(ev, excerpt="forged", mac="0" * 64)
        try:
            row = append_feedback(d, {"kind": "correction", "layer": "critique", "text": "no", "anchor": b["anchor"],
                                      "evidence": forged})
        except ValueError:
            row = {"refused": True}
        ok(row.get("refused") or (row.get("evidence") or {}).get("excerpt") != "forged",
           "RW9b an evidence snapshot the server did not issue is not stored as such (round 4: refused)")
        opens, real = [], builtins.open
        builtins.open = lambda f, *a, **k: (opens.append(f) if str(f).endswith("critique_domain.yaml") else None) \
            or real(f, *a, **k)
        _CACHE.clear()
        try:
            evidence(d, b["anchor"])
        finally:
            builtins.open = real
        ok(not opens, f"RW9c evidence() reads the file once, through the fd reader, and parses those bytes ({opens})")
    case("RW9", rw9)

    # RW10 the citation index holds only what the prompt kept
    def rw10():
        d = _toy_run(os.path.join(td, "rw10", "a"))
        yaml.safe_dump({"reviewer": "critique-redteam", "round": 1, "blockers": [
            {"id": f"Z-{i:03d}", "severity": "HIGH", "claim": "x" * 300, "resolved": False} for i in range(10)]},
            open(os.path.join(d, "critique_redteam.yaml"), "w"))
        old = BUDGET["blockers"]
        BUDGET["blockers"] = 400
        try:
            ctx = chat_context(d)
        finally:
            BUDGET["blockers"] = old
        c = check_citations("[[critique_redteam.yaml#Z-009]]", ctx["seen"])
        ok("Z-009" not in ctx["run"] and c and c[0]["status"] != "checked",
           f"RW10 a blocker cut from the prompt is not a checked citation ({c})")
    case("RW10", rw10)

    # RW11 lineage stays inferred while the harness records no parent run
    def rw11():
        t = os.path.join(td, "rw11")
        for i, lab in enumerate(("A", "B")):
            d = _toy_run(os.path.join(t, lab), "FINAL", None, "completed",
                         path=("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "ANALYZE", "FINAL"))
            st_ = json.load(open(os.path.join(d, "run_state.json")))
            st_["question"] = "Toy lineage question two."
            for h in st_["history"]:
                h["at"] = h["at"].replace("2026-01-01", f"2026-01-0{i + 1}")
            json.dump(st_, open(os.path.join(d, "run_state.json"), "w"))
            open(os.path.join(d, "decision_spec.yaml"), "w").write("objective: {outcome: dalys}\n")
        pj = projects(discover([t]))
        rel = [r["relation"] for r in pj[0]["lineage"]][1:] if len(pj) == 1 else []
        ok(rel and all("(inferred)" in str(x) for x in rel), f"RW11 an equal spec sha is still an inferred relation ({rel})")
    case("RW11", rw11)


def _self_test_review(ok, td: str) -> None:
    """RV1-RV17: the Codex review of PR #124. Each case was written failing first, against the code as merged."""
    from types import SimpleNamespace as NS
    import urllib.request

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- a missing function or a crash is a failing case
            ok(False, f"{name}: {type(e).__name__}: {str(e)[:300]}")

    def serve(con):
        srv = make_server(con, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return srv, f"http://127.0.0.1:{srv.server_address[1]}"

    def post(base, path, body, token):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"X-Console-Token": token, "Content-Type": "application/json"})
        try:
            return json.load(urllib.request.urlopen(req))
        except urllib.error.HTTPError as e:
            return {"ok": False, "http": e.code, **json.loads(e.read() or b"{}")}

    def get(base, path):
        return json.load(urllib.request.urlopen(base + path))

    def spec_run(d):
        """A toy run whose decision spec waits for the owner's sign-off."""
        from toolkit.spec import schema
        _toy_run(d, "SIGN", "awaiting_signoff", None, path=("SPEC", "SIGN"))
        os.remove(os.path.join(d, "scope_signoff_request.json"))
        schema.toy_spec(d)
        return d

    def need(run, sub):
        return next((c for c in run["cards"] if c["kind"] == "decision_needed" and c.get("sub") == sub), {})

    # RV1 XSS: artifact text never reaches an attribute unescaped; a CSP forbids inline script
    def rv1():
        src = _page_sources()
        bad = [(a, e) for a, e in _attr_interpolations(src) if not _SAFE_ATTR.match(e) and not _LIT_TERNARY.match(e)]
        ok(not bad, f"RV1a every ${{...}} in an HTML attribute is esc(), ref(), q() or a ternary of literals "
                    f"({len(bad)} not: {bad[:6]})")
        d = _toy_run(os.path.join(td, "rv1", "runs", "r"))
        open(os.path.join(d, "figures", "x.svg"), "w").write('<svg xmlns="http://www.w3.org/2000/svg"><script>'
                                                              'alert(1)</script></svg>')
        con = Console([os.path.join(td, "rv1")], log=lambda *a: None)
        srv, base = serve(con)
        try:
            r = urllib.request.urlopen(base + "/")
            page, csp = r.read().decode(), r.headers.get("Content-Security-Policy") or ""
            f = urllib.request.urlopen(base + "/api/file?" + urllib.parse.urlencode({"run": d, "path": "figures/x.svg"}))
            fcsp = f.headers.get("Content-Security-Policy") or ""
        finally:
            srv.shutdown()
            srv.server_close()
        inline = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>\s*\S", page)
        ok("connect-src 'self'" in csp and "script-src 'self'" in csp and "unsafe-inline" not in csp.split(
            "script-src")[-1].split(";")[0] and not inline,
           f"RV1b the page has a CSP (script-src 'self', connect-src 'self') and no inline script ({csp!r}; "
           f"{len(inline)} inline)")
        ok("sandbox" in fcsp and "default-src 'none'" in fcsp, f"RV1c a served artifact (an SVG may hold a script) "
           f"is sandboxed ({fcsp!r})")
        node = shutil.which("node")
        if node:
            js = src[src.index("const esc ="):]
            fn = js[:js.index("\n", 0)] + "\n" + js[js.index("function num("):js.index("\n}\n", js.index(
                "function num(")) + 3]
            out = subprocess.run([node, "-e", fn + '\nprocess.stdout.write(num("<img src=x onerror=alert(1)>"))'],
                                 capture_output=True, text=True, timeout=30).stdout
            ok("<img" not in out and out, f"RV1d num() escapes a non-number from an artifact ({out!r})")
    case("RV1", rv1)

    # RV2 the console's writes never follow a symlink out of owner/, _drafts/ or _owner/
    def rv2():
        out1, out2, out3 = (os.path.join(td, f"rv2_out{i}") for i in (1, 2, 3))
        for o in (out1, out2, out3):
            os.makedirs(o)
        d = _toy_run(os.path.join(td, "rv2", "a"))
        os.symlink(out1, os.path.join(d, "owner"))
        d2 = _toy_run(os.path.join(td, "rv2", "b"))
        os.makedirs(os.path.join(d2, "owner"))
        tgt = os.path.join(out2, "victim.jsonl")
        open(tgt, "w").write("")
        os.symlink(tgt, os.path.join(d2, "owner", "feedback.jsonl"))
        refused = []
        for rd_ in (d, d2):
            try:
                append_feedback(rd_, {"kind": "guidance", "layer": "writer", "text": "x"})
                refused.append(False)
            except (ValueError, OSError):
                refused.append(True)
        ok(all(refused) and os.listdir(out1) == [] and open(tgt).read() == "",
           f"RV2a feedback is not written through a symlinked owner/ or owner/feedback.jsonl ({refused})")
        root = os.path.join(td, "rv2")
        os.symlink(out3, os.path.join(root, "_drafts"))
        os.symlink(out3, os.path.join(root, "_owner"))
        res = []
        for fn in (lambda: build_on(d2, os.path.join(root, "_drafts")),
                   lambda: export_digest([d2], os.path.join(root, "_owner"))):
            try:
                fn()
                res.append(False)
            except (ValueError, OSError):
                res.append(True)
        ok(all(res) and os.listdir(out3) == [], f"RV2b a draft and a digest are not written through a symlinked "
           f"_drafts/ or _owner/ ({res}, {os.listdir(out3)})")
    case("RV2", rv2)

    # RV3 every artifact read stays inside the run dir
    def rv3():
        d = _toy_run(os.path.join(td, "rv3", "r"))
        sec = os.path.join(td, "rv3_secret.yaml")
        yaml.safe_dump({"question": "OUTSIDE-SECRET", "decision_changes": True}, open(sec, "w"))
        open(os.path.join(d, ".env"), "w").write("ANTHROPIC_API_KEY=ENV-SECRET\n")
        yaml.safe_dump({"data": {"tests": [{"test_id": "DT1", "path": os.path.relpath(sec, d), "verdict": "NO_CHANGE"},
                                           {"test_id": "DT2", "path": sec, "verdict": "NO_CHANGE"},
                                           {"test_id": "DT3", "path": ".env", "verdict": "NO_CHANGE"}]}},
                       open(os.path.join(d, "analysis", "decision_tests.yaml"), "w"))
        cards = decision_test_cards(d)
        blob = json.dumps(cards, default=str)
        ok("OUTSIDE-SECRET" not in blob and "ENV-SECRET" not in blob and len(cards) == 3
           and all(c.get("state") == "error" for c in cards),
           f"RV3a a decision test's path outside the run dir, absolute or a credential file is an error, not read "
           f"({[c.get('state') for c in cards]})")
        md_out = os.path.join(td, "rv3_results.md")
        open(md_out, "w").write("# R\n\n## Recommendation\n\nOUTSIDE-SECRET averts 9 events.\n")
        os.remove(os.path.join(d, "results.md"))
        os.symlink(md_out, os.path.join(d, "results.md"))
        st_out = os.path.join(td, "rv3_state.json")
        shutil.copy(os.path.join(d, "run_state.json"), st_out)
        d2 = _toy_run(os.path.join(td, "rv3", "r2"))
        os.remove(os.path.join(d2, "run_state.json"))
        os.symlink(st_out, os.path.join(d2, "run_state.json"))
        r = json.dumps(read_run(d), default=str)
        ok("OUTSIDE-SECRET" not in r and "OUTSIDE-SECRET" not in chat_context(d)["run"]
           and load_run_state(d2)[0] is None,
           "RV3b fixed-name artifacts (results.md, run_state.json) that link outside the run dir are not read")
    case("RV3", rv3)

    # RV4 a sign-off is bound to the run it was shown for
    def rv4():
        root = os.path.join(td, "rv4")
        a = spec_run(os.path.join(root, "A"))
        b = os.path.join(root, "B")
        shutil.copytree(a, b)
        con = Console([root], log=lambda *a: None)
        srv, base = serve(con)
        try:
            card = need(get(base, "/api/run?run=" + urllib.parse.quote(a)), "spec")
            r = post(base, "/api/sign_spec", {"run": b, "note": "n", "confirm": True, "ticket": card.get("ticket"),
                                              "spec_sha256": card.get("spec_sha256")}, con.token)
        finally:
            srv.shutdown()
            srv.server_close()
        from toolkit.spec import signoff
        ok(card.get("ticket") and not r.get("ok") and signoff.status(b) == "awaiting" and signoff.status(a) == "awaiting",
           f"RV4a a sign request for run B carrying the card shown for run A is refused ({r.get('refused') or r})")
        src = _page_sources()
        ok("S.loadSeq" in src and re.search(r"seq\s*!==\s*S\.loadSeq", src) and "S.loading" in src,
           "RV4b the page discards a stale /api/run answer (request sequence) and disables decisions while loading")
    case("RV4", rv4)

    # RV5 a remote answer names the remote label and run dir it answers
    def rv5():
        m = os.path.join(td, "rv5")
        d = _toy_run(os.path.join(m, "F1", "runs", "2026_x"))
        d2 = _toy_run(os.path.join(td, "rv5b", "2026_y"))
        con = Console([], mirror=m, cloud_config="c.py", log=lambda *a: None)
        con2 = Console([], mirror=os.path.join(td, "rv5b"), cloud_config="c.py", log=lambda *a: None)
        ok(con.remote_target(d) == ("F1", "2026_x"), "RV5a a mirrored <label>/runs/<run> names its label and run")
        try:
            con2.remote_target(d2)
            ok(False, "RV5b a mirrored run with no label is refused without --remote-label")
        except ValueError:
            ok(True, "RV5b a mirrored run with no label is refused without --remote-label")
        seen = []
        fp = (inbox(d)["scope"]["items"] or [{}])[0].get("fingerprint")
        r = answer_scope(d, {fp: "accept"}, "remote", True, mirror=True, cloud_config="c.py", fingerprints=[fp],
                         remote=("F1", "2026_x"), runner=lambda cmd, **k: seen.append(cmd)
                         or NS(returncode=0, stdout="ok", stderr=""))
        cmd = seen[0] if seen else []
        ok(r.get("ok") and cmd[1:4] == ["cloud/run_cluster.py", "c.py", "sign"] and cmd[5:9] == ["--label", "F1", "--run",
                                                                                                  "2026_x"],
           f"RV5c the remote sign passes --label and --run ({cmd})")
    case("RV5", rv5)

    # RV6 the answer is bound to the content shown: the spec's sha256, the scope items' fingerprints
    def rv6():
        root = os.path.join(td, "rv6")
        a = spec_run(os.path.join(root, "A"))
        s = _toy_run(os.path.join(root, "S"))
        con = Console([root], log=lambda *a: None)
        srv, base = serve(con)
        from toolkit.spec import signoff
        import scope_signoff as SS
        try:
            card = need(get(base, "/api/run?run=" + urllib.parse.quote(a)), "spec")
            with open(os.path.join(a, "decision_spec.yaml"), "a") as f:
                f.write("\n# changed after the owner read it\n")
            r = post(base, "/api/sign_spec", {"run": a, "note": "n", "confirm": True, "ticket": card.get("ticket"),
                                              "spec_sha256": card.get("spec_sha256")}, con.token)
            ok(card.get("spec_sha256") and not r.get("ok") and signoff.status(a) == "awaiting",
               f"RV6a a spec edited after it was shown is not signed ({r.get('refused') or r.get('out') or r})")
            sc = need(get(base, "/api/run?run=" + urllib.parse.quote(s)), "scope")
            fps = sc.get("fingerprints") or []
            rq = json.load(open(os.path.join(s, SS.REQUEST)))
            rq["items"][0]["fingerprint"] = "fp-changed-0000"
            json.dump(rq, open(os.path.join(s, SS.REQUEST), "w"))
            r2 = post(base, "/api/scope", {"run": s, "note": "n", "confirm": True, "ticket": sc.get("ticket"),
                                           "fingerprints": fps, "answers": {f: "accept" for f in fps}}, con.token)
            ok(fps and not r2.get("ok") and not os.path.exists(os.path.join(s, SS.SIGNOFF)),
               f"RV6b a scope request changed after it was shown is not answered ({r2.get('refused') or r2})")
            sc2 = need(get(base, "/api/run?run=" + urllib.parse.quote(s)), "scope")
            r3 = post(base, "/api/scope", {"run": s, "note": "n", "confirm": True, "ticket": sc2.get("ticket"),
                                           "fingerprints": sc2.get("fingerprints"),
                                           "answers": {f: "accept" for f in sc2.get("fingerprints") or []}}, con.token)
            ok(r3.get("ok") and (SS.load_answers(s)[0].get("fp-changed-0000") or {}).get("decision") == "accept",
               f"RV6c answers keyed by the fingerprints shown are recorded ({r3.get('refused') or r3.get('out')})")
        finally:
            srv.shutdown()
            srv.server_close()
    case("RV6", rv6)

    # RV7 a run under a mirror root is a mirror, whatever the order the roots are walked in
    def rv7():
        t = os.path.join(td, "rv7")
        _toy_run(os.path.join(t, "local"))
        _toy_run(os.path.join(t, "live", "2026_m"))
        runs = {r["name"]: r["mirror"] for r in Console([t], mirror=os.path.join(t, "live"), log=lambda *a: None).runs()}
        ok(runs == {"local": False, "2026_m": True}, f"RV7 overlapping roots keep the mirror mark ({runs})")
    case("RV7", rv7)

    class Msgs:
        def __init__(self, tokens=1000, stream=None):
            self.tokens, self.stream_fn, self.calls = tokens, stream, []

        def count_tokens(self, **kw):
            if isinstance(self.tokens, Exception):
                raise self.tokens
            return NS(input_tokens=self.tokens)

    def msg(text="ok", usage=None):
        return NS(content=[NS(type="text", text=text)], stop_reason="end_turn", model=MODEL,
                  usage=usage or NS(input_tokens=10, output_tokens=10, cache_creation_input_tokens=0,
                                    cache_read_input_tokens=0))

    def client_with(stream, tokens=1000):
        m = Msgs(tokens)
        return NS(messages=m, beta=NS(messages=NS(stream=stream)))

    # RV8 concurrent chats: each reserves its worst case under the lock; one run's turns are serialized
    def rv8():
        gate, calls = threading.Event(), []

        class Slow:
            def __init__(self, kw):
                calls.append(kw)

            def __enter__(self):
                gate.wait(10)
                return self

            def __exit__(self, *a):
                return False

            def get_final_message(self):
                return msg(f"reply {len(calls)}")
        os.environ["ANTHROPIC_API_KEY"], prev = "sk-test-x", os.environ.get("ANTHROPIC_API_KEY")
        try:
            r1, r2 = _toy_run(os.path.join(td, "rv8", "a")), _toy_run(os.path.join(td, "rv8", "b"))
            cl = client_with(lambda **kw: Slow(kw), 1000)
            cs = ChatSession(1.25 * worst_case_usd(1000), lambda: cl, log=lambda *a: None)
            res = []
            ts = [threading.Thread(target=lambda d=d: res.append(cs.send(d, "q"))) for d in (r1, r2)]
            for th in ts:
                th.start()
                time.sleep(0.3)
            gate.set()
            for th in ts:
                th.join(10)
            ok(len(calls) == 1 and sum(1 for x in res if not x.get("ok") and "chat cap" in (x.get("error") or "")) == 1,
               f"RV8a two concurrent messages that together pass the cap: one is refused before its call "
               f"({len(calls)} calls; {[x.get('error') for x in res]})")
            gate.clear()
            calls.clear()
            cs2 = ChatSession(100.0, lambda: cl, log=lambda *a: None)
            r3 = _toy_run(os.path.join(td, "rv8", "c"))
            ts = [threading.Thread(target=lambda t=t: cs2.send(r3, t)) for t in ("first", "second")]
            for th in ts:
                th.start()
                time.sleep(0.3)
            gate.set()
            for th in ts:
                th.join(10)
            later = calls[-1]["messages"] if len(calls) == 2 else []
            ok(len(calls) == 2 and any(m_["role"] == "assistant" for m_ in later),
               f"RV8b two messages on one run are serialized: the second carries the first reply "
               f"({[m_['role'] for m_ in later]})")
        finally:
            gate.set()
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev
    case("RV8", rv8)

    # RV9 failed streams and the worst-case bound
    def rv9():
        os.environ["ANTHROPIC_API_KEY"], prev = "sk-test-x", os.environ.get("ANTHROPIC_API_KEY")
        try:
            d = _toy_run(os.path.join(td, "rv9", "a"))

            class Boom:
                def __init__(self, kw, snap=None):
                    self.snap = snap

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                @property
                def current_message_snapshot(self):
                    if self.snap is None:
                        raise AssertionError("no snapshot")
                    return self.snap

                def get_final_message(self):
                    raise RuntimeError("stream broke")
            cs = ChatSession(5.0, lambda: client_with(lambda **kw: Boom(kw), 2000), log=lambda *a: None)
            r = cs.send(d, "q")
            ok(not r["ok"] and abs(cs.spent - worst_case_usd(2000)) < 1e-9,
               f"RV9a a failed stream with no usage keeps its reservation ({cs.spent})")
            part = NS(input_tokens=2000, output_tokens=50, cache_creation_input_tokens=0, cache_read_input_tokens=0)
            cs = ChatSession(5.0, lambda: client_with(lambda **kw: Boom(kw, NS(usage=part, model=MODEL)), 2000),
                             log=lambda *a: None)
            cs.send(d, "q")
            ok(abs(cs.spent - worst_case_usd(2000)) < 1e-9,
               f"RV9b a failed stream with partial usage keeps its whole reservation (round 2: partial usage is a "
               f"lower bound) ({cs.spent})")
            made = []
            cs = ChatSession(2.0, lambda: client_with(lambda **kw: made.append(1), 1_000_000), log=lambda *a: None)
            r = cs.send(d, "q")
            ok(not r["ok"] and "chat cap" in r["error"] and not made,
               "RV9c the input bound is the API's token count: a 1M-token prompt is refused under a $2 cap")
            sysb = [{"type": "text", "text": "é" * 100}]
            n, how = input_token_bound(None, sysb, [{"role": "user", "content": "😀" * 10}])
            ok(n >= 200 + 40 and "byte" in how, f"RV9d without a count the bound is the UTF-8 byte length ({n}, {how})")
            ok(PRICE == {"in": 4.0, "out": 20.0, "cache_write": 5.0, "cache_read": 0.20}
               and FALLBACK_PRICE["out"] >= 25.0 and FALLBACK_PRICE["cache_write"] >= 6.25
               and abs(worst_case_usd(1000) - (1000 * (5.0 + FALLBACK_PRICE["cache_write"])
                                              + MAX_TOKENS * (20.0 + FALLBACK_PRICE["out"])) / 1e6) < 1e-9,
               "RV9e the worst case: every input token a cache write and full max_tokens, on Opus 5.5 and again on "
               "the refusal fallback (claude-api skill price table)")
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev
    case("RV9", rv9)

    # RV10 four decision-test states: changed, unchanged, not run, error
    def rv10():
        d = _toy_run(os.path.join(td, "rv10", "a"))
        yaml.safe_dump({"data": {"tests": [
            {"test_id": "DT1", "path": "models/decision_tests/DT1.yaml", "verdict": "CHANGES_PLAN"},
            {"test_id": "DT2", "verdict": "NO_CHANGE"}, {"test_id": "DT3", "verdict": "NOT_RUN"},
            {"test_id": "DT4", "path": "models/decision_tests/missing.yaml", "verdict": "NO_CHANGE"}]}},
            open(os.path.join(d, "analysis", "decision_tests.yaml"), "w"))
        st = {c["test_id"]: (c.get("state"), c["title"]) for c in decision_test_cards(d)}
        ok({k: v[0] for k, v in st.items()} == {"DT1": "changed", "DT2": "unchanged", "DT3": "not_run", "DT4": "error"}
           and "does not change" not in st["DT3"][1] + st["DT4"][1],
           f"RV10a NOT_RUN and an unreadable result are their own states ({st})")
        ok("not run" in chat_context(d)["run"] and run_summary(d)["tests"].get("DT3") == "not_run",
           "RV10b chat context and the run summary (compare) keep the not-run state")
        src = _page_sources()
        ok('"not_run"' in src and "not run" in src and '"error"' in src, "RV10c the page shows not run and error")
    case("RV10", rv10)

    # RV11 the mirror's sync status is visible; a stale mirror cannot be approved remotely
    def rv11():
        m = os.path.join(td, "rv11")
        d = _toy_run(os.path.join(m, "F1", "runs", "2026_x"))
        con = Console([], mirror=m, sync_cmd="true", sync_s=60, cloud_config="c.py", log=lambda *a: None)
        con.sync_once(runner=lambda *a, **k: NS(returncode=0, stdout="", stderr=""), now=1000.0)
        fresh = con.sync_status(now=1030.0)
        for t in (1060.0, 1120.0):
            con.sync_once(runner=lambda *a, **k: NS(returncode=1, stdout="", stderr="rsync: refused"), now=t)
        stale = con.sync_status(now=1130.0)
        ok(not fresh["stale"] and stale["stale"] and stale["failures"] == 2 and stale["last_ok_at"]
           and "refused" in stale["last_error"], f"RV11a sync status: last success, failures, stale after 2 ({stale})")
        fp = inbox(d)["scope"]["items"][0]["fingerprint"]
        r = con.answer(d, "scope", {"fingerprints": [fp], "answers": {fp: "accept"}, "note": "n", "confirm": True},
                       now=1130.0)
        ok(not r.get("ok") and "stale" in (r.get("refused") or ""), f"RV11b a stale mirror is not approved ({r})")
        ok("S.meta.sync" in _page_sources() and "sync" in re.search(r"const PAGE_NEEDS = \[([^\]]*)\]",
                                                                     _page_sources()).group(1),
           "RV11c the page shows the sync status")
    case("RV11", rv11)

    # RV12 a refresh does not wipe an answer being written
    def rv12():
        src = _page_sources()
        body = src[src.index("function renderStream"):][:2500]
        ok("saveForms()" in body and "restoreForms(" in body, "RV12 renderStream keeps the open answer forms' state")
    case("RV12", rv12)

    # RV13 the parse cache sees a same-size, same-mtime replacement; a sync clears it
    def rv13():
        f = os.path.join(td, "rv13.yaml")
        open(f, "w").write("x: 1\n")
        st0 = os.stat(f)
        first = _read(f)[0]
        tmp = f + ".new"
        open(tmp, "w").write("x: 2\n")
        os.utime(tmp, ns=(st0.st_atime_ns, st0.st_mtime_ns))
        os.replace(tmp, f)
        ok(first == {"x": 1} and _read(f)[0] == {"x": 2}, "RV13a a replaced file with the same size and mtime is read "
                                                          "again (inode and ctime in the key)")
        con = Console([], mirror=td, sync_cmd="true", log=lambda *a: None)
        _read(f)
        con.sync_once(runner=lambda *a, **k: NS(returncode=0, stdout="", stderr=""))
        ok(not _CACHE, "RV13b each sync clears the parse cache")
    case("RV13", rv13)

    # RV14 feedback carries an evidence snapshot
    def rv14():
        d = _toy_run(os.path.join(td, "rv14", "a"))
        row = append_feedback(d, {"kind": "correction", "layer": "critique", "text": "no",
                                  "anchor": {"kind": "blocker", "id": "T-001", "file": "critique_domain.yaml"}})
        ev = row.get("evidence") or {}
        ok(ev.get("sha256") == _sha(os.path.join(d, "critique_domain.yaml")) and ev.get("round") == 1
           and "The interval omits one source." in (ev.get("excerpt") or ""),
           f"RV14 feedback stores the anchored artifact's round, sha256 and an excerpt ({ev})")
    case("RV14", rv14)

    # RV15 lineage honesty
    def rv15():
        t = os.path.join(td, "rv15")
        spec = {"objective": {"outcome": "dalys"}, "analysis": {"decision_tests": [{"id": "DT1", "values": [1, 2]}]}}
        ds = []
        for i, (lab, sp, note) in enumerate((("A", spec, "a"), ("B", spec, "b"),
                                             ("C", {"objective": {"outcome": "deaths"}, "analysis": {
                                                 "decision_tests": [{"id": "DT1", "values": [1, 3]}]}}, "c"))):
            d = _toy_run(os.path.join(t, lab), "FINAL", None, "completed",
                         path=("SPEC", "SIGN", "EVIDENCE", "ENGINE_CHOICE", "ANALYZE", "FINAL"))
            st_ = json.load(open(os.path.join(d, "run_state.json")))
            st_["question"] = "Toy lineage question."
            for h in st_["history"]:
                h["at"] = h["at"].replace("2026-01-01", f"2026-01-0{i + 1}")
            json.dump(st_, open(os.path.join(d, "run_state.json"), "w"))
            open(os.path.join(d, "decision_spec.yaml"), "w").write(yaml.safe_dump(sp) + f"# {note}\n")
            json.dump({"engine": {"ref": "toy"}, "at": f"2026-01-0{i + 1}"}, open(os.path.join(d, "engine_choice.json"), "w"))
            ds.append(d)
        pj = projects(discover([t]))
        lin = {r["name"]: r for r in pj[0]["lineage"]} if len(pj) == 1 else {}
        ok(lin and lin["B"]["relation"] == "related run (inferred)" and lin["B"]["scenario"] != "same signed spec",
           f"RV15a a different spec sha is a related run (inferred), never 'same signed spec' "
           f"({lin.get('B', {}).get('relation')}, {lin.get('B', {}).get('scenario')!r})")
        fields = {x["field"] for x in lin.get("C", {}).get("diff") or []}
        ok("objective.outcome" in fields and any(f.startswith("analysis.decision_tests.DT1") for f in fields),
           f"RV15b the diff includes objective.outcome and the decision tests' values ({sorted(fields)})")
    case("RV15", rv15)

    # RV16 chat: the anchored artifact's excerpt is in the context; citations are checked against what was sent
    def rv16():
        os.environ["ANTHROPIC_API_KEY"], prev = "sk-test-x", os.environ.get("ANTHROPIC_API_KEY")
        try:
            d = _toy_run(os.path.join(td, "rv16", "a"))
            calls = []

            class S1:
                def __init__(self, kw):
                    calls.append(kw)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def get_final_message(self):
                    return msg("It averts 120 [[results.md#Recommendation]]; see [[nowhere.md#x]].")
            cs = ChatSession(5.0, lambda: client_with(lambda **kw: S1(kw)), log=lambda *a: None)
            r = cs.send(d, "what does it show?", {"kind": "figure", "id": "figures/plan.png", "file": "figures/plan.png"})
            last = calls[-1]["messages"][-1]["content"] if calls else ""
            cits = {(c["file"], c["status"]) for c in (r.get("reply") or {}).get("citations") or []}
            ok("plan option counts" in last, "RV16a an anchored message carries the artifact's caption or excerpt")
            ok(cits == {("results.md", "checked"), ("nowhere.md", "unchecked")},
               f"RV16b each citation is checked against the files in the context; others are unchecked ({cits})")
        finally:
            if prev is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = prev
    case("RV16", rv16)

    # RV17 editing the console does not invalidate numerical batch reuse
    def rv17():
        import compute as C
        here = os.path.realpath(__file__)
        under = [x for x in C.REPO_CODE_DIRS if here.startswith(os.path.join(os.path.realpath(C.REPO), x) + os.sep)]
        ok(not under, f"RV17 the console is outside compute.code_fingerprint's repo dirs ({under})")
    case("RV17", rv17)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", action="append", default=[], help="a run directory to open (repeatable)")
    ap.add_argument("--roots", nargs="*", default=None, help="directories searched for runs (default: runs/)")
    ap.add_argument("--mirror", help="a local mirror of a remote run tree (read-only for the console)")
    ap.add_argument("--sync-cmd", help="a command that refreshes --mirror (run every --sync-s seconds)")
    ap.add_argument("--sync-s", type=float, default=120.0)
    ap.add_argument("--cloud-config", help="cloud/configs/<x>.py: the remote sign path and the compute panel")
    ap.add_argument("--remote-label", help="the cloud config's label of the mirrored run (default: the mirror's "
                                            "<label>/runs/<run> layout); remote answers name it")
    ap.add_argument("--port", type=int, default=None,
                    help=f"default: the first free port from {DEFAULT_PORT} (an explicit port must be free)")
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
    con = Console(roots, a.run_dir, a.mirror, a.sync_cmd, a.sync_s, a.cloud_config, a.chat_cap_usd, a.core_hours_cap,
                  remote_label=a.remote_label)
    if a.sync_cmd:
        threading.Thread(target=con.sync_loop, daemon=True).start()
    threading.Thread(target=con.runs, daemon=True).start()   # warm the parse cache before the first page load
    try:
        srv = bind(con, a.port)
    except ValueError as e:
        print(f"Run console: {e}", file=sys.stderr)
        return 1
    print(f"Run console: http://127.0.0.1:{srv.server_address[1]}/  (chat cap ${a.chat_cap_usd:.2f}; "
          f"chat {'on' if api_key() else 'off: ANTHROPIC_API_KEY is not set or is a placeholder'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
