"""Launch and manage a pipeline run on IDM Coiled from one config file.

    python cloud/run_cluster.py <config.py> launch            # cluster, run(s), keeper; then safe to disconnect
    python cloud/run_cluster.py <config.py> status            # running / done per run, cluster and keeper state
    python cloud/run_cluster.py <config.py> collect           # fetch finished runs (from storage once stopped)
    python cloud/run_cluster.py <config.py> brief|report      # decision brief / convergence measures of a live run
    python cloud/run_cluster.py <config.py> sign <answers.yaml> [note]
    python cloud/run_cluster.py <config.py> relaunch <label>
    python cloud/run_cluster.py <config.py> shutdown          # cluster and keeper, by hand
    python cloud/run_cluster.py --self-test

A config is a Python file defining CONFIG = RunConfig(...) (see cloud/configs/).

Lifecycle. Each run is pinned to its own agent VM and submitted with
fire_and_forget, so it keeps running when this process exits. A finished run
uploads its directory (main.py reads every upload back), its transcripts and
its result record to storage; the last run of the launch then shuts the
cluster down, and the keeper VM exits when the cluster stops and idles out.
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
    compute_start: int = 1                     # compute VMs at launch, and the pool's floor
    compute_max: int = 24
    compute_env: dict = dataclasses.field(default_factory=dict)
    keeper: bool = True                        # hold the pool's autoscaler on a small VM
    keeper_hours: float = 48.0
    idle_timeout: str = "1 hour"               # backstop only: a running or holding task keeps it busy
    require_main: bool = True                  # ship exactly origin/main (False for smoke tests of a branch)
    required_files: tuple = ("scripts/results_store.py", "scripts/compute.py", "scripts/decision_brief.py")

    def __post_init__(self):
        self.storage_prefix = self.storage_prefix or f"runs/{self.name}"
        self.out_dir = self.out_dir or os.path.expanduser(f"~/modeling-agentic-keep/{self.name}")
        self.labels, self.flags, self.required_files = tuple(self.labels), tuple(self.flags), tuple(self.required_files)
        bad = [k for k in ("--max-rounds", "--max-sessions") if k in self.flags]
        if bad:
            raise ValueError(f"set {bad} through max_rounds / max_sessions, not flags")
        if not self.labels or len(set(self.labels)) != len(self.labels):
            raise ValueError("labels must be non-empty and distinct")

    @property
    def state_file(self):
        return os.path.join(self.out_dir, "assignments.json")

    @property
    def keeper_name(self):
        return f"{self.name}-keeper"


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


def attach(name: str):
    """Attach to a live cluster; refuse rather than create one."""
    import coiled
    if not is_running(name):
        raise SystemExit(f"cluster {name!r} is not running; refusing to create a new one")
    return coiled.Cluster(name=name, workspace=WORKSPACE, shutdown_on_close=False, wait_for_workers=False)


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
    blob = subprocess.check_output(["git", "-C", cfg.tree, "archive", "--format=tar.gz", "HEAD"])
    return blob, head[:7]


# ---- commands ---------------------------------------------------------------------------

def _submit(cfg, client, label, worker, code, question, keys):
    from distributed import fire_and_forget
    fut = client.submit(ws.run_to_disk, label, code, question, list(cfg.flags), keys["ANTHROPIC_API_KEY"],
                        keys.get("OPENAI_API_KEY") if cfg.cross_model else None, list(cfg.labels),
                        cfg.storage_prefix, cfg.storage_account, cfg.max_rounds, cfg.max_sessions,
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
    workers = sorted(client.scheduler_info()["workers"])[:len(cfg.labels)]
    compute.register_compute_tags(client, exclude=workers)   # the agent VMs never take compute tasks
    assign = {}
    for label, w in zip(cfg.labels, workers):
        _submit(cfg, client, label, w, code, question, keys)
        assign[label] = {"worker": w, "commit": commit, "submitted": time.strftime("%F %T")}
        print(f"fired {label} -> {w} @ {commit}", flush=True)
    json.dump(assign, open(cfg.state_file, "w"), indent=1)
    time.sleep(10)
    if cfg.keeper:
        start_keeper(cfg, compute.ADAPT_OPTIONS)
    print(f"launched {cfg.name}: results go to outputs/{cfg.storage_prefix}/; the cluster stops itself "
          "once every run is stored. Safe to disconnect.")


def start_keeper(cfg, adapt_options):
    import coiled
    import dask
    from distributed import fire_and_forget
    lo, hi = len(cfg.labels) + cfg.compute_start, len(cfg.labels) + cfg.compute_max
    kc = coiled.Cluster(name=cfg.keeper_name, workspace=WORKSPACE, n_workers=1, worker_cpu=2,
                        worker_memory="8 GiB", scheduler_cpu=2, spot_policy="on-demand",
                        idle_timeout="30 minutes", shutdown_on_close=False)
    c = kc.get_client()
    c.wait_for_workers(1, timeout=1200)
    c.upload_file(os.path.join(HERE, "worker_side.py"))
    # The Coiled token goes in as a task argument (memory only, never an env var or a file).
    fut = c.submit(ws.keeper_task, dask.config.get("coiled.token"), cfg.name, WORKSPACE, lo, hi,
                   cfg.keeper_hours, dict(adapt_options), pure=False, key=f"keeper-{cfg.name}")
    fire_and_forget(fut)
    time.sleep(30)
    w = sorted(c.scheduler_info()["workers"])[0]
    print(c.run(ws.keeper_log, workers=[w])[w].strip().splitlines()[-1:], f"(keeper {cfg.keeper_name}, pool {lo}..{hi})")


def status(cfg):
    run, keep = is_running(cfg.name), is_running(cfg.keeper_name)
    print(f"cluster {cfg.name}: {'running' if run else 'stopped'}; keeper: {'running' if keep else 'stopped'}")
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


def collect_from_storage(cfg, store, label):
    """One label from storage: the result record, then the run directory it names."""
    dest = os.path.join(cfg.out_dir, label)
    rj = os.path.join(cfg.out_dir, f"{label}.result.json")
    store.download(f"outputs/{cfg.storage_prefix}/result_{label}.json", rj, cfg.storage_account)
    meta = json.load(open(rj))
    if not meta.get("run_stored"):
        raise SystemExit(f"{label}: the result record names no stored run")
    tgz = os.path.join(cfg.out_dir, f"{label}.run.tar.gz")
    store.download(meta["run_stored"], tgz, cfg.storage_account)
    os.makedirs(os.path.join(dest, "runs"), exist_ok=True)
    with tarfile.open(tgz, mode="r:gz") as t:
        t.extractall(os.path.join(dest, "runs"), filter="data")
    json.dump(meta, open(os.path.join(dest, "result.json"), "w"), indent=1)
    return meta


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
    client = assign = None
    pending = []
    for label in cfg.labels:
        if os.path.exists(os.path.join(cfg.out_dir, label, "result.json")):
            print(f"{label}: already collected")
            continue
        try:
            meta = collect_from_storage(cfg, results_store, label)
            print(f"{label}: exit={meta['exit_code']} {meta['duration_s']}s -> {cfg.out_dir}/{label} (from storage)")
            continue
        except Exception as e:  # noqa: BLE001 — not (yet) in storage
            if not running:
                print(f"{label}: not in storage ({type(e).__name__}) and the cluster has stopped")
                continue
        if client is None:
            client, assign = attach(cfg.name).get_client(), json.load(open(cfg.state_file))
        w = assign[label]["worker"]
        if _on_worker(client, ws.fetch_meta, label, worker=w)["state"] != "done":
            print(f"{label}: still running")
            pending.append(label)
            continue
        tgz = os.path.join(cfg.out_dir, f"{label}.result.tar.gz")
        meta = _download(client, label, w, tgz)
        dest = os.path.join(cfg.out_dir, label)
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
    for name in (cfg.name, cfg.keeper_name):
        if is_running(name):
            attach(name).shutdown()
            print(f"{name}: shutdown requested")
        else:
            print(f"{name}: not running")


def relaunch(cfg, label):
    """Re-fire one run on its pinned agent VM (e.g. after a preflight-only fix)."""
    assign = json.load(open(cfg.state_file))
    code, commit = code_tarball(cfg)
    client = attach(cfg.name).get_client()
    client.upload_file(os.path.join(HERE, "worker_side.py"))
    _submit(cfg, client, label, assign[label]["worker"], code, open(cfg.question_file).read().strip(), read_keys())
    assign[label].update(commit=commit, submitted=time.strftime("%F %T"), relaunched=True)
    json.dump(assign, open(cfg.state_file, "w"), indent=1)
    print(f"re-fired {label} -> {assign[label]['worker']} @ {commit}")


def _side(cfg, fn, *args, label=None):
    label = label or cfg.labels[0]
    assign = json.load(open(cfg.state_file))
    return _on_worker(attach(cfg.name).get_client(), fn, label, *args, worker=assign[label]["worker"])


# ---- self-test --------------------------------------------------------------------------

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
    r1, r2 = f"outputs/{p}/result_R1.json", f"outputs/{p}/result_R2.json"
    ok(not ws.all_stored(Store([(r1, now)]), p, None, labels, now - 60), "RC5 one run of two stored: keep running")
    ok(ws.all_stored(Store([(r1, now), (r2, now)]), p, None, labels, now - 60), "RC6 both stored: shut down")
    ok(not ws.all_stored(Store([(r1, now), (r2, now - 3 * 86400)]), p, None, labels, now - 60),
       "RC7 a record from an earlier launch does not count")

    listing = [{"name": "a", "current_state": {"state": "stopped"}}, {"name": "a", "current_state": {"state": "ready"}},
               {"name": "b", "current_state": "Stopping"}, {"name": "c", "current_state": None}]
    ok(is_running("a", lambda: listing), "RC8 a ready cluster among stopped ones of the same name is running")
    ok(not is_running("b", lambda: listing) and not is_running("c", lambda: listing)
       and not is_running("zz", lambda: listing), "RC9 stopping / no state / absent are not attachable")

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
        st = Store(files={"outputs/runs/t/result_R1.json": rec, "outputs/runs/t/r.tar.gz": run})
        m = collect_from_storage(cfg, st, "R1")
        ok(m["exit_code"] == 0 and open(os.path.join(d, "R1", "runs", "r", "report.md")).read() == "# report",
           "RC12 collect from storage unpacks the stored run directory")
        try:
            collect_from_storage(cfg, Store(files={"outputs/runs/t/result_R1.json": b'{"run_stored": null}'}), "R1")
            ok(False, "RC13 a record naming no stored run fails")
        except SystemExit:
            ok(True, "RC13 a record naming no stored run fails")
        cf = os.path.join(d, "c.py")
        open(cf, "w").write("from run_cluster import RunConfig\nCONFIG = RunConfig(name='z', question_file='q')\n")
        ok(load_config(cf).name == "z", "RC14 a config file loads")
        open(cf, "w").write("X = 1\n")
        try:
            load_config(cf)
            ok(False, "RC15 a config without CONFIG is refused")
        except SystemExit:
            ok(True, "RC15 a config without CONFIG is refused")
    for f in sorted(os.listdir(os.path.join(HERE, "configs"))):
        if f.endswith(".py"):
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
    elif cmd == "keeper-log":
        c = attach(cfg.keeper_name).get_client()
        w = sorted(c.scheduler_info()["workers"])[0]
        print(c.run(ws.keeper_log, workers=[w])[w])
    elif cmd == "brief":
        r = _side(cfg, ws.brief_side)
        print(f"run dir: {r.get('run_dir')}\nstatus: {r.get('status') or r.get('state')}\n")
        print(r.get("brief_md") or "(no decision_brief.md yet)")
    elif cmd == "report":
        print(_side(cfg, ws.report_side))
    elif cmd == "sign":
        r = _side(cfg, ws.sign_side, open(rest[0]).read(), " ".join(rest[1:]))
        print(r["out"])
        return r["rc"]
    else:
        sys.exit(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
