"""Launch and manage a pipeline run on IDM Coiled from one config file.

    python cloud/run_cluster.py <config.py> launch            # cluster, run(s), keeper; then safe to disconnect
    python cloud/run_cluster.py <config.py> status            # running / done per run, cluster and keeper state
    python cloud/run_cluster.py <config.py> collect           # fetch finished runs (from storage once stopped)
    python cloud/run_cluster.py <config.py> brief|report      # decision brief / convergence measures of a live run
    python cloud/run_cluster.py <config.py> health [--loop] [--interval S] [--log PATH] [--thresholds YAML]
                                                              # anomalies of a live run (cloud/run_health.py)
    python cloud/run_cluster.py <config.py> sign <answers.yaml> [note]   # the brief, or pending scope declarations
    python cloud/run_cluster.py <config.py> relaunch <label>
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

Rules: the IDM Coiled default workspace only (never the prod subscription);
attach-only for everything but `launch` (coiled.Cluster(name=...) on a
cluster that is gone creates a new, empty one); API keys go in as task
arguments and are never printed or written; answer keys (`_truth/`) are
never stored.
"""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import worker_side as ws  # noqa: E402 — uploaded to the VMs under this name
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts", "lib"))
import eval_paths  # noqa: E402 — Phase 50 α: what a launch tarball must never hold
sys.modules.setdefault("run_cluster", sys.modules[__name__])   # configs import RunConfig from here

WORKSPACE = "idm-coiled-idmad-r2"
ENV_FILE = os.path.expanduser("~/modeling-agentic/.env")
CHUNK = 8 * 2**20
_ATTACH = {"ready", "running", "pending", "starting", "scaling"}


@dataclasses.dataclass
class RunConfig:
    name: str                                  # cluster name; the keeper is "<name>-keeper"
    question_file: str
    tree: str = os.path.expanduser("~/modeling-agentic-launch")
    labels: tuple = ("R1",)
    flags: tuple = ()                          # extra main.py flags
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
        for f in ("scale_down_after", "adapt_interval", "overflow_hold") + (("target_duration",) if self.target_duration else ()):
            seconds(getattr(self, f))   # fail at load, not on the keeper VM
        if seconds(self.adapt_interval) <= 0:
            raise ValueError("adapt_interval must be positive")
        if self.overflow_max and not self.overflow_vm_types:
            raise ValueError("overflow_max needs overflow_vm_types")

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
    import coiled
    cid = live_id(name)
    if cid is None:
        raise SystemExit(f"cluster {name!r} is not running; refusing to create a new one")
    cl = coiled.Cluster(name=name, workspace=WORKSPACE, shutdown_on_close=False, wait_for_workers=False)
    return ws.verify_attached(cl, cid)


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
    return blob, head[:7]


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


# ---- commands ---------------------------------------------------------------------------

def _submit(cfg, client, label, worker, code, question, keys, launch_id):
    from distributed import fire_and_forget
    fut = client.submit(ws.run_to_disk, label, code, question, list(cfg.flags), keys["ANTHROPIC_API_KEY"],
                        keys.get("OPENAI_API_KEY") if cfg.cross_model else None, list(cfg.labels),
                        cfg.storage_prefix, cfg.storage_account, launch_id, cfg.max_rounds, cfg.max_sessions,
                        workers=[worker], allow_other_workers=False, pure=False,
                        key=f"run-{label}-{int(time.time())}")
    fire_and_forget(fut)


