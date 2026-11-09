"""Launch and manage a pipeline run on IDM Coiled from one config file.

    python cloud/run_cluster.py <config.py> launch            # cluster, run(s), keeper; then safe to disconnect
    python cloud/run_cluster.py <config.py> status            # running / done per run, cluster and keeper state
    python cloud/run_cluster.py <config.py> collect           # fetch finished runs (from storage once stopped)
    python cloud/run_cluster.py <config.py> brief|report      # decision brief / convergence measures of a live run
    python cloud/run_cluster.py <config.py> health [--loop] [--interval S] [--log PATH] [--thresholds YAML]
                                                              # anomalies of a live run (cloud/run_health.py)
    python cloud/run_cluster.py <config.py> sign <answers.yaml> [--label L] [--run RUN] [--action spec|scope]
                         [--expect-spec-sha256 SHA] [--expect-derivations-sha256 SHA|none] [--expect-sheet-sha256 SHA] [note]
                                                              # pending scope declarations, a v2 spec, or the brief
    python cloud/run_cluster.py <config.py> relaunch <label>
    python cloud/run_cluster.py <config.py> keeper            # reconcile, then start missing helpers for the run
                                                              # cluster clusters.json records (after exit 3 or 5)
    python cloud/run_cluster.py <config.py> ack-probe         # a fast task through the acknowledgement (smoke)
    python cloud/run_cluster.py <config.py> smoke-run [--minutes 20] [--interrupt-on TEXT]
                                                              # a smoke: `launch` as a child under a wall-clock bound;
                                                              # waits for it and every cluster it tagged; at the limit
                                                              # terminates and reaps it, then stops them by id
    python cloud/run_cluster.py <config.py> reuse-check <run_dir> [--expect-all]
                                                              # how many stored engine rows the code this launch
                                                              # would ship reuses (local, no cloud call)
    python cloud/run_cluster.py <config.py> shutdown          # cluster, overflow pool and keeper, by hand
    python cloud/run_cluster.py <config.py> report-cost [--ledger PATH]... [--start ISO] [--end ISO]
                                                              # Coiled credits per useful core-hour (billing API only)
    python cloud/run_cluster.py --self-test

Detached jobs on a single VM (the dev VM): cloud/vm_job.py.

A config is a Python file defining CONFIG = RunConfig(...) (see cloud/configs/).

Lifecycle. Each run is pinned to its own agent VM and submitted with
fire_and_forget, so it keeps running when this process exits. A finished run
uploads its directory (main.py reads every upload back), its transcripts and
its result record to storage; the last run of the launch then shuts the
cluster down, and the keeper exits when the cluster stops and shuts its own VM down.
If an upload fails, the agent VM holds the results on disk and stays busy
(up to 24 h) until `collect` fetches them, after which `collect` shuts the
cluster down. Nothing waits on a laptop: E7 attempt 7 sat idle 9.5 h after
its results were stored because the launcher waited for a manual collect.

Exit codes of `launch` (and `relaunch`), for a watchdog. Once a run is submitted the launch is never
reported as failed, because the run keeps going whatever happens to this process (F5 resume launch,
2026-11-08: the scheduler listed the run, the laptop's connection then dropped, and a watchdog that read the
error as a failed launch shut the clusters down). `clusters.json` names every cluster created, with the launch
id, the launch time, a `status` and this invocation's token, from the moment the run cluster exists. The token
is the first line printed (`invocation <token>`), or MA_LAUNCH_INVOCATION when the watchdog sets it. One launch,
`keeper` or `relaunch` of a config runs at a time (a lock in its out_dir); a refusal (already running, preflight,
lock held) writes no record, so an earlier launch's record stays as it was. Every cluster is created with the
Coiled tag ma-invocation=<token> (for people: Coiled does not list tags) and the config's cluster_timeout. A
cluster is this invocation's only when no live one of its name was listed just before the constructor, Coiled
logged that it created it (not "Attaching to existing cluster"), and its created time is no earlier than the
construction's start less 120 s; otherwise it is refused before any record (out_dir/constructions.jsonl keeps
each construction's start and outcome).
  0 launched: every run listed by the scheduler, overflow pool and keeper confirmed (status "launched").
  1 failed: this invocation submitted nothing. Stopping clusters is authorized only by a clusters.json whose
    `invocation` is this invocation's token and whose status is "failed"; then stop the ids it names. Any other
    record (none, another token, another status) belongs to an earlier launch: stop nothing.
  3 submitted, connection lost: the scheduler listed every run, then this process lost its connection
    (status "submitted_connection_lost"). Do not shut down; watch with `status` / `health --loop`, and start
    a missing keeper with `keeper`.
  4 submitted, not confirmed: a run was sent, but the scheduler's acceptance of it was not seen (its future
    finished or erred, the scheduler lists it as processing, or holds it queued or waiting); connection lost
    first, not seen in time, or interrupted (status "submitted_unconfirmed"). It may be running: do not shut
    down; check `status`. `relaunch` exits 0 or 4 once it has submitted.
  5 submitted, launch incomplete: every run listed, but a later step failed, the record write included (overflow
    pool or keeper; status "submitted_launch_incomplete"). Do not shut down; fix and run `keeper`.
Any other code (a usage error, an exception, an interrupt) comes from before any submission: a failure, under
the same rule as 1.

Rules: the IDM Coiled default workspace only (never the prod subscription);
attach-only for everything but `launch` (coiled.Cluster(name=...) on a
cluster that is gone creates a new, empty one); API keys go in as task
arguments and are never printed or written; answer keys (`_truth/`) are
never stored.
"""
from __future__ import annotations

import contextlib
import dataclasses
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import worker_side as ws  # noqa: E402 — uploaded to the VMs under this name
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "lib"))
import eval_paths  # noqa: E402 — Phase 50 α: what a launch tarball must never hold
import pipeline_id  # noqa: E402 — which pipeline main.py runs; the identity each run records
sys.modules.setdefault("run_cluster", sys.modules[__name__])   # configs import RunConfig from here

WORKSPACE = ws.WORKSPACE
ENV_FILE = os.path.expanduser("~/modeling-agentic/.env")
CHUNK = 8 * 2**20
_ATTACH = {"ready", "running", "pending", "starting", "scaling"}


@dataclasses.dataclass
class RunConfig:
    name: str                                  # cluster name; the keeper is "<name>-keeper"
    question_file: str
    tree: str = os.path.expanduser("~/modeling-agentic-launch")
    labels: tuple = ("R1",)
    flags: tuple = ()                          # extra main.py flags (never --v1/--v2: set `pipeline`)
    pipeline: str = ""                         # "v1" or "v2", required to launch; passed to main.py explicitly
    max_rounds: int = 8
    max_sessions: int = 5
    cross_model: bool = False                  # pass the OpenAI key for the cross-model critic
    storage_account: str = "modelingagentic"
    storage_prefix: str = ""                   # outputs/<prefix>/...; default runs/<name>
    out_dir: str = ""                          # local collect dir; default ~/modeling-agentic-keep/<name>
    compute_start: int = 1                     # compute VMs at launch, and the pool's floor (see below)
    compute_max: int = 24
    compute_env: dict = dataclasses.field(default_factory=dict)
    keeper: bool = True                        # hold the pool's autoscaler on a small VM
    keeper_hours: float = 48.0
    idle_timeout: str = "1 hour"               # backstop only: a running or holding task keeps it busy
    require_main: bool = True                  # ship exactly origin/main (False for smoke tests of a branch)
    required_files: tuple = ("scripts/results_store.py", "scripts/compute.py", "scripts/decision_brief.py")
    # Phase 42 β (A5): VM reuse. Attempt 8 created 734 compute VMs in 18 h (each lived
    # minutes: up per batch, down 2 min after it) and billed ~6.5 credits per useful
    # core-hour. An idle compute VM now stays `scale_down_after` before the pool retires
    # it, so the next batch reuses it: 19 of attempt 8's 22 gaps between batches were
    # under 10 minutes. The floor stays at one compute VM (`compute_start`): the typical
    # small batch (a 1,100-draw UQ, 2.25 core-hours) ran 24-36 min on one VM, and each
    # further floor VM costs ~144 core-hours over an 18 h run whether used or not.
    scale_down_after: str = "10 minutes"
    adapt_interval: str = "15s"                # how often the autoscaler re-plans
    target_duration: str = ""                  # "" keeps compute.ADAPT_OPTIONS' (how hard a batch bursts)
    # Phase 42 β (A6): quota fallback. Coiled cannot change a live cluster's VM types and
    # does not fall back on a quota refusal; while the primary family is refused, the
    # keeper grows "<name>-overflow" (these VM types, separate quota families: 8 vCPU,
    # 32 GiB, enough for the attempt-8 batches' ~0.7 GB per process) whose VMs join the
    # run cluster's scheduler. overflow_max=0 disables it; None means compute_max.
    overflow_vm_types: tuple = ("Standard_D8s_v5", "Standard_D8as_v5")
    overflow_max: int | None = None
    overflow_hold: str = "30 minutes"          # keep using overflow this long after the last refusal
    # S1-8: cloud-side lifetimes (a smoke sets them short): main.py is killed past run_timeout_hours (None: no
    # limit), and a run whose upload failed is held for `collect` at most hold_hours
    run_timeout_hours: float | None = None
    hold_hours: float = 24.0
    # Coiled's cluster_timeout (coiled.Cluster(cluster_timeout=...), "40 minutes"): a cluster is shut down that long
    # after it starts, busy or not. "" for none (a real run's length is not known in advance)
    cluster_timeout: str = ""

    def __post_init__(self):
        self.storage_prefix = self.storage_prefix or f"runs/{self.name}"
        self.out_dir = self.out_dir or os.path.expanduser(f"~/modeling-agentic-keep/{self.name}")
        self.labels, self.flags, self.required_files = tuple(self.labels), tuple(self.flags), tuple(self.required_files)
        self.overflow_vm_types = tuple(self.overflow_vm_types)
        if self.overflow_max is None:
            self.overflow_max = self.compute_max
        bad = [k for k in ("--max-rounds", "--max-sessions") if k in self.flags]
        if bad:
            raise ValueError(f"set {bad} through max_rounds / max_sessions, not flags")
        if not self.labels or len(set(self.labels)) != len(self.labels):
            raise ValueError("labels must be non-empty and distinct")
        if self.pipeline and self.pipeline not in pipeline_id.PIPELINES:
            raise ValueError(f"pipeline must be one of {pipeline_id.PIPELINES}, not {self.pipeline!r}")
        if {"--v1", "--v2"} & set(self.flags):
            raise ValueError("set the pipeline with pipeline='v1' or 'v2', not flags: the launcher passes it to main.py")
        for f in ("scale_down_after", "adapt_interval", "overflow_hold") + (("target_duration",) if self.target_duration else ()):
            seconds(getattr(self, f))   # fail at load, not on the keeper VM
        if seconds(self.adapt_interval) <= 0:
            raise ValueError("adapt_interval must be positive")
        if self.overflow_max and not self.overflow_vm_types:
            raise ValueError("overflow_max needs overflow_vm_types")
        if self.compute_start < 1 and (self.compute_env or {}).get("MA_COMPUTE_RESOURCE"):
            raise ValueError("compute_start must be at least 1 when compute tasks need a resource tag "
                             "(MA_COMPUTE_RESOURCE): with no tagged worker the autoscaler's target stays at the "
                             "current pool, so the pool never grows")

    @property
    def state_file(self):
        return os.path.join(self.out_dir, "assignments.json")

    @property
    def keeper_name(self):
        return f"{self.name}-keeper"

    @property
    def overflow_name(self):
        return f"{self.name}-overflow"

    @property
    def clusters_file(self):
        return os.path.join(self.out_dir, "clusters.json")


_UNITS = {"s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1, "m": 60, "min": 60, "mins": 60, "minute": 60,
          "minutes": 60, "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600}


def seconds(d) -> float:
    """'15s', '10 minutes', '1 hour', 90 -> seconds (the forms dask and Coiled accept)."""
    import re
    if isinstance(d, (int, float)):
        return float(d)
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*", str(d))
    if not m or (m.group(2) and m.group(2).lower() not in _UNITS):
        raise ValueError(f"not a duration: {d!r}")
    return float(m.group(1)) * _UNITS[m.group(2).lower() or "s"]


def adapt_options(cfg, base: dict) -> dict:
    """The keeper's adapt() options: the tree's ADAPT_OPTIONS, with the config's re-plan
    interval and scale-down delay (dask retires a worker after `wait_count` consecutive
    re-plans that suggest it, so the delay is interval x wait_count)."""
    import math
    iv = seconds(cfg.adapt_interval)
    out = {**base, "interval": f"{iv:g}s", "wait_count": max(1, math.ceil(seconds(cfg.scale_down_after) / iv))}
    if cfg.target_duration:
        out["target_duration"] = cfg.target_duration
    return out


def load_config(path: str) -> RunConfig:
    spec = importlib.util.spec_from_file_location("run_config", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_config"] = mod
    spec.loader.exec_module(mod)
    cfg = getattr(mod, "CONFIG", None)
    if not isinstance(cfg, RunConfig):
        raise SystemExit(f"{path} defines no CONFIG = RunConfig(...)")
    return cfg


# ---- cluster state (one implementation of the attach guard) ------------------------------

def cluster_states(name: str, lister=None) -> list[str]:
    """States of every cluster with this name (Coiled keeps stopped ones listed)."""
    if lister is None:
        import coiled
        lister = lambda: coiled.list_clusters(workspace=WORKSPACE)  # noqa: E731
    cs = lister()
    items = cs if isinstance(cs, list) else list(cs.values())
    return [ws.state_of(c) for c in items if c.get("name") == name]


def is_running(name: str, lister=None) -> bool:
    return any(s in _ATTACH for s in cluster_states(name, lister))


def live_id(name: str, lister=None):
    """Id of the live cluster with this name, or None."""
    if lister is None:
        import coiled
        lister = lambda: coiled.list_clusters(workspace=WORKSPACE)  # noqa: E731
    cs = lister()
    items = cs if isinstance(cs, list) else list(cs.values())
    ids = [c.get("id") for c in items if c.get("name") == name and ws.state_of(c) in _ATTACH]
    return max(ids) if ids else None


def attach(name: str):
    """Attach to a live cluster; refuse rather than create one. The check and
    the attach are not atomic, so the cluster we get must be the one we checked
    (otherwise it is a new empty one: shut down and refused)."""
    cid = live_id(name)
    if cid is None:
        raise SystemExit(f"cluster {name!r} is not running; refusing to create a new one")
    return ws.attach_existing(name, WORKSPACE, cid)


def _compute(cfg):
    sys.path.insert(0, os.path.join(cfg.tree, "scripts"))
    import compute
    return compute


def _on_worker(client, fn, *args, worker):
    """client.run, not submit: the agent VM's only thread is busy with the run."""
    return client.run(fn, *args, workers=[worker], wait=True, on_error="raise")[worker]


def read_keys(path=ENV_FILE) -> dict:
    """ANTHROPIC_API_KEY / OPENAI_API_KEY only; values are never printed."""
    keys = {}
    with open(path) as f:
        for line in f:
            k, _, v = line.strip().partition("=")
            if k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
                keys[k] = v.strip().strip('"').strip("'")
    return keys


def code_tarball(cfg) -> tuple[bytes, str]:
    """git archive of HEAD, after the guards: clean tree, origin/main unless
    the config allows a branch, and the files this launch depends on."""
    git = lambda *a: subprocess.check_output(["git", "-C", cfg.tree, *a], text=True).strip()  # noqa: E731
    subprocess.run(["git", "-C", cfg.tree, "fetch", "-q", "origin"], check=True)
    head, main_ = git("rev-parse", "HEAD"), git("rev-parse", "origin/main")
    if git("status", "--porcelain"):
        raise SystemExit(f"{cfg.tree} has uncommitted changes: refusing")
    if cfg.require_main and head != main_:
        raise SystemExit(f"{cfg.tree} is at {head[:7]}, origin/main is {main_[:7]}: refusing")
    names = set(git("ls-files").splitlines())
    missing = [n for n in cfg.required_files if n not in names]
    if missing:
        raise SystemExit(f"code to launch lacks {missing}: refusing")
    refuse_hidden_tree(cfg.tree)
    blob = subprocess.check_output(["git", "-C", cfg.tree, "archive", "--format=tar.gz", "HEAD"])
    refuse_hidden(blob, cfg.tree)
    return blob, archive_commit(blob) or head[:7]   # the commit that ships (stamped by git archive)


def _archive_member(code: bytes, name: str) -> bytes | None:
    try:
        with tarfile.open(fileobj=io.BytesIO(code), mode="r:gz") as t:
            m = next((m for m in t.getmembers() if m.isfile() and m.name.removeprefix("./") == name), None)
            return t.extractfile(m).read() if m else None
    except (tarfile.TarError, OSError):
        return None


def archive_commit(code: bytes) -> str | None:
    """The 7-character commit git archive stamped into scripts/lib/code_commit.txt, or None."""
    raw = (_archive_member(code, "scripts/lib/" + pipeline_id.COMMIT_FILE) or b"").decode("ascii", "replace").strip()
    return raw[:7] if len(raw) >= 7 and all(c in "0123456789abcdef" for c in raw) else None


def refuse_hidden_tree(tree: str, tracked: set[str] | None = None) -> None:
    """Coiled package sync uploads the launch tree as a wheel, untracked and ignored
    files included, so export-ignore does not keep eval material off the workers: a
    tree holding any on disk (eval_paths.tree_hidden_material) is refused. Move the
    material out of the tree (or launch from a clean clone) and launch again."""
    found = eval_paths.tree_hidden_material(tree, tracked)
    if found:
        raise SystemExit(f"{tree} holds hidden eval-suite material that package sync would upload "
                         f"({len(found)}: {', '.join(found[:5])}{', ...' if len(found) > 5 else ''}); "
                         "move it out of the launch tree: refusing")


def refuse_hidden(blob: bytes, tree: str) -> None:
    """Phase 50 α: eval answer keys, generators and rubrics never reach a worker.
    .gitattributes export-ignores them; a tree whose attributes are missing or wrong
    (an old branch, a deleted line) is refused here instead of shipped."""
    hidden = eval_paths.hidden_members(blob)
    if hidden:
        raise SystemExit(f"{tree}: the code tarball would ship eval-suite files agents must not see "
                         f"({len(hidden)}, e.g. {hidden[:3]}); check .gitattributes: refusing")


def main_flags(cfg) -> list:
    """main.py's arguments from the config: the declared pipeline, stated explicitly (never the
    default: it changed once, 2026-11-05), the launcher's name for metadata.json, then the flags."""
    return [f"--{cfg.pipeline}", "--launcher", f"run_cluster:{cfg.name}", *cfg.flags]


def pipeline_preflight(cfg, code: bytes, python: str = sys.executable) -> str:
    """The pipeline the shipped main.py runs with this config's arguments, asked of the shipped
    code itself (its scripts/lib/pipeline_id.py, standard library only, run isolated). Refuses a
    config that declares no pipeline, a tree that cannot state one (from before 2026-11-05, when
    main.py defaulted to v1 and nothing recorded it), and any disagreement with the declaration."""
    import tempfile
    if cfg.pipeline not in pipeline_id.PIPELINES:
        raise SystemExit(f"{cfg.name}: the config declares no pipeline (set pipeline='v1' or 'v2'): refusing")
    want = "scripts/lib/pipeline_id.py"
    src = _archive_member(code, want)
    if src is None:
        raise SystemExit(f"{cfg.name}: the code to launch has no {want}, so it cannot state which pipeline main.py "
                         "runs (a tree from before 2026-11-05 runs v1 by default): refusing")
    main_src = (_archive_member(code, "main.py") or b"").decode("utf-8", "replace")
    if not all(k in main_src for k in ("pipeline_id.select(", '"--v1"', '"--v2"', '"--launcher"')):
        raise SystemExit(f"{cfg.name}: the shipped main.py does not take --v1/--v2/--launcher or does not choose its "
                         "pipeline through pipeline_id.select (a mixed or older tree): refusing")
    with tempfile.TemporaryDirectory(prefix="pipeline-preflight-") as td:
        f = os.path.join(td, "pipeline_id.py")
        open(f, "wb").write(src)
        r = subprocess.run([python, "-I", f, "which", "--", *main_flags(cfg)], capture_output=True, text=True,
                           timeout=60, cwd=td)
    got = r.stdout.strip()
    if r.returncode or got != cfg.pipeline:
        raise SystemExit(f"{cfg.name}: the config declares pipeline {cfg.pipeline}, but the shipped main.py would run "
                         f"{got or 'nothing'} ({(r.stderr or '').strip()[:200]}): refusing")
    return got


def launch_record(cfg, worker, commit, launch_id, cluster_id, code, pipeline) -> dict:
    """One label's entry in assignments.json (the grader reads it: scripts/eval_suite.py)."""
    return {"worker": worker, "commit": commit, "submitted": time.strftime("%F %T"), "launch_id": launch_id,
            "cluster_id": cluster_id, "pipeline": pipeline, "launcher": f"run_cluster:{cfg.name}",
            "eval_inputs_sha256": eval_paths.archive_sha256(code)}   # Phase 50 α: the inputs shipped


# ---- commands ---------------------------------------------------------------------------

INVOCATION_TAG = "ma-invocation"     # Coiled cluster tag: the invocation that created the cluster (round 4, P1)


def _lifetime(cfg, inv) -> dict:
    """Constructor arguments every cluster of a launch gets: its invocation tag, and Coiled's cluster_timeout."""
    return {"tags": {INVOCATION_TAG: inv}, **({"cluster_timeout": cfg.cluster_timeout} if cfg.cluster_timeout else {})}


OWN_SKEW_S = 120.0                    # clock skew allowed between this machine and Coiled's `created` time
JOURNAL = "constructions.jsonl"       # in the config's out_dir: every cluster construction this machine began
_CREATED, _ATTACHED = "Creating Cluster (name: {}, ", "Attaching to existing cluster (name: {}, "


def _items(cs) -> list:
    return cs if isinstance(cs, list) else list((cs or {}).values())


def _ts(v):
    """An ISO time from the Coiled API as epoch seconds (UTC when it has no zone), or None."""
    import datetime as dt
    try:
        t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)).timestamp()


def read_journal(cfg) -> list:
    try:
        with open(os.path.join(cfg.out_dir, JOURNAL)) as f:
            return [json.loads(ln) for ln in f if ln.strip()]
    except (OSError, ValueError):
        return []


def _journal(cfg, entry: dict) -> None:
    os.makedirs(cfg.out_dir, exist_ok=True)
    with open(os.path.join(cfg.out_dir, JOURNAL), "a") as f:
        f.write(json.dumps(entry) + "\n")


@contextlib.contextmanager
def _coiled_events():
    """The messages coiled.Cluster logs to the "coiled" logger while it runs. coiled 1.135.6 keeps "created or
    attached" in a local (v2/cluster.py:1672 `cluster_created = not cluster_existed`) and reports it only by
    logging "Creating Cluster (name: ...)" (v2/cluster.py:1685) or "Attaching to existing cluster (name: ...)"
    (v2/cluster.py:1688); no attribute or return value carries it."""
    import logging

    class H(logging.Handler):
        def emit(self, record):
            try:
                got.append(record.getMessage())
            except Exception:  # noqa: BLE001
                pass
    got, lg, h = [], logging.getLogger("coiled"), H(level=logging.INFO)
    old = lg.level
    if old == logging.NOTSET or old > logging.INFO:
        lg.setLevel(logging.INFO)
    lg.addHandler(h)
    try:
        yield got
    finally:
        lg.removeHandler(h)
        lg.setLevel(old)


