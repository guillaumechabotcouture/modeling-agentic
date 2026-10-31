"""Live run-health monitor: flags harness anomalies while a run is going.

    python cloud/run_cluster.py <config.py> health [--loop] [--interval S] [--log PATH] [--thresholds YAML]
    python cloud/run_health.py replay <run_dir> [--scheduler SNAPSHOTS.jsonl] [--thresholds YAML]
    python cloud/run_health.py --self-test

Phase 51 γ. Live runs found harness defects only after hours: on attempt 8
compute queued on one VM while 23 sat idle for most of a day (A16), the
round-5 gate went from 19 to 83 findings, a session was killed at 07:00 UTC,
and a caller that died was charged 400 core-hours. Each was visible in data
the run already writes; nothing read it while the run was live.

Rules (thresholds in cloud/run_health.yaml; each alert fires once per key):
  idle_compute     compute chunks queued (a compute-tagged worker holds more
                   than it runs, or compute tasks sit unrunnable) while another
                   compute-tagged worker has had nothing processing for more
                   than `idle_minutes` (the scheduler, read as compute.py reads it)
  gate_spike       a gate's finding count is more than `ratio` x the last gate
                   of the previous round, and at least `min_findings`
  session_error    a `session_error` event in trace.jsonl
  stall            no heartbeat in status.json for `heartbeat_minutes`, or no
                   tool_use in trace.jsonl for `tool_use_minutes`
  spend_early      spend over `fraction` of the cap before round `before_round`
  compute_no_rows  a compute-ledger call charged at least `min_core_hours`
                   with no completed row

Inputs are a snapshot: {"at": epoch s, "trace": [...], "validator_history":
[...], "compute_ledger": {...}, "budget": {...}, "status": {...} | None,
"scheduler": {...} | None}. `check()` is pure apart from the Monitor's memory
(idle-since times and alerts already raised), so a recorded run replays
through it exactly as a live one does. Nothing here attaches to a cluster;
run_cluster.py's `health` command collects the snapshot (attach-only) and
calls it. Alerts go to a log file and stdout; there are no notifications.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
THRESHOLDS = os.path.join(HERE, "run_health.yaml")
DEFAULTS = {
    "idle_compute": {"idle_minutes": 15, "min_idle_workers": 1},
    "gate_spike": {"ratio": 3.0, "min_findings": 20},
    "session_error": {"enabled": True},
    "stall": {"heartbeat_minutes": 10, "tool_use_minutes": 45},
    "spend_early": {"fraction": 0.5, "before_round": 4},
    "compute_no_rows": {"min_core_hours": 1.0},
}
RESOURCE = "compute"                 # the tag compute.py's ComputeTagPlugin sets
DEMAND_RESOURCE = "ma-compute-demand"   # compute.py's demand markers (never run, by design)


# --------------------------------------------------------------------------- #
# Thresholds
# --------------------------------------------------------------------------- #

def load_thresholds(path: str = THRESHOLDS) -> dict:
    """The YAML config merged over DEFAULTS. Unknown rules or keys, and values
    that are not finite non-negative numbers (or booleans for `enabled`), are
    errors: a typo must not switch a rule off."""
    import yaml
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: top level must be a mapping of rule -> settings")
    out = {k: dict(v) for k, v in DEFAULTS.items()}
    for rule, settings in doc.items():
        if rule not in DEFAULTS:
            raise ValueError(f"{path}: unknown rule {rule!r} (known: {sorted(DEFAULTS)})")
        if not isinstance(settings, dict):
            raise ValueError(f"{path}: {rule} must be a mapping")
        for k, v in settings.items():
            if k not in DEFAULTS[rule]:
                raise ValueError(f"{path}: {rule}.{k} is not a setting (known: {sorted(DEFAULTS[rule])})")
            want_bool = isinstance(DEFAULTS[rule][k], bool)
            if want_bool:
                if not isinstance(v, bool):
                    raise ValueError(f"{path}: {rule}.{k} must be true or false")
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                raise ValueError(f"{path}: {rule}.{k} must be a finite number >= 0")
            out[rule][k] = v
    return out


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _epoch(ts) -> float | None:
    """ISO timestamp (naive = UTC, as trace.jsonl writes) or epoch -> epoch s."""
    if isinstance(ts, bool) or ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts) if math.isfinite(ts) else None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(x) -> float:
    if isinstance(x, bool):
        return 0.0
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _alert(rule: str, key: str, at: float, msg: str) -> dict:
    return {"rule": rule, "key": key, "at": at, "at_iso": _iso(at), "message": msg}


# --------------------------------------------------------------------------- #
# Rules: each takes (snapshot, settings, memory) and returns alerts
# --------------------------------------------------------------------------- #

def rule_idle_compute(snap: dict, cfg: dict, mem: dict) -> list[dict]:
    sch = snap.get("scheduler")
    now = snap["at"]
    if not isinstance(sch, dict) or not isinstance(sch.get("workers"), dict):
        return []
    # A VM counts as starved from the first snapshot in which it is idle while
    # work is queued; idle with nothing queued is the pool's normal slack.
    starved = mem.setdefault("starved_since", {})
    tagged = {a: w for a, w in sch["workers"].items() if isinstance(w, dict) and w.get("compute")}
    slots = {a: max(1, int(_num(w.get("slots")) or 1)) for a, w in tagged.items()}
    queued = sum(max(0, int(_num(w.get("processing"))) - slots[a]) for a, w in tagged.items())
    queued += int(_num(sch.get("unrunnable_compute")))
    idle = [a for a, w in tagged.items()
            if int(_num(w.get("processing"))) == 0 and w.get("status", "running") == "running"]
    if queued <= 0:
        starved.clear()
        mem.pop("episode", None)
        return []
    for a in list(starved):
        if a not in idle:
            starved.pop(a)
    for a in idle:
        starved.setdefault(a, now)
    long_idle = [a for a in idle if now - starved[a] >= cfg["idle_minutes"] * 60]
    if len(long_idle) < max(1, cfg["min_idle_workers"]):
        if not long_idle:
            mem.pop("episode", None)
        return []
    episode = mem.setdefault("episode", now)
    mins = round((now - min(starved[a] for a in long_idle)) / 60)
    return [_alert("idle_compute", f"idle_compute@{episode}", now,
                   f"{queued} compute chunk(s) queued while {len(long_idle)} of {len(tagged)} compute VMs "
                   f"have had nothing to run for {mins}+ min (A16 pattern: work not reaching free workers)")]


def _gates(history: list) -> list[tuple[float, int, int]]:
    out = []
    for g in history or []:
        if not isinstance(g, dict) or not isinstance(g.get("findings"), list):
            continue
        r = g.get("round")
        if isinstance(r, bool) or not isinstance(r, int):
            continue
        out.append((_epoch(g.get("at")) or 0.0, r, len(g["findings"])))
    return out


def rule_gate_spike(snap: dict, cfg: dict, mem: dict) -> list[dict]:
    out = []
    gates = _gates(snap.get("validator_history"))
    last_by_round: dict[int, int] = {}
    for at, r, n in gates:
        prev_rounds = [k for k in last_by_round if k < r]
        if prev_rounds and r not in last_by_round:
            base = last_by_round[max(prev_rounds)]
            if n >= cfg["min_findings"] and n > cfg["ratio"] * max(base, 1):
                out.append(_alert("gate_spike", f"gate_spike@r{r}", at or snap["at"],
                                  f"round {r} gate: {n} findings, {n / max(base, 1):.1f}x the {base} of round "
                                  f"{max(prev_rounds)} (threshold {cfg['ratio']}x)"))
        last_by_round[r] = n
    return out


def rule_session_error(snap: dict, cfg: dict, mem: dict) -> list[dict]:
    if not cfg["enabled"]:
        return []
    out = []
    for e in snap.get("trace") or []:
        if isinstance(e, dict) and e.get("type") == "session_error":
            at = _epoch(e.get("ts")) or snap["at"]
            err = clip(e.get("error") or "", 160).replace("\n", " ")
            out.append(_alert("session_error", f"session_error@{e.get('ts')}", at,
                              f"session {e.get('session')} ended with an error: {err}"))
    return out


def rule_stall(snap: dict, cfg: dict, mem: dict) -> list[dict]:
    now, out = snap["at"], []
    if _finished(snap):
        return []
    st = snap.get("status")
    if isinstance(st, dict):
        hb = _epoch(st.get("heartbeat"))
        if hb is None or now - hb > cfg["heartbeat_minutes"] * 60:
            age = "never" if hb is None else f"{round((now - hb) / 60)} min ago"
            out.append(_alert("stall", f"heartbeat@{st.get('heartbeat')}", now,
                              f"no heartbeat in status.json for {cfg['heartbeat_minutes']}+ min (last: {age})"))
    tools = [_epoch(e.get("ts")) for e in snap.get("trace") or []
             if isinstance(e, dict) and e.get("type") == "tool_use"]
    tools = [t for t in tools if t is not None]
    if tools and now - max(tools) > cfg["tool_use_minutes"] * 60:
        out.append(_alert("stall", f"tool_use@{max(tools)}", now,
                          f"no tool use for {round((now - max(tools)) / 60)} min (threshold "
                          f"{cfg['tool_use_minutes']} min); last at {_iso(max(tools))}"))
    return out


def _finished(snap: dict) -> bool:
    tr = [e for e in snap.get("trace") or [] if isinstance(e, dict)]
    return bool(tr) and tr[-1].get("type") == "result" and not any(
        e.get("type") == "session_start" and (_epoch(e.get("ts")) or 0) > (_epoch(tr[-1].get("ts")) or 0) for e in tr)


def _current_round(snap: dict) -> int:
    st = snap.get("status") if isinstance(snap.get("status"), dict) else {}
    r = (st.get("gate") or {}).get("round") if isinstance(st.get("gate"), dict) else None
    gates = _gates(snap.get("validator_history"))
    rounds = [g[1] for g in gates] + ([r] if isinstance(r, int) and not isinstance(r, bool) else [])
    return max(rounds) + 1 if rounds else 1      # the round being worked on


def rule_spend_early(snap: dict, cfg: dict, mem: dict) -> list[dict]:
    st = snap.get("status") if isinstance(snap.get("status"), dict) else {}
    led = st.get("ledger") if isinstance(st.get("ledger"), dict) else {}
    bud = snap.get("budget") if isinstance(snap.get("budget"), dict) else {}
    cap = _num(led.get("cap_usd")) or _num(bud.get("cap_usd"))
    spent = max(_num(led.get("spent_usd")), _num(st.get("est_spend_run_total_usd")), _num(bud.get("spent_usd")))
    rnd = _current_round(snap)
    if cap and rnd < cfg["before_round"] and spent > cfg["fraction"] * cap:
        return [_alert("spend_early", "spend_early", snap["at"],
                       f"${spent:.0f} spent ({spent / cap:.0%} of the ${cap:.0f} cap) in round {rnd}, before round "
                       f"{int(cfg['before_round'])} (threshold {cfg['fraction']:.0%})")]
    return []


def rule_compute_no_rows(snap: dict, cfg: dict, mem: dict) -> list[dict]:
    led = snap.get("compute_ledger")
    calls = led.get("calls") if isinstance(led, dict) else None
    out = []
    for c in calls if isinstance(calls, list) else []:
        if not isinstance(c, dict):
            continue
        core_h = _num(c.get("task_core_s")) / 3600
        n, failed = c.get("n"), c.get("n_failed")
        ok_rows = (int(_num(n)) - int(_num(failed))) if n is not None else 0
        if core_h >= cfg["min_core_hours"] and ok_rows <= 0:
            at = _epoch(c.get("ts")) or snap["at"]
            why = clip(c.get("stopped") or "no rows", 100)
            out.append(_alert("compute_no_rows", f"compute_no_rows@{c.get('ts')}:{c.get('fn')}", at,
                              f"compute call charged {core_h:.1f} core-hours with no completed row "
                              f"({os.path.basename(str(c.get('fn') or '?'))}; {why})"))
    return out


RULES = {"idle_compute": rule_idle_compute, "gate_spike": rule_gate_spike, "session_error": rule_session_error,
         "stall": rule_stall, "spend_early": rule_spend_early, "compute_no_rows": rule_compute_no_rows}


class Monitor:
    """Holds what must persist between polls; `check` returns only new alerts."""

    def __init__(self, thresholds: dict | None = None):
        self.t = thresholds or {k: dict(v) for k, v in DEFAULTS.items()}
        self.mem: dict = {}
        self.seen: set = set()

    def check(self, snap: dict) -> list[dict]:
        new = []
        for name, fn in RULES.items():
            try:
                alerts = fn(snap, self.t[name], self.mem.setdefault(name, {}))
            except Exception as e:  # noqa: BLE001 — a monitor that crashes watches nothing
                alerts = [_alert("monitor_error", f"monitor_error:{name}:{type(e).__name__}", snap["at"],
                                 f"rule {name} failed on this snapshot: {type(e).__name__}: {e}")]
            for a in alerts:
                if a["key"] not in self.seen:
                    self.seen.add(a["key"])
                    new.append(a)
        return new


_R = "[REDACTED]"
_SECRET_PATTERNS = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), _R),                      # Anthropic API keys
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{16,}"), _R),             # OpenAI-style keys
    (re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 " + _R),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), _R),   # JWTs
    (re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@"), r"\1" + _R + "@"),  # URL userinfo
    (re.compile(r"(?i)([?&;\s](?:sig|signature|token|access_token|refresh_token|id_token|api[_-]?key|key|"
                r"password|passwd|pwd|secret|client_secret|code|credential|auth)=)[^&\s;\"']+"), r"\1" + _R),
    (re.compile(r"(?i)\b(AccountKey|SharedAccessKey|SharedAccessSignature|Password)=[^;\s\"']+"), r"\1=" + _R),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), _R),                   # GitHub tokens
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), _R),                  # AWS access key ids
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), _R),                # Slack tokens
    # Quoted JSON / Python-dict fields: "api_key": "...", 'access_token': '...'
    (re.compile(r"""(?i)(["'](?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|"""
                r"""secret|password|passwd|token|authorization|auth|sas|sig|signature|accountkey|"""
                r"""sharedaccesskey|credential)s?["']\s*:\s*)(["'])(?:\\.|(?!\2).)*\2"""), r"\1\2" + _R + r"\2"),
]