def launch(cfg):
    import coiled
    if is_running(cfg.name):
        raise SystemExit(f"{cfg.name} is already running: use status / relaunch")
    os.makedirs(cfg.out_dir, exist_ok=True)
    question = open(cfg.question_file).read().strip()
    keys = read_keys()
    code, commit = code_tarball(cfg)
    compute = _compute(cfg)
    n0 = len(cfg.labels) + cfg.compute_start
    cluster = coiled.Cluster(
        name=cfg.name, workspace=WORKSPACE, n_workers=n0, worker_vm_types=compute.WORKER_VM_TYPES,
        worker_disk_size=100, idle_timeout=cfg.idle_timeout, worker_options=compute.WORKER_OPTIONS,
        environ={"MA_STORAGE_ACCOUNT": cfg.storage_account, "MA_STORAGE_PREFIX": cfg.storage_prefix,
                 **cfg.compute_env},
        spot_policy="on-demand", scheduler_cpu=2, shutdown_on_close=False)
    client = cluster.get_client()
    client.wait_for_workers(n0, timeout=1800)
    client.upload_file(os.path.join(HERE, "worker_side.py"))
    workers = sorted(ws.scheduler_workers(lambda: ws.live_client(cluster, client), min_workers=n0))[:len(cfg.labels)]
    compute.register_compute_tags(client, exclude=workers)   # the agent VMs never take compute tasks
    launch_id = time.strftime("%Y%m%d-%H%M%S")
    assign = {}
    for label, w in zip(cfg.labels, workers):
        _submit(cfg, client, label, w, code, question, keys, launch_id)
        assign[label] = {"worker": w, "commit": commit, "submitted": time.strftime("%F %T"), "launch_id": launch_id,
                         "cluster_id": cluster.cluster_id,
                         "eval_inputs_sha256": eval_paths.archive_sha256(code)}   # Phase 50 α: the inputs shipped
        print(f"fired {label} -> {w} @ {commit}", flush=True)
    json.dump(assign, open(cfg.state_file, "w"), indent=1)
    clusters = {"run": {"name": cfg.name, "id": cluster.cluster_id}, "launch_id": launch_id,
                "launched_utc": time.strftime("%FT%TZ", time.gmtime())}
    overflow = None
    if cfg.keeper and cfg.overflow_max:
        overflow = start_overflow(cfg, compute)
        clusters["overflow"] = {"name": cfg.overflow_name, "id": overflow["id"]}
        overflow["primary_vm_types"] = list(compute.WORKER_VM_TYPES)
    json.dump(clusters, open(cfg.clusters_file, "w"), indent=1)
    time.sleep(10)
    if cfg.keeper:
        kc = start_keeper(cfg, adapt_options(cfg, compute.ADAPT_OPTIONS), cluster.cluster_id, overflow)
        clusters["keeper"] = {"name": cfg.keeper_name, "id": kc.cluster_id}
        json.dump(clusters, open(cfg.clusters_file, "w"), indent=1)   # recorded before any read that can fail
        time.sleep(30)
        confirm_keeper(cfg.keeper_name, kc)
    print(f"launched {cfg.name}: results go to outputs/{cfg.storage_prefix}/; the cluster stops itself "
          "once every run is stored. Safe to disconnect.")


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


def start_overflow(cfg, compute) -> dict:
    """The quota-fallback pool: a scheduler-only cluster of other VM families, created
    here (so it gets this launch's software environment) and scaled by the keeper. Its
    idle timeout outlasts the keeper; the keeper deletes it on exit."""
    import coiled
    _fresh(cfg.overflow_name)
    oc = coiled.Cluster(
        name=cfg.overflow_name, workspace=WORKSPACE, n_workers=0, worker_vm_types=list(cfg.overflow_vm_types),
        worker_disk_size=100, idle_timeout=f"{int(cfg.keeper_hours * 60) + 30} minutes",
        worker_options=compute.WORKER_OPTIONS,
        environ={"MA_STORAGE_ACCOUNT": cfg.storage_account, "MA_STORAGE_PREFIX": cfg.storage_prefix, **cfg.compute_env},
        spot_policy="on-demand", scheduler_cpu=2, shutdown_on_close=False, wait_for_workers=False)
    oc.get_client().upload_file(os.path.join(HERE, "worker_side.py"))
    print(f"{cfg.overflow_name}: ready at 0 workers ({', '.join(cfg.overflow_vm_types)}; up to {cfg.overflow_max})")
    return {"name": cfg.overflow_name, "id": oc.cluster_id, "max": cfg.overflow_max,
            "vm_types": list(cfg.overflow_vm_types), "hold_s": seconds(cfg.overflow_hold),
            "scale_down_s": seconds(cfg.scale_down_after), "hours": cfg.keeper_hours}


def start_keeper(cfg, adapt_opts, cluster_id, overflow=None):
    import coiled
    import dask
    from distributed import fire_and_forget
    lo, hi = len(cfg.labels) + cfg.compute_start, len(cfg.labels) + cfg.compute_max
    # An earlier launch's keeper (still polling, or stopping) must not be reused:
    # its task would hold the old cluster id and exit, leaving this pool unscaled.
    _fresh(cfg.keeper_name)
    kc = coiled.Cluster(name=cfg.keeper_name, workspace=WORKSPACE, n_workers=1, worker_cpu=2,
                        worker_memory="8 GiB", scheduler_cpu=2, spot_policy="on-demand",
                        idle_timeout="30 minutes", shutdown_on_close=False)
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