def _construct(cfg, name, inv, ctor, lister=None):
    """Build a cluster that is this invocation's, or refuse before its id is recorded anywhere (Codex round 4, P1;
    the owner's check, 2026-11-09: coiled 1.135.6 lists no tags, so a tag cannot be read back). coiled.Cluster(name=...)
    attaches to a live cluster of that name instead of creating one (another launcher, on another machine, may have
    created it). Ours only if all three hold, any doubt meaning not ours: no live cluster of the name is listed just
    before the constructor; Coiled logged that it created it (_coiled_events); and its `created` time is no earlier
    than this construction's start less OWN_SKEW_S. The journal records the start and the outcome, so `smoke-run`
    owns what this invocation created, and a construction that never finished."""
    import secrets
    if lister is None:
        import coiled
        lister = lambda: coiled.list_clusters(workspace=WORKSPACE)  # noqa: E731
    live = sorted(c.get("id") for c in _items(lister())
                  if c.get("name") == name and ws.state_of(c) not in ("stopped", "error"))
    if live:
        raise SystemExit(f"{name}: a cluster of this name is live or stopping ({live}) just before the constructor; "
                         "not creating it; refusing, with nothing recorded")
    seq, t0 = secrets.token_hex(4), time.time()
    _journal(cfg, {"inv": inv, "name": name, "t": t0, "status": "constructing", "seq": seq})
    with _coiled_events() as ev:
        cl = ctor()
    cid = getattr(cl, "cluster_id", None)
    attached = any(m.startswith(_ATTACHED.format(name)) or m.startswith(f"Using existing cluster: '{name} ") for m in ev)
    created = any(m.startswith(_CREATED.format(name)) for m in ev)
    rec = next((c for c in _items(lister()) if c.get("id") == cid), None) if cid is not None else None
    ts = _ts((rec or {}).get("created"))
    why = ("Coiled attached to an existing cluster" if attached else "Coiled logged no creation" if not created
           else "the listing shows no created time" if ts is None
           else f"created {t0 - ts:.0f} s before this construction began" if ts < t0 - OWN_SKEW_S else None)
    _journal(cfg, {"inv": inv, "name": name, "id": cid, "seq": seq, "why": why,
                   "status": "created" if why is None else "attached" if attached else "unknown"})
    if why:
        raise SystemExit(f"{name} ({cid}): not created by this invocation ({why}); refusing, with nothing recorded "
                         "and nothing to clean up")
    return cl


def owned_clusters(cfg, inv, items, journal) -> dict:
    """{id: name} of the listed live clusters of this config's names that are this invocation's: the ids its
    journal says it created, and, for a construction it began and never finished (interrupted), a cluster of that
    name created after it began (less OWN_SKEW_S). An id it attached to is never ours."""
    names = {cfg.name, cfg.keeper_name, cfg.overflow_name}
    mine = [e for e in journal if e.get("inv") == inv]
    done = {e.get("seq"): e for e in mine if e.get("status") in ("created", "attached", "unknown")}
    ids = {e.get("id") for e in done.values() if e["status"] == "created"}
    never = {e.get("id") for e in done.values() if e["status"] != "created"}
    pending = [e for e in mine if e.get("status") == "constructing" and e.get("seq") not in done]
    out = {}
    for c in items:
        cid, name = c.get("id"), c.get("name")
        if name not in names or ws.state_of(c) not in _ATTACH or cid in never:
            continue
        ts = _ts(c.get("created"))
        if cid in ids or (ts is not None and any(p.get("name") == name and ts >= float(p.get("t", 0)) - OWN_SKEW_S
                                                  for p in pending)):
            out[cid] = name
    return out


def _task_key(label) -> str:
    return f"run-{label}-{int(time.time())}"


def _submit(cfg, client, label, worker, code, question, keys, launch_id, key=None):
    """Fire one run on its agent VM; returns (task key, future). The future is held through the acknowledgement: a
    run that finishes between two polls is seen through it (Codex P2-7 on #161)."""
    from distributed import fire_and_forget
    key = key or _task_key(label)       # callers record it in assignments.json first (Codex round 2, P2-2)
    fut = client.submit(ws.run_to_disk, label, code, question, main_flags(cfg), keys["ANTHROPIC_API_KEY"],
                        keys.get("OPENAI_API_KEY") if cfg.cross_model else None, list(cfg.labels),
                        cfg.storage_prefix, cfg.storage_account, launch_id, cfg.max_rounds, cfg.max_sessions,
                        workers=[worker], allow_other_workers=False, pure=False, key=key,
                        run_timeout_s=cfg.run_timeout_hours * 3600 if cfg.run_timeout_hours else None,
                        hold_s=cfg.hold_hours * 3600)
    fire_and_forget(fut)
    return key, fut


EXIT_LAUNCHED, EXIT_FAILED, EXIT_SUBMITTED_LOST, EXIT_UNCONFIRMED, EXIT_INCOMPLETE = 0, 1, 3, 4, 5   # module doc
ACK_ATTEMPTS, ACK_DELAY_S = 36, 5.0      # about 3 minutes for the scheduler to list a run (F5 resume: 43 s)
# a lost connection to the scheduler: distributed's CommClosedError and connect timeouts are OSErrors
CONNECTION_ERRORS = (OSError, TimeoutError, ws.SchedulerInfoUnavailable)
# scheduler task states that mean it has the run (released/forgotten do not)
ACK_STATES = ("waiting", "no-worker", "queued", "processing", "memory", "erred")


# scheduler task states of a run that is still to run or running (relaunch refuses a second copy)
LIVE_STATES = ("waiting", "no-worker", "queued", "processing")


def _scheduler_state(client, key):
    """The scheduler's state of task `key` (worker_side.task_state, sent by value: the scheduler has no
    worker_side module), or None when it does not hold it. Errors are raised."""
    try:
        import cloudpickle
        cloudpickle.register_pickle_by_value(ws)
    except ImportError:
        pass
    return client.run_on_scheduler(ws.task_state, key)


def _acknowledged(client, key, fut) -> str | None:
    """What shows the scheduler has the run, or None."""
    st = getattr(fut, "status", None)
    if st in ("finished", "error"):          # it ran: a fast run leaves no listing behind
        return f"future {'finished' if st == 'finished' else 'erred'}"
    if any(key in (ks or ()) for ks in (client.processing() or {}).values()):
        return "processing"
    try:
        state = _scheduler_state(client, key)
    except CONNECTION_ERRORS:
        raise
    except Exception:  # noqa: BLE001 — no evidence either way; the processing listing still counts
        return None
    return f"scheduler state {state}" if state in ACK_STATES else None


def wait_listed(client, key, fut=None, attempts=ACK_ATTEMPTS, delay_s=ACK_DELAY_S, sleep=None, why=None) -> bool:
    """True once the scheduler has the run: its future finished or erred, the scheduler lists `key` as processing,
    or holds it in an accepted state (queued, waiting, ...); `why` (a list) gets the evidence. False if not within
    `attempts` polls. A lost connection is raised to the caller: it is not evidence either way."""
    sleep = sleep or time.sleep
    for i in range(max(1, int(attempts))):
        got = _acknowledged(client, key, fut)
        if got:
            if why is not None:
                why.append(got)
            return True
        if i < attempts - 1:
            sleep(delay_s)
    return False


def _invocation() -> str:
    """This invocation's token: the watchdog's (MA_LAUNCH_INVOCATION) or a new one. Printed first; every record this
    invocation writes carries it, so an exit 1 can be matched to a record of this attempt only (Codex P1-3)."""
    import secrets
    return os.environ.get("MA_LAUNCH_INVOCATION") or f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{secrets.token_hex(3)}"


class RecordOwned(RuntimeError):
    """clusters.json belongs to another invocation."""


def _atomic_json(path: str, obj) -> None:
    """Write through a unique temporary file in the same directory, then rename."""
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=1)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_clusters(cfg, clusters: dict, replace: bool = False) -> None:
    """clusters.json, atomically. Only `launch` replaces another invocation's record (`replace`, under the config
    lock, once no cluster of the name is live); every other write refuses a record it does not own (Codex P2-4)."""
    if not replace and os.path.exists(cfg.clusters_file):
        try:
            owner = json.load(open(cfg.clusters_file)).get("invocation")
        except (OSError, ValueError, AttributeError):
            owner = None
        if owner != clusters.get("invocation"):
            raise RecordOwned(f"{cfg.clusters_file} belongs to invocation {owner}, not {clusters.get('invocation')}")
    _atomic_json(cfg.clusters_file, clusters)


@contextlib.contextmanager
def _config_lock(cfg):
    """One launch, keeper or relaunch of a config at a time on this machine (Codex P2-4)."""
    import fcntl
    os.makedirs(cfg.out_dir, exist_ok=True)
    f = open(os.path.join(cfg.out_dir, ".run_cluster.lock"), "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        raise SystemExit(f"{cfg.name}: another launch, keeper or relaunch of this config is in progress: refusing")
    try:
        yield
    finally:
        f.close()


def start_helpers(cfg, compute, cluster_id, clusters: dict, keep_overflow=None, keep_keeper=False) -> None:
    """The overflow pool and the keeper for run cluster `cluster_id`, each recorded in clusters.json right after its
    constructor, before any step of its start-up that can fail (Codex P2-5). `keep_overflow` (an id) and
    `keep_keeper`: helpers of this launch that `keeper` found healthy (Codex round 2, P2-1)."""
    def record(role, name, cid):
        clusters[role] = {"name": name, "id": cid}
        write_clusters(cfg, clusters)
    overflow = None
    if cfg.keeper and cfg.overflow_max:
        overflow = (_overflow_info(cfg, keep_overflow) if keep_overflow
                    else start_overflow(cfg, compute, record=record, inv=clusters.get("invocation")))
        overflow["primary_vm_types"] = list(compute.WORKER_VM_TYPES)
    if cfg.keeper and not keep_keeper:
        time.sleep(10)
        kc = start_keeper(cfg, adapt_options(cfg, compute.ADAPT_OPTIONS), cluster_id, overflow, record=record,
                          inv=clusters.get("invocation"))
        time.sleep(30)
        confirm_keeper(cfg.keeper_name, kc, run_alive=lambda: is_running(cfg.name), run_id=cluster_id,
                       overflow_id=(overflow or {}).get("id"))


def _outcome(sent: list, acked: list, e: BaseException) -> tuple[int, str]:
    """The exit code after a submission: never EXIT_FAILED (module doc)."""
    if acked != sent:
        return EXIT_UNCONFIRMED, "submitted_unconfirmed"
    if isinstance(e, CONNECTION_ERRORS):
        return EXIT_SUBMITTED_LOST, "submitted_connection_lost"
    return EXIT_INCOMPLETE, "submitted_launch_incomplete"


def _handover(cfg, clusters: dict, e: BaseException, sent: list, acked: list) -> int:
    """After a submission, whatever went wrong: the safe exit code is chosen first; recording it and saying how to
    attach later are best effort, interrupts included, so nothing here can turn into a failed launch (Codex P1-1)."""
    code, status = _outcome(sent, acked, e)
    try:
        clusters.update(status=status, detail=f"{type(e).__name__}: {str(e)[:300]}", submitted=sent, listed=acked,
                        not_submitted=[x for x in cfg.labels if x not in sent])
        me = "python cloud/run_cluster.py <config> "
        print(f"\n{cfg.name}: {status} (exit {code}) after {type(e).__name__}: {str(e)[:200]}\n"
              f"  submitted {sent or '-'}; listed by the scheduler {acked or '-'}.\n  The run keeps going without "
              f"this process: do not shut down. Attach later with `{me}status` or `{me}health --loop`.", flush=True)
        if clusters["not_submitted"]:
            print(f"  Not submitted: {clusters['not_submitted']}; `{me}relaunch <label>` once `status` shows the "
                  "cluster (their assignments are recorded).", flush=True)
        if cfg.keeper and "keeper" not in clusters:
            print(f"  No keeper yet (the pool stays at its start size): start it with `{me}keeper`.", flush=True)
        write_clusters(cfg, clusters)
        state = "live" if live_id(cfg.name) == clusters["run"]["id"] else "not live"   # the Coiled API, not the scheduler
        print(f"  run cluster {clusters['run']['id']} is {state}.", flush=True)
    except BaseException:  # noqa: BLE001 — reporting only; the code above is the answer
        pass
    return code


def launch(cfg) -> int:
    inv = _invocation()
    print(f"invocation {inv}", flush=True)
    with _config_lock(cfg):
        return _launch_locked(cfg, inv)


def _launch_locked(cfg, inv) -> int:
    import coiled
    if is_running(cfg.name):
        raise SystemExit(f"{cfg.name} is already running: use status / relaunch")
    question = open(cfg.question_file).read().strip()
    code, commit = code_tarball(cfg)
    pipeline = pipeline_preflight(cfg, code)      # before anything is created or any key is read
    print(f"pipeline {pipeline} (declared by the config, confirmed by the shipped main.py) @ {commit}", flush=True)
    keys = read_keys()
    compute = _compute(cfg)
    n0 = len(cfg.labels) + cfg.compute_start
    cluster = _construct(cfg, cfg.name, inv, lambda: coiled.Cluster(   # this invocation's, or refused unrecorded
        name=cfg.name, workspace=WORKSPACE, n_workers=n0, worker_vm_types=compute.WORKER_VM_TYPES,
        worker_disk_size=100, idle_timeout=cfg.idle_timeout, worker_options=compute.WORKER_OPTIONS,
        environ={"MA_STORAGE_ACCOUNT": cfg.storage_account, "MA_STORAGE_PREFIX": cfg.storage_prefix,
                 **cfg.compute_env},
        spot_policy="on-demand", scheduler_cpu=2, shutdown_on_close=False, **_lifetime(cfg, inv)))
    launch_id = time.strftime("%Y%m%d-%H%M%S")
    # S1-8: recorded once the run cluster exists, before any submission or acknowledgement wait; this invocation's
    # record replaces the previous launch's (no cluster of the name was live, and the config lock is held)
    clusters = {"run": {"name": cfg.name, "id": cluster.cluster_id}, "launch_id": launch_id, "invocation": inv,
                "launched_utc": time.strftime("%FT%TZ", time.gmtime()), "status": "submitting"}
    write_clusters(cfg, clusters, replace=True)
    sent, acked = [], []
    try:
        client = cluster.get_client()
        client.wait_for_workers(n0, timeout=1800)
        client.upload_file(os.path.join(HERE, "worker_side.py"))
        box = [client]

        def run_client():   # a client closed since the upload is replaced, and the replacement kept
            box[0] = ws.live_client(cluster, box[0])
            return box[0]
        workers = sorted(ws.scheduler_workers(run_client, min_workers=n0))[:len(cfg.labels)]
        client = box[0]
        compute.register_compute_tags(client, exclude=workers)   # the agent VMs never take compute tasks
        # Codex P2-6: every label's assignment before the first submission, so `relaunch` works for any of them
        assign = {label: dict(launch_record(cfg, w, commit, launch_id, cluster.cluster_id, code, pipeline),
                              submission="pending", invocation=inv) for label, w in zip(cfg.labels, workers)}
        _atomic_json(cfg.state_file, assign)
        for label, w in zip(cfg.labels, workers):
            key = _task_key(label)
            assign[label].update(submission="sent", submitted=time.strftime("%F %T"), task_key=key)
            _atomic_json(cfg.state_file, assign)
            sent.append(label)            # from the submit call on, the run may be going
            key, fut = _submit(cfg, client, label, w, code, question, keys, launch_id, key=key)
            print(f"fired {label} -> {w} @ {commit} (pipeline {pipeline})", flush=True)
            why = []
            if not wait_listed(client, key, fut, why=why):
                raise TimeoutError(f"the scheduler did not list {key} within {ACK_ATTEMPTS} polls")
            acked.append(label)
            assign[label]["submission"] = "listed"
            _atomic_json(cfg.state_file, assign)
            print(f"listed {label} ({key}) by the scheduler ({why[0]})", flush=True)
        clusters["status"] = "submitted"
        write_clusters(cfg, clusters)
        start_helpers(cfg, compute, cluster.cluster_id, clusters)
    except BaseException as e:  # noqa: BLE001 — after a submission nothing here may read as a failed launch
        if sent:
            return _handover(cfg, clusters, e, sent, acked)
        try:
            clusters.update(status="failed", detail=f"{type(e).__name__}: {str(e)[:300]}")
            write_clusters(cfg, clusters)
        except BaseException:  # noqa: BLE001 — the original error is the one to raise
            pass
        raise
    try:   # confirmed: recording and reporting it are best effort
        clusters["status"] = "launched"
        write_clusters(cfg, clusters)
        print(f"launched {cfg.name}: results go to outputs/{cfg.storage_prefix}/; the cluster stops itself "
              "once every run is stored. Safe to disconnect.", flush=True)
    except BaseException:  # noqa: BLE001
        pass
    return EXIT_LAUNCHED


def _stop_id(name, cid) -> None:
    """Stop one cluster by id (never an attach by name)."""
    import coiled
    coiled.delete_cluster(cluster_id=cid, workspace=WORKSPACE)
    print(f"{name} ({cid}): stop requested by id")


def _reconcile(cfg, clusters: dict, role: str, name: str):
    """The id of the live `name` helper when it is the one clusters.json records for this launch (kept), else None.
    A live helper with another id belongs to an earlier launch: it is stopped by that id first (Codex round 2, P2-1)."""
    rec, live = (clusters.get(role) or {}).get("id"), live_id(name)
    if live is not None and live == rec:
        print(f"{name} ({live}): this launch's, live: kept")
        return live
    if live is not None:
        print(f"{name} ({live}): live but not this launch's ({rec}): an earlier launch's helper")
        _stop_id(name, live)
    clusters.pop(role, None)
    return None


def ack_probe(cfg) -> int:
    """`ack-probe`: the acknowledgement on a genuinely fast task. Submits worker_side.ack_probe (it returns at
    once) to the live run cluster, lets it finish, then runs wait_listed and prints what acknowledged it."""
    from distributed import fire_and_forget
    client = attach(cfg.name).get_client()
    key = f"ack-probe-{int(time.time())}"
    fut = client.submit(ws.ack_probe, pure=False, key=key)
    fire_and_forget(fut)
    print(f"submitted {key}", flush=True)
    time.sleep(10)                        # long past the task's run time: nothing is left to list as processing
    why = []
    got = wait_listed(client, key, fut, attempts=3, why=why)
    print(f"ack-probe {key}: {'acknowledged by ' + why[0] if got else 'NOT acknowledged'} (future {fut.status})")
    return 0 if got else 1


def _bounded(fn, *a, timeout_s: float = 60.0):
    """fn(*a) in a daemon thread; TimeoutError when it has not returned within `timeout_s`."""
    import threading
    box = {}

    def run():
        try:
            box["v"] = fn(*a)
        except BaseException as e:  # noqa: BLE001
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        raise TimeoutError(f"{getattr(fn, '__name__', fn)} did not return within {timeout_s} s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def smoke_watch(cfg, inv, launcher_alive, minutes: float = 20.0, lister=None, stopper=None, clock=None, sleep=None,
                every_s: float = 30.0, call_timeout_s: float = 60.0, terminate=None, journal=None) -> dict:
    """A smoke's laptop-side bound. The clusters it owns are this invocation's by its construction journal
    (owned_clusters: what it created, and what a construction it began and never finished created).
    Until the wall-clock limit it waits for the launcher to exit and for every owned cluster to stop. At the limit
    it terminates the launcher first (`terminate`, which reaps it), then stops every owned cluster by id. Every API
    call is bounded (`call_timeout_s`)."""
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    if lister is None:
        import coiled
        lister = lambda: coiled.list_clusters(workspace=WORKSPACE)  # noqa: E731
    stopper = stopper or _stop_id

    def live():
        items = _items(_bounded(lister, timeout_s=call_timeout_s))
        return owned_clusters(cfg, inv, items, journal if journal is not None else read_journal(cfg))
    deadline = clock() + minutes * 60
    while clock() < deadline:
        try:
            lv = live()
        except Exception as e:  # noqa: BLE001 — unknown this round; the limit still holds
            print(f"smoke-watch: listing failed ({type(e).__name__}); retrying", flush=True)
            lv = None
        if lv == {} and not launcher_alive():
            print("smoke-watch: the launcher has exited and no cluster of this invocation is live", flush=True)
            return {"clean": True, "stopped": []}
        sleep(every_s)
    print(f"smoke-watch: limit of {minutes} min reached; terminating the launcher, then stopping by id", flush=True)
    if terminate is not None:
        try:
            terminate()                 # no new cluster can appear after this
        except Exception as e:  # noqa: BLE001
            print(f"smoke-watch: terminating the launcher failed: {type(e).__name__}", flush=True)
    stopped, end = [], clock() + min(300.0, minutes * 60 / 4)
    while clock() < end:
        try:
            lv = live()
        except Exception:  # noqa: BLE001
            lv = None
        if lv == {}:
            break
        for cid, name in (lv or {}).items():
            if cid not in stopped:
                try:
                    _bounded(stopper, name, cid, timeout_s=call_timeout_s)
                    stopped.append(cid)
                except Exception as e:  # noqa: BLE001 — tried again next round
                    print(f"smoke-watch: stop {name} ({cid}) failed: {type(e).__name__}", flush=True)
        sleep(every_s)
    return {"clean": False, "stopped": stopped}


def smoke_run(cfg, inv, cmd, minutes: float = 20.0, interrupt_on: str | None = None, **watch) -> dict:
    """`smoke-run`: run the launcher `cmd` as this process's child under MA_LAUNCH_INVOCATION=`inv`, echo its
    output, send it SIGINT once on the first line containing `interrupt_on` (the smoke's lost-connection case), and
    watch it with smoke_watch; at the limit the child is terminated (then killed) and reaped before the sweep."""
    import signal
    import threading
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, "MA_LAUNCH_INVOCATION": inv})
    sent = []

    def pump():
        for line in proc.stdout:
            print(line, end="", flush=True)
            if interrupt_on and not sent and interrupt_on in line:
                sent.append(line.strip())
                proc.send_signal(signal.SIGINT)
    reader = threading.Thread(target=pump, daemon=True)
    reader.start()

    def terminate():
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()
        proc.wait()
    r = smoke_watch(cfg, inv, lambda: proc.poll() is None, minutes, terminate=terminate, **watch)
    terminate()
    reader.join(5)
    return {**r, "launcher_rc": proc.returncode, "interrupted": bool(sent)}


def keeper_later(cfg) -> int:
    """`keeper`: the overflow pool and keeper of a launch that ended before starting them (exit 3 or 5), for the run
    cluster clusters.json records, and only while that cluster is the live one of its name."""
    with _config_lock(cfg):
        clusters = json.load(open(cfg.clusters_file))
        cid = (clusters.get("run") or {}).get("id")
        live = live_id(cfg.name)
        if cid is None or live != cid:
            raise SystemExit(f"{cfg.name}: clusters.json records run cluster {cid}, the live one is {live}: refusing")
        keep_o = _reconcile(cfg, clusters, "overflow", cfg.overflow_name) if cfg.keeper and cfg.overflow_max else None
        keep_k = _reconcile(cfg, clusters, "keeper", cfg.keeper_name) if cfg.keeper else None
        if keep_k is not None:            # this launch's keeper: kept only while it holds this run and pool
            try:
                if cfg.overflow_max and keep_o is None:
                    raise SystemExit("its overflow pool is gone: a new pool needs a new keeper")
                confirm_keeper(cfg.keeper_name, ws.attach_existing(cfg.keeper_name, WORKSPACE, keep_k),
                               run_alive=lambda: is_running(cfg.name), run_id=cid, overflow_id=keep_o)
            except SystemExit as e:
                print(f"{cfg.keeper_name} ({keep_k}): this launch's keeper is not holding the pool ({str(e)[:120]}); "
                      "stopping it by id and starting a new one")
                _stop_id(cfg.keeper_name, keep_k)
                clusters.pop("keeper", None)
                keep_k = None
        start_helpers(cfg, _compute(cfg), cid, clusters, keep_overflow=keep_o, keep_keeper=keep_k is not None)
        clusters["status"] = "launched"
        write_clusters(cfg, clusters)
    print(f"{cfg.name}: keeper started for run cluster {cid}")
    return EXIT_LAUNCHED


def reuse_check(cfg, run_dir: str, code: bytes | None = None, python: str = sys.executable,
                expect_all: bool = False) -> dict:
    """Would this launch reuse the stored engine rows of `run_dir`? Asked of the code it would ship (the exact
    tarball, extracted to a temporary directory), through that tree's `compute.py reuse-check`: local only."""
    import tempfile
    code = code if code is not None else code_tarball(cfg)[0]
    with tempfile.TemporaryDirectory(prefix="reuse-check-") as td:
        with tarfile.open(fileobj=io.BytesIO(code), mode="r:gz") as t:
            t.extractall(td, filter="data")
        r = subprocess.run([python, os.path.join(td, "scripts", "compute.py"), "reuse-check",
                            os.path.abspath(run_dir)] + (["--expect-all"] if expect_all else []),
                           capture_output=True, text=True, timeout=1800, cwd=td)
    lines = r.stdout.strip().splitlines()
    try:
        got = json.loads(lines[-1])
    except (ValueError, IndexError):
        raise SystemExit(f"reuse-check gave no answer (rc {r.returncode}): {(r.stderr or '').strip()[-300:]}")
    return {**got, "rc": r.returncode}