def redact(text) -> str:
    """Credentials removed from a free-form string (an error message, a trace
    line) before it is printed or logged. Applied to every alert in
    format_alert, which every output path goes through."""
    s = str(text)
    for pat, repl in _SECRET_PATTERNS:
        s = pat.sub(repl, s)
    return s


def clip(text, n: int) -> str:
    """Redact, then truncate: a secret cut by truncation would leave a prefix
    the patterns no longer match. Every captured free-form string uses this."""
    return redact(text)[:n]


def format_alert(a: dict, label: str = "") -> str:
    return redact(f"{a['at_iso']} {label + ' ' if label else ''}HEALTH {a['rule']}: {a['message']}")


def log_alerts(alerts: list[dict], path: str | None, label: str = "") -> None:
    lines = [format_alert(a, label) for a in alerts]
    for ln in lines:
        print(ln, flush=True)
    if path and lines:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Collecting a live snapshot. These run remotely (pickled by value): the
# scheduler view on the run cluster's scheduler, the file view on the run's
# agent VM. Neither is called by the self-test.
# --------------------------------------------------------------------------- #

def scheduler_view(resource=RESOURCE, demand=DEMAND_RESOURCE, dask_scheduler=None) -> dict:
    """Runs on the scheduler, as compute.py's `_sched_view` does: per worker,
    whether it carries the compute tag, tasks processing there, its threads
    and status; and compute tasks waiting unrunnable (demand markers, which
    never run by design, excluded)."""
    import time as _t
    s = dask_scheduler
    un = 0
    for ts in list(getattr(s, "unrunnable", ()) or ()):
        rr = getattr(ts, "resource_restrictions", None) or {}
        if resource in rr and demand not in rr:
            un += 1
    return {"at": _t.time(), "unrunnable_compute": un,
            "workers": {a: {"compute": resource in (ws.resources or {}), "processing": len(ws.processing),
                            "slots": int((ws.resources or {}).get(resource, 1) or 1),
                            "status": getattr(ws.status, "name", str(ws.status))}
                        for a, ws in s.workers.items()}}