def confirm_keeper(keeper_name, kc, attempts=6, delay_s=5.0, sleep=None):
    """Print the keeper's last log line. The keeper cluster's client can be closed by now (a
    failed reconnect closes it and empties its scheduler identity: v2blind1's
    `KeyError: 'workers'`), so a closed client is replaced and the read retried with
    backoff. Past the bound the launch fails with a message saying what to check; the run
    is already fired and clusters.json already names the keeper."""
    box = [None]

    def client():
        box[0] = ws.live_client(kc, box[0])
        return box[0]
    try:
        w = sorted(ws.scheduler_workers(client, attempts=attempts, delay_s=delay_s, min_workers=1, sleep=sleep,
                                        log=print))[0]
        line = box[0].run(ws.keeper_log, workers=[w])[w].strip().splitlines()[-1:]
    except Exception as e:  # noqa: BLE001 — reported, never ignored
        raise SystemExit(f"keeper {keeper_name} (cluster {kc.cluster_id}) was submitted but not confirmed holding "
                         f"the pool: {type(e).__name__}: {str(e)[:200]}. The run is fired and the clusters file "
                         f"names the keeper; check `status` and `keeper-log` now (without a keeper the pool "
                         f"stays at its start size).")
    print(line, f"(keeper {keeper_name})")
    return line


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


def collect_from_storage(cfg, store, label, launch_id):
    """One label from storage: this launch's result record, then the run directory it names."""
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
            meta = collect_from_storage(cfg, results_store, label, assign[label]["launch_id"])
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
        json.dump({**meta, **assign[label]}, open(os.path.join(dest, "result.json"), "w"), indent=1)
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


def relaunch(cfg, label):
    """Re-fire one run on its pinned agent VM (e.g. after a preflight-only fix)."""
    assign = json.load(open(cfg.state_file))
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
    code, commit = code_tarball(cfg)
    client.upload_file(os.path.join(HERE, "worker_side.py"))
    _submit(cfg, client, label, assign[label]["worker"], code, open(cfg.question_file).read().strip(), read_keys(),
            launch_id)
    assign[label].update(commit=commit, submitted=time.strftime("%F %T"), relaunched=True,
                         eval_inputs_sha256=eval_paths.archive_sha256(code))
    json.dump(assign, open(cfg.state_file, "w"), indent=1)
    print(f"re-fired {label} -> {assign[label]['worker']} @ {commit}")


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
        def __init__(self, infos, status="running", log="12:00:00 keeper holding x 3..9 {}\n12:00:01 workers=3"):
            self.infos, self.status, self.log, self.calls = list(infos), status, log, 0

        def scheduler_info(self):
            self.calls += 1
            return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

        def run(self, fn, workers):
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
    late.run = lambda fn, workers: {w: next(logs) for w in workers}
    with contextlib.redirect_stdout(io.StringIO()):
        line = confirm_keeper("x-keeper", types.SimpleNamespace(cluster_id=11, get_client=lambda: late),
                              attempts=4, delay_s=0.0, sleep=lambda s: None)
    ok("workers=3" in str(line), f"RC52i a keeper whose first tick comes late is confirmed once it ticks: {line}")

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
    fake_coiled.Cluster = lambda **kw: run_cl
    with tempfile.TemporaryDirectory() as d:
        q = os.path.join(d, "q.txt")
        open(q, "w").write("question")
        lcfg = RunConfig(name="lt", question_file=q, labels=("R1",), out_dir=d, keeper=False, overflow_max=0)
        with mock.patch.dict(sys.modules, {"coiled": fake_coiled}), \
                mock.patch.object(sys.modules[__name__], "is_running", return_value=False), \
                mock.patch.object(sys.modules[__name__], "read_keys", return_value={"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(sys.modules[__name__], "code_tarball", return_value=(b"", "c0")), \
                mock.patch.object(sys.modules[__name__], "_compute", return_value=comp), \
                mock.patch.object(sys.modules[__name__], "_submit", side_effect=lambda cfg, c, *a: used.append(c)), \
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
       and st.get("workers") == 2 and "workers=2" in log,
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
        return _self_test()
    if len(argv) < 3:
        sys.exit(__doc__)
    cfg, cmd, rest = load_config(argv[1]), argv[2], argv[3:]
    if cmd == "launch":
        launch(cfg)
    elif cmd == "status":
        status(cfg)
    elif cmd == "collect":
        collect(cfg)
    elif cmd == "shutdown":
        shutdown(cfg)
    elif cmd == "relaunch":
        relaunch(cfg, rest[0])
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
        r = _side(cfg, ws.sign_side, open(rest[0]).read(), " ".join(rest[1:]))
        print(r["out"])
        return r["rc"]
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