def keeper_wait(states: list[str]) -> str:
    """What to do about an existing keeper cluster before starting a new one:
    'start' (none live), 'stop' (an earlier launch's keeper is live), 'wait'
    (one is stopping: attaching to a stopping cluster is never safe)."""
    live = [s for s in states if s not in ("stopped", "error", "")]
    if not live:
        return "start"
    return "wait" if all(s == "stopping" for s in live) else "stop"


def _fresh(name):
    """Before creating a helper cluster: an earlier launch's (live or stopping) is never
    reused. A live one is shut down; a stopping one is waited out."""
    for _ in range(40):
        act = keeper_wait(cluster_states(name))
        if act == "start":
            return
        if act == "stop":
            attach(name).shutdown()
            print(f"{name}: an earlier launch's cluster was running; shut it down")
        time.sleep(15)
    raise SystemExit(f"{name} did not stop; start it later (the pool keeps its floor)")


def start_overflow(cfg, compute, record=None, inv=None) -> dict:
    """The quota-fallback pool: a scheduler-only cluster of other VM families, created
    here (so it gets this launch's software environment) and scaled by the keeper. Its
    idle timeout outlasts the keeper; the keeper deletes it on exit."""
    import coiled
    _fresh(cfg.overflow_name)
    oc = _construct(cfg, cfg.overflow_name, inv, lambda: coiled.Cluster(
        name=cfg.overflow_name, workspace=WORKSPACE, n_workers=0, worker_vm_types=list(cfg.overflow_vm_types),
        worker_disk_size=100, idle_timeout=f"{int(cfg.keeper_hours * 60) + 30} minutes",
        worker_options=compute.WORKER_OPTIONS,
        environ={"MA_STORAGE_ACCOUNT": cfg.storage_account, "MA_STORAGE_PREFIX": cfg.storage_prefix, **cfg.compute_env},
        spot_policy="on-demand", scheduler_cpu=2, shutdown_on_close=False, wait_for_workers=False,
        **_lifetime(cfg, inv)))
    if record:
        record("overflow", cfg.overflow_name, oc.cluster_id)     # before the upload, which can fail (Codex P2-5)
    oc.get_client().upload_file(os.path.join(HERE, "worker_side.py"))
    print(f"{cfg.overflow_name}: ready at 0 workers ({', '.join(cfg.overflow_vm_types)}; up to {cfg.overflow_max})")
    return _overflow_info(cfg, oc.cluster_id)


def _overflow_info(cfg, oid) -> dict:
    """What the keeper needs to scale the overflow pool `oid`."""
    return {"name": cfg.overflow_name, "id": oid, "max": cfg.overflow_max,
            "vm_types": list(cfg.overflow_vm_types), "hold_s": seconds(cfg.overflow_hold),
            "scale_down_s": seconds(cfg.scale_down_after), "hours": cfg.keeper_hours}


def start_keeper(cfg, adapt_opts, cluster_id, overflow=None, record=None, inv=None):
    import coiled
    import dask
    from distributed import fire_and_forget
    lo, hi = len(cfg.labels) + cfg.compute_start, len(cfg.labels) + cfg.compute_max
    # An earlier launch's keeper (still polling, or stopping) must not be reused:
    # its task would hold the old cluster id and exit, leaving this pool unscaled.
    _fresh(cfg.keeper_name)
    kc = _construct(cfg, cfg.keeper_name, inv, lambda: coiled.Cluster(
        name=cfg.keeper_name, workspace=WORKSPACE, n_workers=1, worker_cpu=2, worker_memory="8 GiB", scheduler_cpu=2,
        spot_policy="on-demand", idle_timeout="30 minutes", shutdown_on_close=False, **_lifetime(cfg, inv)))
    if record:
        record("keeper", cfg.keeper_name, kc.cluster_id)         # before anything that can fail (Codex P2-5)
    c = kc.get_client()
    c.wait_for_workers(1, timeout=1200)
    c.upload_file(os.path.join(HERE, "worker_side.py"))
    # The Coiled token goes in as a task argument (memory only, never an env var or a file).
    fut = c.submit(ws.keeper_task, dask.config.get("coiled.token"), cfg.name, WORKSPACE, lo, hi,
                   cfg.keeper_hours, dict(adapt_opts), cluster_id, overflow, pure=False,
                   key=f"keeper-{cfg.name}-{cluster_id}")   # a new key per run cluster: never an old future
    fire_and_forget(fut)
    print(f"keeper {cfg.keeper_name} submitted (pool {lo}..{hi})", flush=True)
    return kc


KEEPER_TICK = re.compile(r"\bworkers=\d+ \(overflow \d+\)")   # _keeper_loop's tick line
KEEPER_FRESH_S = 180.0      # three ticks: a status older than this is not a heartbeat


def keeper_fresh(h: dict, run_id=None, overflow_id=None, max_age_s: float = KEEPER_FRESH_S) -> tuple[bool, str]:
    """(healthy, why) from worker_side.keeper_health: a tick line with no error or exit after it, a status the
    keeper wrote at most `max_age_s` ago by its own clock, and, when `run_id` is given, a status for that run
    cluster and overflow pool (`overflow_id`, None for none). Codex round 3 on #161: any old tick is not enough."""
    lines = str((h or {}).get("log") or "").strip().splitlines()
    ticks = [i for i, ln in enumerate(lines) if KEEPER_TICK.search(ln)]
    if not ticks:
        return False, (lines[-1:] or ["(empty log)"])[0][:200]
    bad = [ln for ln in lines[ticks[-1] + 1:] if "keeper error" in ln or "exiting" in ln]
    if bad:
        return False, f"after its last tick: {bad[-1][:160]}"
    st = (h or {}).get("status")
    if not isinstance(st, dict) or not st.get("updated"):
        return False, "no status file"
    age = float(h.get("now") or 0) - float(st["updated"])
    if age > max_age_s:
        return False, f"its status is {age:.0f} s old (limit {max_age_s:.0f} s)"
    if run_id is not None:
        if st.get("cluster_id") != run_id:
            return False, f"it holds run cluster {st.get('cluster_id')}, not {run_id}"
        if (st.get("overflow") or {}).get("id") != overflow_id:
            return False, f"it drives overflow pool {(st.get('overflow') or {}).get('id')}, not {overflow_id}"
    return True, lines[ticks[-1]][:200]


def confirm_keeper(keeper_name, kc, attempts=8, delay_s=5.0, max_delay_s=60.0, sleep=None, run_alive=None,
                   run_id=None, overflow_id=None):
    """Wait (bounded, doubling backoff) for the keeper's first tick line: it is written only
    after adapt() and a worker-table read, so it is the evidence the pool is held; a readable
    log alone ("(no log yet)", "keeper error ...; retrying") is not. The keeper cluster's
    client can be closed by now (a failed reconnect closes it and empties its scheduler
    identity: v2blind1's `KeyError: 'workers'`), so a closed client is replaced. Past the
    bound the launch fails with a message saying what to check; the run is already fired
    and clusters.json already names the keeper."""
    sleep = sleep or time.sleep
    box = [None]

    def client():
        box[0] = ws.live_client(kc, box[0])
        return box[0]
    delay, last = delay_s, ["(not read)"]
    for i in range(max(1, int(attempts))):
        try:
            w = sorted(ws.scheduler_workers(client, attempts=3, delay_s=delay_s, min_workers=1, sleep=sleep,
                                            log=print))[0]
            good, why = keeper_fresh(box[0].run(ws.keeper_health, workers=[w])[w], run_id, overflow_id)
            if good:
                print([why], f"(keeper {keeper_name})")
                return [why]
            last = [why]
        except Exception as e:  # noqa: BLE001 — retried, then reported
            last = [f"{type(e).__name__}: {str(e)[:200]}"]
        if i < attempts - 1:
            sleep(delay)
            delay = min(max_delay_s, delay * 2)
    # A run that ended (or paused) before the keeper's first tick stops its own cluster, and the keeper then exits
    # by design: nothing is left to hold, so this is reported, not a launch failure.
    try:
        stopped = run_alive is not None and not run_alive()
    except Exception:  # noqa: BLE001 — unknown run state: fail as before
        stopped = False
    if stopped:
        msg = (f"keeper {keeper_name}: not confirmed, but the run cluster has already stopped (the run ended or paused "
               "before the keeper's first tick), so the keeper exited by design; check `status` before relaunching")
        print(msg)
        return [msg]
    raise SystemExit(f"keeper {keeper_name} (cluster {kc.cluster_id}) was submitted but not confirmed holding "
                     f"the pool after {attempts} tries (last: {last[0][:200]}). The run is fired and the clusters "
                     f"file names the keeper; check `status` and `keeper-log` now (without a keeper the pool "
                     f"stays at its start size).")


def keeper_lines(st: dict | None, now: float | None = None) -> list[str]:
    """The keeper's status file as `status` prints it: pool, active VM family, quota refusals."""
    if not st:
        return ["keeper status: none yet (the keeper writes it every minute)"]
    now = time.time() if now is None else now
    age = round(now - float(st.get("updated") or now))
    ovf = st.get("overflow") or {}
    out = [f"keeper: {st.get('workers', '?')} workers ({st.get('overflow_workers', 0)} overflow), "
           f"target {st.get('target', '?')}, pool {st.get('pool')}, updated {age}s ago",
           f"  active: {st.get('active', '?')}; primary {st.get('primary_vm_types') or '(launch list)'}"
           + (f"; overflow {ovf.get('name')} {ovf.get('vm_types')} up to {ovf.get('max')}" if ovf.get("id") else
              "; no overflow pool")]
    n, last = st.get("refusal_count", 0), (st.get("refusals") or [None])[-1]
    if n:
        out.append(f"  quota refusals: {n}; last {round((now - last['at']) / 60)} min ago: {last['family']} "
                   f"{last.get('usage')}/{last.get('limit')} cores (+{last.get('required')} needed)")
    else:
        out.append("  quota refusals: none seen")
    if st.get("refusal_scan", "ok") != "ok":
        out.append(f"  refusal scan: {st['refusal_scan']}")
    out += [f"  error: {e}" for e in (st.get("errors") or [])[-3:]]
    return out


def status(cfg):
    run, keep = is_running(cfg.name), is_running(cfg.keeper_name)
    ovf = is_running(cfg.overflow_name)
    print(f"cluster {cfg.name}: {'running' if run else 'stopped'}; keeper: {'running' if keep else 'stopped'}; "
          f"overflow: {'running' if ovf else 'stopped'}")
    if keep:
        try:
            kc = attach(cfg.keeper_name).get_client()
            w = sorted(ws.scheduler_workers(lambda: kc, attempts=3, min_workers=1))[0]
            print("\n".join(keeper_lines(kc.run(ws.keeper_status, workers=[w])[w])))
        except Exception as e:  # noqa: BLE001 — a keeper from before Phase 42 β has no status file
            print(f"keeper status unavailable: {type(e).__name__}: {str(e)[:120]}")
    if not run:
        return
    assign = json.load(open(cfg.state_file))
    client = attach(cfg.name).get_client()
    for label in cfg.labels:
        m = _on_worker(client, ws.fetch_meta, label, worker=assign[label]["worker"])
        if m["state"] != "done":
            print(f"{label}: {m['state']} (log {m.get('log_bytes', 0)} bytes)")
        else:
            r = m["meta"]
            print(f"{label}: done exit={r['exit_code']} {r['duration_s']}s run_dir={r['run_dir']} "
                  f"stored={r.get('run_stored')}")


def _run_identity_on_disk(dest: str) -> dict:
    """The collected run's own identity (its metadata.json), "unknown" for anything missing."""
    import glob
    for mp in sorted(glob.glob(os.path.join(dest, "runs", "*", "metadata.json"))):
        try:
            meta = json.load(open(mp))
        except (OSError, ValueError):
            meta = {}
        p, c = pipeline_id.label(meta)
        return {"pipeline": p, "commit": c, "launcher": (meta.get("launcher") if isinstance(meta, dict) else None)
                or pipeline_id.UNKNOWN}
    return {"pipeline": pipeline_id.UNKNOWN, "commit": pipeline_id.UNKNOWN, "launcher": pipeline_id.UNKNOWN}


def _identity_note(label, assigned: dict | None, run_ident: dict) -> None:
    a = assigned or {}
    if a.get("pipeline") and run_ident["pipeline"] != a["pipeline"]:
        print(f"WARNING {label}: launched as pipeline {a['pipeline']}, the run records {run_ident['pipeline']}")
    if a.get("commit") and not pipeline_id.same_commit(a["commit"], run_ident["commit"]):
        print(f"WARNING {label}: launched at {a['commit']}, the run records commit {run_ident['commit']}")


def collect_from_storage(cfg, store, label, launch_id, assigned: dict | None = None):
    """One label from storage: this launch's result record, then the run directory it names.
    result.json records the launch's pipeline and commit (`assigned`, from assignments.json)
    and the run's own (`run_identity`, from its metadata.json)."""
    dest = os.path.join(cfg.out_dir, label)
    rj = os.path.join(cfg.out_dir, f"{label}.result.json")
    store.download(ws.record_blob(cfg.storage_prefix, launch_id, label), rj, cfg.storage_account)
    meta = json.load(open(rj))
    if not meta.get("run_stored"):
        raise SystemExit(f"{label}: the result record names no stored run")
    tgz = os.path.join(cfg.out_dir, f"{label}.run.tar.gz")
    store.download(meta["run_stored"], tgz, cfg.storage_account)
    os.makedirs(os.path.join(dest, "runs"), exist_ok=True)
    with tarfile.open(tgz, mode="r:gz") as t:
        t.extractall(os.path.join(dest, "runs"), filter="data")
    meta["launch_id"] = launch_id
    meta["run_identity"] = _run_identity_on_disk(dest)
    for k in ("pipeline", "commit", "launcher"):
        meta[k] = (assigned or {}).get(k) or pipeline_id.UNKNOWN
    _identity_note(label, assigned, meta["run_identity"])
    json.dump(meta, open(os.path.join(dest, "result.json"), "w"), indent=1)
    return meta