def run_files_view(label, work="/tmp/ab", trace_tail=3000) -> dict:
    """Runs on the run's agent VM: the newest run directory's monitor inputs.
    trace.jsonl is read from its last `trace_tail` lines; unreadable lines and
    files are reported in `errors`, never dropped silently."""
    import glob, json, os, time as _t  # noqa: E401
    rds = sorted(glob.glob(f"{work}/{label}/runs/*/"))
    out = {"at": _t.time(), "run_dir": rds[-1] if rds else None, "errors": [], "trace": [],
           "validator_history": [], "compute_ledger": None, "budget": None, "status": None}
    if not rds:
        out["errors"].append("no run directory yet")
        return out
    rd = rds[-1]

    def jl(p, tail=None):
        rows = []
        if not os.path.exists(p):
            return rows
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            out["errors"].append(f"{os.path.basename(p)}: {e}")
            return rows
        for ln in lines[-tail:] if tail else lines:
            if ln.strip():
                try:
                    rows.append(json.loads(ln))
                except ValueError:
                    out["errors"].append(f"{os.path.basename(p)}: unparseable line")
        return rows

    def js(p):
        if not os.path.exists(p):
            return None
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            out["errors"].append(f"{os.path.basename(p)}: {type(e).__name__}")
            return None

    out["trace"] = jl(rd + "trace.jsonl", trace_tail)
    out["validator_history"] = jl(rd + "validator_history.jsonl")
    out["compute_ledger"] = js(rd + "models/compute_ledger.json")
    out["budget"] = js(rd + "budget_ledger.json")
    out["status"] = js(rd + "status.json")
    return out


