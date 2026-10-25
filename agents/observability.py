"""Phase 29: live visibility into a running pipeline.

E7 attempt 5 died after 2.9 h with no record of why (probably VM memory), and
spend inside a session was invisible until it ended. A heartbeat thread in
the harness now writes, every minute:

- {run}/status.json — stage, round, active agent, last gate decision and its
  HIGH count, sessions, ledger spend and a live cost estimate from the token
  usage the SDK streams, elapsed time, last heartbeat;
- {run}/resources.jsonl — memory, CPU, disk and the largest processes;

and, when a storage account is configured, uploads status.json every
heartbeat and a full checkpoint of the run every 30 minutes, so progress can
be watched from a laptop (scripts/watch_run.py) and a dead VM still leaves
everything up to its last checkpoint.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime

STATUS_FILE = "status.json"
RESOURCES_FILE = "resources.jsonl"
# $ per weighted token, same weighting as scripts/telemetry.py (fitted on 96 E5 cases)
USD_PER_WEIGHTED_TOKEN = {"claude-opus-5-5": 4.131e-6, "claude-sonnet-5-5": 3.08e-6,
                          # not fitted (no Haiku cases): ~1/4 of the Opus rate; smoke runs only
                          "claude-haiku-5-5": 1.0e-6}
RATIOS = {"input_tokens": 1.0, "cache_read_input_tokens": 0.1,
          "cache_creation_input_tokens": 1.25, "output_tokens": 5.0}
STAGE_OF = {"planner": "PLAN", "data-agent": "DATA", "critique-premortem": "PRE_MORTEM",
            "modeler": "MODEL", "analyst": "ANALYZE", "writer": "WRITE",
            "critique-methods": "CRITIQUE", "critique-domain": "CRITIQUE",
            "critique-presentation": "CRITIQUE", "critique-redteam": "CRITIQUE",
            "critique-sufficiency": "POST_WRITE"}


class LiveState:
    """Updated from the message stream (main.log_message); read by the heartbeat."""

    def __init__(self):
        self.lock = threading.Lock()
        self.session = 0
        self.active_agents: dict[str, str] = {}     # tool_use_id -> agent type
        self.last_agent = None
        self.last_event = None
        self.last_event_ts = None
        self.est_usd = 0.0
        self._seen_msgs: set = set()
        # Stage-boundary snapshots: the run's state the moment a stage is
        # launched, i.e. exactly what that stage will read (replay fixtures).
        self.snapshot_requests: list[str] = []
        self.wake = threading.Event()

    def on_message(self, message) -> None:
        from claude_agent_sdk import AssistantMessage, UserMessage
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            self.last_event_ts = now
            if isinstance(message, AssistantMessage):
                mid = getattr(message, "message_id", None) or id(message)
                usage = getattr(message, "usage", None) or {}
                if usage and mid not in self._seen_msgs:
                    self._seen_msgs.add(mid)
                    top = max(USD_PER_WEIGHTED_TOKEN.values())
                    w = sum(float(usage.get(k) or 0) * r for k, r in RATIOS.items())
                    self.est_usd += w * USD_PER_WEIGHTED_TOKEN.get(getattr(message, "model", None), top)
                for b in message.content:
                    name = getattr(b, "name", None)
                    if name in ("Agent", "Task"):
                        agent = (getattr(b, "input", None) or {}).get("subagent_type") or "?"
                        self.active_agents[getattr(b, "id", "")] = agent
                        self.last_agent = agent
                        self.last_event = f"spawned {agent}"
                        if agent in STAGE_OF and not getattr(message, "parent_tool_use_id", None):
                            self.snapshot_requests.append(agent)
                            self.wake.set()
                    elif name:
                        self.last_event = f"{name}"
            elif isinstance(message, UserMessage) and isinstance(message.content, list):
                for b in message.content:
                    tid = getattr(b, "tool_use_id", None)
                    if tid in self.active_agents:
                        self.last_event = f"{self.active_agents.pop(tid)} finished"


def _gate(run_path: str) -> dict | None:
    p = os.path.join(run_path, "validator_history.jsonl")
    try:
        last = None
        for line in open(p):
            if line.strip():
                last = json.loads(line)
        if not last:
            return None
        sev = [f.get("severity") for f in last.get("findings") or [] if isinstance(f, dict)]
        return {"round": last.get("round"), "action": last.get("action"),
                "high": sev.count("HIGH"), "medium": sev.count("MEDIUM"), "at": last.get("at")}
    except (OSError, ValueError):
        return None


def _ledger(run_path: str) -> dict:
    try:
        led = json.load(open(os.path.join(run_path, "budget_ledger.json")))
        return {"spent_usd": led.get("spent_usd"), "cap_usd": led.get("cap_usd"),
                "sessions": len(led.get("sessions") or [])}
    except (OSError, ValueError):
        return {}


def resource_sample(run_path: str, top_n: int = 5) -> dict:
    import psutil
    vm = psutil.virtual_memory()
    procs = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info"]):
        try:
            rss = p.info["memory_info"].rss if p.info["memory_info"] else 0
            procs.append((rss, p.info["pid"], " ".join(p.info["cmdline"] or [p.info["name"] or ""])[:120]))
        except (psutil.Error, TypeError):
            continue
    procs.sort(reverse=True)
    try:
        du = psutil.disk_usage(run_path)
        disk = {"used_pct": du.percent, "free_gb": round(du.free / 2**30, 1)}
    except OSError:
        disk = {}
    return {"ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mem_used_pct": vm.percent, "mem_avail_gb": round(vm.available / 2**30, 2),
            "cpu_pct": psutil.cpu_percent(interval=None), "load1": round(os.getloadavg()[0], 2),
            "disk": disk,
            "top": [{"rss_gb": round(r / 2**30, 2), "pid": pid, "cmd": c} for r, pid, c in procs[:top_n]]}


def build_status(run_path: str, state: LiveState, run_start: datetime, extra: dict | None = None) -> dict:
    with state.lock:
        live = {"session": state.session, "active_agents": sorted(set(state.active_agents.values())),
                "last_agent": state.last_agent, "stage": STAGE_OF.get(state.last_agent or "", None),
                "last_event": state.last_event, "last_event_ts": state.last_event_ts,
                "est_spend_this_process_usd": round(state.est_usd, 2)}
    return {"heartbeat": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_min": round((datetime.now() - run_start).total_seconds() / 60, 1),
            **live, "gate": _gate(run_path), "ledger": _ledger(run_path), **(extra or {})}


class Heartbeat(threading.Thread):
    """Daemon thread: status + resources every `interval_s`; when a storage
    account is configured, uploads status every beat and a full checkpoint
    every `checkpoint_s`. Never raises into the run."""

    def __init__(self, run_path: str, state: LiveState, run_start: datetime,
                 interval_s: float = 60, checkpoint_s: float = 1800, uploader=None):
        super().__init__(daemon=True, name="ma-heartbeat")
        self.run_path, self.state, self.run_start = run_path, state, run_start
        self.interval_s, self.checkpoint_s = interval_s, checkpoint_s
        self.stop_event = threading.Event()
        self.uploader = uploader          # callable(local_path, blob_suffix) or None
        self.last_checkpoint = time.time()
        self.errors = 0
        self.snapshots = 0

    def beat(self) -> dict:
        status = build_status(self.run_path, self.state, self.run_start)
        try:
            res = resource_sample(self.run_path)
            status["resources"] = {k: res[k] for k in ("mem_used_pct", "mem_avail_gb", "cpu_pct", "disk")}
            with open(os.path.join(self.run_path, RESOURCES_FILE), "a") as f:
                f.write(json.dumps(res) + "\n")
        except Exception as e:  # noqa: BLE001
            status["resources_error"] = f"{type(e).__name__}: {e}"[:200]
        tmp = os.path.join(self.run_path, STATUS_FILE + ".tmp")
        with open(tmp, "w") as f:
            json.dump(status, f, indent=1, default=str)
        os.replace(tmp, os.path.join(self.run_path, STATUS_FILE))
        if self.uploader:
            self.uploader(os.path.join(self.run_path, STATUS_FILE), "status.json")
            if os.path.exists(os.path.join(self.run_path, RESOURCES_FILE)):
                self.uploader(os.path.join(self.run_path, RESOURCES_FILE), RESOURCES_FILE)   # full history
            if time.time() - self.last_checkpoint >= self.checkpoint_s:
                self.checkpoint()
        return status

    def checkpoint(self) -> None:
        if not self.uploader:
            return
        import results_store
        name = os.path.basename(os.path.normpath(self.run_path))
        secrets = [os.environ.get(k, "") for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")]
        data, _ = results_store.archive_dir(self.run_path, name, secrets)
        tmp = os.path.join(os.path.dirname(os.path.normpath(self.run_path)), f".{name}.checkpoint.tar.gz")
        with open(tmp, "wb") as f:
            f.write(data)
        self.uploader(tmp, "checkpoint.tar.gz")
        os.remove(tmp)
        # The CLI transcripts (every LLM message and tool call) live outside the
        # run dir; checkpoint them too so a lost VM does not lose them (Codex).
        tdir = os.environ.get("MA_TRANSCRIPTS_DIR") or os.path.expanduser("~/.claude/projects")
        if os.path.isdir(tdir):
            tdata, _ = results_store.archive_dir(tdir, "transcripts", secrets)
            ttmp = tmp.replace(".checkpoint.", ".transcripts.")
            with open(ttmp, "wb") as f:
                f.write(tdata)
            self.uploader(ttmp, "transcripts.tar.gz")
            os.remove(ttmp)
        self.last_checkpoint = time.time()

    def snapshot(self, agent: str) -> None:
        """Archive the run as it is when `agent`'s stage starts; upload as
        snapshots/NNN_<stage>_<agent>.tar.gz (a replay fixture)."""
        if not self.uploader:
            return
        import results_store
        self.snapshots += 1
        name = os.path.basename(os.path.normpath(self.run_path))
        secrets = [os.environ.get(k, "") for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")]
        data, _ = results_store.archive_dir(self.run_path, name, secrets)
        tmp = os.path.join(os.path.dirname(os.path.normpath(self.run_path)), f".{name}.snapshot.tar.gz")
        with open(tmp, "wb") as f:
            f.write(data)
        self.uploader(tmp, f"snapshots/{self.snapshots:03d}_{STAGE_OF.get(agent, 'X')}_{agent}.tar.gz")
        os.remove(tmp)

    def _drain_snapshots(self) -> None:
        while True:
            with self.state.lock:
                if not self.state.snapshot_requests:
                    self.state.wake.clear()
                    return
                agent = self.state.snapshot_requests.pop(0)
            try:
                self.snapshot(agent)
            except Exception:  # noqa: BLE001
                self.errors += 1

    def run(self) -> None:
        next_beat = time.time() + self.interval_s
        while not self.stop_event.is_set():
            # wake early for a stage snapshot, otherwise every interval
            self.state.wake.wait(max(0.0, next_beat - time.time()))
            if self.stop_event.is_set():
                break
            self._drain_snapshots()
            if time.time() >= next_beat:
                next_beat = time.time() + self.interval_s
                try:
                    self.beat()
                except Exception:  # noqa: BLE001 — visibility must never break the run
                    self.errors += 1

    def stop(self, timeout_s: float = 180) -> None:
        """Stop the thread, then drain queued stage snapshots and write a last
        beat + checkpoint, all within `timeout_s` so storage can never block
        finalize (Codex, Phase 29)."""
        self.stop_event.set()
        self.state.wake.set()
        self.join(5)

        def last():
            try:
                self._drain_snapshots()
                self.beat()
                self.checkpoint()
            except Exception:  # noqa: BLE001
                self.errors += 1
        t = threading.Thread(target=last, daemon=True, name="ma-heartbeat-final")
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            self.errors += 1          # left running in the background; finalize proceeds


def storage_uploader(run_path: str):
    """Uploads to outputs/<MA_STORAGE_PREFIX>/live/<run name>/<suffix>, or None
    when no storage account is configured."""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
    import results_store
    account = results_store.configured_account()
    if not account:
        return None
    name = os.path.basename(os.path.normpath(run_path))
    prefix = os.environ.get("MA_STORAGE_PREFIX", "runs")

    def up(local, suffix):
        results_store.upload_retry(local, f"outputs/{prefix}/live/{name}/{suffix}", account, attempts=2)
    return up


def self_test() -> list[str]:
    import tempfile
    from claude_agent_sdk import AssistantMessage, ToolUseBlock, ToolResultBlock, UserMessage
    fails = []
    saved_tdir = os.environ.get("MA_TRANSCRIPTS_DIR")
    tdir = tempfile.mkdtemp()                       # never archive the operator's own transcripts
    os.environ["MA_TRANSCRIPTS_DIR"] = tdir
    try:
        return _self_test_body(fails)
    finally:
        if saved_tdir is None:
            os.environ.pop("MA_TRANSCRIPTS_DIR", None)
        else:
            os.environ["MA_TRANSCRIPTS_DIR"] = saved_tdir


def _self_test_body(fails: list[str]) -> list[str]:
    import tempfile
    from claude_agent_sdk import AssistantMessage, ToolUseBlock, ToolResultBlock, UserMessage
    with tempfile.TemporaryDirectory() as d:
        st = LiveState()
        st.on_message(AssistantMessage(content=[ToolUseBlock("a1", "Agent", {"subagent_type": "modeler"})],
                                       model="claude-opus-5-5", usage={"cache_read_input_tokens": 1_000_000},
                                       message_id="m1"))
        st.on_message(AssistantMessage(content=[], model="claude-opus-5-5",
                                       usage={"cache_read_input_tokens": 1_000_000}, message_id="m1"))  # repeat
        s = build_status(d, st, datetime.now())
        if s["stage"] != "MODEL" or s["active_agents"] != ["modeler"]:
            fails.append(f"OB1 the active agent and stage come from the stream: {s}")
        if abs(s["est_spend_this_process_usd"] - round(1_000_000 * 0.1 * 4.131e-6, 2)) > 0.01:
            fails.append(f"OB2 live cost counts each message once: {s}")
        st.on_message(UserMessage(content=[ToolResultBlock("a1", "done")]))
        if build_status(d, st, datetime.now())["active_agents"] != []:
            fails.append("OB3 a finished subagent is no longer active")
        with open(os.path.join(d, "validator_history.jsonl"), "w") as f:
            f.write(json.dumps({"round": 2, "action": "PATCH_OR_RETHINK",
                                "findings": [{"severity": "HIGH"}, {"severity": "MEDIUM"}]}) + "\n")
        g = build_status(d, st, datetime.now())["gate"]
        if g != {"round": 2, "action": "PATCH_OR_RETHINK", "high": 1, "medium": 1, "at": None}:
            fails.append(f"OB4 the last gate decision is reported: {g}")
        uploaded = []
        hb = Heartbeat(d, st, datetime.now(), interval_s=0.05, checkpoint_s=0,
                       uploader=lambda p, sfx: uploaded.append((sfx, os.path.exists(p))))
        sys_path_ok = True
        try:
            import sys as _s
            _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
            hb.beat()
        except Exception as e:  # noqa: BLE001
            sys_path_ok = False
            fails.append(f"OB5 a beat writes status/resources and uploads: {e}")
        if sys_path_ok:
            if not os.path.exists(os.path.join(d, STATUS_FILE)) or not os.path.exists(os.path.join(d, RESOURCES_FILE)):
                fails.append("OB5 status.json and resources.jsonl are written")
            if ("status.json", True) not in uploaded or ("checkpoint.tar.gz", True) not in uploaded \
                    or ("transcripts.tar.gz", True) not in uploaded or ("resources.jsonl", True) not in uploaded:
                fails.append(f"OB6 status and checkpoint are uploaded: {uploaded}")
            r = json.loads(open(os.path.join(d, RESOURCES_FILE)).readline())
            if not (0 <= r["mem_used_pct"] <= 100 and r["top"]):
                fails.append(f"OB7 a resource sample has memory and the largest processes: {r}")

        st2 = LiveState()
        up2 = []
        hb3 = Heartbeat(d, st2, datetime.now(), interval_s=30, uploader=lambda p, sfx: up2.append(sfx))
        hb3.start()
        st2.on_message(AssistantMessage(content=[ToolUseBlock("a9", "Agent", {"subagent_type": "modeler"})],
                                        model="claude-opus-5-5", message_id="m9"))
        st2.on_message(AssistantMessage(content=[ToolUseBlock("a10", "Agent", {"subagent_type": "writer"})],
                                        model="claude-opus-5-5", message_id="m10", parent_tool_use_id="x"))
        t_end = time.time() + 5
        while time.time() < t_end and not any("snapshots/" in u for u in up2):
            time.sleep(0.05)
        hb3.stop_event.set(); st2.wake.set(); hb3.join(5)
        snaps = [u for u in up2 if u.startswith("snapshots/")]
        if snaps != ["snapshots/001_MODEL_modeler.tar.gz"]:
            fails.append(f"OB9 a stage launched by the lead is snapshotted at once (not nested spawns): {up2}")

        def boom(*_):
            raise OSError("storage down")
        hb2 = Heartbeat(d, st, datetime.now(), interval_s=0.01, uploader=boom)
        hb2.start()
        time.sleep(0.1)
        hb2.stop_event.set()
        hb2.join(2)
        if hb2.is_alive() or hb2.errors == 0:
            fails.append("OB8 a failing upload is counted, never raised into the run")
    return fails