def local_is_current(dest: str, launch_id: str) -> bool | None:
    """True: dest holds this launch's collected run; False: an earlier launch's
    (set aside before collecting); None: nothing collected yet."""
    p = os.path.join(dest, "result.json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p)).get("launch_id") == launch_id
    except (OSError, ValueError):
        return False


def relaunch_refusal(vm_state: str, stored: bool) -> str | None:
    """Why a label may not be re-fired, or None. Only a run that is neither
    running, nor finished on its VM, nor stored may be: re-firing wipes the
    VM's work directory, which holds results whose upload failed."""
    if stored:
        return "already stored in this launch; collect it instead"
    if vm_state == "running":
        return "still running on its VM"
    if vm_state == "done":
        return "finished on its VM but not stored; collect it first"
    return None


def _download(client, label, worker, dest_file):
    """Chunked, resumable, sha256-verified (one 591 MB message reset the Coiled proxy)."""
    import hashlib
    m = _on_worker(client, ws.fetch_meta, label, worker=worker)
    have = os.path.getsize(dest_file) if os.path.exists(dest_file) else 0
    with open(dest_file, "ab") as out:
        while have < m["size"]:
            for attempt in range(5):
                try:
                    b = _on_worker(client, ws.fetch_chunk, label, have, CHUNK, worker=worker)
                    break
                except Exception:  # noqa: BLE001
                    if attempt == 4:
                        raise
                    time.sleep(5)
            out.write(b)
            have += len(b)
    h = hashlib.sha256(open(dest_file, "rb").read()).hexdigest()
    if h != m["sha256"]:
        os.remove(dest_file)
        raise SystemExit(f"{label}: download did not verify; removed, re-run collect")
    return m["meta"]


def collect(cfg):
    sys.path.insert(0, os.path.join(cfg.tree, "scripts"))
    import results_store
    os.makedirs(cfg.out_dir, exist_ok=True)
    running = is_running(cfg.name)
    if not os.path.exists(cfg.state_file):
        raise SystemExit(f"no {cfg.state_file}: this machine did not launch {cfg.name}")
    assign, client, pending = json.load(open(cfg.state_file)), None, []
    for label in cfg.labels:
        dest = os.path.join(cfg.out_dir, label)
        cur = local_is_current(dest, assign[label]["launch_id"])
        if cur:
            print(f"{label}: already collected")
            continue
        if cur is False:   # an earlier launch's copy: keep it, out of the way
            os.rename(dest, f"{dest}.before_{assign[label]['launch_id']}_{int(time.time())}")
        try:
            meta = collect_from_storage(cfg, results_store, label, assign[label]["launch_id"], assign[label])
            print(f"{label}: exit={meta['exit_code']} {meta['duration_s']}s -> {cfg.out_dir}/{label} (from storage)")
            continue
        except Exception as e:  # noqa: BLE001 — not (yet) in storage
            if not running:
                print(f"{label}: not in storage ({type(e).__name__}) and the cluster has stopped")
                continue
        if client is None:
            client = attach(cfg.name).get_client()
        w = assign[label]["worker"]
        if _on_worker(client, ws.fetch_meta, label, worker=w)["state"] != "done":
            print(f"{label}: still running")
            pending.append(label)
            continue
        tgz = os.path.join(cfg.out_dir, f"{label}.result.tar.gz")
        if os.path.exists(tgz):
            os.remove(tgz)   # a partial download may be from an earlier launch
        meta = _download(client, label, w, tgz)
        os.makedirs(dest, exist_ok=True)
        with tarfile.open(tgz, mode="r:gz") as t:
            t.extractall(dest, filter="data")
        ident = _run_identity_on_disk(dest)
        _identity_note(label, assign[label], ident)
        json.dump({**meta, **assign[label], "run_identity": ident}, open(os.path.join(dest, "result.json"), "w"),
                  indent=1)
        _on_worker(client, ws.mark_collected, label, worker=w)
        print(f"{label}: collected from the VM -> {dest}")
    if running and not pending:
        print("every run is collected: shutting down")
        shutdown(cfg)


def shutdown(cfg):
    for name in (cfg.name, cfg.overflow_name, cfg.keeper_name):
        if is_running(name):
            attach(name).shutdown()
            print(f"{name}: shutdown requested")
        else:
            print(f"{name}: not running")


# ---- report-cost: Coiled credits per useful core-hour (Phase 42 β, A5) --------------------

def billing_events(start_iso: str, end_iso: str, getter=None) -> list[dict]:
    """Every Coiled billing event in the window, all pages. Coiled writes one event per
    instance when it stops, so an instance still running is not billed yet. The API's
    cluster filter is ignored by the server: filter the result client-side."""
    if getter is None:
        import coiled
        getter = lambda page: coiled.get_billing_activity(account=WORKSPACE, start_time=start_iso,  # noqa: E731
                                                          end_time=end_iso, page=page)
    out, page = [], 1
    while True:
        r = getter(page)
        out += list(r.get("results") or [])
        if not r.get("next") or page > 10000:
            return out
        page += 1


def summarize_billing(events: list[dict], clusters: dict) -> dict:
    """{role: {credits, instances, workers}} for the launch's clusters. `clusters` is
    {role: {"name", "id"}}: an id matches exactly (names are reused across launches);
    without an id the name matches within the window."""
    out = {}
    for role, c in clusters.items():
        mine = [e for e in events if (e.get("cluster_id") == c["id"] if c.get("id") is not None
                                      else e.get("cluster") == c["name"])]
        inst = {e.get("instance") for e in mine}
        out[role] = {"name": c["name"], "id": c.get("id"), "credits": round(-sum(float(e.get("amount_credits") or 0)
                                                                              for e in mine), 2),
                     "instances": len(inst), "workers": sum(1 for i in inst if "-worker-" in str(i)),
                     "last_event": max((e.get("event_datetime") or "" for e in mine), default=None)}
    return out


_WASTED = ("interrupted", "caller died")


def _ledger_ts(ts):
    """A ledger timestamp (the VM's clock, UTC, written without a zone) as epoch seconds."""
    t = str(ts or "")
    return ws._epoch(t if t.endswith("Z") or "+" in t[10:] else t + "+00:00")


def ledger_hours(led: dict, start: float | None = None, end: float | None = None) -> dict:
    """Core-hours from a run's compute ledger. `charged` is what the run's budget counts
    (it includes reservations charged for a dead caller); `useful` is the measured task
    time of batches that completed: an interrupted call's or a charged reservation's
    core-seconds are left out. The ledger has no per-row times, so a completed batch's
    failed rows stay in `useful`; `failed_rows` reports how many there were. With a window
    (epoch seconds), only batches that ended inside it count, and `charged` is their sum,
    so the hours match a billing window instead of the run's lifetime."""
    calls = [c for c in (led or {}).get("calls", []) if isinstance(c, dict)]
    windowed = start is not None or end is not None
    if windowed:
        def inside(c):
            t = _ledger_ts(c.get("ts"))
            return t is not None and (start is None or t >= start) and (end is None or t <= end)
        calls = [c for c in calls if inside(c)]
    done = [c for c in calls if not str(c.get("stopped") or "").startswith(_WASTED)]
    useful = sum(float(c.get("task_core_s") or 0) for c in done)
    charged = (sum(float(c.get("task_core_s") or 0) for c in calls) if windowed
               else float((led or {}).get("core_seconds") or 0))
    return {"charged": round(charged / 3600, 2), "useful": round(useful / 3600, 2),
            "calls": len(calls), "failed_rows": sum(int(c.get("n_failed") or 0) for c in done),
            "as_of": max((str(c.get("ts") or "") for c in calls), default=None)}


def cost_lines(bill: dict, hours: dict | None, window_h: float) -> list[str]:
    total = sum(b["credits"] for b in bill.values())
    out = [f"{role}: {b['name']} (id {b['id']}): {b['credits']} credits, {b['instances']} VMs "
           f"({b['workers']} workers), last billed {b['last_event']}" for role, b in bill.items()]
    run = bill.get("run", {})
    out.append(f"total: {total:.1f} credits over {window_h:.1f} h; run-cluster workers created per hour: "
               f"{run.get('workers', 0) / window_h:.1f}" if window_h > 0 else f"total: {total:.1f} credits")
    if hours is None:
        out.append("useful core-hours: unknown (no compute ledger: pass --ledger, or collect the run)")
    else:
        u = hours["useful"]
        out.append(f"compute ledger (as of {hours['as_of']}): {u} core-hours in completed batches of "
                   f"{hours['charged']} charged, {hours['calls']} batches ({hours.get('failed_rows', 0)} failed rows "
                   "inside completed batches)")
        out.append(f"credits per useful core-hour: {total / u:.2f}" if u > 0 else
                   "credits per useful core-hour: undefined (no useful core-hours)")
    out.append("note: Coiled bills an instance when it stops, so a live cluster's running VMs are not counted yet")
    return out


def _find_ledgers(cfg, store=None) -> list[str]:
    """This launch's ledgers only: per label, the collected copy if it is this launch's
    (local_is_current), else the stored run's. Earlier launches' copies are never mixed in."""
    import glob
    if not os.path.exists(cfg.state_file):
        return []
    assign, found = json.load(open(cfg.state_file)), []
    for label in cfg.labels:
        dest = os.path.join(cfg.out_dir, label)
        if label in assign and local_is_current(dest, assign[label]["launch_id"]):
            local = sorted(glob.glob(os.path.join(dest, "runs", "*", "models", "compute_ledger.json")))
            if local:
                found.append(local[-1])
                continue
        if store is None or label not in assign:
            continue
        try:   # a stored run: the ledger member of its tarball only
            rj = os.path.join(cfg.out_dir, f"{label}.result.json")
            store.download(ws.record_blob(cfg.storage_prefix, assign[label]["launch_id"], label), rj, cfg.storage_account)
            tgz = os.path.join(cfg.out_dir, f"{label}.cost.run.tar.gz")   # always this record's run
            store.download(json.load(open(rj))["run_stored"], tgz, cfg.storage_account)
            with tarfile.open(tgz, mode="r:gz") as t:
                m = next(m for m in t.getmembers() if m.name.endswith("models/compute_ledger.json"))
                dest = os.path.join(cfg.out_dir, f"{label}.compute_ledger.json")
                open(dest, "wb").write(t.extractfile(m).read())
                found.append(dest)
        except Exception as e:  # noqa: BLE001 — not stored yet
            print(f"{label}: no stored compute ledger ({type(e).__name__})")
    return found


def _window_start(cfg) -> str | None:
    if os.path.exists(cfg.clusters_file):
        return json.load(open(cfg.clusters_file)).get("launched_utc")
    if os.path.exists(cfg.state_file):
        sub = min(v["submitted"] for v in json.load(open(cfg.state_file)).values())
        t = time.mktime(time.strptime(sub, "%Y-%m-%d %H:%M:%S")) - 1800   # the cluster starts before the submit
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    return None


def report_cost(cfg, args):
    """report-cost [--ledger PATH]... [--start ISO] [--end ISO]: billing API only (no attach)."""
    ledgers, start, end, i = [], None, None, 0
    while i < len(args):
        k = args[i]
        if k in ("--ledger", "--start", "--end") and i + 1 < len(args):
            v = args[i + 1]
            if k == "--ledger":
                ledgers.append(os.path.expanduser(v))
            elif k == "--start":
                start = v
            else:
                end = v
            i += 2
        else:
            raise SystemExit(f"report-cost: unknown argument {k!r}")
    start = start or _window_start(cfg)
    if not start:
        raise SystemExit("report-cost: no launch record here; pass --start <ISO time, UTC>")
    end = end or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    clusters = {"run": {"name": cfg.name, "id": None}, "keeper": {"name": cfg.keeper_name, "id": None},
                "overflow": {"name": cfg.overflow_name, "id": None}}
    if os.path.exists(cfg.clusters_file):
        for role, c in json.load(open(cfg.clusters_file)).items():
            if isinstance(c, dict) and role in clusters:
                clusters[role] = c
    elif os.path.exists(cfg.state_file):
        clusters["run"]["id"] = next(iter(json.load(open(cfg.state_file)).values())).get("cluster_id")
    if not ledgers:
        sys.path.insert(0, os.path.join(cfg.tree, "scripts"))
        try:
            import results_store
        except ImportError:
            results_store = None
        ledgers = _find_ledgers(cfg, results_store)
    hours = None
    for p in ledgers:
        h = ledger_hours(json.load(open(p)), ws._epoch(start), ws._epoch(end))
        hours = h if hours is None else {"charged": round(hours["charged"] + h["charged"], 2),
                                         "useful": round(hours["useful"] + h["useful"], 2),
                                         "calls": hours["calls"] + h["calls"],
                                         "failed_rows": hours["failed_rows"] + h["failed_rows"], "as_of": max(hours["as_of"] or "", h["as_of"] or "")}
    bill = summarize_billing(billing_events(start, end), clusters)
    wh = (ws._epoch(end) - ws._epoch(start)) / 3600
    print(f"{cfg.name}: Coiled billing {start} .. {end}" + (f"; ledgers: {', '.join(ledgers)}" if ledgers else ""))
    print("\n".join(cost_lines(bill, hours, wh)))


def relaunch(cfg, label) -> int:
    """Re-fire one run on its pinned agent VM (e.g. after a preflight-only fix, or a label a launch never
    submitted). The same submission boundary as `launch`: once the submit call is made, the exit is 0 or 4."""
    inv = _invocation()
    print(f"invocation {inv}", flush=True)
    with _config_lock(cfg):
        assign = json.load(open(cfg.state_file))
        if label not in assign:
            raise SystemExit(f"{label}: no assignment in {cfg.state_file}; not relaunching")
        sys.path.insert(0, os.path.join(cfg.tree, "scripts"))
        import results_store
        launch_id = assign[label]["launch_id"]
        names = {b["name"] for b in results_store.list_blobs(f"outputs/{cfg.storage_prefix}/launch_{launch_id}/",
                                                              cfg.storage_account)}
        client = attach(cfg.name).get_client()
        why = relaunch_refusal(_on_worker(client, ws.fetch_meta, label, worker=assign[label]["worker"])["state"],
                               ws.record_blob(cfg.storage_prefix, launch_id, label) in names)
        if why:
            raise SystemExit(f"{label}: {why}; not relaunching")
        if assign[label].get("submission") in ("sent", "listed"):   # a VM with no run.log yet may be preparing it
            tk = assign[label].get("task_key")
            if not tk:
                raise SystemExit(f"{label}: assignment is {assign[label]['submission']} but records no task key, so "
                                 "the original task cannot be checked; not relaunching")
            try:
                st = _scheduler_state(client, tk)
            except Exception as e:  # noqa: BLE001 — unknown: refuse rather than run a second copy
                raise SystemExit(f"{label}: the state of task {tk} cannot be read ({type(e).__name__}); not "
                                 "relaunching") from None
            if st in LIVE_STATES:
                raise SystemExit(f"{label}: task {tk} is still {st} on the scheduler (accepted, queued or preparing); "
                                 "not relaunching")
        code, commit = code_tarball(cfg)
        pipeline = pipeline_preflight(cfg, code)
        client.upload_file(os.path.join(HERE, "worker_side.py"))
        question, keys = open(cfg.question_file).read().strip(), read_keys()
        assign[label].update(commit=commit, submitted=time.strftime("%F %T"), relaunched=True, pipeline=pipeline,
                             launcher=f"run_cluster:{cfg.name}", eval_inputs_sha256=eval_paths.archive_sha256(code),
                             submission="sent", invocation=inv, task_key=_task_key(label))
        _atomic_json(cfg.state_file, assign)
        sent = []
        try:
            sent.append(label)            # from the submit call on, the run may be going
            key, fut = _submit(cfg, client, label, assign[label]["worker"], code, question, keys, launch_id,
                               key=assign[label]["task_key"])
            print(f"re-fired {label} -> {assign[label]['worker']} @ {commit} (pipeline {pipeline})", flush=True)
            if not wait_listed(client, key, fut):
                raise TimeoutError(f"the scheduler did not list {key} within {ACK_ATTEMPTS} polls")
        except BaseException as e:  # noqa: BLE001 — after the submit call nothing may read as a failure
            code_, _ = _outcome(sent, [], e)
            try:
                print(f"{label}: submitted, not confirmed ({type(e).__name__}: {str(e)[:200]}); it may be running: "
                      "do not shut down, check `status`", flush=True)
            except BaseException:  # noqa: BLE001
                pass
            return code_
        try:
            assign[label]["submission"] = "listed"
            _atomic_json(cfg.state_file, assign)
            print(f"listed {label} ({key}) by the scheduler", flush=True)
        except BaseException:  # noqa: BLE001
            pass
        return EXIT_LAUNCHED


def parse_sign_args(rest: list, labels) -> dict:
    """`sign <answers.yaml> [--label L] [--run RUN_DIR_NAME] [--expect-spec-sha256 SHA] [note ...]`. The options
    name the remote run the owner was shown (the run console passes all three); without --label the config's
    first label is used, as before."""
    if not rest:
        raise SystemExit("sign needs <answers.yaml>")
    out, i = {"answers": rest[0], "label": None, "run": None, "expect": None, "action": None,
              "expect_derivations": None, "expect_sheet": None}, 1
    flags = {"--label": "label", "--run": "run", "--expect-spec-sha256": "expect", "--action": "action",
             "--expect-derivations-sha256": "expect_derivations", "--expect-sheet-sha256": "expect_sheet"}
    while i < len(rest) and rest[i] in flags:
        if i + 1 >= len(rest):
            raise SystemExit(f"sign: {rest[i]} needs a value")
        out[flags[rest[i]]] = rest[i + 1]
        i += 2
    out["note"] = " ".join(rest[i:])
    if out["action"] not in (None, "spec", "scope"):
        raise SystemExit(f"sign: --action is spec or scope, not {out['action']}")
    if out["label"] is not None and out["label"] not in labels:
        raise SystemExit(f"sign: label {out['label']} is not one this config runs ({', '.join(labels)})")
    return out


def _side(cfg, fn, *args, label=None):
    label = label or cfg.labels[0]
    assign = json.load(open(cfg.state_file))
    return _on_worker(attach(cfg.name).get_client(), fn, label, *args, worker=assign[label]["worker"])


def health_snapshots(cfg, client, assign: dict, now: float | None = None) -> dict:
    """Phase 51 γ: {label: snapshot} for run_health.Monitor, read from a live
    cluster (attach-only; the caller passes the client). The scheduler view
    runs on the scheduler and the file view on each run's agent VM; both are
    sent by value, so runs launched before this code have them too. What
    cannot be read becomes a `monitor_error` entry, never a quiet snapshot."""
    import run_health as rh
    try:
        import cloudpickle
        cloudpickle.register_pickle_by_value(rh)
    except ImportError:
        pass
    now = time.time() if now is None else now
    res = (cfg.compute_env or {}).get("MA_COMPUTE_RESOURCE", rh.RESOURCE)
    errors = []
    try:
        sched = client.run_on_scheduler(rh.scheduler_view, res)
    except Exception as e:  # noqa: BLE001 — reported as a monitor error
        sched, errors = None, [f"scheduler view: {type(e).__name__}: {rh.clip(e, 160)}"]
    out = {}
    for label in cfg.labels:
        errs = list(errors)
        try:
            files = _on_worker(client, rh.run_files_view, label, worker=assign[label]["worker"])
        except Exception as e:  # noqa: BLE001
            files = {}
            errs.append(f"run files: {type(e).__name__}: {rh.clip(e, 160)}")
        errs += list(files.get("errors") or [])
        out[label] = {**files, "at": now, "scheduler": sched, "input_errors": errs}
    return out


def connect_existing(name: str, lister=None, security_fn=None, client_cls=None):
    """A Client on the live cluster `name`, or None. Built from the cluster's
    scheduler address and TLS security as the Coiled API reports them, then a
    plain distributed.Client: nothing on this path can create a cluster
    (a coiled Cluster opened by name on a cluster that has just stopped makes a new
    one). A cluster that is gone, or stops between the list and the connect,
    gives None."""
    cid = live_id(name, lister)
    if cid is None:
        return None
    if security_fn is None:
        def security_fn(cluster_id):
            from coiled.v2.core import CloudV2
            with CloudV2() as cloud:
                return cloud.security(cluster_id, workspace=WORKSPACE)
    try:
        sec, info = security_fn(cid)
    except RuntimeError:            # the scheduler is no longer in the started state
        return None
    if client_cls is None:
        from distributed import Client as client_cls
    return client_cls(info["address_to_use"], security=sec, timeout="60s")


def health(cfg, args, connect_fn=None, sleep=time.sleep) -> int:
    """`health`: check a live run once, or every `--interval` s with `--loop`,
    appending new alerts to the log (default <out_dir>/health.log) and stdout.
    It connects with connect_existing, which cannot create a cluster; a
    cluster that is not running is reported and that poll skipped. A poll
    that cannot read something (assignments.json, a run file, the scheduler)
    logs a monitor_error and the next poll tries again: the loop never ends
    on a transient read. Every line goes through run_health.redact."""
    import argparse
    import run_health as rh
    ap = argparse.ArgumentParser(prog="run_cluster.py <config> health")
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--interval", type=float, default=300.0)
    ap.add_argument("--log", default=os.path.join(cfg.out_dir, "health.log"))
    ap.add_argument("--thresholds", default=rh.THRESHOLDS)
    ap.add_argument("--max-polls", type=int, default=0, help=argparse.SUPPRESS)
    a = ap.parse_args(args)
    connect_fn = connect_fn or connect_existing
    thresholds = rh.load_thresholds(a.thresholds)
    monitors = {label: rh.Monitor(thresholds) for label in cfg.labels}
    seen_poll_errors: set = set()
    polls = 0

    def poll_error(what: str, e: Exception) -> None:
        now = time.time()
        key = f"poll:{what}:{type(e).__name__}"
        alert = {"rule": "monitor_error", "key": key, "at": now, "at_iso": rh._iso(now),
                 "message": f"could not read {what}: {type(e).__name__}: {rh.clip(e, 200)} (retrying next poll)"}
        if key not in seen_poll_errors:
            seen_poll_errors.add(key)
            rh.log_alerts([alert], a.log)
        else:
            print(rh.format_alert(alert))

    while True:
        client = None
        try:
            client = connect_fn(cfg.name)
            if client is None:
                print(rh.redact(f"cluster {cfg.name}: not running; poll skipped"))
            else:
                try:
                    with open(cfg.state_file, encoding="utf-8") as f:
                        assign = json.load(f)
                    if not isinstance(assign, dict):
                        raise ValueError("not a mapping")
                except (OSError, ValueError) as e:
                    poll_error(os.path.basename(cfg.state_file), e)
                    assign = None
                for label, snap in (health_snapshots(cfg, client, assign).items() if assign is not None else ()):
                    alerts = monitors[label].check(snap)
                    for err in snap.get("input_errors") or []:
                        key = f"input:{err}"
                        if key not in monitors[label].seen:
                            monitors[label].seen.add(key)
                            alerts.append({"rule": "monitor_error", "key": key, "at": snap["at"],
                                           "at_iso": rh._iso(snap["at"]), "message": f"could not read {err}"})
                    rh.log_alerts(alerts, a.log, label)
                    if not alerts and not a.loop:
                        print(f"{label}: no anomalies ({len(snap.get('trace') or [])} trace events read)")
        except Exception as e:  # noqa: BLE001 — one failed poll must not end the watch
            poll_error("the cluster", e)
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        polls += 1
        if not a.loop or (a.max_polls and polls >= a.max_polls):
            return 0
        sleep(a.interval)


# ---- self-test --------------------------------------------------------------------------

E7R7_REFUSAL = ("Instance Stopped: Failed to create VM -- (OperationNotAllowed) Operation could not be completed as "
                "it results in exceeding approved standardEv5Family Cores quota. Additional details - Deployment "
                "Model: Resource Manager, Location: westus2, Current Limit: 350, Current Usage: 344, Additional "
                "Required: 8, (Minimum) New Limit Required: 352. Setup Alerts when Quota reaches threshold.")


def _self_test_42b(ok, RunConfig) -> None:
    ok(seconds("15s") == 15 and seconds("10 minutes") == 600 and seconds("1 hour") == 3600 and seconds(90) == 90
       and seconds("2.5m") == 150 and seconds("30") == 30, "RC23 durations parse in the forms dask and Coiled accept")
    for bad in ("ten minutes", "5 fortnights", ""):
        try:
            seconds(bad)
            ok(False, f"RC23 rejects {bad!r}")
        except ValueError:
            ok(True, f"RC23 rejects {bad!r}")
    cfg = RunConfig(name="t", question_file="/dev/null")
    a = adapt_options(cfg, {"target_duration": "20s"})
    ok(a == {"target_duration": "20s", "interval": "15s", "wait_count": 40},
       f"RC24 the default scale-down delay is 10 min (15 s x 40 re-plans), target_duration kept: {a}")
    a = adapt_options(RunConfig(name="t", question_file="/dev/null", scale_down_after="2 minutes", adapt_interval="5s",
                                target_duration="3m"), {"target_duration": "20s"})
    ok(a == {"target_duration": "3m", "interval": "5s", "wait_count": 24}, f"RC24b config overrides apply: {a}")
    ok(cfg.overflow_max == cfg.compute_max and cfg.overflow_name == "t-overflow"
       and RunConfig(name="t", question_file="/dev/null", overflow_max=0).overflow_max == 0,
       "RC25 the overflow pool defaults to compute_max; 0 disables it")
    for bad, why in ((dict(scale_down_after="soon"), "a bad delay"), (dict(adapt_interval="0s"), "a zero interval"),
                     (dict(overflow_vm_types=()), "overflow without VM types")):
        try:
            RunConfig(name="t", question_file="/dev/null", **bad)
            ok(False, f"RC25 rejects {why}")
        except ValueError:
            ok(True, f"RC25 rejects {why}")
    # 2026-11-01 (local check against distributed 2026.8.0): with no worker carrying the compute resource, compute.py's
    # queued chunks and demand markers leave adaptive_target at 1 (their duration is unknown until a chunk has run),
    # so a pool that starts with no compute VM never grows and the batch waits forever.
    try:
        RunConfig(name="t", question_file="/dev/null", compute_start=0, compute_env={"MA_COMPUTE_RESOURCE": "compute"})
        ok(False, "RC25b a resource-tagged pool with no compute VM at start is refused (it cannot scale up)")
    except ValueError as e:
        ok("compute_start" in str(e), f"RC25b a resource-tagged pool with no compute VM at start is refused: {e}")
    ok(RunConfig(name="t", question_file="/dev/null", compute_start=1,
                 compute_env={"MA_COMPUTE_RESOURCE": "compute"}).compute_start == 1,
       "RC25c one compute VM at start is accepted")
    q = ws.quota_refusal(E7R7_REFUSAL)
    ok(q == {"family": "standardEv5Family", "limit": 350, "usage": 344, "required": 8},
       f"RC26 the attempt-7 quota refusal parses: {q}")
    ok(ws.quota_refusal("Instance Stopped: Instance did not phone home before timeout") is None
       and ws.quota_refusal("(SkuNotAvailable) The requested VM size is currently not available") is None
       and ws.quota_refusal(None) is None, "RC26b other worker errors are not quota refusals")
    listing = [{"id": 1, "name": "w1", "current_state": {"state": "error", "reason": E7R7_REFUSAL,
                                                         "created": "2026-10-27T06:00:00+00:00"}},
               {"id": 2, "name": "w2", "current_state": {"state": "stopped", "reason": E7R7_REFUSAL}},
               {"id": 3, "name": "w3", "current_state": {"state": "error", "reason": "Dask process stopped"}},
               {"id": 4, "name": "w4", "current_state": {"state": "error", "reason": E7R7_REFUSAL.replace(
                   "standardEv5Family", "standardESv5Family"), "created": "2026-10-27T05:00:00Z"}}]
    r = ws.quota_refusals(listing)
    ok([x["id"] for x in r] == [4, 1] and r[0]["family"] == "standardESv5Family" and r[0]["at"] < r[1]["at"],
       f"RC27 refusals come from error-state workers only, oldest first: {[x['id'] for x in r]}")
    ok([x["id"] for x in ws.quota_refusals(listing, seen={4})] == [1], "RC27b a refusal already counted is skipped")
    t_upd = ws.quota_refusals([{"id": 5, "current_state": {"state": "error", "reason": E7R7_REFUSAL,
                                                           "updated": "2026-10-27T07:00:00+00:00"}}])[0]["at"]
    t_none = ws.quota_refusals([{"id": 6, "current_state": {"state": "error", "reason": E7R7_REFUSAL}}], now=123.0)[0]["at"]
    ok(t_upd == ws._epoch("2026-10-27T07:00:00+00:00") and t_none == 123.0,
       "RC27c a refusal is dated by its state's updated time, or the scan time; never epoch 0")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        c2 = RunConfig(name="t", question_file="/dev/null", out_dir=d, labels=("R1", "R2"))
        json.dump({"R1": {"launch_id": "L2"}, "R2": {"launch_id": "L2"}}, open(c2.state_file, "w"))
        for sub, lid in (("R1", "L2"), ("R1.before_L1_9", "L1"), ("R2", "L1")):
            os.makedirs(os.path.join(d, sub, "runs", "r", "models"))
            open(os.path.join(d, sub, "runs", "r", "models", "compute_ledger.json"), "w").write("{}")
            json.dump({"launch_id": lid}, open(os.path.join(d, sub, "result.json"), "w"))
        got = [os.path.relpath(x, d) for x in _find_ledgers(c2)]
        ok(got == [os.path.join("R1", "runs", "r", "models", "compute_ledger.json")],
           f"RC32b report-cost reads only this launch's ledgers, never an earlier launch's copy: {got}")
    now = 10_000.0
    ok(ws.overflow_desired(20, 5, 24, None, now, 1800) == 0 and ws.overflow_desired(20, 5, 24, now - 60, now, 1800) == 15
       and ws.overflow_desired(40, 5, 24, now - 60, now, 1800) == 24
       and ws.overflow_desired(20, 5, 24, now - 3600, now, 1800) == 0
       and ws.overflow_desired(4, 5, 24, now - 60, now, 1800) == 0 and ws.overflow_desired(20, 5, 0, now, now, 1800) == 0,
       "RC28 overflow wanted = the shortfall, capped, only while refusals are recent")
    p = ws.overflow_plan(3, 0, {}, {}, now, 600)
    ok(p["scale_to"] == 3 and not p["join"] and not p["close"], f"RC29 an empty overflow pool grows: {p}")
    vms = {"a": {"joined": True, "busy": True}, "b": {"joined": False}, "c": {"joined": False}}
    p = ws.overflow_plan(2, 3, vms, {}, now, 600)
    ok(p["scale_to"] is None and p["join"] == ["b"] and not p["close"] and set(p["idle_since"]) == {"c"},
       f"RC29b booted VMs join up to the wanted count; the spare starts its idle clock: {p}")
    p = ws.overflow_plan(0, 3, vms, {"b": now - 700, "c": now - 100, "a": now - 9999}, now, 600)
    ok(p["close"] == ["b"] and "a" not in p["idle_since"] and p["idle_since"].get("c") == now - 100,
       f"RC29c only VMs idle past the delay close, never a busy one: {p}")
    ok(ws.overflow_plan(1, 1, {"a": {"joined": True, "busy": False}}, {"a": now - 9999}, now, 600)["close"] == [],
       "RC29d a VM the wanted count still needs is kept even when idle")
    ev = [{"cluster": "t", "cluster_id": 7, "instance": "t-worker-1-instance", "amount_credits": "-1.5",
           "event_datetime": "2026-10-28T21:00:00Z"},
          {"cluster": "t", "cluster_id": 7, "instance": "t-scheduler-1-instance", "amount_credits": "-0.5"},
          {"cluster": "t", "cluster_id": 3, "instance": "t-worker-9-instance", "amount_credits": "-99"},
          {"cluster": "t-keeper", "cluster_id": 8, "instance": "t-keeper-worker-1-instance", "amount_credits": "-0.25"},
          {"cluster": "other", "cluster_id": 9, "instance": "o", "amount_credits": "-5"}]
    b = summarize_billing(ev, {"run": {"name": "t", "id": 7}, "keeper": {"name": "t-keeper", "id": None}})
    ok(b["run"]["credits"] == 2.0 and b["run"]["instances"] == 2 and b["run"]["workers"] == 1
       and b["keeper"]["credits"] == 0.25, f"RC30 billing is summed per cluster id; a reused name's other launch is excluded: {b}")
    pages = {1: {"results": [1, 2], "next": "p2"}, 2: {"results": [3], "next": None}}
    ok(billing_events("a", "b", lambda pg: pages[pg]) == [1, 2, 3], "RC31 billing pages are all read")
    led = {"core_seconds": 3600 * 410.0, "calls": [
        {"n": 10, "n_failed": 0, "task_core_s": 3600 * 2.0, "ts": "2026-10-28T21:00:00"},
        {"n": 10, "n_failed": 5, "task_core_s": 3600 * 4.0, "ts": "2026-10-28T22:00:00"},
        {"n": 864, "n_failed": 864, "task_core_s": 3600 * 3.0, "stopped": "interrupted: KeyboardInterrupt"},
        {"task_core_s": 3600 * 400.0, "stopped": "caller died; charged its reservation", "ts": "2026-10-28T23:00:00"},
        {"n": 5, "n_failed": 0, "task_core_s": 3600 * 1.0, "stopped": "run compute cap reached", "ts": "x"}]}
    h = ledger_hours(led)
    ok(h["charged"] == 410.0 and h["useful"] == 7.0 and h["calls"] == 5 and h["failed_rows"] == 5,
       f"RC32 useful core-hours leave out interrupted calls and charged reservations: {h}")
    hw = ledger_hours(led, ws._epoch("2026-10-28T21:30:00Z"), ws._epoch("2026-10-28T23:30:00Z"))
    ok(hw["useful"] == 4.0 and hw["charged"] == 404.0 and hw["calls"] == 2,
       f"RC32c with a billing window only the batches that ended inside it count: {hw}")
    lines = cost_lines({"run": {**b["run"], "credits": 20.0}, "keeper": b["keeper"]}, h, 10.0)
    ok(any("credits per useful core-hour: 2.89" in x for x in lines) and any("workers created per hour: 0.1" in x
                                                                             for x in lines),
       f"RC33 the report gives credits per useful core-hour and VMs created per hour: {lines}")
    ok(any("unknown" in x for x in cost_lines(b, None, 1.0)), "RC33b without a ledger the ratio is reported unknown")
    st = {"updated": now - 30, "workers": 9, "overflow_workers": 3, "target": 12, "pool": [2, 25], "active": "primary+overflow",
          "primary_vm_types": ["Standard_E8s_v5"], "overflow": {"name": "t-overflow", "id": 5, "max": 24,
                                                                  "vm_types": ["Standard_D8s_v5"]},
          "refusal_count": 4, "refusals": [{"at": now - 120, "family": "standardESv5Family", "usage": 348, "limit": 350,
                                             "required": 8}], "refusal_scan": "ok", "errors": []}
    kl = "\n".join(keeper_lines(st, now))
    ok("primary+overflow" in kl and "quota refusals: 4; last 2 min ago: standardESv5Family 348/350" in kl
       and "updated 30s ago" in kl, f"RC33c status prints the active family and the refusals: {kl}")
    ok("none yet" in keeper_lines(None)[0], "RC33d status says when the keeper has written no status yet")


def _self_test_52(ok) -> None:
    """The keeper's `KeyError: 'workers'` race (no Coiled, no network). distributed's
    Client.scheduler_info() returns {} when the client is not connected: before its first
    identity reply, while reconnecting, and after a failed reconnect closes it. v2blind1
    (2026-10-31) crashed at the end of `launch` on it, before clusters.json recorded the keeper."""
    import contextlib
    import io
    import tempfile
    import types

    class FakeClient:
        def __init__(self, infos, status="running", log="12:00:00 keeper holding x 3..9 {}\n12:00:01 workers=3 (overflow 0) refusals=0 active=primary"):
            self.infos, self.status, self.log, self.calls = list(infos), status, log, 0

        def scheduler_info(self):
            self.calls += 1
            return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

        def run(self, fn, workers):
            if getattr(fn, "__name__", "") == "keeper_health":   # a fresh status beside the log
                return {w: {"log": self.log, "status": {"updated": 100.0}, "now": 110.0} for w in workers}
            return {w: self.log for w in workers}

    table = {"tls://10.0.0.5:1": {"name": "a"}, "tls://10.0.0.6:1": {"name": "b"}}
    sleeps = []
    c = FakeClient([{}, {"id": "s"}, {"workers": table}])
    got = ws.scheduler_workers(lambda: c, attempts=5, delay_s=2.0, sleep=sleeps.append)
    ok(got == table and sleeps == [2.0, 4.0],
       f"RC52a the worker table is read once the scheduler has populated it, with doubling backoff: {sleeps}")
    sleeps.clear()
    try:
        ws.scheduler_workers(lambda: FakeClient([{}]), attempts=4, delay_s=5.0, max_delay_s=8.0, sleep=sleeps.append)
        ok(False, "RC52b a worker table that never appears is refused after the bound")
    except ws.SchedulerInfoUnavailable as e:
        ok(sleeps == [5.0, 8.0, 8.0] and "4 tries" in str(e) and "no worker table" in str(e),
           f"RC52b a worker table that never appears is refused after the bound, naming why: {e} {sleeps}")
    c = FakeClient([{"workers": {}}, {"workers": table}])
    got = ws.scheduler_workers(lambda: c, attempts=3, delay_s=0.0, sleep=sleeps.append, min_workers=1)
    ok(got == table, "RC52c min_workers: an empty table (no worker joined yet) is waited out")
    closed = FakeClient([{}], status="closed")
    fresh = FakeClient([{"workers": table}])
    ok(ws.live_client(types.SimpleNamespace(get_client=lambda: fresh), closed) is fresh
       and ws.live_client(types.SimpleNamespace(get_client=lambda: closed), fresh) is fresh,
       "RC52d a closed client is replaced from its cluster; an open one is kept")

    # v2blind1's sequence: the keeper cluster's client failed to reconnect ("closing client"),
    # so scheduler_info() returned {}; `launch` died with KeyError: 'workers'.
    calls = iter([closed, fresh])
    kc = types.SimpleNamespace(cluster_id=2307781, get_client=lambda: next(calls, fresh))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        line = confirm_keeper("x-keeper", kc, attempts=3, delay_s=0.0, sleep=lambda s: None)
    ok("keeper holding" in out.getvalue() or "workers=3" in out.getvalue(),
       f"RC52e replay v2blind1: a closed keeper client is replaced and the keeper's log line printed: {line}")
    kc2 = types.SimpleNamespace(cluster_id=9, get_client=lambda: FakeClient([{}], status="closed"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            confirm_keeper("x-keeper", kc2, attempts=2, delay_s=0.0, sleep=lambda s: None)
        ok(False, "RC52f a keeper that cannot be confirmed fails the launch with a clear message")
    except SystemExit as e:
        ok("x-keeper" in str(e) and "not confirmed" in str(e) and "keeper-log" in str(e),
           f"RC52f a keeper that cannot be confirmed fails the launch with a clear message: {e}")

    # Codex round 1: a readable log is not proof; only a tick line (written after adapt()
    # and a worker-table read) confirms the keeper is holding the pool.
    for log in ("(no log yet)", "12:00:00 keeper holding x 3..9 {}\n12:00:01 keeper error OSError: no; retrying"):
        kc3 = types.SimpleNamespace(cluster_id=10, get_client=lambda log=log: FakeClient([{"workers": table}], log=log))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                confirm_keeper("x-keeper", kc3, attempts=2, delay_s=0.0, sleep=lambda s: None)
            ok(False, f"RC52h a keeper log with no tick line is not confirmation: {log[-40:]!r}")
        except SystemExit as e:
            ok("not confirmed" in str(e), f"RC52h a keeper log with no tick line is not confirmation: {log[-40:]!r}")
    late = FakeClient([{"workers": table}], log="(no log yet)")
    logs = iter(["(no log yet)", "12:00:00 keeper holding x 3..9 {}",
                 "12:00:00 keeper holding x 3..9 {}\n12:01:00 workers=3 (overflow 0) refusals=0 active=primary"])
    late.run = lambda fn, workers: {w: {"log": next(logs), "status": {"updated": 100.0}, "now": 110.0} for w in workers}
    with contextlib.redirect_stdout(io.StringIO()):
        line = confirm_keeper("x-keeper", types.SimpleNamespace(cluster_id=11, get_client=lambda: late),
                              attempts=4, delay_s=0.0, sleep=lambda s: None)
    ok("workers=3" in str(line), f"RC52i a keeper whose first tick comes late is confirmed once it ticks: {line}")

    # v2-nigeria-gf13-full (2026-11-01): the run paused for sign-off and its cluster stopped ~5 min after it was fired,
    # before the keeper's first tick; the keeper then exited by design ("run cluster stopped") and the launch failed
    # with "not confirmed holding the pool". A run cluster that has stopped is reported as such, not as a failure.
    kc4 = types.SimpleNamespace(cluster_id=12, get_client=lambda: FakeClient([{}], status="closed"))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            line = confirm_keeper("x-keeper", kc4, attempts=2, delay_s=0.0, sleep=lambda s: None,
                                  run_alive=lambda: False)
        ok("run cluster" in str(line) and "stopped" in str(line),
           f"RC52m a run cluster that stopped before the keeper ticked is reported, not a launch failure: {line}")
    except SystemExit as e:
        ok(False, f"RC52m a run cluster that stopped before the keeper ticked is reported, not a launch failure: {e}")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            confirm_keeper("x-keeper", kc4, attempts=2, delay_s=0.0, sleep=lambda s: None, run_alive=lambda: True)
        ok(False, "RC52n a live run with an unconfirmed keeper still fails the launch")
    except SystemExit as e:
        ok("not confirmed" in str(e), f"RC52n a live run with an unconfirmed keeper still fails the launch: {e}")

    # Codex round 1: launch keeps the replacement client for the rest of the launch.
    from unittest import mock

    class LaunchClient(FakeClient):
        def wait_for_workers(self, *a, **kw):
            pass

        def upload_file(self, *a):
            self.status = "closed"   # the client drops right after the upload

    old_c, new_c = LaunchClient([{}]), LaunchClient([{"workers": {f"tls://a:{i}": {} for i in range(4)}}])
    run_cl = types.SimpleNamespace(cluster_id=12, get_client=lambda: old_c if old_c.status == "running" else new_c)
    used = []
    comp = types.SimpleNamespace(WORKER_VM_TYPES=[], WORKER_OPTIONS={}, ADAPT_OPTIONS={},
                                 register_compute_tags=lambda c, exclude: used.append(c))
    fake_coiled = types.ModuleType("coiled")
    rc52_made = []
    fake_coiled.Cluster = lambda **kw: rc52_made.append({"name": kw.get("name"), "id": 12,
                                                         "created": _fake_coiled_event(kw.get("name")),
                                                         "current_state": {"state": "running"}}) or run_cl
    fake_coiled.list_clusters = lambda workspace: list(rc52_made)
    with tempfile.TemporaryDirectory() as d:
        q = os.path.join(d, "q.txt")
        open(q, "w").write("question")
        lcfg = RunConfig(name="lt", question_file=q, labels=("R1",), out_dir=d, keeper=False, overflow_max=0,
                         pipeline="v2")
        with mock.patch.dict(sys.modules, {"coiled": fake_coiled}), \
                mock.patch.object(sys.modules[__name__], "is_running", return_value=False), \
                mock.patch.object(sys.modules[__name__], "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(sys.modules[__name__], "code_tarball", return_value=(b"", "c0")), \
                mock.patch.object(sys.modules[__name__], "pipeline_preflight", return_value="v2"), \
                mock.patch.object(sys.modules[__name__], "_compute", return_value=comp), \
                mock.patch.object(sys.modules[__name__], "_submit", side_effect=lambda cfg, c, *a: used.append(c)), \
                mock.patch.object(sys.modules[__name__], "wait_listed", return_value=True), \
                mock.patch.object(eval_paths, "archive_sha256", return_value="0"), \
                mock.patch.object(time, "sleep", lambda s: None), contextlib.redirect_stdout(io.StringIO()):
            launch(lcfg)
    ok(used and all(c is new_c for c in used),
       f"RC52j launch uses the replacement client after the first one closed: {[c is new_c for c in used]}")

    # The keeper loop itself, on a fake coiled: the first tick's identity is empty.
    class FakeCluster:
        adapts = []

        def __init__(self, **kw):
            self.cluster_id = 7
            self.client = FakeClient([{}, {"workers": table}])

        def adapt(self, **kw):
            FakeCluster.adapts.append(kw)

        def get_client(self):
            return self.client

    ticks = iter([True] * 6)
    fake = types.ModuleType("coiled")
    fake.list_clusters = lambda workspace: ([{"name": "x", "id": 7, "current_state": {"state": "running"}}]
                                            if next(ticks, False) else [])
    fake.Cluster = FakeCluster
    real_coiled, real_sleep = sys.modules.get("coiled"), time.sleep
    paths = (ws.KEEPER_STATUS, ws.KEEPER_LOG)
    with tempfile.TemporaryDirectory() as d:
        ws.KEEPER_STATUS, ws.KEEPER_LOG = os.path.join(d, "st.json"), os.path.join(d, "keeper.log")
        sys.modules["coiled"] = fake
        time.sleep = lambda s: None
        try:
            r = ws._keeper_loop("tok", "x", "ws", 3, 9, 1.0, {}, cluster_id=7, tick_s=0)
            log = open(ws.KEEPER_LOG).read()
            st = json.load(open(ws.KEEPER_STATUS))
        finally:
            time.sleep = real_sleep
            ws.KEEPER_STATUS, ws.KEEPER_LOG = paths
            if real_coiled is None:
                sys.modules.pop("coiled", None)
            else:
                sys.modules["coiled"] = real_coiled
    ok(r == "keeper exited" and len(FakeCluster.adapts) == 1 and "keeper error" not in log
       and st.get("workers") == 2 and "workers=2" in log and KEEPER_TICK.search(log),
       f"RC52g the keeper loop waits out an unpopulated scheduler identity without dropping its adapt(): "
       f"adapts={len(FakeCluster.adapts)} log={log[-300:]!r}")


def _self_test_51c(ok) -> None:
    """Phase 51 γ: the `health` command on a fake cluster (no Coiled, no network)."""
    import contextlib
    import io
    import tempfile
    import types
    import run_health as rh

    class FakeWS:
        def __init__(self, tagged, processing):
            self.resources = {"compute": 1} if tagged else {}
            self.processing = set(range(processing))
            self.status = types.SimpleNamespace(name="running")

    sched = types.SimpleNamespace(
        workers={"agent": FakeWS(False, 1), "c0": FakeWS(True, 24), "c1": FakeWS(True, 0)},
        unrunnable=[types.SimpleNamespace(resource_restrictions={"compute": 1}),
                    types.SimpleNamespace(resource_restrictions={"compute": 1, rh.DEMAND_RESOURCE: 1})])
    with tempfile.TemporaryDirectory() as d:
        rd = os.path.join(d, "work", "R1", "runs", "2026-10-28_x")
        os.makedirs(os.path.join(rd, "models"))
        old = "2026-10-29T06:00:00"
        with open(os.path.join(rd, "trace.jsonl"), "w") as f:
            f.write(json.dumps({"ts": old, "type": "tool_use"}) + "\n" + "{torn line\n"
                    + json.dumps({"ts": "2026-10-29T07:00:25", "type": "session_error", "session": 2,
                                  "error": "exit code 143"}) + "\n")
        with open(os.path.join(rd, "status.json"), "w") as f:
            json.dump({"heartbeat": old + "+00:00", "gate": {"round": 2}}, f)
        work = os.path.join(d, "work")

        class FakeClient:
            calls = []

            def run_on_scheduler(self, fn, *a):
                self.calls.append("scheduler")
                return fn(*a, dask_scheduler=sched)

            def run(self, fn, *a, workers, **kw):
                self.calls.append(("run", workers))
                return {workers[0]: fn(*a, work=work)}

        client = FakeClient()
        cfg = RunConfig(name="h", question_file="q", out_dir=os.path.join(d, "out"))
        os.makedirs(cfg.out_dir)
        with open(cfg.state_file, "w") as f:
            json.dump({"R1": {"worker": "agent"}}, f)
        snaps = health_snapshots(cfg, client, json.load(open(cfg.state_file)), now=rh._epoch("2026-10-29T08:00:00"))
        s = snaps["R1"]
        ok(s["scheduler"]["unrunnable_compute"] == 1 and s["scheduler"]["workers"]["c0"]["processing"] == 24
           and not s["scheduler"]["workers"]["agent"]["compute"],
           "RC51c-1 the scheduler view counts compute tasks unrunnable (not demand markers) and tags")
        ok(len(s["trace"]) == 2 and any("unparseable" in e for e in s["input_errors"]),
           "RC51c-2 the run-file view reads the trace and reports a torn line instead of dropping it")
        alerts = rh.Monitor(rh.load_thresholds()).check(s)
        ok(sorted({a["rule"] for a in alerts}) == ["session_error", "stall"],
           f"RC51c-3 a live snapshot raises session_error and stall ({sorted(a['rule'] for a in alerts)})")
        connected = []
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = health(cfg, ["--loop", "--interval", "0", "--max-polls", "2"],
                        connect_fn=lambda n: connected.append(n) or client, sleep=lambda s: None)
        log = open(os.path.join(cfg.out_dir, "health.log")).read()
        ok(rc == 0 and connected == ["h", "h"] and log.count("HEALTH session_error") == 1
           and "HEALTH monitor_error: could not read trace.jsonl: unparseable line" in log,
           "RC51c-4 health --loop logs each alert once across polls, input errors included")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = health(cfg, ["--loop", "--interval", "0", "--max-polls", "3"], connect_fn=lambda n: None,
                        sleep=lambda s: None)
        ok(rc == 0 and buf.getvalue().count("not running; poll skipped") == 3,
           "RC51c-5 a cluster that is not running is reported and the poll skipped, every poll")

        # Codex round 1, P1-1: health connects through a path that cannot create a
        # cluster (Coiled API security + a plain distributed Client), even in a stop race.
        class NoCreate(types.ModuleType):
            def __getattr__(self, k):
                raise AssertionError(f"health touched coiled.{k}")
        saved = sys.modules.get("coiled")
        sys.modules["coiled"] = NoCreate("coiled")
        made = []

        def boom(*a, **k):
            raise AssertionError("must not be called")
        try:
            got = connect_existing("h", lister=lambda: [{"name": "h", "id": 7, "current_state": {"state": "running"}}],
                                   security_fn=lambda cid: ("SEC", {"address_to_use": f"tls://10.0.0.{cid}:8786"}),
                                   client_cls=lambda addr, **kw: made.append((addr, kw.get("security"))) or "CLIENT")
            ok(got == "CLIENT" and made == [("tls://10.0.0.7:8786", "SEC")],
               "RC51c-7 connect_existing opens a plain Client on the live cluster's scheduler with its security")

            def stopping(cid):
                raise RuntimeError("Cannot get security info for cluster 7, scheduler state is stopping")
            gone = connect_existing("h", lister=lambda: [{"name": "h", "id": 7, "current_state": {"state": "stopping"}}],
                                    security_fn=boom, client_cls=boom)
            race = connect_existing("h", lister=lambda: [{"name": "h", "id": 7, "current_state": {"state": "running"}}],
                                    security_fn=stopping, client_cls=boom)
            ok(gone is None and race is None,
               "RC51c-8 a stopped cluster, or one stopping between list and connect, gives None, never a new one")
        finally:
            if saved is None:
                sys.modules.pop("coiled", None)
            else:
                sys.modules["coiled"] = saved
        import inspect as _inspect
        src = _inspect.getsource(health) + _inspect.getsource(health_snapshots) + _inspect.getsource(connect_existing)
        ok("coiled.Cluster(" not in src and "attach(" not in src, "RC51c-9 health never calls coiled.Cluster or attach")

        # Codex round 1, P1-2: transient read errors are logged and retried, never fatal.
        os.remove(cfg.state_file)
        polls = []

        def flaky(n):
            polls.append(n)
            if len(polls) == 2:
                with open(cfg.state_file, "w") as f:
                    f.write("{torn")
            if len(polls) == 3:
                with open(cfg.state_file, "w") as f:
                    json.dump({"R1": {"worker": "agent"}}, f)
            return client
        open(os.path.join(cfg.out_dir, "health.log"), "w").close()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = health(cfg, ["--loop", "--interval", "0", "--max-polls", "3"], connect_fn=flaky,
                        sleep=lambda s: None)
        log = open(os.path.join(cfg.out_dir, "health.log")).read()
        ok(rc == 0 and len(polls) == 3 and "FileNotFoundError" in log and "JSONDecodeError" in log
           and "HEALTH session_error" in log,
           "RC51c-10 a missing then torn assignments.json is a monitor error; the loop retries and recovers")
        os.remove(os.path.join(rd, "status.json"))
        with open(os.path.join(rd, "trace.jsonl"), "wb") as f:
            f.write(b"\xff\xfe not text\n")
        s3 = health_snapshots(cfg, client, {"R1": {"worker": "agent"}})["R1"]
        ok(s3["status"] is None and s3["input_errors"], "RC51c-11 an unreadable or vanished run file is an input error")

        class Broken(FakeClient):
            def run_on_scheduler(self, fn, *a):
                raise OSError("scheduler gone")
        s2 = health_snapshots(cfg, Broken(), {"R1": {"worker": "agent"}})["R1"]
        ok(s2["scheduler"] is None and any("scheduler gone" in e for e in s2["input_errors"]),
           "RC51c-6 an unreadable scheduler is an input error, not a quiet snapshot")

        class Leaky(FakeClient):
            def run_on_scheduler(self, fn, *a):
                raise OSError("x" * 140 + "token=" + "sk-ant-api03-LEAKLEAKLEAKLEAK" + " tail")
        s4 = health_snapshots(cfg, Leaky(), {"R1": {"worker": "agent"}})["R1"]
        ok(not any("sk-ant" in e or "LEAK" in e for e in s4["input_errors"]),
           f"RC51c-12 a scheduler error is redacted before it is truncated ({s4['input_errors'][0][-40:]})")


def _self_test_attach(ok) -> None:
    """RC60: one attach call (worker_side.attach_existing) for the launcher, the keeper and the
    overflow pool; one workspace name for the launcher and the VM jobs, equal to the replay backend's."""
    import importlib.util
    import types
    calls = []

    class Cl:
        def __init__(self, **kw):
            calls.append(kw)
            self.cluster_id = kw.pop("_id", 5)
            self.down = False

        def shutdown(self):
            self.down = True

    fake = types.ModuleType("coiled")
    fake.Cluster = Cl
    listed = []
    fake.list_clusters = lambda workspace: listed
    saved = sys.modules.get("coiled")
    sys.modules["coiled"] = fake
    try:
        cl = ws.attach_existing("n", "w", 5)
        ok(calls == [{"name": "n", "workspace": "w", "shutdown_on_close": False, "wait_for_workers": False}]
           and cl.cluster_id == 5, f"RC60a attach_existing attaches by name without waiting for workers: {calls}")
        made = []
        fake.Cluster = lambda **kw: made.append(Cl(**kw)) or made[-1]
        try:
            ws.attach_existing("n", "w", 9)
            ok(False, "RC60b a cluster that is not the one checked is refused")
        except SystemExit as e:
            ok(made and made[-1].down and "stopped while attaching" in str(e),
               f"RC60b a cluster that is not the one checked is shut down and refused: {e}")
        calls.clear()
        fake.Cluster = Cl
        listed[:] = [{"name": "n", "id": 5, "current_state": {"state": "stopping"}}]
        try:
            attach("n")
            ok(False, "RC60c attach refuses a stopping cluster")
        except SystemExit as e:
            ok(not calls and "not running" in str(e), f"RC60c attach never constructs a Cluster for a stopping one: {calls}")
        listed[:] = [{"name": "n", "id": 5, "current_state": {"state": "running"}}]
        cl = attach("n")
        ok(calls == [{"name": "n", "workspace": WORKSPACE, "shutdown_on_close": False, "wait_for_workers": False}]
           and cl.cluster_id == 5, f"RC60d attach checks running, then attaches with wait_for_workers=False: {calls}")
        calls.clear()
        ov = ws._Overflow({"name": "n-overflow", "id": 5}, "w")
        ov.cl = None
        Cl.get_client = lambda self: "C"
        ok(ov.attach() == (ov.cl, "C") and calls == [{"name": "n-overflow", "workspace": "w", "shutdown_on_close": False,
                                                     "wait_for_workers": False}],
           f"RC60e the keeper's overflow handle attaches through the same call: {calls}")
    finally:
        if saved is None:
            sys.modules.pop("coiled", None)
        else:
            sys.modules["coiled"] = saved
    spec = importlib.util.spec_from_file_location(
        "_rc60_replay_coiled", os.path.join(os.path.dirname(HERE), "scripts", "replay_coiled.py"))
    rq = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rq)
    # replay_coiled.py is uploaded to the workers on its own (client.upload_file), so it cannot load
    # worker_side.py at import and keeps its own copy of the name: this check keeps the two equal.
    ok(WORKSPACE == ws.WORKSPACE == rq.WORKSPACE, "RC60f launcher, worker side and replay backend name one workspace")
    _self_test_bwrap(ok)


def _fake_coiled_event(name, created=True) -> str:
    """What coiled.Cluster logs to the "coiled" logger (coiled 1.135.6, v2/cluster.py:1685 / :1688), for fakes."""
    import logging
    logging.getLogger("coiled").info(f"Creating Cluster (name: {name}, https://x ). This usually takes 1-2 minutes..."
                                     if created else f"Attaching to existing cluster (name: {name}, https://x )")
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _self_test_s18(ok, tb) -> None:
    """F6 S1-8 (no Coiled, no network). The F5 resume launch (2026-11-08): the scheduler listed the run after 43 s,
    then the laptop's client lost its connection (`OSError: Timed out trying to connect to tls://...:443 after
    30 s`); the launch raised, a watchdog read that as a failed launch and shut the clusters down, though the run
    kept going. clusters.json had not been written."""
    import contextlib
    import io
    import tempfile
    import types
    from unittest import mock
    g, M = globals(), sys.modules[__name__]
    table = {f"tls://10.0.0.{i}:1": {} for i in range(3)}
    lost = OSError("Timed out trying to connect to tls://10.1.2.3:443 after 30 s")
    listed = {"tls://10.0.0.0:1": ["run-R1-1"]}

    def fake_helper(role, name, cid, ret):
        def f(*a, record=None, **k):
            if record:
                record(role, name, cid)
            return ret
        return f

    class Client:
        def __init__(self, listing, cf):
            self.status, self.listing, self.cf, self.seen = "running", list(listing), cf, []

        def wait_for_workers(self, *a, **kw):
            pass

        def upload_file(self, *a):
            pass

        def scheduler_info(self):
            return {"workers": table}

        def run_on_scheduler(self, fn, *a):
            return None

        def processing(self, workers=None):
            self.seen.append(json.load(open(self.cf)) if os.path.exists(self.cf) else None)
            step = self.listing.pop(0) if len(self.listing) > 1 else self.listing[0]
            if isinstance(step, BaseException):
                raise step
            return step

    def go(listing, keeper=True, overflow_exc=None, tags_exc=None, keeper_exc=None):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.txt")
            open(q, "w").write("question")
            cfg = RunConfig(name="lt", question_file=q, labels=("R1",), out_dir=d, keeper=keeper,
                            overflow_max=4 if keeper else 0, pipeline="v2")
            client = Client(listing, cfg.clusters_file)
            stops = []

            def tags(c, exclude):
                if tags_exc:
                    raise tags_exc
            comp = types.SimpleNamespace(WORKER_VM_TYPES=["a"], WORKER_OPTIONS={}, ADAPT_OPTIONS={},
                                         register_compute_tags=tags)
            fake_coiled = types.ModuleType("coiled")
            made = []

            def cluster(**kw):
                made.append({"name": kw.get("name"), "id": 77, "created": _fake_coiled_event(kw.get("name")),
                             "current_state": {"state": "running"}})
                return types.SimpleNamespace(cluster_id=77, get_client=lambda: client)
            fake_coiled.Cluster = cluster
            fake_coiled.list_clusters = lambda workspace: list(made)
            P = mock.patch.object
            with mock.patch.dict(sys.modules, {"coiled": fake_coiled}), \
                    P(M, "is_running", return_value=False), P(M, "live_id", return_value=77), \
                    P(M, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                    P(M, "code_tarball", return_value=(b"", "c0")), P(M, "pipeline_preflight", return_value="v2"), \
                    P(M, "_compute", return_value=comp), P(M, "_submit", side_effect=lambda c_, cl, lab, *a, **k: (f"run-{lab}-1", types.SimpleNamespace(status="pending"))), \
                    P(M, "start_overflow", side_effect=overflow_exc or fake_helper("overflow", "lt-overflow", 78, {"id": 78})), \
                    P(M, "start_keeper", side_effect=fake_helper("keeper", "lt-keeper", 79, types.SimpleNamespace(cluster_id=79))), \
                    P(M, "confirm_keeper", side_effect=keeper_exc or (lambda *a, **k: ["tick"])), \
                    P(M, "shutdown", side_effect=lambda c_: stops.append("shutdown")), \
                    P(M, "attach", side_effect=lambda n: stops.append(n)), \
                    P(eval_paths, "archive_sha256", return_value="0"), P(time, "sleep", lambda s: None):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    try:
                        rc = launch(cfg)
                    except SystemExit as e:
                        rc = ("exit", e.code)
                    except Exception as e:  # noqa: BLE001
                        rc = ("raised", type(e).__name__)
            cl = json.load(open(cfg.clusters_file)) if os.path.exists(cfg.clusters_file) else {}
            return rc, cl, client.seen, stops, out.getvalue()

    codes = {k: g.get(k) for k in ("EXIT_LAUNCHED", "EXIT_FAILED", "EXIT_SUBMITTED_LOST", "EXIT_UNCONFIRMED",
                                   "EXIT_INCOMPLETE")}
    ok(codes == {"EXIT_LAUNCHED": 0, "EXIT_FAILED": 1, "EXIT_SUBMITTED_LOST": 3, "EXIT_UNCONFIRMED": 4,
                 "EXIT_INCOMPLETE": 5}, f"LR0 the launch exit codes are named: {codes}")
    rc, cl, seen, stops, out = go([{}, listed], overflow_exc=lost)
    ok(rc == 3 and not stops, f"LR1 replay F5: listed, then the connection drops: 'submitted, connection lost' "
                              f"(exit 3), nothing shut down: rc={rc} stops={stops}")
    ok(cl.get("status") == "submitted_connection_lost" and "do not shut" in out.lower() and "status" in out,
       f"LR1b the handover is recorded in clusters.json and says how to attach later: {cl.get('status')} {out[-300:]!r}")
    ok(seen and seen[0] and (seen[0].get("run") or {}).get("id") == 77 and seen[0].get("launch_id")
       and seen[0].get("launched_utc"), f"LR2 clusters.json names the run cluster, launch id and time before the "
                                        f"acknowledgement wait: {seen[:1]}")
    rc, cl, seen, stops, out = go([lost])
    ok(rc == 4 and not stops and cl.get("status") == "submitted_unconfirmed",
       f"LR3 the connection drops before the scheduler lists the run: 'submitted, not confirmed' (exit 4), nothing "
       f"shut down: rc={rc} {cl.get('status')} stops={stops}")
    rc, cl, seen, stops, out = go([{}], keeper=False)
    ok(rc == 4 and not stops, f"LR3b connected, but never listed within the bound: exit 4, nothing shut down: rc={rc}")
    rc, cl, seen, stops, out = go([listed], tags_exc=RuntimeError("tags"))
    ok(isinstance(rc, tuple) and (cl.get("run") or {}).get("id") == 77 and cl.get("status") == "failed",
       f"LR4 a failure before any submission is a failed launch, and clusters.json names what to stop by id: "
       f"rc={rc} {cl}")
    rc, cl, seen, stops, out = go([listed], keeper_exc=SystemExit("keeper not confirmed"))
    ok(rc == 5 and not stops and cl.get("status") == "submitted_launch_incomplete",
       f"LR5 acknowledged, then a later step fails: 'submitted, launch incomplete' (exit 5), not a failed launch: "
       f"rc={rc} {cl.get('status')}")
    rc, cl, seen, stops, out = go([{}, {}, listed])
    ok(rc == 0 and cl.get("status") == "launched" and (cl.get("keeper") or {}).get("id") == 79
       and (cl.get("overflow") or {}).get("id") == 78, f"LR6 a clean launch exits 0 with every cluster recorded: rc={rc} {cl}")
    doc = __doc__ or ""
    ok("Exit codes" in doc and all(f"  {n} " in doc for n in (0, 1, 3, 4, 5)) and "connection lost" in doc,
       "LR7 the module doc lists the launch exit codes for an external watchdog")
    with tempfile.TemporaryDirectory() as d:
        cf = os.path.join(d, "c.py")
        open(cf, "w").write("from run_cluster import RunConfig\nCONFIG = RunConfig(name='z', question_file='q')\n")
        with mock.patch.object(M, "launch", return_value=3):
            ok(main(["x", cf, "launch"]) == 3, "LR8 main returns launch's exit code")
    # the real _submit hands back the key the scheduler will list
    sub = []
    fc = types.SimpleNamespace(submit=lambda *a, **kw: sub.append(kw) or "fut")
    with mock.patch.dict(sys.modules, {"distributed": types.SimpleNamespace(fire_and_forget=lambda f: None)}):
        cfg = RunConfig(name="z", question_file="q", pipeline="v2")
        got = _submit(cfg, fc, "R1", "w", b"", "q", {"ANTHROPIC_API_KEY": "k"}, "L")
    key = got[0] if isinstance(got, tuple) else got
    ok(sub and isinstance(got, tuple) and got[1] == "fut" and key == sub[0].get("key") and str(key).startswith("run-R1-"),
       f"LR9 _submit returns its task key and the future it holds (P2-7): {got}")
    wl = g.get("wait_listed")
    if wl:
        sl = []
        c = Client([{}, {}, listed], "/nonexistent/clusters.json")
        ok(wl(c, "run-R1-1", attempts=5, delay_s=5.0, sleep=sl.append) is True and sl == [5.0, 5.0],
           f"LR10 wait_listed polls the scheduler until it lists the key: {sl}")
        ok(wl(Client([{}], "/nonexistent/clusters.json"), "run-R1-1", attempts=3, delay_s=0, sleep=sl.append) is False,
           "LR10b a key never listed within the bound is not acknowledged")
        try:
            wl(Client([lost], "/nonexistent/clusters.json"), "run-R1-1", attempts=3, delay_s=0, sleep=sl.append)
            ok(False, "LR10c a lost connection while waiting is raised to the caller")
        except OSError:
            ok(True, "LR10c a lost connection while waiting is raised to the caller")
    else:
        ok(False, "LR10 wait_listed exists")
    # attach later: start the keeper for the recorded run cluster
    kl = g.get("keeper_later")
    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="lt", question_file="q", out_dir=d, pipeline="v2", overflow_max=0)
        json.dump({"run": {"name": "lt", "id": 77}, "launch_id": "L", "status": "submitted_connection_lost"},
                  open(cfg.clusters_file, "w"))
        P = mock.patch.object
        refused = None
        with P(M, "_compute", return_value=types.SimpleNamespace(ADAPT_OPTIONS={}, WORKER_VM_TYPES=[])), \
                P(M, "start_keeper", side_effect=fake_helper("keeper", "lt-keeper", 79, types.SimpleNamespace(cluster_id=79))) as sk, \
                P(M, "confirm_keeper", return_value=["tick"]), P(time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(io.StringIO()):
            with P(M, "live_id", side_effect=lambda n: 77 if n == "lt" else None):
                rc = kl(cfg) if kl else None
            cl = json.load(open(cfg.clusters_file))
            started = sk.call_args[0][2] if sk.call_args else None
            with P(M, "live_id", return_value=99):
                try:
                    kl(cfg) if kl else None
                except SystemExit as e:
                    refused = str(e)
    ok(rc == 0 and (cl.get("keeper") or {}).get("id") == 79 and started == 77,
       f"LR11 `keeper` starts the keeper for the recorded run cluster later: rc={rc} {cl}")
    ok(refused and "77" in refused, f"LR11b `keeper` refuses when the live run cluster is not the recorded one: {refused}")
    # would this reuse? the shipped compute.py answers, from the exact tarball
    rcheck = g.get("reuse_check")
    stub = (b"import json, os, sys\nprint('noise')\nprint(json.dumps({'argv': sys.argv[1:], 'here': __file__, "
            b"'stored': 5, 'reusable': 4}))\n")
    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="z", question_file="q", pipeline="v2")
        r = rcheck(cfg, d, code=tb({"scripts/compute.py": stub})) if rcheck else {}
    ok(r.get("stored") == 5 and r.get("reusable") == 4 and r.get("argv", [None])[:2] == ["reuse-check", d]
       and not str(r.get("here", "")).startswith(os.path.dirname(HERE)),
       f"LR12 reuse_check runs the tarball's own compute.py reuse-check on the run dir: {r}")


def _self_test_s18_r1(ok) -> None:
    """Codex round 1 on #161 (no Coiled, no network): every post-submit path of launch and relaunch ends in a
    do-not-shut-down code (P1-1, P1-2); exit 1 never points at a previous launch's record (P1-3); a per-config lock and
    record ownership (P2-4); helper ids recorded at construction (P2-5); every assignment before the first submit
    (P2-6); a fast or queued run is acknowledged (P2-7)."""
    import contextlib
    import fcntl
    import io
    import tempfile
    import types
    from unittest import mock
    M, P = sys.modules[__name__], mock.patch.object
    table = {f"tls://10.0.0.{i}:1": {} for i in range(3)}
    lost = OSError("Timed out trying to connect to tls://10.1.2.3:443 after 30 s")

    class Client:
        def __init__(self, listing=None, state=None):
            self.status, self.listing, self.state = "running", list(listing or [{}]), state

        def wait_for_workers(self, *a, **kw):
            pass

        def upload_file(self, *a):
            pass

        def scheduler_info(self):
            return {"workers": table}

        def run_on_scheduler(self, fn, *a):
            return self.state

        def processing(self, workers=None):
            step = self.listing.pop(0) if len(self.listing) > 1 else self.listing[0]
            if isinstance(step, BaseException):
                raise step
            return step

    def lists(*labels):
        return {"tls://10.0.0.0:1": [f"run-{x}-1" for x in labels]}

    def go(listing=None, labels=("R1",), keeper=False, fut_status="pending", state=None, writes_fail=(),
           live_exc=None, overflow_exc=None, pre=None, running=False, preflight_exc=None, real_overflow=False,
           hold_lock=False, env=None):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.txt")
            open(q, "w").write("question")
            cfg = RunConfig(name="lt", question_file=q, labels=labels, out_dir=d, keeper=keeper,
                            overflow_max=4 if keeper else 0, pipeline="v2")
            if pre is not None:
                json.dump(pre, open(cfg.clusters_file, "w"))
            client = Client(listing if listing is not None else [lists(*labels)], state)
            real_write = g.get("write_clusters")

            def write(cfg_, cl, *a, **kw):
                if cl.get("status") in writes_fail:
                    raise OSError(f"disk full writing {cl.get('status')}")
                return real_write(cfg_, cl, *a, **kw)

            made = []

            def cluster(**kw):
                cl = types.SimpleNamespace(cluster_id=78 if kw.get("name", "").endswith("-overflow") else 77,
                                           get_client=lambda: client)
                made.append({"name": kw.get("name"), "id": cl.cluster_id, "created": _fake_coiled_event(kw.get("name")),
                             "current_state": {"state": "running"}})
                if kw.get("name", "").endswith("-overflow"):
                    def boom(*a):
                        raise RuntimeError("upload failed")
                    cl.get_client = lambda: types.SimpleNamespace(upload_file=boom)
                return cl
            fake_coiled = types.ModuleType("coiled")
            fake_coiled.Cluster = cluster
            fake_coiled.list_clusters = lambda workspace: list(made)
            comp = types.SimpleNamespace(WORKER_VM_TYPES=["a"], WORKER_OPTIONS={}, ADAPT_OPTIONS={},
                                         register_compute_tags=lambda c, exclude: None)
            pats = [P(M, "is_running", return_value=running),
                    P(M, "live_id", side_effect=live_exc or (lambda n: 77)),
                    P(M, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}),
                    P(M, "code_tarball", return_value=(b"", "c0")),
                    P(M, "pipeline_preflight", side_effect=preflight_exc or (lambda *a: "v2")),
                    P(M, "_compute", return_value=comp),
                    P(M, "_submit", side_effect=lambda c_, cl, lab, *a, **k: (f"run-{lab}-1",
                                                                         types.SimpleNamespace(status=fut_status))),
                    P(M, "start_keeper", return_value=types.SimpleNamespace(cluster_id=79)),
                    P(M, "confirm_keeper", return_value=["tick"]),
                    P(eval_paths, "archive_sha256", return_value="0"), P(time, "sleep", lambda s: None)]
            if not real_overflow:
                pats.append(P(M, "start_overflow", side_effect=overflow_exc or (lambda *a, **k: {"id": 78})))
            if real_write:
                pats.append(P(M, "write_clusters", side_effect=write))
            lock = None
            if hold_lock:
                lock = open(os.path.join(d, ".run_cluster.lock"), "a")
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            out = io.StringIO()
            with contextlib.ExitStack() as es:
                es.enter_context(mock.patch.dict(sys.modules, {"coiled": fake_coiled}))
                es.enter_context(mock.patch.dict(os.environ, env or {}))
                for p_ in pats:
                    es.enter_context(p_)
                es.enter_context(contextlib.redirect_stdout(out))
                try:
                    rc = launch(cfg)
                except SystemExit as e:
                    rc = ("exit", e.code)
                except BaseException as e:  # noqa: BLE001
                    rc = ("raised", type(e).__name__)
            if lock:
                lock.close()
            cl = json.load(open(cfg.clusters_file)) if os.path.exists(cfg.clusters_file) else {}
            asg = json.load(open(cfg.state_file)) if os.path.exists(cfg.state_file) else {}
            return rc, cl, asg, out.getvalue()

    g = globals()
    # P1-1: the whole post-submit path
    rc, cl, _, out = go(writes_fail=("submitted", "submitted_connection_lost", "submitted_unconfirmed",
                                     "submitted_launch_incomplete"))
    ok(rc in (3, 5), f"R1a a record write that fails after the submission still exits 3 or 5, never raises: {rc}")
    rc, cl, _, out = go(writes_fail=("launched",))
    ok(rc == 0, f"R1b the final record write is best effort after a confirmed launch: {rc}")
    rc, cl, _, out = go(keeper=True, overflow_exc=lost, live_exc=KeyboardInterrupt)
    ok(rc == 3, f"R1c an interrupt during the handover's own reporting keeps the safe code: {rc}")
    rc, cl, _, out = go(listing=[KeyboardInterrupt()])
    ok(rc == 4, f"R1d an interrupt while waiting for the listing is 'submitted, not confirmed': {rc}")
    # P1-3: exit 1 never points at an older record
    old = {"run": {"name": "lt", "id": 5}, "launch_id": "old", "invocation": "old-token", "status": "launched"}
    for kw, what in ((dict(running=True), "already running"), (dict(preflight_exc=SystemExit("no pipeline")), "preflight"),
                     (dict(hold_lock=True), "lock held")):
        rc, cl, _, out = go(pre=old, **kw)
        ok(isinstance(rc, tuple) and cl == old and "invocation " in out and "old-token" not in out.split("invocation ")[-1][:40],
           f"R3 a refusal ({what}) leaves the earlier record untouched and prints its own invocation token: {rc} {cl}")
    rc, cl, _, out = go(env={"MA_LAUNCH_INVOCATION": "wd-123"})
    ok(rc == 0 and cl.get("invocation") == "wd-123", f"R3b the record carries the watchdog's invocation token: {cl}")
    # P2-4: record ownership
    wc = g.get("write_clusters")
    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="lt", question_file="q", out_dir=d, pipeline="v2")
        json.dump({"invocation": "B", "status": "submitting"}, open(cfg.clusters_file, "w"))
        try:
            wc(cfg, {"invocation": "A", "status": "submitted"}) if wc else None
            refused = False
        except Exception:  # noqa: BLE001
            refused = True
        ok(refused and json.load(open(cfg.clusters_file))["invocation"] == "B",
           "R4 a record owned by another invocation is never replaced")
        ok(not [f for f in os.listdir(d) if f.endswith(".tmp")], "R4b no temporary record file is left behind")
    # P2-5: a helper id is recorded right after its constructor
    rc, cl, _, out = go(keeper=True, real_overflow=True)
    ok(rc == 5 and (cl.get("overflow") or {}).get("id") == 78,
       f"R5 an overflow pool whose upload fails is still in clusters.json: rc={rc} {cl.get('overflow')}")
    # P2-6: every assignment before the first submission
    rc, cl, asg, out = go(labels=("R1", "R2"), listing=[{}])
    ok(rc == 4 and set(asg) == {"R1", "R2"} and asg["R2"].get("submission") == "pending"
       and asg["R1"].get("submission") == "sent" and "relaunch" in out and "R2" in out,
       f"R6 an unconfirmed R1 leaves R2 assigned and pending, so `relaunch R2` can run: rc={rc} "
       f"{ {k: v.get('submission') for k, v in asg.items()} }")
    rc, cl, asg, out = go(labels=("R1", "R2"), listing=[lists("R1", "R2")])
    ok(rc == 0 and all(v.get("submission") == "listed" and str(v.get("task_key", "")).startswith(f"run-{k}-")
                       for k, v in asg.items()), f"R6b both listed, each with its task key: {rc} {asg}")
    # P2-7: a run that finished between polls, and one the scheduler queued
    rc, cl, _, out = go(listing=[{}], fut_status="finished")
    ok(rc == 0, f"R7 a run that finished before the first poll is acknowledged by its future: {rc}")
    rc, cl, _, out = go(listing=[{}], state="queued")
    ok(rc == 0, f"R7b a run the scheduler holds as queued is acknowledged: {rc}")
    why = []
    wl = g.get("wait_listed")
    try:
        got = wl(Client([{}], None), "k", types.SimpleNamespace(status="finished"), attempts=1, why=why)
    except TypeError as e:
        got = e
    ok(got is True and why == ["future finished"], f"R7d wait_listed reports what acknowledged the run: {got} {why}")
    rc, cl, _, out = go(listing=[{}], state="released")
    ok(rc == 4, f"R7c a released key is not an acknowledgement: {rc}")

    # P1-2: relaunch, same boundary and guards
    def re(submit=None, listing=None, state=None, rec=None, vm="none", sched_exc=None, calls=None):
        with tempfile.TemporaryDirectory() as d:
            q = os.path.join(d, "q.txt")
            open(q, "w").write("question")
            cfg = RunConfig(name="lt", question_file=q, labels=("R1", "R2"), out_dir=d, pipeline="v2", tree=d)
            json.dump({x: dict({"worker": "tls://10.0.0.0:1", "launch_id": "L", "submission": "pending"},
                               **(rec or {})) for x in cfg.labels}, open(cfg.state_file, "w"))
            client = Client(listing or [lists("R2")], state)
            if sched_exc:
                def rs_(fn, *a):
                    raise sched_exc
                client.run_on_scheduler = rs_
            sub = submit or (lambda c_, cl, lab, *a, **k: (k.get("key") or f"run-{lab}-1",
                                                           types.SimpleNamespace(status="pending")))

            def submit_(*a, **k):
                if calls is not None:
                    calls.append(json.load(open(cfg.state_file))["R2"].get("task_key") == k.get("key") and k.get("key"))
                return sub(*a, **k)
            rs = types.ModuleType("results_store")
            rs.list_blobs = lambda *a: []
            out = io.StringIO()
            with mock.patch.dict(sys.modules, {"results_store": rs}), \
                    P(M, "attach", return_value=types.SimpleNamespace(get_client=lambda: client)), \
                    P(M, "_on_worker", return_value={"state": vm}), \
                    P(M, "code_tarball", return_value=(b"", "c0")), P(M, "pipeline_preflight", return_value="v2"), \
                    P(M, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                    P(M, "_submit", side_effect=submit_), P(M, "_task_key", side_effect=lambda lab: f"run-{lab}-1"), \
                    P(eval_paths, "archive_sha256", return_value="0"), P(time, "sleep", lambda s: None), \
                    contextlib.redirect_stdout(out):
                try:
                    return relaunch(cfg, "R2")
                except SystemExit as e:
                    return ("exit", e.code)
                except BaseException as e:  # noqa: BLE001
                    return ("raised", type(e).__name__)

    def accepted_then_raise(*a, **k):
        raise RuntimeError("submit raised after the scheduler accepted it")
    ok(re() == 0, f"R2a relaunch of a pending label, listed: exit 0 ({re()})")
    ok(re(submit=accepted_then_raise) == 4, f"R2b relaunch: _submit raising is 'not confirmed', never 1: {re(submit=accepted_then_raise)}")
    ok(re(listing=[KeyError("workers")]) == 4, f"R2c relaunch: a non-connection error while waiting is 4: "
                                                f"{re(listing=[KeyError('workers')])}")
    ok(re(listing=[KeyboardInterrupt()]) == 4, "R2d relaunch: an interrupt after the submission is 4")
    # Codex round 2, P2-2: the exact task key is recorded before the submission, and a task the scheduler still holds
    # (queued, or preparing: no run.log yet, so the VM reads "missing") is never submitted a second time
    calls = []
    r = re(calls=calls)
    ok(r == 0 and calls and calls[0], f"R8 relaunch records the exact task key before it submits: {r} {calls}")
    sent = {"submission": "sent", "task_key": "run-R2-0"}
    for st_ in ("processing", "queued", "waiting"):
        calls = []
        r = re(rec=sent, vm="missing", state=st_, calls=calls)
        ok(isinstance(r, tuple) and "run-R2-0" in str(r[1]) and not calls,
           f"R8b a task still {st_} on the scheduler (no run.log yet) is not relaunched: {r} {calls}")
    calls = []
    r = re(rec=sent, vm="missing", state=None, calls=calls, listing=[{}, lists("R2")])
    ok(r != ("exit", None) and calls, f"R8c a task the scheduler no longer holds may be relaunched: {r} {calls}")
    calls = []
    r = re(rec=sent, vm="missing", sched_exc=OSError("lost"), calls=calls)
    ok(isinstance(r, tuple) and not calls, f"R8d the original task's state cannot be read: refused, not relaunched: {r}")
    calls = []
    r = re(rec={"submission": "sent"}, vm="missing", calls=calls)
    ok(isinstance(r, tuple) and not calls, f"R8e a 'sent' assignment with no recorded key is refused: {r}")


def _self_test_s18_r2(ok) -> None:
    """Codex round 2 on #161, P2-1: `keeper` reconciles the recorded helpers first. A live helper with the recorded id
    belongs to this launch and is kept (the keeper only once it is confirmed holding the pool); a live helper with
    another id is an earlier launch's and is stopped by that id; a missing one is started."""
    import contextlib
    import io
    import tempfile
    import types
    from unittest import mock
    M, P = sys.modules[__name__], mock.patch.object

    def go(rec, live, confirm_ok=True):
        with tempfile.TemporaryDirectory() as d:
            cfg = RunConfig(name="lt", question_file="q", out_dir=d, pipeline="v2", overflow_max=4)
            json.dump(dict({"run": {"name": "lt", "id": 77}, "launch_id": "L", "invocation": "I",
                            "status": "submitted_launch_incomplete"}, **rec), open(cfg.clusters_file, "w"))
            calls = {"stopped": [], "overflow": 0, "keeper": [], "fresh": []}

            def so(cfg_, comp, record=None, **k):
                calls["overflow"] += 1
                record("overflow", cfg.overflow_name, 88)
                return {"name": cfg.overflow_name, "id": 88}

            def sk(cfg_, ao, cid, overflow=None, record=None, **k):
                calls["keeper"].append((cid, (overflow or {}).get("id")))
                record("keeper", cfg.keeper_name, 89)
                return types.SimpleNamespace(cluster_id=89)

            def conf(name, kc, **k):
                calls.setdefault("conf", []).append({x: k.get(x) for x in ("run_id", "overflow_id")})
                if not confirm_ok and getattr(kc, "cluster_id", None) != 89:
                    raise SystemExit("not confirmed")
                return ["tick"]
            out = io.StringIO()
            with P(M, "_compute", return_value=types.SimpleNamespace(ADAPT_OPTIONS={}, WORKER_VM_TYPES=["a"])), \
                    P(M, "live_id", side_effect=lambda n: live.get(n)), \
                    P(M, "start_overflow", side_effect=so), P(M, "start_keeper", side_effect=sk), \
                    P(M, "confirm_keeper", side_effect=conf), \
                    P(ws, "attach_existing", side_effect=lambda n, w, cid: types.SimpleNamespace(cluster_id=cid)), \
                    P(M, "_stop_id", side_effect=lambda n, cid: calls["stopped"].append(cid), create=True), \
                    P(M, "_fresh", side_effect=lambda n: calls["fresh"].append(n)), \
                    P(time, "sleep", lambda s: None), contextlib.redirect_stdout(out):
                try:
                    rc = keeper_later(cfg)
                except SystemExit as e:
                    rc = ("exit", e.code)
            cl = json.load(open(cfg.clusters_file))
            return rc, calls, cl

    both = {"overflow": {"name": "lt-overflow", "id": 78}, "keeper": {"name": "lt-keeper", "id": 79}}
    rc, calls, cl = go(both, {"lt": 77, "lt-overflow": 78, "lt-keeper": 79})
    ok(rc == 0 and not calls["stopped"] and calls["overflow"] == 0 and not calls["keeper"]
       and cl["overflow"]["id"] == 78 and cl["keeper"]["id"] == 79,
       f"R9 healthy helpers of this launch are kept, nothing stopped or recreated: {rc} {calls}")
    rc, calls, cl = go({"overflow": both["overflow"]}, {"lt": 77, "lt-overflow": 78})
    ok(rc == 0 and not calls["stopped"] and calls["overflow"] == 0 and calls["keeper"] == [(77, 78)],
       f"R9b a kept overflow pool is handed to the new keeper: {rc} {calls}")
    rc, calls, cl = go(both, {"lt": 77, "lt-overflow": 78, "lt-keeper": 95})
    ok(rc == 0 and calls["stopped"] == [95] and calls["overflow"] == 0 and calls["keeper"] == [(77, 78)],
       f"R9c a live keeper with another id is an earlier launch's: stopped by that id, then started: {rc} {calls}")
    rc, calls, cl = go(both, {"lt": 77, "lt-overflow": 78, "lt-keeper": 79}, confirm_ok=False)
    ok(rc == 0 and calls["stopped"] == [79] and calls["keeper"] == [(77, 78)],
       f"R9d this launch's keeper that is not holding the pool is stopped by id and restarted: {rc} {calls}")
    rc, calls, cl = go(both, {"lt": 77, "lt-overflow": 78, "lt-keeper": 79})
    ok((calls.get("conf") or [{}])[0] == {"run_id": 77, "overflow_id": 78},
       f"R9e (round 3, P2-1) the kept keeper is confirmed against this run cluster and overflow pool: {calls.get('conf')}")
    rc, calls, cl = go(both, {"lt": 77, "lt-keeper": 79})
    ok(rc == 0 and calls["stopped"] == [79] and calls["overflow"] == 1 and calls["keeper"] == [(77, 88)]
       and cl["keeper"]["id"] == 89 and cl["overflow"]["id"] == 88,
       f"R9f (round 3, P2-1) the overflow pool is gone but the keeper lives: a new pool, and the keeper (still aimed at "
       f"the old pool) replaced: {rc} {calls}")


def _self_test_s18_r3(ok) -> None:
    """Codex round 3 on #161: a keeper is healthy only with a fresh heartbeat for this run and overflow pool (P2-2);
    every self-test entry refuses the real coiled (P2-3); the smoke watch is bounded by the wall clock, bounds every
    API call and stops only after the launcher has exited and every cluster of the config's names is down (P2-4)."""
    import types
    g, M = globals(), sys.modules[__name__]
    kf = g.get("keeper_fresh")
    tick = "12:00:00 keeper holding lt 2..3 {}\n12:01:00 workers=2 (overflow 0) refusals=0 active=primary"
    st = {"updated": 1000.0, "cluster_id": 77, "overflow": {"id": 78}}
    cases = [("fresh", dict(status=st, now=1060.0, log=tick), True),
             ("stale", dict(status=st, now=1000.0 + 3600, log=tick), False),
             ("another run cluster", dict(status=dict(st, cluster_id=5), now=1060.0, log=tick), False),
             ("another overflow pool", dict(status=dict(st, overflow={"id": 5}), now=1060.0, log=tick), False),
             ("an error after the tick", dict(status=st, now=1060.0, log=tick + "\n12:01:30 keeper error OSError: x; retrying"), False),
             ("exited after the tick", dict(status=st, now=1060.0, log=tick + "\n12:02:00 lt is not running; keeper exiting"), False),
             ("no tick", dict(status=st, now=1060.0, log="12:00:00 keeper holding lt 2..3 {}"), False),
             ("no status", dict(status=None, now=1060.0, log=tick), False)]
    for what, h, want in cases:
        got = kf(h, run_id=77, overflow_id=78) if kf else (None, "")
        ok(got[0] is want, f"K1 keeper_fresh, {what}: {got}")
    # confirm_keeper reads the heartbeat through the keeper's own worker and checks the ids
    table = {"tls://k:1": {}}

    class C:
        status = "running"

        def __init__(self, h):
            self.h = h

        def scheduler_info(self):
            return {"workers": table}

        def run(self, fn, workers):
            return {w: (self.h if getattr(fn, "__name__", "") == "keeper_health" else self.h["log"]) for w in workers}
    for h, want in ((dict(status=st, now=1060.0, log=tick), True), (dict(status=dict(st, overflow=None), now=1060.0, log=tick), False)):
        kc = types.SimpleNamespace(cluster_id=79, get_client=lambda h=h: C(h))
        try:
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()):
                confirm_keeper("lt-keeper", kc, attempts=1, delay_s=0, sleep=lambda s: None, run_id=77, overflow_id=78)
            got = True
        except (SystemExit, TypeError):
            got = False
        ok(got is want, f"K2 confirm_keeper needs a fresh heartbeat for run 77 / overflow 78: want {want}, got {got}")
    # one refusing guard for every self-test entry that can reach the cloud
    guard = None
    try:
        import no_cloud as guard
    except ImportError:
        pass
    ok(guard is not None and guard.active(), "G1 run_cluster's self-test runs under the shared refusing guard (cloud/no_cloud.py)")
    refused = False
    if guard is not None and guard.active():                       # never probed without the guard: unguarded, this would be a real call
        try:
            import coiled
            coiled.list_clusters(workspace="w")
        except RuntimeError:
            refused = True
    ok(refused, "G1b a real coiled call from a run_cluster self-test is refused")
    # the smoke watch: ownership is this invocation's construction journal (ids it created; a construction it began
    # and never finished: clusters of that name created after it began); at the limit the launcher goes first
    import datetime as _dt
    sw = g.get("smoke_watch")
    cfg = RunConfig(name="lt", question_file="q", pipeline="v2")
    T0 = 1_800_000_000.0                                    # the invocation's constructions began here (epoch)

    def iso(t):
        return _dt.datetime.fromtimestamp(t, _dt.timezone.utc).isoformat()

    def cl_(name, cid, created=T0 + 30):
        return {"name": name, "id": cid, "created": iso(created), "current_state": {"state": "running"}}
    journal = [{"inv": "INV", "name": "lt", "t": T0, "status": "constructing", "seq": 1},
               {"inv": "INV", "name": "lt", "id": 77, "status": "created", "seq": 1},
               {"inv": "INV", "name": "lt-keeper", "t": T0 + 60, "status": "constructing", "seq": 2},
               {"inv": "INV", "name": "lt-keeper", "id": 79, "status": "created", "seq": 2}]

    class World:
        def __init__(self, script, launcher_alive_until):
            self.t, self.script, self.until, self.stopped, self.events = 0.0, script, launcher_alive_until, [], []

        def clock(self):
            return self.t

        def sleep(self, s):
            self.t += s

        def lister(self):
            self.t += 7.0                       # each listing takes time: the bound is the clock, not a count
            return [c for t0, c in self.script if t0 <= self.t and c["id"] not in self.stopped]

        def stop(self, name, cid):
            self.events.append(("stop", cid))
            self.stopped.append(cid)

        def alive(self):
            return self.t < self.until

        def terminate(self):
            self.events.append(("terminate", None))
            self.until = -1
    run, keeper = cl_("lt", 77), cl_("lt-keeper", 79, T0 + 70)
    theirs = cl_("lt", 66, created=T0 + 40)          # same name, created by another invocation (another machine)
    if sw:
        class W1(World):
            def lister(self):
                self.t += 7.0
                out = [theirs]
                if self.t < 200:
                    out.append(run)
                if 150 <= self.t < 400:
                    out.append(keeper)
                return out
        w = W1([], 100)
        r = sw(cfg, "INV", w.alive, minutes=20, lister=w.lister, stopper=w.stop, clock=w.clock, sleep=w.sleep,
               every_s=30, terminate=w.terminate, journal=journal)
        ok(r.get("clean") and not w.stopped and w.t >= 400,
           f"W1 the watch waits for a late keeper to stop by itself, then ends clean: t={w.t} {r}")
        w = World([(0, run), (0, keeper), (0, theirs)], 10 ** 9)
        r = sw(cfg, "INV", w.alive, minutes=20, lister=w.lister, stopper=w.stop, clock=w.clock, sleep=w.sleep,
               every_s=30, terminate=w.terminate, journal=journal)
        ok(not r.get("clean") and sorted(w.stopped) == [77, 79] and 1200 <= w.t <= 1200 + 300,
           f"W2 at the wall-clock limit it stops by id every cluster this invocation created, nothing else: "
           f"t={w.t} stopped={w.stopped} {r}")
        ok(w.events[:1] == [("terminate", None)], f"W4 at the limit the launcher is terminated before the sweep: "
                                                  f"{w.events}")
        w = World([(0, theirs)], 10 ** 9)
        r = sw(cfg, "INV", w.alive, minutes=20, lister=w.lister, stopper=w.stop, clock=w.clock, sleep=w.sleep,
               every_s=30, terminate=w.terminate, journal=journal)
        ok(not w.stopped and ("terminate", None) in w.events,
           f"W4b another invocation's cluster of the same name is never stopped: {w.events}")
        # a construction interrupted before its outcome was journaled: clusters of that name created after it began
        pend = journal + [{"inv": "INV", "name": "lt-overflow", "t": T0 + 100, "status": "constructing", "seq": 3}]
        late, old = cl_("lt-overflow", 80, created=T0 + 110), cl_("lt-overflow", 81, created=T0 - 600)
        w = World([(0, late), (0, old)], 10 ** 9)
        r = sw(cfg, "INV", w.alive, minutes=20, lister=w.lister, stopper=w.stop, clock=w.clock, sleep=w.sleep,
               every_s=30, terminate=w.terminate, journal=pend)
        ok(w.stopped == [80], f"W7 an unfinished construction owns only a cluster of its name created after it began: "
                              f"{w.stopped}")
        att = journal + [{"inv": "INV", "name": "lt-overflow", "t": T0 + 100, "status": "constructing", "seq": 3},
                         {"inv": "INV", "name": "lt-overflow", "id": 80, "status": "attached", "seq": 3}]
        w = World([(0, late)], 10 ** 9)
        sw(cfg, "INV", w.alive, minutes=20, lister=w.lister, stopper=w.stop, clock=w.clock, sleep=w.sleep, every_s=30,
           terminate=w.terminate, journal=att)
        ok(not w.stopped, f"W7b a cluster this invocation attached to (not created) is never stopped: {w.stopped}")

        def hang():
            import time as _t
            _t.sleep(5)
            return []
        import time as _time
        t0 = _time.monotonic()
        r = sw(cfg, "INV", lambda: False, minutes=0.01, lister=hang, stopper=lambda n, c: None, every_s=0.1,
               call_timeout_s=0.5, terminate=lambda: None, journal=[])
        ok(_time.monotonic() - t0 < 4, f"W3 a hung API call is bounded by its timeout: {_time.monotonic() - t0:.1f}s {r}")
    else:
        ok(False, "W1 smoke_watch exists")
    # smoke-run owns the launcher process: it can interrupt it on a line, and terminates and reaps it at the limit
    srun = g.get("smoke_run")
    if srun:
        import io
        import contextlib
        script = "import time, sys; print('listed S1 (run-S1-1) by the scheduler', flush=True); time.sleep(60)"
        t0 = _time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()):
            r = srun(cfg, "INV", [sys.executable, "-c", script], minutes=0.05, interrupt_on="listed S1",
                     lister=lambda: [], stopper=lambda n, c: None, every_s=0.1, journal=[])
        ok(r.get("launcher_rc") is not None and r.get("interrupted") and _time.monotonic() - t0 < 20,
           f"W5 smoke-run interrupts the launcher on its line and reaps it: {r}")
        with contextlib.redirect_stdout(io.StringIO()):
            r = srun(cfg, "INV", [sys.executable, "-c", "import time; time.sleep(60)"], minutes=0.02,
                     lister=lambda: [], stopper=lambda n, c: None, every_s=0.1, journal=[])
        ok(r.get("launcher_rc") is not None and not r.get("clean") and _time.monotonic() - t0 < 40,
           f"W6 (round 4) a launcher still running at the limit is terminated and reaped: {r}")
    else:
        ok(False, "W5 smoke_run exists")
    # the job's cloud-side lifetime: the run's wall time and the hold for collection come from the config
    sub = []
    fc = types.SimpleNamespace(submit=lambda *a, **kw: sub.append((a, kw)) or "fut")
    from unittest import mock
    with mock.patch.dict(sys.modules, {"distributed": types.SimpleNamespace(fire_and_forget=lambda f: None)}):
        try:
            c2 = RunConfig(name="z", question_file="q", pipeline="v2", run_timeout_hours=0.25, hold_hours=0.1)
            _submit(c2, fc, "R1", "w", b"", "q", {"ANTHROPIC_API_KEY": "k"}, "L")
            got = sub[0][1].get("run_timeout_s"), sub[0][1].get("hold_s")
        except TypeError as e:
            got = e
    ok(got == (900.0, 360.0), f"T1 the run's time limit and collection hold reach run_to_disk: {got}")
    rm = getattr(ws, "_run_main", None)
    if rm:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            t0 = _time.monotonic()
            with open(os.path.join(d, "log"), "w") as log:
                rc_ = rm([sys.executable, "-c", "import time; time.sleep(30)"], d, dict(os.environ), log, timeout_s=1)
            ok(rc_ == "timeout" and _time.monotonic() - t0 < 10, f"T2 main.py past its time limit is killed: {rc_}")
    else:
        ok(False, "T2 worker_side._run_main exists")
    hold = getattr(ws, "_hold", None)
    clk = [0.0]
    if hold:
        with tempfile.TemporaryDirectory() as d:
            hold(d, 600, sleep=lambda s: clk.__setitem__(0, clk[0] + s), clock=lambda: clk[0])
        ok(600 <= clk[0] <= 630, f"T3 the hold for collection ends at its bound: {clk[0]}")
    else:
        ok(False, "T3 worker_side._hold exists")