# --------------------------------------------------------------------------- #
# Replay of a recorded run
# --------------------------------------------------------------------------- #

def _read_jsonl(p: str) -> list:
    rows = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            for ln in f:
                if ln.strip():
                    rows.append(json.loads(ln))
    return rows


def _read_json(p: str):
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_recorded(run_dir: str) -> dict:
    """trace.jsonl, validator_history.jsonl, models/compute_ledger.json and
    budget_ledger.json of a finished run (status.json is only the last beat,
    so it is not replayed)."""
    led = _read_json(os.path.join(run_dir, "models", "compute_ledger.json"))
    if led is None:
        led = _read_json(os.path.join(run_dir, "compute_ledger.json"))
    return {"trace": _read_jsonl(os.path.join(run_dir, "trace.jsonl")),
            "validator_history": _read_jsonl(os.path.join(run_dir, "validator_history.jsonl")),
            "compute_ledger": led or {},
            "budget": _read_json(os.path.join(run_dir, "budget_ledger.json")) or {}}


def reduce_recorded(rec: dict) -> dict:
    """Only the fields the monitor reads, so a recorded run can be kept as a
    small fixture without its agent text (tool summaries, purposes, paths)."""
    keep_types = {"session_start", "session_error", "result", "tool_use"}
    trace = [{k: e[k] for k in ("ts", "type", "session", "error", "total_cost_usd") if k in e}
             for e in rec.get("trace") or [] if isinstance(e, dict) and e.get("type") in keep_types]
    hist = [{"round": g.get("round"), "at": g.get("at"), "action": g.get("action"),
             "findings": [{"kind": f.get("kind"), "severity": f.get("severity")} for f in g.get("findings") or []
                          if isinstance(f, dict)]}
            for g in rec.get("validator_history") or [] if isinstance(g, dict)]
    calls = [{"ts": c.get("ts"), "workers": c.get("workers"), "n": c.get("n"), "n_failed": c.get("n_failed"),
              "task_core_s": c.get("task_core_s"), "wall_s": c.get("wall_s"),
              "stopped": (str(c["stopped"]).split(":")[0] if c.get("stopped") else None),
              "fn": os.path.basename(str(c.get("fn") or "")).split("::")[-1] or None}
             for c in (rec.get("compute_ledger") or {}).get("calls") or [] if isinstance(c, dict)]
    bud = rec.get("budget") or {}
    budget = {"cap_usd": bud.get("cap_usd"), "spent_usd": bud.get("spent_usd"),
              "sessions": [{"session": s.get("session"), "reported_usd": s.get("reported_usd")}
                           for s in bud.get("sessions") or [] if isinstance(s, dict)]}
    return {"trace": trace, "validator_history": hist, "compute_ledger": {"calls": calls}, "budget": budget}


def write_recorded(rec: dict, out_dir: str) -> None:
    os.makedirs(os.path.join(out_dir, "models"), exist_ok=True)
    for name, rows in (("trace.jsonl", rec["trace"]), ("validator_history.jsonl", rec["validator_history"])):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.writelines(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
    with open(os.path.join(out_dir, "models", "compute_ledger.json"), "w", encoding="utf-8") as f:
        json.dump(rec["compute_ledger"], f, indent=0)
    with open(os.path.join(out_dir, "budget_ledger.json"), "w", encoding="utf-8") as f:
        json.dump(rec["budget"], f, indent=1)


def _spend_steps(rec: dict) -> list[tuple[float, float, float]]:
    """(time, spent, cap) known from the recorded run: each session's charge,
    from budget_ledger.json, at the time that session ended in the trace."""
    cap = _num((rec.get("budget") or {}).get("cap_usd"))
    sessions = {s.get("session"): s for s in (rec.get("budget") or {}).get("sessions") or [] if isinstance(s, dict)}
    out, cur, spent = [], None, 0.0
    for e in rec.get("trace") or []:
        if e.get("type") == "session_start":
            cur = e.get("session")
        if e.get("type") in ("result", "session_error") and cur in sessions:
            spent = max(spent, _num(sessions[cur].get("reported_usd")))
            out.append((_epoch(e.get("ts")) or 0.0, spent, cap))
    return out


def replay(rec: dict, scheduler: list | None = None, thresholds: dict | None = None,
           step_s: float = 60.0) -> list[dict]:
    """Feed a recorded run to a Monitor one `step_s` at a time, each snapshot
    holding only what had been written by then. `scheduler` is a list of
    scheduler snapshots ({"at": epoch, ...}), applied from their time on."""
    times = [t for t in (_epoch(e.get("ts")) for e in rec.get("trace") or []) if t]
    for g in rec.get("validator_history") or []:
        t = _epoch(g.get("at"))
        if t:
            times.append(t)
    sched = sorted((s for s in scheduler or [] if isinstance(s, dict) and _epoch(s.get("at"))),
                   key=lambda s: _epoch(s["at"]))
    times += [_epoch(s["at"]) for s in sched]
    if not times:
        return []
    t0, t1 = min(times), max(times)
    mon = Monitor(thresholds)
    trace = sorted(rec.get("trace") or [], key=lambda e: _epoch(e.get("ts")) or 0)
    hist = sorted(rec.get("validator_history") or [], key=lambda g: _epoch(g.get("at")) or 0)
    calls = sorted(((rec.get("compute_ledger") or {}).get("calls") or []), key=lambda c: _epoch(c.get("ts")) or 0)
    spend = _spend_steps(rec)
    out = []
    ti = hi = ci = si = 0
    sch_now = None
    tr, hs, cs = [], [], []
    t = t0
    while t <= t1 + step_s:
        while ti < len(trace) and (_epoch(trace[ti].get("ts")) or 0) <= t:
            tr.append(trace[ti])
            ti += 1
        while hi < len(hist) and (_epoch(hist[hi].get("at")) or 0) <= t:
            hs.append(hist[hi])
            hi += 1
        while ci < len(calls) and (_epoch(calls[ci].get("ts")) or 0) <= t:
            cs.append(calls[ci])
            ci += 1
        while si < len(sched) and _epoch(sched[si]["at"]) <= t:
            sch_now = sched[si]
            si += 1
        known = [s for s in spend if s[0] <= t]
        budget = {"spent_usd": known[-1][1], "cap_usd": known[-1][2]} if known else {}
        snap = {"at": t, "trace": tr, "validator_history": hs, "compute_ledger": {"calls": cs},
                "budget": budget, "status": None, "scheduler": sch_now}
        out += mon.check(snap)
        t += step_s
    return out


def synthetic_a16_snapshots(rec: dict, pool: int = 24, min_rows: int = 100, step_s: float = 300.0) -> list[dict]:
    """Scheduler snapshots reconstructed for attempt 8, whose live scheduler
    state was not stored. Phase 45 δ measured it once: one compute worker held
    all queued chunks while the other 23 compute VMs were idle. Here every
    ledger call that ran on `workers: 1` with at least `min_rows` rows is
    assumed to have looked like that measurement for its wall time; every
    other moment, an idle pool with nothing queued. Labelled synthetic in
    every use."""
    snaps = []
    for c in (rec.get("compute_ledger") or {}).get("calls") or []:
        end = _epoch(c.get("ts"))
        wall = _num(c.get("wall_s"))
        if not end or not wall or c.get("workers") != 1 or int(_num(c.get("n"))) < min_rows:
            continue
        start = end - wall
        t = start
        while t <= end:
            w = {"vm00": {"compute": True, "processing": pool, "slots": 1, "status": "running"}}
            w.update({f"vm{i:02d}": {"compute": True, "processing": 0, "slots": 1, "status": "running"}
                      for i in range(1, pool)})
            snaps.append({"at": t, "workers": w, "unrunnable_compute": 0, "synthetic": True})
            t += step_s
        snaps.append({"at": end + 1, "unrunnable_compute": 0, "synthetic": True,
                      "workers": {f"vm{i:02d}": {"compute": True, "processing": 0, "slots": 1, "status": "running"}
                                  for i in range(pool)}})
    return sorted(snaps, key=lambda s: s["at"])


# --------------------------------------------------------------------------- #
# Self-test (no cluster, no network)
# --------------------------------------------------------------------------- #

FIXTURE = os.path.join(HERE, "fixtures", "e7r8_health")


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            fails.append(label)

    T = load_thresholds(THRESHOLDS)
    ok(T == DEFAULTS, "RH0 cloud/run_health.yaml states the defaults")
    with tempfile.TemporaryDirectory() as td:
        for bad, label in (("idle_compute: {idle_minuts: 5}\n", "a misspelt setting"),
                           ("gate_spikes: {ratio: 2}\n", "an unknown rule"),
                           ("gate_spike: {ratio: .nan}\n", "NaN"),
                           ("gate_spike: {ratio: -1}\n", "a negative threshold"),
                           ("session_error: {enabled: 1}\n", "a non-boolean switch"),
                           ("- 1\n", "a list")):
            p = os.path.join(td, "t.yaml")
            with open(p, "w") as f:
                f.write(bad)
            try:
                load_thresholds(p)
                ok(False, f"RH1 thresholds refuse {label}")
            except ValueError:
                ok(True, f"RH1 thresholds refuse {label}")

    # Replays of attempt 8's stored trace, gate history and compute ledger.
    rec = load_recorded(FIXTURE)
    ok(len(rec["trace"]) > 1000 and len(rec["validator_history"]) == 21 and rec["compute_ledger"].get("calls"),
       "RH2 attempt 8's recorded files load from the fixture")
    alerts = replay(rec, thresholds=T, step_s=300)
    by = {}
    for a in alerts:
        by.setdefault(a["rule"], []).append(a)
    se = by.get("session_error", [])
    ok(len(se) == 1 and se[0]["at_iso"] == "2026-10-29T07:00:25Z" and "143" in se[0]["message"],
       f"RH3 the round-2 session_error (exit 143) fires at 07:00 UTC ({[a['at_iso'] for a in se]})")
    gs = by.get("gate_spike", [])
    ok(len(gs) == 1 and "round 5 gate: 83 findings" in gs[0]["message"] and gs[0]["at_iso"].startswith("2026-10-30T01:44"),
       f"RH4 the round-5 gate spike (19 -> 83) fires, and only it ({[a['message'] for a in gs]})")
    cn = by.get("compute_no_rows", [])
    ok(any("400.0 core-hours" in a["message"] for a in cn) and len(cn) == 4,
       f"RH5 compute charged with no completed row: the 400 core-hour dead caller and 3 interrupted calls ({len(cn)})")
    ok(not by.get("stall") and not by.get("spend_early") and not by.get("monitor_error"),
       f"RH6 no stall, early-spend or monitor error on attempt 8's recorded files ({sorted(by)})")

    # Synthetic scheduler snapshots: the A16 measurement (one VM holds all
    # queued chunks, 23 idle), and the healthy and edge cases around it.
    def sched(at, busy, idle, held=1, unrun=0, tag=True):
        w = {f"b{i}": {"compute": tag, "processing": held, "slots": 1, "status": "running"} for i in range(busy)}
        w.update({f"i{i}": {"compute": tag, "processing": 0, "slots": 1, "status": "running"} for i in range(idle)})
        return {"at": at, "workers": w, "unrunnable_compute": unrun}

    def run(snaps, t=T):
        m = Monitor(t)
        out = []
        for s in snaps:
            out += [a for a in m.check({"at": s["at"], "scheduler": s}) if a["rule"] == "idle_compute"]
        return out

    a16 = [sched(60 * k, 1, 23, held=24) for k in range(0, 41)]
    got = run(a16)
    ok(len(got) == 1 and got[0]["at"] == 15 * 60 and "23 queued" not in got[0]["message"]
       and "23 of 24 compute VMs" in got[0]["message"],
       f"RH7 A16 pattern fires once, at 15 min idle ({[(a['at'], a['message']) for a in got]})")
    ok(run([sched(60 * k, 24, 0, held=1) for k in range(41)]) == [], "RH8 a busy pool with nothing queued: silent")
    ok(run([sched(60 * k, 1, 23, held=1) for k in range(41)]) == [], "RH9 idle VMs with nothing queued: silent")
    ok(run([sched(60 * k, 1, 23, held=24) for k in range(14)]) == [], "RH10 idle under 15 min: silent")
    flap = [sched(60 * k, 1, 23, held=24) if k % 10 else sched(60 * k, 24, 0, held=2) for k in range(41)]
    ok(run(flap) == [], "RH11 workers that pick up work every 10 min never count as idle for 15")
    ok(len(run([sched(60 * k, 0, 2, unrun=5) for k in range(20)])) == 1,
       "RH12 unrunnable compute tasks with idle tagged VMs fire")
    ok(run([sched(60 * k, 1, 23, held=24, tag=False) for k in range(41)]) == [],
       "RH13 untagged workers (the agent VM) are not compute VMs")
    two = a16 + [sched(60 * k, 24, 0) for k in range(41, 50)] + [sched(60 * k, 1, 23, held=24) for k in range(50, 80)]
    ok(len(run(two)) == 2, "RH14 a second idle episode after recovery fires again")
    syn = synthetic_a16_snapshots(rec)
    syn_alerts = [a for a in replay(rec, scheduler=syn, thresholds=T, step_s=300) if a["rule"] == "idle_compute"]
    ok(len(syn_alerts) >= 10 and all(s.get("synthetic") for s in syn),
       f"RH15 attempt 8 with ledger-derived synthetic snapshots: idle_compute fires per long workers:1 call ({len(syn_alerts)})")

    # Other rules on small synthetic snapshots.
    now = 1_800_000_000.0
    m = Monitor(T)
    st = {"heartbeat": _iso(now - 11 * 60), "ledger": {"cap_usd": 600, "spent_usd": 320},
          "gate": {"round": 2}}
    al = m.check({"at": now, "status": st, "trace": [{"ts": _iso(now - 50 * 60), "type": "tool_use"}]})
    rules = sorted(a["rule"] for a in al)
    ok(rules == ["spend_early", "stall", "stall"],
       f"RH16 stale heartbeat, 50 min without tool use and 53% spent in round 3 all fire ({rules})")
    ok(m.check({"at": now + 60, "status": st, "trace": [{"ts": _iso(now - 50 * 60), "type": "tool_use"}]}) == [],
       "RH17 each alert fires once")
    done = [{"ts": _iso(now - 50 * 60), "type": "tool_use"}, {"ts": _iso(now - 49 * 60), "type": "result"}]
    ok(not [a for a in Monitor(T).check({"at": now, "trace": done}) if a["rule"] == "stall"],
       "RH18 a finished session is not a stall")
    st2 = {"heartbeat": _iso(now), "ledger": {"cap_usd": 600, "spent_usd": 320}, "gate": {"round": 4}}
    ok(Monitor(T).check({"at": now, "status": st2, "trace": []}) == [], "RH19 the same spend in round 5 is silent")
    m2 = Monitor(T)
    al2 = m2.check({"at": now, "scheduler": {"workers": "garbage"}, "validator_history": [{"round": True}],
                    "compute_ledger": {"calls": [{"task_core_s": float("nan"), "n": 3, "n_failed": 3}]},
                    "trace": ["x", {"type": "session_error", "ts": "2026-02-30"}]})
    ok([a["rule"] for a in al2] == ["session_error"], f"RH20 malformed inputs neither crash nor fire falsely ({al2})")
    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "h", "health.log")
        log_alerts(se, lp, "R1")
        with open(lp) as f:
            ok(f.read().startswith("2026-10-29T07:00:25Z R1 HEALTH session_error:"), "RH21 alerts are appended to the log")

    # Codex round 1, P1-3: credentials in any free-form string are redacted
    # before an alert is printed or logged (one place: format_alert).
    secrets = {
        "anthropic key": "sk-ant-api03-AbCdEf0123456789_-AbCdEf0123456789",
        "openai key": "sk-proj-AbCdEf0123456789AbCdEf0123456789",
        "bearer": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl",
        "url userinfo": "https://alice:s3cretPass@storage.example.net/x",
        "query secret": "https://api.example.net/v1?api_key=AbC123SECRET&x=1",
        "azure sas": "https://acct.blob.core.windows.net/c/b?sv=2022-11-02&ss=b&sig=AbCdEf%2Bgh%3D&se=2026",
        "azure connection string": "DefaultEndpointsProtocol=https;AccountName=a;AccountKey=QUJDREVGR0g=;Endpoint",
        "github token": "ghp_AbCdEf0123456789AbCdEf0123456789AbCd",
        "aws key": "AKIAABCDEFGHIJKLMNOP",
    }
    leaked = {"AbCdEf0123456789_-AbCdEf0123456789", "AbCdEf0123456789AbCdEf0123456789", "c2lnbmF0dXJl",
              "s3cretPass", "AbC123SECRET", "AbCdEf%2Bgh%3D", "QUJDREVGR0g=", "ghp_AbCdEf", "AKIAABCDEFGHIJKLMNOP"}
    for label, sec in secrets.items():
        a = _alert("monitor_error", f"k:{label}", now, f"could not read status.json: {sec} (exit 1)")
        line = format_alert(a)
        ok(not any(x in line for x in leaked) and "[REDACTED]" in line and "exit 1" in line,
           f"RH22 a {label} is redacted from an alert ({line[-90:]})")
    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "h.log")
        log_alerts([_alert("session_error", "x", now, "error: " + secrets["anthropic key"])], lp, "R1")
        ok("sk-ant" not in open(lp).read(), "RH23 the log file gets the redacted text too")
    ok(redact("tls://10.0.0.7:8786 round 5 gate: 83 findings") == "tls://10.0.0.7:8786 round 5 gate: 83 findings",
       "RH24 ordinary text, addresses and numbers are left alone")
    # Codex round 2, example 1: a key that straddles a truncation boundary.
    key = "sk-ant-api03-ZZsecretZZsecretZZsecretZZ"
    err = "x" * 150 + key + " trailing"
    al = Monitor(T).check({"at": now, "trace": [{"ts": _iso(now), "type": "session_error", "session": 4,
                                                 "error": err}]})
    line = format_alert(al[0]) if al else ""
    ok(al and "sk-ant" not in line and "ZZsecret" not in line,
       f"RH25 a key cut by truncation is redacted first, not left as a prefix ({line[-60:]})")
    ok("sk-ant" not in clip(err, 160) and clip("a" * 300, 10) == "a" * 10, "RH25b clip redacts, then truncates")
    # Codex round 2, example 2: quoted JSON / dict credential fields with colon separators.
    for raw in ('{"api_key": "AKsecret123"}', "{'access_token': 'AKsecret123'}", '"Refresh_Token":"AKsecret123"',
                '{"secret": "AKsecret123", "n": 1}', "{'Password': 'AKsecret123'}", '"token": "AKsecret123"',
                '"authorization": "Basic AKsecret123"', "{'sas': 'AKsecret123'}", '"sig":"AKsecret123"',
                '"AccountKey": "AKsecret123"'):
        out = redact(raw)
        ok("AKsecret123" not in out and "[REDACTED]" in out, f"RH26 quoted credential field redacted: {raw} -> {out}")
    ok(redact('{"round": 5, "findings": 83, "key_count": 2}') == '{"round": 5, "findings": 83, "key_count": 2}',
       "RH27 ordinary JSON fields are left alone")

    if fails:
        print(f"FAILED: {len(fails)} self-test case(s).")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    rp = sub.add_parser("replay", help="replay a recorded run directory through the monitor")
    rp.add_argument("run_dir")
    rp.add_argument("--scheduler", help="JSONL of scheduler snapshots")
    rp.add_argument("--synthetic-a16", action="store_true",
                    help="add scheduler snapshots reconstructed from the ledger (attempt 8's A16 state)")
    rp.add_argument("--thresholds", default=THRESHOLDS)
    rp.add_argument("--step", type=float, default=60.0)
    fx = sub.add_parser("fixture", help="write a recorded run's monitor inputs, reduced, as a fixture")
    fx.add_argument("run_dir")
    fx.add_argument("out_dir")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.cmd == "fixture":
        write_recorded(reduce_recorded(load_recorded(a.run_dir)), a.out_dir)
        print(f"wrote {a.out_dir}")
        return 0
    if a.cmd != "replay":
        ap.print_help()
        return 2
    rec = load_recorded(a.run_dir)
    sched = _read_jsonl(a.scheduler) if a.scheduler else []
    if a.synthetic_a16:
        sched += synthetic_a16_snapshots(rec)
    alerts = replay(rec, sched, load_thresholds(a.thresholds), a.step)
    for al in alerts:
        print(format_alert(al))
    print(f"{len(alerts)} alert(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