def _self_test_s18_r4(ok) -> None:
    """Codex round 4 on #161, P1, as rebuilt after the owner's read-only check (2026-11-09: coiled 1.135.6 lists no
    tags). A cluster is this invocation's only when (a) coiled.Cluster logged that it created it ("Creating Cluster
    (name: ...)", coiled/v2/cluster.py:1685; "Attaching to existing cluster", :1688, means attached), (b) no live
    cluster of that name was listed just before the constructor, and (c) its `created` time is no earlier than this
    invocation's start less 120 s. Two launchers race on one config name on two machines."""
    import contextlib
    import datetime as _dt
    import io
    import tempfile
    import types
    from unittest import mock
    M, P, g = sys.modules[__name__], mock.patch.object, globals()
    table = {f"tls://10.0.0.{i}:1": {} for i in range(3)}

    class Client:
        status = "running"

        def wait_for_workers(self, *a, **kw):
            pass

        def upload_file(self, *a):
            pass

        def scheduler_info(self):
            return {"workers": table}

        def processing(self, workers=None):
            return {"tls://10.0.0.0:1": ["run-R1-1"]}

        def run_on_scheduler(self, fn, *a):
            return None

    def world(stale_listing=False, log=True, created_shift_s=0.0):
        registry, lists_ = {}, [0]

        def cluster(**kw):
            name = kw["name"]
            if name in registry:                             # Coiled attaches by name
                if log:
                    _fake_coiled_event(name, created=False)
            else:
                ts = _fake_coiled_event(name) if log else _dt.datetime.now(_dt.timezone.utc).isoformat()
                if created_shift_s:
                    ts = (_dt.datetime.fromisoformat(ts) + _dt.timedelta(seconds=created_shift_s)).isoformat()
                registry[name] = {"name": name, "id": 100 + len(registry), "created": ts,
                                  "current_state": {"state": "running"}, "kw": kw}
            return types.SimpleNamespace(cluster_id=registry[name]["id"], get_client=lambda: Client())

        def lister(workspace=None):
            lists_[0] += 1
            return [] if stale_listing and lists_[0] == 1 else list(registry.values())
        fake = types.ModuleType("coiled")
        fake.Cluster, fake.list_clusters = cluster, lister
        return fake, registry

    def go(fake, inv, d, submits, ctor_calls=None):
        q = os.path.join(d, "q.txt")
        open(q, "w").write("question")
        cfg = RunConfig(name="lt", question_file=q, labels=("R1",), out_dir=d, keeper=False, overflow_max=0,
                        pipeline="v2", cluster_timeout="40 minutes")
        comp = types.SimpleNamespace(WORKER_VM_TYPES=["a"], WORKER_OPTIONS={}, ADAPT_OPTIONS={},
                                     register_compute_tags=lambda c, exclude: None)
        if ctor_calls is not None:
            real = fake.Cluster
            fake.Cluster = lambda **kw: ctor_calls.append(kw["name"]) or real(**kw)
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"coiled": fake}), mock.patch.dict(os.environ, {"MA_LAUNCH_INVOCATION": inv}), \
                P(M, "is_running", return_value=False), P(M, "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                P(M, "code_tarball", return_value=(b"", "c0")), P(M, "pipeline_preflight", return_value="v2"), \
                P(M, "_compute", return_value=comp), P(M, "live_id", return_value=None), \
                P(M, "_submit", side_effect=lambda c_, cl, lab, *a, **k: submits.append(inv) or
                  (k.get("key") or "run-R1-1", types.SimpleNamespace(status="pending"))), \
                P(M, "_task_key", side_effect=lambda lab: f"run-{lab}-1"), \
                P(eval_paths, "archive_sha256", return_value="0"), P(time, "sleep", lambda s: None), \
                contextlib.redirect_stdout(out):
            try:
                rc = launch(cfg)
            except SystemExit as e:
                rc = ("exit", str(e.code))
        cl = json.load(open(cfg.clusters_file)) if os.path.exists(cfg.clusters_file) else None
        jr = g.get("read_journal")
        return rc, cl, (jr(cfg) if jr else [])
    # (a) the race the pre-listing cannot see: A creates between B's listing and B's constructor
    fake, registry = world(stale_listing=True)
    submits = []
    with tempfile.TemporaryDirectory() as da, tempfile.TemporaryDirectory() as db:
        rc_a, cl_a, j_a = go(fake, "INV-A", da, submits)
        fake.list_clusters = (lambda reg: (lambda workspace=None, n=[0]: (n.__setitem__(0, n[0] + 1) or
                                                                          ([] if n[0] == 1 else list(reg.values())))))(registry)
        rc_b, cl_b, j_b = go(fake, "INV-B", db, submits)
    kw = registry.get("lt", {}).get("kw", {})
    ok((kw.get("tags") or {}).get(INVOCATION_TAG if "INVOCATION_TAG" in g else "ma-invocation") == "INV-A"
       and kw.get("cluster_timeout") == "40 minutes",
       f"O1 the run cluster is created with the invocation tag (for people) and the config's cluster_timeout: "
       f"{ {k: kw.get(k) for k in ('tags', 'cluster_timeout')} }")
    ok(rc_a == 0 and (cl_a or {}).get("invocation") == "INV-A" and (cl_a or {}).get("run", {}).get("id") == registry["lt"]["id"]
       and any(e.get("status") == "created" and e.get("id") == registry["lt"]["id"] for e in j_a),
       f"O2 the launcher whose constructor created the cluster records it, in clusters.json and its journal: {rc_a} {cl_a} {j_a}")
    ok(isinstance(rc_b, tuple) and cl_b is None and submits == ["INV-A"] and "attached" in rc_b[1]
       and any(e.get("status") == "attached" for e in j_b) and not any(e.get("status") == "created" for e in j_b),
       f"O3 the racing launcher's constructor attached (Coiled said so): refused, no record, nothing submitted, "
       f"journaled as attached: {rc_b} {cl_b} {submits} {j_b}")
    # (b) the pre-listing sees a live cluster of the name: refused before the constructor is called
    fake, registry = world()
    ctor = []
    with tempfile.TemporaryDirectory() as da, tempfile.TemporaryDirectory() as db:
        go(fake, "INV-A", da, [])
        rc_b, cl_b, _ = go(fake, "INV-B", db, [], ctor_calls=ctor)
    ok(isinstance(rc_b, tuple) and cl_b is None and not ctor,
       f"O4 a live cluster of the name listed just before the constructor: refused without constructing: {rc_b} {ctor}")
    # (c) no creation event (any doubt), and (d) a created time before the invocation began (less 120 s)
    for kwargs, what in ((dict(log=False), "no creation event"), (dict(created_shift_s=-3600), "created an hour before")):
        fake, registry = world(**kwargs)
        with tempfile.TemporaryDirectory() as d:
            rc, cl, j = go(fake, "INV-C", d, [])
        ok(isinstance(rc, tuple) and cl is None, f"O5 {what}: not this invocation's, refused before any record: {rc} {cl}")


def _self_test_bwrap(ok) -> None:
    """RC61: the agent VM's best-effort bubblewrap install (worker_side.ensure_bwrap), shell calls stubbed."""
    from types import SimpleNamespace as NS

    def case(present, rcs, platform="linux", sudo=True, raise_on=None):
        calls, have = [], {"bwrap": present}

        def which(name):
            return "/usr/bin/" + name if (name == "sudo" and sudo) or (name == "bwrap" and have["bwrap"]) else None

        def run(cmd, **k):
            calls.append(" ".join(cmd[2:]))
            if raise_on and raise_on in cmd:
                raise OSError("no apt")
            rc = rcs.pop(0) if rcs else 1
            if rc == 0 and "install" in cmd:
                have["bwrap"] = True
            return NS(returncode=rc)
        return ws.ensure_bwrap(run=run, which=which, platform=platform), calls
    note, calls = case(True, [])
    ok(note == "bwrap: present" and calls == [], f"RC61a bwrap present: nothing is run ({note}, {calls})")
    note, calls = case(False, [0])
    ok(note == "bwrap: installed" and calls == ["apt-get install -y bubblewrap"],
       f"RC61b missing: one sudo -n apt-get install ({note}, {calls})")
    note, calls = case(False, [100, 0, 0])
    ok(note == "bwrap: installed" and calls == ["apt-get install -y bubblewrap", "apt-get update",
                                                "apt-get install -y bubblewrap"],
       f"RC61c a failed install is retried after apt-get update ({note}, {calls})")
    note, calls = case(False, [100, 0, 100])
    ok(note.startswith("bwrap: install failed") and "fail closed" in note,
       f"RC61d both installs fail: a note, no exception ({note})")
    note, calls = case(False, [], raise_on="apt-get")
    ok(note.startswith("bwrap: install failed"), f"RC61e a raising shell call is a note, not a failure ({note})")
    note, calls = case(False, [], sudo=False)
    ok("no sudo" in note and calls == [], f"RC61f no sudo: nothing is run ({note})")
    note, calls = case(False, [], platform="darwin")
    ok("not Linux" in note and calls == [], f"RC61g not Linux: nothing is run ({note})")


def _self_test_sign(ok) -> None:
    """RS1-RS5 (run console review, finding 5): `sign` names the remote run it answers. The label and the run
    dir name come from the caller; the worker refuses a mismatch and signs a v2 spec through stage_machine.py."""
    import tempfile
    try:
        a = parse_sign_args(["ans.yaml", "--label", "F2", "--run", "2026_b", "--expect-spec-sha256", "ab12",
                             "looks", "right"], ["F1", "F2"])
        ok({k: a.get(k) for k in ("answers", "note", "label", "run", "expect")}
           == {"answers": "ans.yaml", "note": "looks right", "label": "F2", "run": "2026_b", "expect": "ab12"},
           f"RS1 sign parses --label, --run and --expect-spec-sha256 before the note ({a})")
        try:
            parse_sign_args(["ans.yaml", "--label", "F9", "n"], ["F1", "F2"])
            ok(False, "RS2 a label the config does not run is refused")
        except SystemExit as e:
            ok("F9" in str(e), f"RS2 a label the config does not run is refused ({e})")
        old = parse_sign_args(["ans.yaml", "a", "note"], ["F1"])
        ok(old["label"] is None and old["note"] == "a note", "RS2b the old form (answers, note) still parses")
    except Exception as e:  # noqa: BLE001 -- before the fix there is no parse_sign_args
        ok(False, f"RS1-RS2 sign arguments: {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory() as td:
        for n in ("2026_a", "2026_b"):
            os.makedirs(os.path.join(td, "F1", "runs", n))
        open(os.path.join(td, "F1", "runs", "2026_b", "run_state.json"), "w").write("{}")
        seen = []

        def fake(cmd, **k):
            seen.append(cmd)
            return type("R", (), {"returncode": 0, "stdout": "none\n" if "status" in cmd else "{}", "stderr": ""})()
        old_work = ws.WORK
        ws.WORK = td
        try:
            r = ws.sign_side("F1", "", "n", run_name="2026_a", _runner=fake)
            ok(r["rc"] != 0 and "2026_b" in r["out"] and "2026_a" in r["out"] and not any("sign" in c for c in seen),
               f"RS3 the worker refuses a sign for another run than the newest on that label ({r})")
            seen.clear()
            r = ws.sign_side("F1", "", "n", run_name="2026_b", expect_spec_sha256="ab12", _runner=fake)
            cmd = seen[-1] if seen else []
            ok(r["rc"] == 0 and "agents/stage_machine.py" in cmd and "sign" in cmd and "--expect-sha256" in cmd
               and "ab12" in cmd, f"RS4 a v2 run's spec is signed through stage_machine.py sign --expect-sha256 ({cmd})")
            seen.clear()
            r = ws.sign_side("F1", "", "n", run_name="2026_b", _runner=fake)
            ok(r["rc"] != 0 and not any("stage_machine" in " ".join(c) for c in seen),
               f"RS5 a v2 spec sign without the displayed spec's sha256 is refused ({r})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"RS3-RS5 worker sign: {type(e).__name__}: {e}")
        # RS6-RS8 (round 2, P1-6): the action is explicit; an expected spec sha always means sha-bound spec signing
        try:
            os.remove(os.path.join(td, "F1", "runs", "2026_b", "run_state.json"))
            seen.clear()
            r = ws.sign_side("F1", "", "n", run_name="2026_b", expect_spec_sha256="ab12", _runner=fake)
            cmd = seen[-1] if seen else []
            ok(r["rc"] == 0 and "agents/stage_machine.py" in cmd and "ab12" in cmd
               and not any("decision_brief.py" in c for c in seen),
               f"RS6 with an expected spec sha and no run_state.json the spec is still signed sha-bound ({cmd})")
            seen.clear()
            r = ws.sign_side("F1", "", "n", run_name="2026_b", action="spec", _runner=fake)
            ok(r["rc"] != 0 and not any("sign" in c for c in seen), f"RS7 action spec without a sha fails closed ({r})")
            a = parse_sign_args(["a.yaml", "--action", "spec", "--expect-derivations-sha256", "none",
                                 "--expect-sheet-sha256", "cd", "n"], ["F1"])
            ok(a.get("action") == "spec" and a.get("expect_derivations") == "none" and a.get("expect_sheet") == "cd",
               f"RS8 sign parses --action and the snapshot's other shas ({a})")
        except Exception as e:  # noqa: BLE001
            ok(False, f"RS6-RS8 worker sign action: {type(e).__name__}: {e}")
        finally:
            ws.WORK = old_work


def _self_test_pipeline(ok, tb) -> None:
    """RP (2026-11-05): the eval launcher ran v1 for a v2 test. cloud/configs/_eval.py passed no
    pipeline flag, main.py defaulted to v1, and neither the launch record nor the run said which ran."""
    import tempfile
    lib = os.path.join(os.path.dirname(HERE), "scripts", "lib")
    real = open(os.path.join(lib, "pipeline_id.py"), "rb").read()
    real_main = open(os.path.join(os.path.dirname(HERE), "main.py"), "rb").read()
    g = globals()
    for f in sorted(os.listdir(os.path.join(HERE, "configs"))):
        if not f.endswith(".py") or f.startswith("_"):
            continue
        try:
            c = load_config(os.path.join(HERE, "configs", f))
        except Exception as e:  # noqa: BLE001 -- RC16 reports the load itself
            ok(False, f"RP1 configs/{f} declares its pipeline: {e}")
            continue
        ok(getattr(c, "pipeline", "") in ("v1", "v2"), f"RP1 configs/{f} declares its pipeline ({getattr(c, 'pipeline', None)})")
        if f.startswith("eval_"):
            ok(getattr(c, "pipeline", None) == "v2", f"RP2 configs/{f} runs v2 ({getattr(c, 'pipeline', None)})")
        mf = g.get("main_flags")
        ok(mf is not None and f"--{getattr(c, 'pipeline', None)}" in mf(c) and not {"--v1", "--v2"} & set(c.flags),
           f"RP3 configs/{f}: the launcher passes the declared pipeline to main.py, flags never do")
    for bad in (dict(pipeline="v3"), dict(pipeline="v2", flags=("--v1",)), dict(pipeline="v1", flags=("--v2",))):
        try:
            RunConfig(name="t", question_file="/dev/null", **bad)
            ok(False, f"RP4 a config with {bad} is refused")
        except (ValueError, TypeError):
            ok(True, f"RP4 a config with {bad} is refused")
    pf = g.get("pipeline_preflight")
    ok(pf is not None, "RP5 the launcher has a pipeline preflight")
    if pf is None:
        return
    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="ev-x", question_file="/dev/null", out_dir=d, pipeline="v2",
                        flags=("--max-budget-usd", "35", "--signoff", "lead"))
        code = tb({"main.py": real_main, "scripts/lib/pipeline_id.py": real})
        ok(pf(cfg, code) == "v2", "RP5 the shipped pipeline_id agrees with the declared v2")
        ok(pf(RunConfig(name="t1", question_file="/dev/null", out_dir=d, pipeline="v1"), code) == "v1",
           "RP6 an explicit v1 config passes the preflight")
        for why, c2, code2 in (
                ("a tree with no pipeline_id.py (from before 2026-11-05: defaults to v1)", cfg, tb({"main.py": b"x"})),
                ("a config that declares no pipeline", RunConfig(name="t2", question_file="/dev/null", out_dir=d), code),
                ("a main.py that does not dispatch through pipeline_id (Codex r1 P2: old main.py, new helper)",
                 RunConfig(name="t3", question_file="/dev/null", out_dir=d, pipeline="v1"),
                 tb({"main.py": b"parser.add_argument('--v2')\n", "scripts/lib/pipeline_id.py": real})),
                ("shipped code that would run another pipeline", cfg,
                 tb({"main.py": real_main, "scripts/lib/pipeline_id.py":
                     b"import sys\nprint('v1')\n"}))):
            try:
                pf(c2, code2)
                ok(False, f"RP7 the preflight refuses {why}")
            except SystemExit as e:
                ok(True, f"RP7 the preflight refuses {why}: {str(e)[:90]}")
        rec = g["launch_record"](cfg, worker="w1", commit="abc1234", launch_id="L1", cluster_id=9, code=code,
                                 pipeline="v2")
        ok(rec.get("pipeline") == "v2" and rec.get("commit") == "abc1234" and rec.get("launch_id") == "L1"
           and rec.get("launcher") == "run_cluster:ev-x" and isinstance(rec.get("eval_inputs_sha256"), dict),
           f"RP8 the launch record (assignments.json) carries pipeline, commit and launcher ({rec})")
        ok(ws.run_identity(tb({"runs/r/metadata.json": json.dumps(
            {"pipeline": "v2", "commit": "abc1234def", "launcher": "run_cluster:ev-x"}).encode()}))
           == {"pipeline": "v2", "commit": "abc1234def", "launcher": "run_cluster:ev-x"},
           "RP9 the worker reads the run's identity from its metadata.json")
        ok(ws.run_identity(tb({"runs/r/metadata.json": b"{}"}))["pipeline"] == "unknown"
           and ws.run_identity(b"junk")["commit"] == "unknown", "RP10 a run with no identity reads unknown")

        class Store:
            def __init__(self, files):
                self.files = files

            def download(self, blob, path, account):
                open(path, "wb").write(self.files[blob])
        run = tb({"r/metadata.json": json.dumps({"pipeline": "v2", "commit": "abc1234def"}).encode()})
        res = json.dumps({"exit_code": 0, "duration_s": 5, "run_stored": "outputs/x/r.tar.gz"}).encode()
        m = collect_from_storage(cfg, Store({ws.record_blob(cfg.storage_prefix, "L1", "R1"): res,
                                             "outputs/x/r.tar.gz": run}), "R1", "L1", rec)
        on_disk = json.load(open(os.path.join(d, "R1", "result.json")))
        ok(on_disk.get("run_identity", {}).get("pipeline") == "v2" and on_disk.get("pipeline") == "v2"
           and on_disk.get("commit") == "abc1234" and m.get("launch_id") == "L1",
           f"RP11 the collected result.json records the launch's and the run's pipeline and commit ({on_disk})")
    import inspect
    for fn in (_launch_locked, relaunch):
        src = inspect.getsource(fn)
        ok("pipeline_preflight(cfg, code)" in src and src.index("pipeline_preflight(") < src.index("_submit(")
           and ("coiled.Cluster(" not in src or src.index("pipeline_preflight(") < src.index("coiled.Cluster(")),
           f"RP12 {fn.__name__} checks the pipeline before it creates or submits anything")
    ok("main_flags(cfg)" in inspect.getsource(_submit), "RP13 every submission passes main_flags (the pipeline)")
    ac = g.get("archive_commit")
    ok(ac is not None and ac(tb({"scripts/lib/code_commit.txt": b"0123456789abcdef0123456789abcdef01234567\n"}))
       == "0123456"[:7] and ac(tb({"scripts/lib/code_commit.txt": b"$Format:%H$\n"})) is None and ac(tb({})) is None,
       "RP14 the launch commit is read from the archive itself (the commit that ships, not HEAD later)")


def _self_test() -> int:
    import datetime as dt
    import io
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def tb(members):
        b = io.BytesIO()
        with tarfile.open(fileobj=b, mode="w:gz") as t:
            for name, data in members.items():
                ti = tarfile.TarInfo(name)
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
        return b.getvalue()

    meta = lambda **m: json.dumps(m).encode()  # noqa: E731
    ok(ws.run_stored(tb({"runs/r/metadata.json": meta(stored="outputs/p/r.tar.gz", store_error=None)}))
       == "outputs/p/r.tar.gz", "RC1 a verified upload is found in the run's metadata")
    ok(ws.run_stored(tb({"runs/r/metadata.json": meta(stored="x", store_error="boom")})) is None,
       "RC2 a store error means not stored")
    ok(ws.run_stored(tb({"runs/r/models/metadata.json": meta(stored="x")})) is None,
       "RC3 a nested metadata.json does not count")
    ok(ws.run_stored(b"not a tarball") is None, "RC4 an unreadable tarball fails closed")
    # RC50 (Phase 50 α): a tarball holding eval answer keys, generators or rubrics is refused.
    refused = []
    for members in ({"main.py": b"x", "evals/q/_truth/truth.json": b"{}"}, {"evals/q/generate.py": b"x"},
                    {"docs/design/eval-suite.md": b"x"}):
        try:
            refuse_hidden(tb(members), "t")
        except SystemExit:
            refused.append(True)
    ok(len(refused) == 3, f"RC50 a tarball with eval-suite files is refused ({len(refused)}/3)")
    try:
        refuse_hidden(tb({"main.py": b"x", "data/evals/q/districts.csv": b"x"}), "t")
        ok(True, "RC50b the shipped eval inputs (data/evals/) pass")
    except SystemExit as e:
        ok(False, f"RC50b the shipped eval inputs (data/evals/) pass: {e}")
    import inspect
    ok("refuse_hidden(blob" in inspect.getsource(code_tarball), "RC50c code_tarball checks the blob it returns")
    # RC50d-f: Coiled package sync ships the launch tree itself (untracked and ignored files
    # included), so a tree holding hidden eval material on disk is refused before launch.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "evals", "q"))
        open(os.path.join(td, "evals", "q", "question.txt"), "w").close()
        try:
            refuse_hidden_tree(td, tracked={"evals/q/question.txt"})
            ok(True, "RC50d a tree with only the public eval files passes")
        except SystemExit as e:
            ok(False, f"RC50d a tree with only the public eval files passes: {e}")
        for rel in ("evals/q/rubric.yaml", "evals_private/v1/q/_truth/truth.json", "outputs/bundle.enc"):
            os.makedirs(os.path.dirname(os.path.join(td, rel)), exist_ok=True)
            open(os.path.join(td, rel), "w").close()
            try:
                refuse_hidden_tree(td, tracked={"evals/q/question.txt"})
                ok(False, f"RC50e a tree holding {rel} is refused")
            except SystemExit as e:
                ok(rel in str(e), f"RC50e the refusal names {rel}: {e}")
            os.remove(os.path.join(td, rel))
    src = inspect.getsource(code_tarball)
    ok("refuse_hidden_tree(cfg.tree" in src and src.index("refuse_hidden_tree(") < src.index('"archive"'),
       "RC50f code_tarball checks the launch tree on disk before it builds anything")

    now = time.time()

    class Store:
        def __init__(self, blobs=(), files=None):
            self.blobs, self.files = list(blobs), files or {}

        def list_blobs(self, prefix, account):
            return [{"name": n, "last_modified": dt.datetime.fromtimestamp(t, dt.timezone.utc)}
                    for n, t in self.blobs if n.startswith(prefix)]

        def download(self, blob, path, account):
            if blob not in self.files:
                raise FileNotFoundError(blob)
            open(path, "wb").write(self.files[blob])

    p, labels = "runs/x", ["R1", "R2"]
    r1, r2 = ws.record_blob(p, "L2", "R1"), ws.record_blob(p, "L2", "R2")
    ok(not ws.all_stored(Store([(r1, now)]), p, None, labels, "L2"), "RC5 one run of two stored: keep running")
    ok(ws.all_stored(Store([(r1, now), (r2, now)]), p, None, labels, "L2"), "RC6 both stored: shut down")
    ok(not ws.all_stored(Store([(ws.record_blob(p, "L1", "R1"), now - 60), (r2, now)]), p, None, labels, "L2"),
       "RC7 a record an earlier launch wrote minutes ago does not count")
    ok(not ws.all_stored(Store([(f"outputs/{p}/result_R1.json", now), (r2, now)]), p, None, labels, "L2"),
       "RC7b a record outside any launch folder does not count")

    listing = [{"name": "a", "current_state": {"state": "stopped"}}, {"name": "a", "current_state": {"state": "ready"}},
               {"name": "b", "current_state": "Stopping"}, {"name": "c", "current_state": None}]
    ok(is_running("a", lambda: listing), "RC8 a ready cluster among stopped ones of the same name is running")
    ok(not is_running("b", lambda: listing) and not is_running("c", lambda: listing)
       and not is_running("zz", lambda: listing), "RC9 stopping / no state / absent are not attachable")

    class FakeCluster:
        def __init__(self, cid):
            self.cluster_id, self.stopped = cid, False

        def shutdown(self):
            self.stopped = True

    fc = FakeCluster(7)
    ok(ws.verify_attached(fc, 7) is fc and not fc.stopped, "RC17 attaching to the checked cluster is kept")
    fc = FakeCluster(8)
    try:
        ws.verify_attached(fc, 7)
        ok(False, "RC18 a replacement cluster created by the attach is refused")
    except SystemExit:
        ok(fc.stopped, "RC18 a replacement cluster created by the attach is shut down and refused")
    ok(live_id("a", lambda: [{"name": "a", "id": 3, "current_state": {"state": "stopped"}},
                             {"name": "a", "id": 5, "current_state": {"state": "ready"}}]) == 5
       and live_id("a", lambda: [{"name": "a", "id": 3, "current_state": {"state": "stopped"}}]) is None,
       "RC19 the live id ignores stopped clusters of the same name")
    ok(keeper_wait([]) == "start" and keeper_wait(["stopped", "stopped"]) == "start"
       and keeper_wait(["stopped", "stopping"]) == "wait" and keeper_wait(["stopped", "ready"]) == "stop"
       and keeper_wait(["stopping", "running"]) == "stop",
       "RC22 a live or stopping keeper from an earlier launch is never reused")
    ok(relaunch_refusal("missing", False) is None and relaunch_refusal("running", False)
       and relaunch_refusal("done", False) and relaunch_refusal("missing", True),
       "RC20 relaunch only when the run is neither running, finished on its VM, nor stored")

    with tempfile.TemporaryDirectory() as d:
        cfg = RunConfig(name="t", question_file="/dev/null", out_dir=d, labels=("R1",))
        ok(cfg.storage_prefix == "runs/t" and cfg.keeper_name == "t-keeper", "RC10 config defaults")
        for bad, why in ((dict(flags=("--max-rounds", "2")), "rounds via flags"), (dict(labels=()), "no labels"),
                         (dict(labels=("R1", "R1")), "duplicate labels")):
            try:
                RunConfig(name="t", question_file="/dev/null", **bad)
                ok(False, f"RC11 rejects {why}")
            except ValueError:
                ok(True, f"RC11 rejects {why}")
        run = tb({"r/metadata.json": b"{}", "r/report.md": b"# report"})
        rec = json.dumps({"exit_code": 0, "duration_s": 5, "run_stored": "outputs/runs/t/r.tar.gz"}).encode()
        st = Store(files={ws.record_blob("runs/t", "L2", "R1"): rec, "outputs/runs/t/r.tar.gz": run})
        m = collect_from_storage(cfg, st, "R1", "L2")
        ok(local_is_current(os.path.join(d, "R1"), "L2") is True and local_is_current(os.path.join(d, "R1"), "L3") is False
           and local_is_current(os.path.join(d, "R9"), "L2") is None,
           "RC21 a local copy counts as collected only for its own launch")
        ok(m["exit_code"] == 0 and open(os.path.join(d, "R1", "runs", "r", "report.md")).read() == "# report",
           "RC12 collect from storage unpacks the stored run directory")
        try:
            collect_from_storage(cfg, Store(files={ws.record_blob("runs/t", "L2", "R1"): b'{"run_stored": null}'}),
                                 "R1", "L2")
            ok(False, "RC13 a record naming no stored run fails")
        except SystemExit:
            ok(True, "RC13 a record naming no stored run fails")
        try:
            collect_from_storage(cfg, Store(files={ws.record_blob("runs/t", "L1", "R1"): rec,
                                                   "outputs/runs/t/r.tar.gz": run}), "R1", "L2")
            ok(False, "RC13b an earlier launch's record is not collected for this launch")
        except FileNotFoundError:
            ok(True, "RC13b an earlier launch's record is not collected for this launch")
        cf = os.path.join(d, "c.py")
        open(cf, "w").write("from run_cluster import RunConfig\nCONFIG = RunConfig(name='z', question_file='q')\n")
        ok(load_config(cf).name == "z", "RC14 a config file loads")
        open(cf, "w").write("X = 1\n")
        try:
            load_config(cf)
            ok(False, "RC15 a config without CONFIG is refused")
        except SystemExit:
            ok(True, "RC15 a config without CONFIG is refused")
    _self_test_42b(ok, RunConfig)
    _self_test_51c(ok)
    _self_test_52(ok)
    _self_test_attach(ok)
    _self_test_sign(ok)
    _self_test_pipeline(ok, tb)
    try:
        _self_test_s18(ok, tb)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"LR: {type(e).__name__}: {e}")
    try:
        _self_test_s18_r1(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"R: {type(e).__name__}: {e}")
    try:
        _self_test_s18_r2(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"R9: {type(e).__name__}: {e}")
    try:
        _self_test_s18_r3(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"K: {type(e).__name__}: {e}")
    try:
        _self_test_s18_r4(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"O: {type(e).__name__}: {e}")
    for f in sorted(os.listdir(os.path.join(HERE, "configs"))):
        if f.endswith(".py") and not f.startswith("_"):   # _*.py: shared helpers (configs/_eval.py)
            try:
                c = load_config(os.path.join(HERE, "configs", f))
                ok(c.storage_prefix.startswith(("runs/", "_checks/")), f"RC16 configs/{f} loads")
            except Exception as e:  # noqa: BLE001
                ok(False, f"RC16 configs/{f} loads: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv) -> int:
    if argv[1:2] == ["--self-test"]:
        import no_cloud
        with no_cloud.refusing():          # no cloud call from a self-test (cloud/no_cloud.py)
            return _self_test()
    if len(argv) < 3:
        sys.exit(__doc__)
    cfg, cmd, rest = load_config(argv[1]), argv[2], argv[3:]
    if cmd == "launch":
        return launch(cfg)
    elif cmd == "keeper":
        return keeper_later(cfg)
    elif cmd == "ack-probe":
        return ack_probe(cfg)
    elif cmd == "smoke-run":
        mins = float(rest[rest.index("--minutes") + 1]) if "--minutes" in rest else 20.0
        stop_on = rest[rest.index("--interrupt-on") + 1] if "--interrupt-on" in rest else None
        inv = _invocation()
        print(f"smoke-run invocation {inv}", flush=True)
        r = smoke_run(cfg, inv, [sys.executable, os.path.abspath(__file__), argv[1], "launch"], mins, stop_on)
        print(f"smoke-run: {json.dumps(r)}", flush=True)
        return 0 if r["clean"] else 1
    elif cmd == "reuse-check":
        if not rest or rest[0].startswith("-"):
            sys.exit(__doc__)
        r = reuse_check(cfg, rest[0], expect_all="--expect-all" in rest[1:])
        print(json.dumps({k: v for k, v in r.items() if k != "rc"}, indent=1))
        print(f"{r.get('reusable')} of {r.get('stored')} stored rows would be reused by the code this launch ships")
        return r["rc"]
    elif cmd == "status":
        status(cfg)
    elif cmd == "collect":
        collect(cfg)
    elif cmd == "shutdown":
        shutdown(cfg)
    elif cmd == "relaunch":
        return relaunch(cfg, rest[0])
    elif cmd == "report-cost":
        report_cost(cfg, rest)
    elif cmd == "keeper-log":
        c = attach(cfg.keeper_name).get_client()
        w = sorted(ws.scheduler_workers(lambda: c, attempts=3, min_workers=1))[0]
        print(c.run(ws.keeper_log, workers=[w])[w])
    elif cmd == "brief":
        r = _side(cfg, ws.brief_side)
        print(f"run dir: {r.get('run_dir')}\nstatus: {r.get('status') or r.get('state')}\n")
        print(r.get("brief_md") or "(no decision_brief.md yet)")
    elif cmd == "report":
        print(_side(cfg, ws.report_side))
    elif cmd == "health":
        return health(cfg, rest)
    elif cmd == "sign":
        a = parse_sign_args(rest, cfg.labels)
        try:   # sent by value: a worker uploaded before --run existed still runs this sign_side
            import cloudpickle
            cloudpickle.register_pickle_by_value(ws)
        except ImportError:
            pass
        r = _side(cfg, ws.sign_side, open(a["answers"]).read(), a["note"], a["run"], a["expect"], None, a["action"],
                  a["expect_derivations"], a["expect_sheet"], label=a["label"])
        print(r["out"])
        return r["rc"]
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
