#!/usr/bin/env python3
"""
Phase 24 α — compute service: run a model function over many parameter sets.

    python3 scripts/compute.py map {run_dir} --fn models/outcome_fn.py::outcome_fn \
        --inputs models/grid_points.jsonl --out models/grid_results.jsonl

  --inputs   JSONL, one parameter dict per line (or a JSON list)
  --out      JSONL, one row per input, in input order
  a summary is printed and written next to --out (`<out>.summary.json`)

Backends (auto-selected):
  dask   when MA_COMPUTE_SCHEDULER is set (the launcher sets it on Coiled).
  local  otherwise: a process pool on this machine (cores - 1).

Usage:
    python3 scripts/compute.py map ...
    python3 scripts/compute.py reuse-check <run_dir> [--expect-all]
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import compute_worker
from compute_worker import (  # noqa: F401 — re-exported for callers & tests
    BREAKER_MIN, CAP_STOP, INFRA, MEM_HEADROOM, MEM_USABLE, MODEL, STOPPED, TAIL,
    _CALLS_DIR, _CHILD_EVENTS, _FP_HASH_MB, _FP_REPO_SKIP, _OBSERVED,
    _audit, _avail_mb, _breaker, _budget_deadline, _call, _cancel_call, _chunks, _claim,
    _digest, _err_key, _file_digest, _group_alive, _guard_registration, _had_child,
    _jsonable, _kill_chunk_group_others, _kill_groups, _killpg, _known_core_s,
    _local_chunk, _local_init, _map_local, _mem_slots, _no_symlink_path, _observe,
    _observed_files, _peak_mb, _pid_alive, _pid_start, _proc_stat, _ps_start,
    _read_inputs, _remote_task, _revoke_chunk, _rows_done, _same_process, _stop_chunk,
    _task_seen, load_fn, recycle_every,
)

REPO_CODE_DIRS = ("scripts", "toolkit")
DEFAULT_MAX_TASKS = 20000
RUN_CAP_CORE_HOURS = "200"         # MA_COMPUTE_MAX_CORE_HOURS when unset: the run-wide cap
CORES_PER_WORKER = 8               # MA_COMPUTE_CORES_PER_WORKER when unset and no worker reports its cores
DEFAULT_MAX_FILE_MB = 100
LEDGER_REL = os.path.join("models", "compute_ledger.json")

RETIRED_EVENTS = 500       # scheduler events read for workers removed on purpose (_sched_view)
SCALEDOWN_RETRIES = 8      # resubmissions per input after a scale-down retired its worker, before they count as losses
INFRA_RETRIES = 3          # MA_COMPUTE_INFRA_RETRIES when unset: resubmissions per input after its chunk was lost
INFRA_BACKOFF_S = 10.0     # MA_COMPUTE_INFRA_BACKOFF_S when unset: wait before the first resubmission, doubled after
NO_WORKER_WAIT_S = 1800    # MA_COMPUTE_NO_WORKER_WAIT_S when unset: how long a call waits with no compute worker
COMM_RETRIES = 5           # MA_COMPUTE_COMM_RETRIES when unset: further tries of a scheduler call whose connection dropped
COMM_WAIT_S = 120.0        # MA_COMPUTE_COMM_WAIT_S when unset: the (doubling) waits before those tries add up to this


# v2 (Nigeria full run, 2026-11-02): scripts/pipeline.py's fixed 1-h limit killed an engine batch at 184 of 256
# design points. A pipeline step that runs the run's batches gets its wall-clock limit from the run cap instead.
STEP_WALL_MARGIN = 2.0              # workers join late, tasks do not pack perfectly, a lost VM reruns its inputs
STEP_WALL_OWN_S = 3600.0            # the step's own work besides the batches (what the fixed limit allowed)
STEP_WALL_CEILING_S = 24 * 3600.0


def step_wall_s(environ=None) -> float | None:
    """Soft deadline for a pipeline step that runs the run's engine batches (scripts/pipeline.py warns past it and
    kills only at its backstop, BACKSTOP_MULTIPLE x it or BACKSTOP_FLOOR_S): the time the cluster's target pool
    (MA_COMPUTE_TARGET_WORKERS x MA_COMPUTE_PROCS_PER_WORKER or MA_COMPUTE_CORES_PER_WORKER cores) takes to use
    the whole run cap (MA_COMPUTE_MAX_CORE_HOURS), times STEP_WALL_MARGIN, plus STEP_WALL_OWN_S, at most
    STEP_WALL_CEILING_S. The ledger holds every call in the step to the run cap, so this limit is a backstop that
    the cap reaches first on a busy pool. None without a cluster (none of MA_COMPUTE_SCHEDULER, MA_COMPUTE_RESOURCE,
    MA_COMPUTE_TARGET_WORKERS): the runner's default applies. A value that is not a finite number of the right
    kind raises ValueError (fail closed: the run must not start on a budget nobody can read)."""
    env = os.environ if environ is None else environ
    if not any(env.get(k) for k in ("MA_COMPUTE_SCHEDULER", "MA_COMPUTE_RESOURCE", "MA_COMPUTE_TARGET_WORKERS")):
        return None

    def num(key, default, kind):
        raw = env.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            v = kind(str(raw).strip())
        except ValueError:
            raise ValueError(f"{key}={raw!r} is not {'an integer' if kind is int else 'a number'}") from None
        if v != v or v in (float("inf"), float("-inf")) or v < 0:
            raise ValueError(f"{key}={raw!r} must be a finite number >= 0")
        return v
    cap_h = num("MA_COMPUTE_MAX_CORE_HOURS", float(RUN_CAP_CORE_HOURS), float)
    workers = max(1, num("MA_COMPUTE_TARGET_WORKERS", 1, int))      # 0 or unset: no target, count one
    cores = num("MA_COMPUTE_PROCS_PER_WORKER", None, int)
    if cores is None:
        cores = num("MA_COMPUTE_CORES_PER_WORKER", CORES_PER_WORKER, int)
    if cores < 1:
        raise ValueError(f"cores per worker must be >= 1, got {cores}")
    return min(STEP_WALL_CEILING_S, STEP_WALL_MARGIN * cap_h * 3600 / (workers * cores) + STEP_WALL_OWN_S)


# --------------------------------------------------------------------------- #
# Dask backend
# --------------------------------------------------------------------------- #

def _built_engine_prod_files(run_dir: str, *, confined: bool = False) -> list[str]:
    """Production subset of `engine_build/` shipped to Coiled workers and fingerprinted (docs/design/
    agent-built-engines.md §5.1, BE34): `admission.json`, `signoff.json`, `request.json`, the signed
    snapshot (`snapshots/<sha>.json` + `snapshots/<sha>/*`), and `bench/*`. Never includes `.hidden/`,
    `reference/`, `try/`, `hidden_reveal.json`, or working copies under `models/ext/engine_*`.
    Refuses symlinked parent directories and reads metadata through no-follow fds (`lib.safe_io`)."""
    from lib import safe_io as SIO

    eb = os.path.join(run_dir, "engine_build")
    if os.path.islink(eb):
        if confined:
            raise ValueError("engine_build/ is a link: not fingerprinted (confined)")
        return []
    if not os.path.isdir(eb):
        return []

    def _reject_or_skip(rel_p: str) -> None:
        if confined:
            raise ValueError(f"{rel_p} is a link: not fingerprinted (confined)")

    out: list[str] = []
    for name in ("admission.json", "signoff.json", "request.json"):
        p = os.path.join(eb, name)
        if os.path.islink(p):
            _reject_or_skip(f"engine_build/{name}")
            continue
        if os.path.isfile(p):
            out.append(p)
    # Identify the signed snapshot sha256 via no-follow reads (from signoff.json, else admission.json)
    snap_sha = None
    for cand in ("signoff.json", "admission.json"):
        try:
            raw, _ = SIO.read_nofollow(run_dir, f"engine_build/{cand}")
        except SIO.LinkRefused:
            _reject_or_skip(f"engine_build/{cand}")
            continue
        except (FileNotFoundError, OSError):
            continue
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        s = doc.get("snapshot_sha256") if isinstance(doc, dict) else None
        if isinstance(s, str) and len(s) == 64 and all(c in "0123456789abcdef" for c in s):
            snap_sha = s
            break
    if snap_sha:
        snaps = os.path.join(eb, "snapshots")
        if os.path.islink(snaps):
            _reject_or_skip("engine_build/snapshots")
        elif os.path.isdir(snaps):
            man = os.path.join(snaps, f"{snap_sha}.json")
            if os.path.islink(man):
                _reject_or_skip(f"engine_build/snapshots/{snap_sha}.json")
            elif os.path.isfile(man):
                out.append(man)
            sdir = os.path.join(snaps, snap_sha)
            if os.path.islink(sdir):
                _reject_or_skip(f"engine_build/snapshots/{snap_sha}")
            elif os.path.isdir(sdir):
                for fn in sorted(os.listdir(sdir)):
                    fp = os.path.join(sdir, fn)
                    if os.path.islink(fp):
                        _reject_or_skip(f"engine_build/snapshots/{snap_sha}/{fn}")
                    elif os.path.isfile(fp):
                        out.append(fp)
    bench = os.path.join(eb, "bench")
    if os.path.islink(bench):
        _reject_or_skip("engine_build/bench")
    elif os.path.isdir(bench):
        for fn in sorted(os.listdir(bench)):
            fp = os.path.join(bench, fn)
            if os.path.islink(fp):
                _reject_or_skip(f"engine_build/bench/{fn}")
            elif os.path.isfile(fp):
                out.append(fp)
    return sorted(set(out))


def _built_engine_bundle_extras(run_dir: str) -> list[str]:
    """Top-level run files that `BuiltEngine.__init__` verifies on a fresh Coiled worker
    (`run_state.json`, `decision_spec.yaml`, and spec sign-off record). Shipped in `make_bundle`
    when `engine_build/` production files exist; never hashed into `_run_files_digest` so RS1 holds."""
    out: list[str] = []
    for name in ("run_state.json", "decision_spec.yaml", "engine_choice.json",
                 "decision_spec.signoff.json"):
        p = os.path.join(run_dir, name)
        if os.path.isfile(p) and not os.path.islink(p):
            out.append(p)
    return out


def make_bundle(run_dir: str, max_file_mb: float = DEFAULT_MAX_FILE_MB) -> tuple[bytes, dict]:
    """tar.gz of the run's models/ and data/, the repo's scripts/ and toolkit/, and the declared data set
    (FP_DATA_SET: repo data/ excluding eval dirs, and non-Python files of toolkit/), with
    ABSOLUTE member paths so workers recreate the same layout. The data set is exactly the files the reuse key's
    data_set part hashes (_data_set_files), shipped whatever their size: a worker that reads one computes with
    the launcher's copy, the one the key names (BD1-BD4). Logs and the records this module keeps beside a batch
    are left out (`_bundle_skips`; a long run's logs and partial rows were shipped to every VM), except a file of
    the data set, which always ships (CD6c)."""
    run_dir = os.path.realpath(run_dir)
    skipped, n, n_out = [], 0, 0
    data_set = {os.path.join(REPO, r) for r in _data_set_files(FP_DATA_SET, skip=run_dir)}
    added, unreadable = set(), []
    buf = io.BytesIO()
    # dereference: symlinked inputs are stored as files (workers extract with
    # the "data" filter, which rejects absolute links).
    with tarfile.open(fileobj=buf, mode="w:gz", dereference=True) as t:
        for root in (os.path.join(run_dir, "models"), os.path.join(run_dir, "data"),
                 *(os.path.join(REPO, x) for x in REPO_CODE_DIRS)):
            # Follow symlinked dirs; stop only at a loop (a dir that is its own
            # ancestor). Two names for one dir are both bundled: models use either.
            ancestors = {root: frozenset()}
            for dp, dirs, files in os.walk(root, followlinks=True):
                real = os.path.realpath(dp)
                anc = ancestors.pop(dp, frozenset())
                if real in anc:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git") and not d.startswith("_truth")
                           and not (root == os.path.join(run_dir, "models")
                                    and os.path.realpath(dp) == os.path.realpath(os.path.join(run_dir, "models", "ext"))
                                    and d.startswith("engine_"))]
                for d in dirs:
                    ancestors[os.path.join(dp, d)] = anc | {real}
                names, in_data = set(files), root == os.path.join(run_dir, "data")
                for fn in files:
                    p = os.path.join(dp, fn)
                    if p not in data_set and _bundle_skips(fn, names, in_data, dp == os.path.join(run_dir, "models")):
                        n_out += 1
                        continue
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    if size > max_file_mb * 2**20 and p not in data_set:
                        skipped.append((os.path.relpath(p, run_dir), round(size / 2**20, 1)))
                        continue
                    if p in added:
                        continue
                    t.add(p, arcname=p.lstrip("/"))
                    added.add(p)
                    n += 1
        be_prod = _built_engine_prod_files(run_dir)
        for p in be_prod + (_built_engine_bundle_extras(run_dir) if be_prod else []):
            if p in added:
                continue
            try:
                t.add(p, arcname=p.lstrip("/"))
            except OSError:
                continue
            added.add(p)
            n += 1
        for p in sorted(data_set - added):     # repo data/ (toolkit/ is above)
            try:
                t.add(p, arcname=p.lstrip("/"))
            except OSError:                    # a dangling link: the key hashes it as unreadable; no worker has it
                unreadable.append(os.path.relpath(p, REPO))
                continue
            added.add(p)
            n += 1
    data = buf.getvalue()
    return data, {"files": n, "bundle_mb": round(len(data) / 2**20, 1), "skipped_large": skipped,
                  "skipped_outputs": n_out,
                  "data_set_files": len(data_set & added), "data_set_unreadable": unreadable,
                  "sha256": hashlib.sha256(data).hexdigest()}


try:
    from distributed.diagnostics.plugin import SchedulerPlugin as _SchedulerPlugin, WorkerPlugin as _WorkerPlugin
except ImportError:          # local backend only
    _SchedulerPlugin = _WorkerPlugin = object


class ComputeTagPlugin(_WorkerPlugin):
    """Phase 30: tag every worker that joins the cluster with the compute
    resource, except the run's own workers, so an autoscaling pool works with
    resource-restricted tasks. Register with client.register_plugin(...)."""

    name = "ma-compute-tag"

    def __init__(self, resource: str = "compute", exclude: list[str] | tuple = ()):
        self.resource, self.exclude = resource, set(exclude)

    async def setup(self, worker):
        if worker.address not in self.exclude:
            await worker.set_resources(**{self.resource: 1})

    def teardown(self, worker):
        return None


class BusyWorkerGuard(_SchedulerPlugin):
    """2026-11-02 (Nigeria v2 full run F3): the autoscaler never retires a
    worker that is running a task. Adaptive scaling (dask's, and Coiled's on
    top of it) asks `Scheduler.workers_to_close(target=n)`; given a target, it
    takes idle workers first and then busy ones, and `retire_workers` does not
    wait for a running task. A pinned chunk there can run nowhere else, so its
    work was lost (F3: 2 chunks, then their 2 resubmissions). Every caller of
    workers_to_close filters its candidates through `valid_workers_downscaling`,
    so this holds whichever process runs adapt (the keeper, client-side). It
    also keeps the run VM's worker (busy with the run) from being chosen.
    Registered by the launcher (`register_compute_tags`) and by every dask
    call (idempotent: a cluster started before this gets it on its next batch)."""

    name = "ma-compute-busy-guard"
    idempotent = True

    def valid_workers_downscaling(self, scheduler, workers):
        return [ws for ws in workers if not ws.processing]


# Launcher settings for an autoscaling pool (Coiled check, Phase 30). Coiled's
# adapt adds VMs only when queued work exceeds `target_duration` (default 3 min),
# and dask counts each worker thread as a parallel slot although a compute VM
# runs one chunk at a time on all its cores: with the defaults the pool stayed
# at 1 VM for a 45-min batch. Use as coiled.Cluster(worker_options=WORKER_OPTIONS)
# and cluster.adapt(..., **ADAPT_OPTIONS).
WORKER_OPTIONS = {"nthreads": 1}
# Phase 38: compute VMs in priority order across Azure VM families with separate core
# quotas (8 vCPU, 64 GiB each). E7 attempt 7: workers were refused for most of the run at
# 344 of 350 standardEv5Family cores, a quota shared with the subscription's other users.
# Coiled tries a list in order for availability but does not fall back on a quota refusal
# (E7 attempt 8: refused at 348 of 350 standardESv5Family cores with this list); the
# launcher's overflow pool covers that (cloud/run_cluster.py, Phase 42 β).
WORKER_VM_TYPES = ["Standard_E8s_v5", "Standard_E8as_v5", "Standard_E8ds_v5"]
# Phase 42 β: an idle VM is retired after interval x wait_count = 10 min (dask's default is
# 5 s x 24 = 2 min), so consecutive batches reuse VMs; attempt 8 created 734 VMs in 18 h.
# cloud/run_cluster.py derives these from RunConfig.scale_down_after / adapt_interval.
# Whatever the target, a worker running a chunk is never the one retired (BusyWorkerGuard).
ADAPT_OPTIONS = {"target_duration": "20s", "interval": "15s", "wait_count": 40}


def _upload_compute(c) -> None:
    """Ship compute_worker.py then compute.py to remote nodes without reloading an in-process scheduler."""
    import uuid
    cw_path = os.path.abspath(compute_worker.__file__)
    c_path = os.path.abspath(__file__)
    has_sched = False
    if hasattr(c, "run_on_scheduler"):
        try:
            has_sched = bool(c.run_on_scheduler(
                lambda: "compute" in sys.modules and "compute_worker" in sys.modules
            ))
        except Exception:  # noqa: BLE001
            has_sched = False
    if not has_sched:
        c.upload_file(cw_path)
        c.upload_file(c_path)
        return
    try:
        from distributed.diagnostics.plugin import UploadFile
        c.register_plugin(UploadFile(cw_path, load=True), name=f"{cw_path}{uuid.uuid4()}")
        c.register_plugin(UploadFile(c_path, load=True), name=f"{c_path}{uuid.uuid4()}")
    except Exception:  # noqa: BLE001
        c.upload_file(cw_path)
        c.upload_file(c_path)


def register_compute_tags(client, exclude: list[str] | tuple = (), resource: str = "compute") -> None:
    """Tag compute workers on an autoscaling cluster. Uploads this module first
    (as a worker plugin, so workers that join later get it too): the plugin is
    pickled by reference to `compute`, which cluster workers cannot otherwise
    import (Coiled check, Phase 30; same class of bug as PR #20)."""
    _upload_compute(client)
    client.register_plugin(ComputeTagPlugin(resource, exclude=list(exclude)), name=ComputeTagPlugin.name)
    client.register_plugin(BusyWorkerGuard(), name=BusyWorkerGuard.name)


def export_worker_env(tls_dir: str, worker=None) -> dict:
    """Call INSIDE a dask worker task (the launcher's run job). Returns the
    env a subprocess needs to reach this cluster: the scheduler address and,
    for TLS clusters such as Coiled, the worker's own client credentials
    written to files only this user can read. Keep `tls_dir` outside the run
    directory (run trees are archived)."""
    if worker is None:
        from distributed import get_worker
        worker = get_worker()
    env = {"MA_COMPUTE_SCHEDULER": worker.scheduler.address}
    sec = getattr(worker, "security", None)
    # Coiled workers hold only worker credentials (tls_client_* unset); the
    # scheduler accepts them from a client too, so fall back to them.
    creds = {}
    if sec is not None:
        creds = {
            "ca.pem": getattr(sec, "tls_ca_file", None),
            "cert.pem": getattr(sec, "tls_client_cert", None) or getattr(sec, "tls_worker_cert", None),
            "key.pem": getattr(sec, "tls_client_key", None) or getattr(sec, "tls_worker_key", None),
        }
    if any(creds.values()):
        os.makedirs(tls_dir, mode=0o700, exist_ok=True)
        os.chmod(tls_dir, 0o700)                 # mode= is ignored for an existing dir
        for fn, v in creds.items():
            if not v:
                continue
            dest = os.path.join(tls_dir, fn)
            data = v if str(v).lstrip().startswith("-----BEGIN") else open(os.path.abspath(v)).read()
            if os.path.lexists(dest):
                os.unlink(dest)                  # O_CREAT's mode applies only to new files
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(data)
        env["MA_COMPUTE_TLS_DIR"] = tls_dir
    return env


def _security():
    d = os.environ.get("MA_COMPUTE_TLS_DIR")
    if not d:
        return None
    from distributed.security import Security
    pem = lambda n: (lambda p: p if os.path.exists(p) else None)(os.path.join(d, n))  # noqa: E731
    return Security(tls_ca_file=pem("ca.pem"), tls_client_cert=pem("cert.pem"),
                    tls_client_key=pem("key.pem"), require_encryption=True)


# The scheduler's worker table. distributed's Client.scheduler_info() returns {} (no
# "workers" key) whenever the client is not connected: before its first identity reply,
# while it reconnects, after a failed reconnect (PR #99: v2blind1's launch died on
# KeyError: 'workers'). Every read here goes through cloud/worker_side.py's bounded
# reader, which waits that out with doubling backoff and past the bound raises
# SchedulerInfoUnavailable: an unpopulated identity is never read as "no workers".
SCHED_ATTEMPTS, SCHED_DELAY_S = 6, 5.0                 # dispatch: up to ~2 min of waiting
SCHED_SETTLE_ATTEMPTS, SCHED_SETTLE_DELAY_S = 3, 2.0   # settling a call that is ending


def _worker_side():
    """cloud/worker_side.py (imports nothing at module level), loaded once by path under a
    private name, so a run's models/worker_side.py on sys.path cannot shadow it."""
    mod = sys.modules.get("ma_compute_worker_side")
    if mod is None:
        spec = importlib.util.spec_from_file_location("ma_compute_worker_side",
                                                      os.path.join(REPO, "cloud", "worker_side.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules["ma_compute_worker_side"] = mod
    return mod


def _sched_workers(c, attempts=None, delay_s=None) -> dict:
    """{address: info} from client `c`, read with worker_side's bounded reader."""
    return _worker_side().scheduler_workers(
        lambda: c, attempts=attempts or SCHED_ATTEMPTS, delay_s=SCHED_DELAY_S if delay_s is None else delay_s,
        log=lambda m: print(f"compute: {m}", file=sys.stderr, flush=True))


def _cancel_remote(c, call_id, exclude) -> dict:
    """Ask every worker (but the excluded run workers) to stop `call_id`'s chunks. Raises
    SchedulerInfoUnavailable when the worker table cannot be read: the caller then leaves
    the stop unconfirmed instead of reaching no worker and calling that confirmed."""
    w = _sched_workers(c, SCHED_SETTLE_ATTEMPTS, SCHED_SETTLE_DELAY_S)
    return c.run(_cancel_call, call_id, workers=[a for a in w if a not in exclude])


def _gone_workers(c, workers) -> list:
    """`workers` no longer in the scheduler's table. Raises SchedulerInfoUnavailable when the
    table cannot be read (the caller then charges every worker as busy for the call)."""
    live = _sched_workers(c, SCHED_SETTLE_ATTEMPTS, SCHED_SETTLE_DELAY_S)
    return [w for w in workers if w not in live]


# Phase 45 δ (A16): a demand marker asks for this resource, which no worker has,
# so it waits in the scheduler's unrunnable set and never runs.
DEMAND_RESOURCE = "ma-compute-demand"


def _demand_marker():
    """Never runs (see DEMAND_RESOURCE); it only shows the autoscaler queued work."""
    return None


def _sched_view(res=None, key=None, dask_scheduler=None) -> dict:
    """Runs on the scheduler (Phase 45 δ): {"workers": {address: [carries the
    resource tag, tasks processing there, accepting work]}, "held": whether
    some worker still holds `key` (the scattered bundle), "retired": the
    recently removed workers whose removal the scheduler records as expected
    (a scale-down or retire, not a crash)}."""
    ts = dask_scheduler.tasks.get(key) if key is not None else None
    retired = set()
    try:
        for _t, e in tuple(dask_scheduler.get_events("all"))[-RETIRED_EVENTS:]:
            if isinstance(e, dict) and e.get("action") == "remove-worker" and e.get("worker"):
                (retired.add if e.get("expected") else retired.discard)(e["worker"])
    except Exception:  # noqa: BLE001 — unknown: every departure counts as a loss
        retired = set()
    return {"workers": {a: [res is None or res in (ws.resources or {}), len(ws.processing),
                            getattr(ws.status, "name", str(ws.status)) == "running"]
                        for a, ws in dask_scheduler.workers.items()},
            "held": bool(ts is not None and ts.who_has), "retired": sorted(retired)}


def _say(msg: str) -> None:
    """A notice on stderr that can never end the call (a closed pipe under it is ignored)."""
    try:
        print(msg, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _comm_retry(call, what: str, progress: dict | None = None, deadline: float | None = None):
    """`call()`, tried again when the scheduler connection drops under it (an OSError; distributed's
    CommClosedError is one): up to MA_COMPUTE_COMM_RETRIES more times, after doubling waits that add up to
    MA_COMPUTE_COMM_WAIT_S, while the dask client reconnects by itself. After that the error is raised, as it
    was at once before. A TimeoutError is the caller's own time limit and is never retried. No new client is
    made: one that has closed for good fails each try, and the call ends as it did. No wait goes past
    `deadline` (the batch's own)."""
    n = max(0, int(os.environ.get("MA_COMPUTE_COMM_RETRIES") or COMM_RETRIES))
    total = float(os.environ.get("MA_COMPUTE_COMM_WAIT_S") or COMM_WAIT_S)
    for k in range(n + 1):
        try:
            return call()
        except TimeoutError:
            raise
        except OSError as e:
            if k == n:
                raise
            wait_s = total * 2 ** k / (2 ** n - 1)
            if progress is not None:
                progress["comm_retries"] = progress.get("comm_retries", 0) + 1
            if deadline is not None:
                wait_s = min(wait_s, max(0.0, deadline - time.time()))
            _say(f"compute: scheduler connection lost under {what} ({type(e).__name__}: {str(e)[:120]}); "
                 f"try {k + 2} of {n + 1} in {wait_s:.3g} s")
            time.sleep(wait_s)


def _retired_on_purpose(exc, worker, read_view, last_retired=()) -> bool:
    """Whether a chunk whose result raised `exc` lost its worker to a scale-down. dask reports such a loss on
    the future (KilledWorker, CancelledError), and that can arrive before the dispatch loop's next scheduler
    view shows the worker gone. A fresh view (`read_view`) must show the worker absent and its removal recorded
    as expected; with the view unread, the last view's retired set decides. Any other error is a loss."""
    if not {k.__name__ for k in type(exc).__mro__} & {"KilledWorker", "CancelledError"}:
        return False
    got = read_view()
    if got is None:
        return worker in last_retired
    return worker not in got[1] and worker in got[3]


def _map_dask(run_dir, fn_spec, inputs, max_workers, chunks_per_worker=4, deadline=None, allowance_s=None,
              progress=None, peak_mb=None, prior_ok=0):
    """The allowance is split over the chunks that can run at once; each chunk
    enforces its share from when it starts (queueing behind other calls on a
    shared pool does not count). Whenever the call ends with chunks still
    out (cap, timeout, interrupt), those chunks are stopped on the workers.

    Phase 35: what one chunk learns about memory (its pool broke, or its
    measured peak allowed fewer processes) sets the processes for every chunk
    submitted after it. A chunk whose subprocess died is resubmitted once with
    half the processes. A chunk lost with its VM (left, died, unconfirmed) is
    resubmitted up to MA_COMPUTE_INFRA_RETRIES times per input (the first time
    with half the processes), each lost attempt charged; with no compute worker
    taking work, the call waits MA_COMPUTE_NO_WORKER_WAIT_S for the pool, then
    its queued inputs fail as infra. A model error is never resubmitted; one
    repeating across chunks stops the call (`_breaker`).

    Phase 45 δ (A16): this loop places every chunk itself. Each chunk is
    pinned to a compute worker that has nothing processing, and the worker
    list is re-read every MA_COMPUTE_DISPATCH_S seconds, so a VM that joins
    mid-batch gets a chunk at once. Dask's work stealing is not relied on:
    E7 attempt 8's 34-hour pipeline task on the same scheduler inflated the
    mean occupancy so far that no compute worker counted as saturated, and
    24 queued chunks sat on one VM while 23 others idled. Work this call
    could run on more VMs is shown to the autoscaler as demand markers
    (`_demand_marker`), never as real chunks queued on a busy worker. A chunk
    that has not claimed its start (`token`) on a worker that runs another
    task or has stopped taking work is revoked (`_revoke_chunk`) and placed
    again; a started chunk is never retried while it may be running."""
    import random
    import uuid
    import cloudpickle
    from dask.utils import funcname
    from distributed import Client, wait
    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    cloudpickle.register_pickle_by_value(compute_worker)
    addr = os.environ["MA_COMPUTE_SCHEDULER"]
    exclude = {w.strip() for w in os.environ.get("MA_COMPUTE_EXCLUDE_WORKERS", "").split(",") if w.strip()}
    bundle, binfo = make_bundle(run_dir)
    call_id = uuid.uuid4().hex
    t0_call = time.time()
    progress = {} if progress is None else progress
    with Client(addr, timeout="60s", security=_security()) as c:
        # Dask pickles functions by reference when it can, so the scheduler and
        # workers must be able to import this module: ship it to them.
        _upload_compute(c)
        # The autoscaler must not retire a worker under a running chunk. Not installed
        # (the scheduler refuses it): the call goes on, recovery below still applies,
        # and the summary says why.
        try:
            c.register_plugin(BusyWorkerGuard(), name=BusyWorkerGuard.name)
            guard = True
        except Exception as e:  # noqa: BLE001
            guard = f"not installed: {type(e).__name__}: {e}"[:200]
            print(f"compute: busy-worker guard {guard}", file=sys.stderr, flush=True)
        retries = int(os.environ.get("MA_COMPUTE_INFRA_RETRIES") or INFRA_RETRIES)
        backoff0 = float(os.environ.get("MA_COMPUTE_INFRA_BACKOFF_S") or INFRA_BACKOFF_S)
        not_before = {}                             # id(chunk) -> earliest resubmission time (the backoff)
        no_worker_wait = float(os.environ.get("MA_COMPUTE_NO_WORKER_WAIT_S") or NO_WORKER_WAIT_S)
        winfo = _sched_workers(c)
        # Phase 30: on an autoscaling pool, compute workers carry a resource tag
        # (ComputeTagPlugin) and tasks ask for it, so VMs that join mid-batch get
        # work and the run's own VM (untagged) never does.
        res = os.environ.get("MA_COMPUTE_RESOURCE")
        target = int(os.environ.get("MA_COMPUTE_TARGET_WORKERS") or 0)
        if res:
            workers = [w for w in winfo if w not in exclude and res in (winfo[w].get("resources") or {})]
        else:
            workers = [w for w in winfo if w not in exclude]
            if max_workers:
                workers = workers[:max_workers]
            if not workers:
                raise RuntimeError("no compute workers available (all are excluded run workers)")
        env_procs = os.environ.get("MA_COMPUTE_PROCS_PER_WORKER")
        cpus = c.run(os.cpu_count, workers=workers) if workers else {}
        procs = {w: int(env_procs) if env_procs else (cpus.get(w) or 1) for w in workers}   # compute VMs run nothing else
        cores_per_slot = max(procs.values()) if procs else int(env_procs or os.environ.get("MA_COMPUTE_CORES_PER_WORKER") or CORES_PER_WORKER)
        pool = max(len(workers), target if res else 0, 1)        # the pool the batch may grow into
        chunks = _chunks(list(enumerate(inputs)), pool * chunks_per_worker)
        slots = max(1, min(pool, len(chunks)))   # _remote_task runs one chunk at a time per VM
        progress.update(slots=slots, cores_per_slot=cores_per_slot, core_s=0.0)
        # The bundle is scattered once; a worker that runs a chunk fetches it
        # from a peer as a task dependency (no re-scatter per chunk).
        # On a tagged pool it goes to compute workers only, never the run's own VM; with none up yet it is
        # scattered in the loop below when the first one takes work (no chunk is placed before that).
        bfut = None
        if not res:
            bfut = c.scatter(bundle, workers=workers, broadcast=True)
        elif workers:
            try:
                bfut = c.scatter(bundle, workers=workers, broadcast=False, hash=False)
            except Exception:  # noqa: BLE001 — a worker left since the table was read: scattered in the loop
                bfut = None
        # Phase 45 δ: chunks are pinned to one free worker each (submit's `worker`).
        # The resource is still asked for, so a chunk never shares a VM with
        # another call's chunk that dask placed there.
        restrict = {"resources": {res: 1}} if res else {}
        poll_s = float(os.environ.get("MA_COMPUTE_DISPATCH_S", "2"))
        fixed = set(workers)                        # the explicit list (no resource tag)
        pinned, used_w, seen_w, markers = {}, set(), set(workers), []
        toks, claimed = {}, set()                   # submission tokens; chunks known to have started

        # At most `slots` chunks are out at once. Each gets a share of what is
        # left of the allowance after charges and the shares of chunks still
        # out, so the chunks together can never exceed the allowance.
        held = {}                                   # future -> its share
        cur = [None]                                # (future, share) being processed
        learned = {"procs": None, "peak": peak_mb}

        def inflight():
            return [{"n": len(futs[f][0]), "share": held[f] or None, "started": started.get(f, time.time()),
                     "cores": cores_per_slot} for f in held]

        def procs_for(override=None):
            p = [int(x) for x in (env_procs, learned["procs"], override) if x]
            return min(p) if p else None            # None: the worker uses its own core count

        def submit(ch, attempt, override=None, worker=None):
            share = None
            if allowance_s is not None:
                free = allowance_s - progress["core_s"] - sum(held.values())
                share = free / max(1, slots - len(held))
                if share <= 0:
                    return None
            # Recorded before submitting, so a caller killed right after is charged for it too.
            _note(progress, inflight() + [{"n": len(ch), "share": share, "started": time.time(),
                                           "cores": cores_per_slot}], call_id=call_id)
            # A lost chunk is resubmitted once here (and the lost attempt
            # charged), rather than through dask retries, which would hide it.
            tok_ = uuid.uuid4().hex                 # this submission's start claim
            # The key is fixed before the first try: a submission tried again after a dropped connection is
            # the same task for the scheduler, so nothing is placed twice.
            key_, n_ok = f"{funcname(_remote_task)}-{tok_}", prior_ok + sum(1 for r in rows if r.get("ok"))
            f = _comm_retry(lambda: c.submit(
                _remote_task, bfut, binfo["sha256"], run_dir, fn_spec, ch, deadline,
                procs_for(override), share, call_id, REPO, learned["peak"], n_ok, attempt != TAIL, tok_,
                key=key_, pure=False, workers=[worker], allow_other_workers=False, **restrict),
                "submit", progress, deadline)
            futs[f] = (ch, attempt, procs_for(override))
            pinned[f], toks[f] = worker, tok_
            held[f] = share or 0.0
            started[f] = time.time()
            return f
        futs, queue, started, errors = {}, [(ch, 0, None) for ch in chunks], {}, {}
        losses = {}                                 # input -> times its chunk was lost (chunk_lost)
        scaledowns = {}                             # input -> times a scale-down retired its chunk's worker

        def halved(used, worker=None):
            return max(1, int(used or cpus.get(worker) or cores_per_slot) // 2)

        def chunk_lost(f, ch, attempt, used, f_share, msg, scaled_down=False):
            """A chunk whose result is lost (its worker died or left, or the task
            failed): charge it, then resubmit it on the remaining or new workers,
            up to `retries` times per input, the first time with half the processes.
            A worker the scheduler removed on purpose (`scaled_down`: an autoscaler
            scale-down) says nothing about memory or a failing VM: the chunk goes
            back with the processes it had, without a backoff, and uses none of its
            infra retries, up to SCALEDOWN_RETRIES times per input (2026-11-12, P6)."""
            nonlocal lost
            # Its work is unknown: charge the share it was allowed
            # (chunks enforce their share), or estimate it at the end.
            # charged as busy since submission, never above its share
            busy = (time.time() - started.get(f, time.time())) * cores_per_slot
            if f_share:
                progress["core_s"] += min(f_share, busy)
            else:
                lost += 1
            n_scaled = 1 + max(scaledowns.get(i, 0) for i, _ in ch)
            if scaled_down and n_scaled <= SCALEDOWN_RETRIES and (deadline is None or time.time() < deadline):
                scaledowns.update({i: n_scaled for i, _ in ch})
                progress.setdefault("chunks", []).append(
                    {"n": len(ch), "attempt": attempt, "losses": max(losses.get(i, 0) for i, _ in ch),
                     "scaled_down": True, "error": msg[:200], "worker": pinned.get(f), "procs": used,
                     "retried_with_procs": used, "requeued_attempt": attempt,
                     "core_s": round(min(f_share, busy), 1) if f_share else None,
                     "wall_s": round(time.time() - started.get(f, time.time()), 1)})
                print(f"compute: {len(ch)} input(s) back in the queue: a scale-down retired their worker "
                      f"({n_scaled} of {SCALEDOWN_RETRIES})", file=sys.stderr, flush=True)
                # The attempt is kept: a first out-of-memory death after this still reruns with half
                # the processes (that path needs attempt 0; Codex review of #204).
                queue.insert(0, (ch, attempt, used))
                progress["resubmitted"] = progress.get("resubmitted", 0) + 1
                progress["scaled_down"] = progress.get("scaled_down", 0) + 1
                return
            n_lost = 1 + max(losses.get(i, 0) for i, _ in ch)
            losses.update({i: n_lost for i, _ in ch})
            progress.setdefault("chunks", []).append(
                {"n": len(ch), "attempt": attempt, "losses": n_lost, "error": msg[:200], "worker": pinned.get(f),
                 "procs": used, "core_s": round(min(f_share, busy), 1) if f_share else None,
                 "wall_s": round(time.time() - started.get(f, time.time()), 1)})
            errors.update({i: msg for i, _ in ch})
            if n_lost <= retries and (deadline is None or time.time() < deadline):
                # A rerun tail stays one (no chunk breaker); a lost VM says nothing more about memory. Each
                # resubmission waits a doubling backoff (infra only: a model error never comes here).
                wait_s = round(backoff0 * 2 ** (n_lost - 1), 3)
                progress["chunks"][-1]["backoff_s"] = wait_s
                not_before[id(ch)] = time.time() + wait_s
                print(f"compute: {len(ch)} input(s) lost with their chunk ({msg[:120]}); resubmission {n_lost} of "
                      f"{retries} in {wait_s:g} s", file=sys.stderr, flush=True)
                queue.insert(0, (ch, TAIL if attempt == TAIL else 1, halved(used) if n_lost == 1 else used))
                progress["resubmitted"] = progress.get("resubmitted", 0) + 1
                return
            rows.extend({"i": i, "ok": False, "kind": INFRA, "error": errors[i]} for i, _ in ch)

        def view():
            """({address: [tagged, processing, accepting]} for the workers chunks
            may go to, every worker the scheduler knows, whether the bundle is
            still held), or None if unread."""
            try:
                v = c.run_on_scheduler(_sched_view, res, bfut.key if bfut is not None else None)
                allw = v["workers"]
            except Exception:  # noqa: BLE001 — keep going on the last state; nothing is declared lost
                return None
            ok_w = {a: s for a, s in allw.items()
                    if a not in exclude and (s[0] if res else a in fixed)}
            return ok_w, allw, v["held"], set(v.get("retired") or ())

        def demand(n):
            """Keep `n` demand markers queued: the autoscaler counts them as tasks
            waiting for a worker (as it did the queued chunks before Phase 45 δ).
            Their key has the chunks' prefix, so they are weighted by the chunks'
            measured duration; they never run, so they never change it."""
            markers[:] = [m for m in markers if m.status == "pending"]
            if len(markers) > n:
                extra, markers[:] = markers[n:], markers[:n]
                c.cancel(extra)
            while len(markers) < n:
                markers.append(c.submit(_demand_marker, key=f"{funcname(_remote_task)}-{uuid.uuid4().hex}",
                                        resources={res: 1, DEMAND_RESOURCE: 1}, pure=False))
        pending, rows, stop, lost = set(), [], None, 0
        n_compute = len(workers)                    # compute workers in the last scheduler view
        no_w = None                                 # since when no compute worker has been taking work
        blind, retired_w = None, set()              # since when nothing could be placed (view unread, no bundle)
        try:
            while queue or pending:
                _note(progress, inflight(), call_id=call_id)
                got_v, free, unplaced = view(), [], False
                if got_v is not None:
                    ok_w, allw, held_b, retired_w = got_v
                    n_compute = len(ok_w)
                    for a in [a for a in ok_w if a not in seen_w]:       # joined since the call started
                        seen_w.add(a)
                        try:
                            n_c = int(env_procs) if env_procs else int(c.run(os.cpu_count, workers=[a])[a] or 1)
                        except Exception:  # noqa: BLE001 — not answering yet: assume the largest VM seen
                            n_c = cores_per_slot
                        cpus[a] = procs[a] = n_c
                        cores_per_slot = max(cores_per_slot, n_c)         # charges bound its work too
                        progress["cores_per_slot"] = cores_per_slot
                    if res:
                        slots = max(slots, min(len(chunks), len(ok_w)))   # VMs beyond the target may join
                        progress["slots"] = slots
                    # A pinned chunk whose worker has left can run nowhere else.
                    for f in [f for f in pending if pinned[f] not in allw and not f.done()]:
                        pending.discard(f)
                        ch, attempt, used = futs[f]
                        f.cancel()
                        chunk_lost(f, ch, attempt, used, held.pop(f, None),
                                   f"chunk failed: its worker {pinned[f]} left the cluster",
                                   scaled_down=pinned[f] in retired_w)
                    # A chunk that has not started on a worker that has stopped taking
                    # work (closing, paused) or that runs something else (another call
                    # picked the same idle worker from the same snapshot) is revoked
                    # and placed again. Only a revoked chunk moves: one that claimed
                    # its start is never retried while it may be running.
                    for f in [f for f in pending if f not in claimed and pinned[f] in allw
                              and (not allw[pinned[f]][2] or allw[pinned[f]][1] > 1)]:
                        w_f = pinned[f]
                        try:
                            got_r = c.run(_revoke_chunk, call_id, toks[f], workers=[w_f]).get(w_f)
                        except Exception:  # noqa: BLE001 — unknown: leave it; a loss is handled when seen
                            continue
                        if got_r is False:
                            claimed.add(f)
                        elif got_r is True:
                            pending.discard(f)
                            f.cancel()
                            held.pop(f, None)
                            ch, attempt, used = futs[f]
                            queue.insert(0, (ch, attempt, used))
                            progress["moved"] = progress.get("moved", 0) + 1
                    if not res and not ok_w and not pending:
                        stop = "no compute workers left"      # a fixed list cannot grow back
                        break
                    # A pool with no compute worker taking work (none joined, or all are
                    # closing): wait for it to grow back, within a bound. The queued inputs
                    # then fail as infra (the machine's shortfall), so a rerun completes them.
                    if res and queue and not pending and not any(s_[2] for s_ in ok_w.values()):
                        no_w = no_w or time.time()
                        if time.time() - no_w >= no_worker_wait:
                            stop = f"no compute worker for {no_worker_wait:g} s"
                            errors.update({i: f"{stop}: the pool did not grow back" for ch, _, _ in queue
                                           for i, _ in ch if i not in errors})
                            break
                    else:
                        no_w = None
                    busy = {pinned[f] for f in pending}
                    free = [a for a, v in ok_w.items() if v[2] and not v[1] and a not in busy]
                    random.shuffle(free)                    # two calls seldom pick the same worker
                    if free and queue and not held_b:
                        # The bundle's only copy left with a worker (scattered data
                        # cannot be recomputed): scatter it again, once per loss. The
                        # scheduler's view is used, not the client's, which may not
                        # have heard of the loss yet.
                        try:
                            first = bfut is None    # not scattered yet: no compute worker was up at the start
                            bfut = c.scatter(bundle, workers=list(free if first else ok_w),
                                             broadcast=not res, hash=False)
                            progress["bundle_rescatters"] = progress.get("bundle_rescatters", 0) + (not first)
                        except Exception:  # noqa: BLE001 — tried again at the next view; until then no
                            bfut = None    # chunk is placed on a bundle that no worker holds
                    if bfut is None:                # no chunk is placed without the bundle
                        unplaced, free = bool(free), []
                # Nothing out and nothing can be placed (the scheduler's view unread, or the bundle on no
                # worker): waited for within the bound of a pool with no worker, then the queued inputs fail
                # as infra. Without this the loop spun until the deadline, or for good with none.
                if (got_v is None or unplaced) and not pending:
                    blind = blind or time.time()
                    if time.time() - blind >= no_worker_wait:
                        stop = f"scheduler not answering for {no_worker_wait:g} s"
                        errors.update({i: f"{stop}: nothing could be placed" for ch, _, _ in queue
                                       for i, _ in ch if i not in errors})
                        break
                elif got_v is not None:
                    blind = None
                # A loss above may have used the rest of the allowance: stop now
                # rather than wait for a worker with demand still shown.
                if (queue or pending) and allowance_s is not None and progress["core_s"] >= allowance_s:
                    stop = CAP_STOP
                    break
                starved = allowance_s is not None and allowance_s - progress["core_s"] - sum(held.values()) <= 0
                while queue and free and len(pending) < slots and not starved:
                    k_q = next((k for k, q in enumerate(queue) if not_before.get(id(q[0]), 0.0) <= time.time()), None)
                    if k_q is None:                 # every queued chunk is waiting out its backoff
                        break
                    f = submit(*queue[k_q], worker=free.pop(0))
                    if f is None:
                        starved = True
                        break
                    not_before.pop(id(queue[k_q][0]), None)
                    queue.pop(k_q)
                    pending.add(f)
                if res:
                    # One marker stays while chunks wait with the pool full (2026-11-12, P6): with
                    # none, the autoscaler saw no waiting work and shrank the pool as chunks finished.
                    # The held marker counts as more work, so it stays only while the pool is at most its
                    # target: past it, the pool may shrink back (Codex review of #204).
                    n_dem = 0 if starved or not queue else min(len(queue), max(1 if n_compute <= pool else 0,
                                                                               pool - len(pending)))
                    _comm_retry(lambda: demand(n_dem), "demand markers", progress, deadline)
                left = None if deadline is None else max(0.0, deadline - time.time())
                if not pending:
                    if starved and queue:
                        stop = CAP_STOP
                        break
                    if not queue:
                        break
                    if left == 0.0:               # no free worker before the deadline
                        stop = "timed out"
                        break
                    time.sleep(poll_s if left is None else min(poll_s, left))
                    continue
                try:
                    done, pending = _comm_retry(lambda: wait(
                        pending, timeout=poll_s if left is None else min(poll_s, left),
                        return_when="FIRST_COMPLETED"), "wait", progress, deadline)
                except cf.CancelledError:
                    # dask cancelled a chunk (e.g. its bundle copy was lost): its
                    # result() raises below and it goes through the lost-chunk path.
                    done = {f for f in pending if f.done()}
                    pending = pending - done
                except TimeoutError:
                    if deadline is not None and time.time() >= deadline:
                        stop = "timed out"
                        break
                    continue
                for f in done:
                    ch, attempt, used = futs[f]
                    cur[0] = (f, held.get(f))     # an interrupt mid-processing leaves it charged in `finally`
                    f_share = held.pop(f, None)
                    try:
                        got = f.result()
                        if got.get("revoked"):
                            # Revoked, but the revoke's reply was lost (a revoke that
                            # got its reply cancels the future, which never gets here):
                            # nothing ran, so place it again.
                            queue.insert(0, (ch, attempt, used))
                            progress["moved"] = progress.get("moved", 0) + 1
                            continue
                        if got.get("unconfirmed"):
                            chunk_lost(f, ch, attempt, used, f_share, got["rows"][0]["error"] if got["rows"]
                                       else "an earlier attempt of this chunk started; its work is unconfirmed")
                            continue
                        csum = got.get("summary") or {}
                        wk = got["rows"][0].get("worker") if got["rows"] else None
                        used_w.add(wk or pinned.get(f))
                        if csum.get("peak_mb"):
                            learned["peak"] = max(learned["peak"] or 0, float(csum["peak_mb"]))
                        pf = csum.get("procs_final")
                        if pf and (csum.get("breaks") or pf < int(csum.get("workers") or pf)):
                            learned["procs"] = min(learned["procs"] or pf, int(pf))
                        died = got["rows"] and all(r.get("kind") == INFRA for r in got["rows"])
                        if died and attempt == 0 and (deadline is None or time.time() < deadline):
                            progress["core_s"] += float(got["core_s"])
                            learned["procs"] = min(learned["procs"] or 10**6, halved(used, wk))
                            queue.insert(0, (ch, 1, learned["procs"]))
                            progress.setdefault("chunks", []).append(
                                {"n": len(ch), "attempt": attempt, "error": got["rows"][0]["error"][:200],
                                 "core_s": round(float(got["core_s"]), 1), "retried_with_procs": learned["procs"]})
                            continue
                        tail = [r for r in got["rows"] if r.get("kind") == STOPPED
                                and str(r.get("error", "")).startswith("stopped: the same model error")]
                        if tail and attempt != TAIL and _breaker(rows + got["rows"], prior_ok=prior_ok) is None:
                            # The chunk saw only its own rows: the batch as a whole does not
                            # confirm the repeat, so its stopped inputs run again (once).
                            got["rows"] = [r for r in got["rows"] if r not in tail]
                            ids = {r["i"] for r in tail}
                            queue.append(([it for it in ch if it[0] in ids], TAIL, used))
                        rows += got["rows"]
                        progress["core_s"] += float(got["core_s"])
                        _rows_done(progress, got["rows"], float(got["core_s"]))
                        progress.setdefault("chunks", []).append(
                            {"n": len(ch), "core_s": round(float(got["core_s"]), 1),
                             "task_s": round(_known_core_s(got["rows"]), 1),
                             "wall_s": round(time.time() - started.get(f, time.time()), 1),
                             "worker": (got["rows"][0].get("worker") if got["rows"] else None),
                             "chunk_summary": {k: v for k, v in (got.get("summary") or {}).items() if k != "bundle"},
                             "stderr_tail": got.get("stderr_tail") or ""})
                    except Exception as e:  # noqa: BLE001 — a lost worker fails only its chunk
                        # A scale-down can show first as this future's failure: then it uses no infra retry
                        # and keeps its processes, as when the view shows it first.
                        chunk_lost(f, ch, attempt, used, f_share, f"chunk failed: {type(e).__name__}: {e}"[:500],
                                   scaled_down=_retired_on_purpose(e, pinned.get(f), view, retired_w))
                cur[0] = None
                if (pending or queue) and not stop:
                    stop = _breaker(rows, prior_ok=prior_ok)
                    if stop:
                        break
                if (pending or queue) and allowance_s is not None and progress["core_s"] >= allowance_s:
                    stop = CAP_STOP
                    break
        finally:
            if markers:
                try:
                    c.cancel(markers)
                except Exception:  # noqa: BLE001 — released with this client anyway
                    pass
            if pending:
                # Stop the call's running chunks (and keep queued ones from starting),
                # THEN collect what they report: a stopped chunk returns the core-seconds
                # it actually used, a chunk that never started returns 0. Only chunks
                # that report nothing are estimated (E7 attempt 6: three concurrent
                # timed-out calls were each charged the whole pool, ~65 real core-hours
                # booked as 282, and the run cap stopped all further compute).
                try:
                    got_c = _cancel_remote(c, call_id, exclude)
                    if any(v == -1 for v in (got_c or {}).values()):
                        progress["remote_unconfirmed"] = True     # a chunk could not be identified
                except Exception:  # noqa: BLE001 — cannot confirm the remote stop
                    progress["remote_unconfirmed"] = True
                try:
                    wait(pending, timeout=float(os.environ.get("MA_COMPUTE_SETTLE_S", "120")))
                except (OSError, cf.CancelledError):     # a time-out, or the connection is down: bounded below
                    pass
                for f in list(pending):
                    if f.done():
                        try:
                            progress["core_s"] += float(f.result()["core_s"])
                        except Exception:  # noqa: BLE001 — a failed settle stays unknown
                            continue
                        pending.discard(f)
                        held.pop(f, None)
                for f in pending:
                    f.cancel()
            # Phase 42 α: if this call is being interrupted, the chunks not
            # accounted for above (still out, or finished but not yet read) are
            # bounded by their share and their time out, not the allowance.
            if cur[0] and cur[0][0] not in held:
                held[cur[0][0]] = cur[0][1] or 0.0       # may count it twice: over-, never under-charged
            t_end, extra = time.time(), 0.0
            for f in list(held):
                got_s = None
                if f not in pending and f.done():
                    try:
                        got_s = float(f.result(timeout=0)["core_s"])
                    except Exception:  # noqa: BLE001 — unknown: bounded below
                        got_s = None
                busy = (t_end - started.get(f, t0_call)) * cores_per_slot
                cap_f = held[f] + cores_per_slot * SHARE_STOP_GRACE_S if held[f] else None
                extra += got_s if got_s is not None else (min(cap_f, busy) if cap_f else busy)
            progress.update(settled=True, settled_extra_s=extra)
            # The scheduler silently reruns tasks from a worker that died; that
            # attempt's work is invisible here, so a vanished worker is charged
            # as busy for the whole call.
            # On an autoscaling pool, scale-down retires only idle workers (BusyWorkerGuard);
            # a chunk lost in the race between that choice and a placement is seen above.
            try:
                gone = [] if res else _gone_workers(c, workers)
            except Exception:  # noqa: BLE001 — table unreadable: every worker charged as busy
                gone = [] if res else list(workers)
            progress["gone_workers"] = len(gone)
    done_i = {r["i"] for r in rows}
    rows += [{"i": i, "ok": False, "kind": INFRA, "error": msg} for i, msg in errors.items() if i not in done_i]   # keep the real error
    # Phase 45 δ: a call that was not stopped accounts for every input. One that
    # is absent (dropped by the dispatcher, or a chunk returned fewer rows) fails
    # loudly: an infra row and `invariant_violation` in the summary.
    absent = [] if stop else sorted(set(range(len(inputs))) - {r["i"] for r in rows})
    rows += [{"i": i, "ok": False, "kind": INFRA, "error": "input lost by the dispatcher (no result returned)"}
             for i in absent]
    if absent:
        print(f"[compute] INVARIANT VIOLATED: {len(absent)} inputs returned no row", file=sys.stderr, flush=True)
    # "workers": the VMs that returned a chunk (Phase 45 δ), not those up at the start.
    return rows, {"backend": "dask", "workers": len(used_w), "workers_seen": len(seen_w),
                  "pool_target": pool, "slots": slots,
                  "cores_per_slot": cores_per_slot,
                  "core_s": progress["core_s"], "stopped": stop,
                  "unknown_chunks": lost + len(pending) + len(gone), "bundle": binfo,
                  "bundle_rescatters": progress.get("bundle_rescatters", 0), "moved": progress.get("moved", 0),
                  "comm_retries": progress.get("comm_retries", 0),
                  "invariant_violation": (f"{len(absent)} inputs returned no row" if absent else None),
                  "chunks": progress.get("chunks", []), "procs_final": learned["procs"],
                  "peak_mb": learned["peak"], "busy_guard": guard, "resubmitted": progress.get("resubmitted", 0)}


# --------------------------------------------------------------------------- #
class _LedgerLock:
    """Exclusive lock on the run's compute ledger (POSIX advisory lock)."""

    def __init__(self, run_dir):
        self.run_dir = os.path.realpath(run_dir)
        self.path = os.path.join(run_dir, LEDGER_REL)
        self.rel = LEDGER_REL.replace(os.sep, "/")

    def __enter__(self):
        import fcntl
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        from lib.safe_io import open_append_nofollow
        self.f = open_append_nofollow(self.run_dir, self.rel + ".lock")     # never truncated, no link followed
        fcntl.flock(self.f, fcntl.LOCK_EX)
        # Only a missing ledger starts at zero: an unreadable one must not
        # hand out a fresh budget (writes are atomic, so this is not a torn write).
        if not os.path.exists(self.path):
            self.led = {}
        else:
            try:
                self.led = json.load(open(self.path))
            except (OSError, ValueError) as e:
                self.f.close()
                raise RuntimeError(f"compute ledger {self.path} is unreadable ({e}); refusing to run") from e
            if not isinstance(self.led, dict) or not isinstance(self.led.get("core_seconds", 0), (int, float)):
                self.f.close()
                raise RuntimeError(f"compute ledger {self.path} is malformed; refusing to run")
        self.led.setdefault("core_seconds", 0.0)
        self.led.setdefault("calls", [])
        self.led.setdefault("reservations", {})
        return self.led

    def __exit__(self, *exc):
        from lib.safe_io import write_nofollow
        try:
            write_nofollow(self.run_dir, self.rel, json.dumps(self.led, indent=1).encode(), atomic=True)
        finally:
            self.f.close()
        return False


SHARE_STOP_GRACE_S = 15.0            # a chunk's overrun past its share while it is stopped (grace periods)
PROGRESS_DIR = ".compute_progress"   # under the run dir, outside models/ and data/ (not fingerprinted or bundled)
# A ledgered call rewrites its record's `t` at least every PROGRESS_HEARTBEAT_S
# (its own thread, also while one task runs for hours). Its pid cannot be read
# from another host, so a reservation made there is taken as dead when its
# record has not been refreshed for PROGRESS_STALE_S (ten heartbeats: also
# covers clock skew between hosts). With no heartbeat to read (a record from
# before it, or none at all), only an age past the longest step limit counts.
PROGRESS_HEARTBEAT_S = 30.0
PROGRESS_STALE_S = 300.0
PROGRESS_STALE_LEGACY_S = 24 * 3600.0


CAP_OUTLOOK_MIN_TASKS = 8  # finished tasks before a call's own task time is used to project its total


def _cap_history(led: dict, fn_spec: str, n: int, allow: float) -> dict | None:
    """Before launch: the call's projected core-seconds from the ledger's last call of the same function that
    finished a task (its charge per finished task x `n` inputs), against its allowance; None with no history."""
    for k in reversed(led.get("calls") or []):
        try:
            n_done = int(k.get("n") or 0) - int(k["n_failed"])       # a settlement record has none: not history
            per = float(k["task_core_s"]) / n_done if k.get("fn") == fn_spec and n_done > 0 else 0.0
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if per > 0:
            return {"projected_core_s": round(per * n, 1), "allowance_core_s": round(allow, 1),
                    "over": per * n > allow, "basis": f"ledger: the last call of this function ({n_done} tasks done)"}
    return None


def _cap_outlook(progress: dict, extra_core_s: float = 0.0) -> None:
    """While the call runs: project its total from its own finished tasks (charged core-seconds per task x its
    inputs). When that passes the allowance it is recorded (`cap_outlook`, in the summary and the ledger record)
    and said once on stderr. A notice only: the allowance stops the call exactly where it did. (The ledger's
    `estimated_core_s` is not a projection: it is the charge estimated for chunks whose work is unknown.)"""
    n, allow, done = progress.get("_n"), progress.get("_allow"), int(progress.get("done_tasks") or 0)
    if not n or not allow or done < min(n, CAP_OUTLOOK_MIN_TASKS):
        return
    proj = (float(progress.get("core_s") or 0) + extra_core_s) / done * n
    if proj <= allow:
        return
    progress["cap_outlook"] = {"projected_core_s": round(proj, 1), "allowance_core_s": round(allow, 1),
                               "over": True, "basis": f"this call: {done} of {n} tasks done"}
    if not progress.get("_cap_warned"):
        progress["_cap_warned"] = True
        _say(f"[compute] WARNING: this call is projected to need {proj / 3600:.3g} core-hours, over its allowance "
             f"of {allow / 3600:.3g} ({done} of {n} tasks done): the tasks past the allowance will be stopped. "
             "Raise the call's allowance or reduce the batch.")


def _note(progress: dict, out: list, extra_core_s: float = 0.0, **more) -> None:
    """Phase 42 α: persist what a ledgered call has done and what it has in
    flight (`out`: [{"n": tasks, "share": core-s or None, "started": t,
    "cores": cores}]), so a caller killed without settling (an agent's tool
    timeout, SIGKILL) is charged for work done, not its whole reservation.
    Written before new work is submitted. If a write fails the record is
    removed, so settlement falls back to charging the reservation."""
    if progress.get("_beat_err"):
        raise progress["_beat_err"]
    _cap_outlook(progress, extra_core_s)
    if not progress.get("_file"):
        return
    rec = {"t": time.time(), "core_s": round(float(progress.get("core_s") or 0) + extra_core_s, 1),
           "done_tasks": int(progress.get("done_tasks") or 0),
           "max_task_core_s": round(float(progress.get("max_task_core_s") or 0), 2),
           "remote_unconfirmed": bool(progress.get("remote_unconfirmed")), "out": out, **more}
    if progress.get("_hb"):
        rec["hb_s"] = progress["_hb"]        # its heartbeat: another host judges the caller by it
    _write_note(progress, rec)


def _write_note(progress: dict, rec: dict | None) -> None:
    """Write `rec` as the call's progress record, or (None) the last one again
    with a new `t` (the heartbeat). One writer at a time."""
    lock = progress.get("_lock")
    if lock is None:
        return _write_note_locked(progress, rec)
    with lock:
        return _write_note_locked(progress, rec)


def _write_note_locked(progress: dict, rec: dict | None) -> None:
    path = progress.get("_file")
    if not path:
        return
    if rec is None:
        if progress.get("_last") is None:
            return
        rec = dict(progress["_last"], t=time.time())
    progress["_last"] = rec
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from lib.safe_io import publish, remove_path
    try:
        publish(path, json.dumps(rec), progress.get("_root"))
    except (OSError, TypeError, ValueError):
        progress["_file"] = None
        try:
            remove_path(path, progress.get("_root"))
        except FileNotFoundError:
            pass
        except OSError:
            # A stale record left readable would settle a dead caller for too
            # little: make its reservation drop the record, or stop the batch.
            inval = progress.get("_invalidate")
            try:
                if not inval:
                    raise RuntimeError("no ledger hook")
                inval()
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"compute progress record {path} can be neither updated nor removed "
                                   f"({type(e).__name__}); stopping the batch") from e


def _settle_dead(led, r, now=None, run_dir=None, foreign=False) -> tuple[float, str, dict]:
    """Phase 42 α: what a caller that died without settling is charged, and why.

    From its progress record: the core-seconds it had accounted for, plus an
    upper bound for each chunk (or input) still in flight, whose work may go
    on after the caller died. The work in flight is first stopped: leftover
    local pool processes are killed, remote chunks are cancelled through the
    scheduler (`_stop_orphans`). Once stopped, a chunk is bounded by the least
    of its share (chunks enforce it) and its cores x the time since it
    started. Not confirmed stopped, a remote chunk is bounded by its share
    alone, and work with no share (local) charges the reservation. Never more
    than the reservation; no readable record charges it in full. The largest
    per-task core time seen is recorded as an estimate only: it is not a bound
    (a long task after short ones), so it is never what is charged.

    A caller on another host (`foreign`): nothing is signalled from here (its
    pids are that host's; its scheduler may be gone), so its remote chunks are
    bounded by their shares and local work in flight charges the reservation.
    When its record carries a heartbeat, a remote chunk is charged its cores x
    the time from its start to the caller's last heartbeat (plus one interval
    and the stop grace), at most its share: a run resumed from a checkpoint on
    another host was charged every chunk's whole share for work its old
    cluster never did. This is an estimate, not a bound: a chunk on a cluster
    that outlived its caller can run on to its share (`inflight_basis`)."""
    now = time.time() if now is None else now
    held = float(r.get("core_s") or 0)
    path, _ = _progress_path(r.get("progress"), run_dir)
    if not path:
        return held, "caller died; no progress record (reserved before Phase 42, or it could not be kept); charged its reservation", {}
    try:
        p = json.load(open(path))
        done = float(p["core_s"])
        out = list(p.get("out") or [])
        t_rec, hb = float(p["t"]), float(p.get("hb_s") or 0)
    except (OSError, ValueError, KeyError, TypeError) as e:
        return held, f"caller died; progress record unreadable ({type(e).__name__}); charged its reservation", {}
    by_hb = bool(foreign and hb > 0)
    if p.get("remote_unconfirmed"):
        return held, "caller died; remote chunks not confirmed stopped; charged its reservation", {}
    if foreign:
        stopped, how = (False, "on another host: not stopped from here"
                        + ("; charged up to its last heartbeat" if by_hb else "")) if out else (True, "nothing in flight")
    elif out:
        stopped, how = _stop_orphans(p, r.get("backend"))
    else:
        stopped, how = True, "nothing in flight"
        if p.get("pids"):
            _stop_orphans(p, "local")         # idle pool processes outlive their caller: stop them too
    now = max(now, time.time()) if stopped and out else now
    bound = 0.0
    try:
        for c in out:
            cores, started = float(c.get("cores") or 1), float(c["started"])
            # A chunk stops at its share, plus the grace its stop allows (SIGTERM, then SIGKILL).
            share = float(c["share"]) + cores * SHARE_STOP_GRACE_S if c.get("share") else None
            if stopped:
                bound += min(cores * max(0.0, now - started), share if share is not None else float("inf"))
            elif share is not None:
                bound += min(share, cores * (max(0.0, t_rec + hb - started) + SHARE_STOP_GRACE_S)) if by_hb else share
            else:
                return held, f"caller died; work in flight not stopped ({how}); charged its reservation", {}
    except (KeyError, TypeError, ValueError) as e:
        return held, f"caller died; progress record malformed ({type(e).__name__}); charged its reservation", {}
    per = float(p.get("max_task_core_s") or 0)
    est = sum(int(c.get("n") or 1) for c in out) * per if per else None
    charge = min(held, done + bound)
    why = (f"caller died; charged work done ({done:.0f} core-s) + in flight ({len(out)} chunks, "
           f"<= {bound:.0f} core-s; {how}), " + ("not its reservation" if charge < held else "capped at its reservation")
           + f" ({held:.0f} core-s)")
    return charge, why, {"done_core_s": round(done, 1), "inflight_bound_core_s": round(bound, 1),
                         "inflight_estimate_core_s": None if est is None else round(est, 1),
                         "reserved_core_s": round(held, 1),
                         **({"inflight_basis": "last_heartbeat"} if by_hb and out else {})}


def _group_live(pgid: int) -> bool:
    """Does process group `pgid` have a member that is not defunct? Unknown counts as yes."""
    try:
        os.killpg(int(pgid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass                                      # macOS: also a group of zombies only; look at its members
    except (OSError, ValueError):
        return True
    try:
        import psutil
        for pr in psutil.process_iter():
            try:
                if os.getpgid(pr.pid) != pgid:
                    continue
            except OSError:
                continue                          # gone
            try:
                if pr.status() != psutil.STATUS_ZOMBIE:
                    return True
            except (psutil.ZombieProcess, psutil.NoSuchProcess):
                continue
            except psutil.Error:
                return True                       # cannot tell: live
        return False
    except ImportError:
        pass
    import subprocess
    try:
        lines = subprocess.run(["ps", "-A", "-o", "pgid=,stat="], capture_output=True, text=True,
                               timeout=10).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return True
    for ln in lines:
        parts = ln.split()
        if len(parts) >= 2 and parts[0] == str(pgid) and not parts[1].startswith("Z"):
            return True
    return False


def _stop_orphans(p: dict, backend: str | None) -> tuple[bool, str]:
    """Stop the work a dead caller left in flight. Local: kill its pool
    processes still alive (same start token). Dask: cancel its call on the
    workers through MA_COMPUTE_SCHEDULER. Returns (confirmed stopped, how)."""
    if backend == "local" or p.get("pids") is not None:
        if not p.get("pids_complete"):
            return False, "killed while starting pool processes (pids not recorded)"
        # A signal goes only to a process whose CURRENT start token was read and
        # equals the recorded one (each pool process leads its own group, which
        # holds what the model started). Anything else that may still be ours (no
        # recorded token, or the current one unreadable: e.g. a group whose leader
        # exited) is never signalled: it is left unconfirmed and the call is
        # charged in full. A pid now held by another process (both tokens read,
        # different) means ours is gone, with its group: a pid is not reused while
        # its group exists.
        left, unverified = [], []
        for pid, tok in p.get("pids") or []:
            pid = int(pid)
            cur = _pid_start(pid)
            if tok and cur and cur == tok:
                _killpg(pid, 9, direct=True)
                left.append(pid)
            elif tok and cur and cur.split(":")[0] == tok.split(":")[0]:
                continue                          # read the same way, different: pid reused, ours gone
            elif _pid_alive(pid) or _group_live(pid):
                unverified.append(pid)
        end = time.time() + 5
        while left and time.time() < end:
            left = [x for x in left if _group_live(x) or _pid_alive(x)]
            if left:
                time.sleep(0.05)
        if left:
            return False, f"pool process groups {left} still alive"
        if unverified:
            return False, f"processes {unverified} could not be identified as this call's (not signalled)"
        return True, "pool processes stopped"
    call_id, addr = p.get("call_id"), os.environ.get("MA_COMPUTE_SCHEDULER")
    if not call_id or not addr:
        return False, "no scheduler to cancel its remote chunks"
    try:
        import cloudpickle
        from distributed import Client
        cloudpickle.register_pickle_by_value(sys.modules[__name__])
        cloudpickle.register_pickle_by_value(compute_worker)
        exclude = {w.strip() for w in os.environ.get("MA_COMPUTE_EXCLUDE_WORKERS", "").split(",") if w.strip()}
        with Client(addr, timeout="30s", security=_security()) as c:
            got = _cancel_remote(c, call_id, exclude)
        if any(v == -1 for v in (got or {}).values()):
            return False, "a remote chunk could not be identified (not signalled)"
        return True, "remote chunks cancelled"
    except Exception as e:  # noqa: BLE001 — cannot confirm: bounded by shares
        return False, f"remote cancel failed ({type(e).__name__}{': ' + str(e)[:200] if str(e) else ''})"


def _start_heartbeat(progress: dict):
    """Refresh the call's progress record every `progress["_hb"]` seconds until
    the returned stop() is called. A record that can be neither refreshed nor
    removed stops the batch at its next `_note`, as a failed write does."""
    stop = threading.Event()

    def beat():
        while not stop.wait(progress["_hb"]):
            try:
                _write_note(progress, None)
            except Exception as e:  # noqa: BLE001 -- raised in the batch's own thread
                progress["_beat_err"] = e
                return
    th = threading.Thread(target=beat, name="compute-progress-heartbeat", daemon=True)
    th.start()

    def end():
        stop.set()
        th.join()
    return end


def _progress_path(ref: str | None, run_dir: str | None) -> tuple[str | None, str | None]:
    """Where a reservation's progress record is read, and the run dir it was
    recorded under when that is not this one (None otherwise). Records are kept
    relative to the run dir; an absolute one (before that) into another run dir
    (moved, or resumed in another checkout) is found by its
    `.compute_progress/<name>` suffix in this one."""
    if not ref or run_dir is None:
        return ref or None, None
    if not os.path.isabs(ref):
        return os.path.join(run_dir, ref), None
    parent = os.path.dirname(ref)
    if os.path.basename(parent) != PROGRESS_DIR:
        return ref, None                          # not placed by map_fn: read as recorded
    old = os.path.dirname(parent)
    if os.path.realpath(old) == os.path.realpath(run_dir):
        return ref, None
    return os.path.join(run_dir, PROGRESS_DIR, os.path.basename(ref)), old


def _foreign_dead(r: dict, run_dir: str | None, now: float) -> str | None:
    """Why a reservation made on another host is taken as dead (its pid cannot
    be read from here), or None: still held. Dead when its record carries a
    heartbeat not refreshed for PROGRESS_STALE_S; or its record was kept in
    another run dir (this ledger is a copy its caller can no longer settle) and
    is as old; or, with nothing better to read (no heartbeat; no readable
    record: the reservation's time), it is older than PROGRESS_STALE_LEGACY_S.
    Until then it is held; never forever."""
    path, moved = _progress_path(r.get("progress"), run_dir)
    t = hb = None
    try:
        p = json.load(open(path))
        t, hb = float(p["t"]), float(p.get("hb_s") or 0)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        t = None
    if t is not None:
        age = now - t
        if hb and age > PROGRESS_STALE_S:
            return f"has not refreshed its progress record for {age:.0f} s (every {hb:g} s while it runs)"
        if moved and age > PROGRESS_STALE_S:
            return f"kept its progress record under {moved}, not this run dir, {age:.0f} s ago"
    else:
        try:
            t = float(r["started"])
        except (KeyError, TypeError, ValueError):
            return "recorded no time"
    age = now - t
    if age > PROGRESS_STALE_LEGACY_S:
        return f"last recorded {age / 3600:.1f} h ago (over {PROGRESS_STALE_LEGACY_S / 3600:g} h, the longest step limit)"
    return None


def _drop_progress(progress: dict) -> None:
    lock = progress.get("_lock")
    if lock is not None:
        with lock:
            path, progress["_file"] = progress.get("_file"), None
    else:
        path, progress["_file"] = progress.get("_file"), None
    if path:
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        from lib.safe_io import remove_path
        try:
            remove_path(path, progress.get("_root"))
        except OSError:
            pass


def _live_reservations(led, run_dir=None, now=None) -> float:
    """Core-seconds held by calls still running. A call whose process is gone
    (or defunct) without settling is charged for its work done and a bound on
    its chunks still in flight (`_settle_dead`). A call made on another host is
    judged by its progress record's heartbeat (`_foreign_dead`)."""
    host, total = os.uname().nodename, 0.0
    for rid, r in list(led["reservations"].items()):
        gone = None
        if r.get("host") == host:
            try:
                alive = _pid_alive(int(r["pid"]), r.get("pid_start"))
            except (ValueError, KeyError, TypeError):
                alive = False
        else:
            gone = _foreign_dead(r, run_dir, time.time() if now is None else now)
            alive = gone is None
        if alive:
            total += float(r.get("core_s") or 0)
        else:
            charge, why, parts = _settle_dead(led, r, now=now, run_dir=run_dir, foreign=gone is not None)
            if gone:
                why = f"caller on another host ({r.get('host')}) {gone}; {why}"
            del led["reservations"][rid]
            led["core_seconds"] = round(led["core_seconds"] + charge, 1)
            led["calls"].append({"fn": r.get("fn"), "backend": r.get("backend"), "n": r.get("n"),
                                 "purpose": r.get("purpose"), "out": r.get("out"), "stopped": why,
                                 "task_core_s": round(charge, 1), **parts, "ts": time.strftime("%FT%T")})
            path, _ = _progress_path(r.get("progress"), run_dir)
            if path:
                from lib.safe_io import remove_path
                try:
                    remove_path(path, run_dir)
                except OSError:
                    pass
    return total


def map_fn(run_dir: str, fn_spec: str, inputs: list, backend: str = "auto",
           max_workers: int | None = None, max_tasks: int = DEFAULT_MAX_TASKS,
           timeout_s: float | None = None, ledger: bool = True,
           max_core_hours: float | None = None, prior_ok: int = 0, purpose: str | None = None,
           out: str | None = None, on_rows=None) -> tuple[list[dict], dict]:
    """Evaluate fn over inputs. Under the ledger, the call first reserves its
    allowance (`max_core_hours`, default MA_COMPUTE_CALL_CORE_HOURS = 50,
    never more than the run has left) and stops when it is used up;
    unfinished rows fail with "stopped: compute cap reached" or "timed out".
    A call that is interrupted is charged an upper bound of what it used.
    `purpose` (the finding IDs or plan item the batch serves) and `out` are
    recorded with the call, so core-hours can be reported per finding.

    Phase 42 α: a ledgered call keeps a progress record (work done, chunks in
    flight) under `{run_dir}/.compute_progress/`; if its process dies without
    settling, the next call charges it from that record (`_settle_dead`). An
    interrupted call whose chunks were all stopped and accounted for is
    charged what they reported. `on_rows(rows)` receives rows as they finish
    (the CLI writes them to `<out>.partial.jsonl`)."""
    run_dir = os.path.realpath(run_dir)
    if len(inputs) > max_tasks:
        raise ValueError(f"{len(inputs)} tasks exceeds --max-tasks {max_tasks}")
    allowance_s, rid, hint = (None if max_core_hours is None else max_core_hours * 3600), None, None
    if backend == "auto":
        backend = "dask" if os.environ.get("MA_COMPUTE_SCHEDULER") else "local"
    progress = {"_rows": on_rows, "_lock": threading.Lock()}
    end_beat = None
    if ledger:
        cap_s = float(os.environ.get("MA_COMPUTE_MAX_CORE_HOURS", RUN_CAP_CORE_HOURS)) * 3600
        with _LedgerLock(run_dir) as led:
            held = _live_reservations(led, run_dir)
            remaining = cap_s - led["core_seconds"] - held
            if remaining <= 0:
                raise RuntimeError(
                    f"run compute cap reached: {led['core_seconds'] / 3600:.1f} used"
                    + (f" + {held / 3600:.1f} held by running calls" if held else "")
                    + f" of {cap_s / 3600:g} core-hours. Do NOT run these simulations on this VM "
                      "instead (it is the agent's machine: memory exhaustion kills the run). Report the "
                      "cap to the lead, reduce the batch (fewer draws/points), or ask for a higher "
                      "MA_COMPUTE_MAX_CORE_HOURS.")
            per_call = max_core_hours if max_core_hours is not None else \
                float(os.environ.get("MA_COMPUTE_CALL_CORE_HOURS", "50"))
            allowance_s = min(remaining, per_call * 3600)
            # No hard limit is added: the ledger's history says before launch whether the call fits.
            look = _cap_history(led, fn_spec, len(inputs), allowance_s)
            progress.update(_n=len(inputs), _allow=allowance_s, cap_outlook=look, _cap_warned=bool(look and look["over"]))
            if look and look["over"]:
                _say(f"[compute] WARNING: {len(inputs)} tasks are projected to need "
                     f"{look['projected_core_s'] / 3600:.3g} core-hours ({look['basis']}), over this call's "
                     f"allowance of {allowance_s / 3600:.3g}: the tasks past it will be stopped. Raise the call's "
                     "allowance or reduce the batch.")
            rid = f"{os.uname().nodename}:{os.getpid()}:{time.time():.6f}"
            pfile = os.path.join(run_dir, PROGRESS_DIR, hashlib.sha1(rid.encode()).hexdigest()[:16] + ".json")
            try:
                os.makedirs(os.path.dirname(pfile), exist_ok=True)
                progress["_file"], progress["_hb"] = pfile, PROGRESS_HEARTBEAT_S
                progress["_root"] = os.path.realpath(run_dir)
                _note(progress, [])
            except OSError:
                progress["_file"] = None
            led["reservations"][rid] = {"core_s": allowance_s, "pid": os.getpid(),
                                        "pid_start": _pid_start(os.getpid()),
                                        "host": os.uname().nodename, "fn": fn_spec, "backend": backend,
                                        "n": len(inputs), "purpose": purpose, "out": out,
                                        # relative: a moved or resumed run dir still finds it
                                        "progress": progress["_file"] and os.path.relpath(progress["_file"], run_dir),
                                        "started": round(time.time(), 1)}
            if progress["_file"]:
                end_beat = _start_heartbeat(progress)
            # Phase 35: the last measured peak memory of this function starts the
            # call at a concurrency that fits, instead of rediscovering it by crashing.
            hint = ((led.get("mem_profile") or {}).get(fn_spec) or {}).get("peak_mb")
    if rid:
        def _invalidate():
            with _LedgerLock(run_dir) as led:
                if rid in led["reservations"]:
                    led["reservations"][rid]["progress"] = None      # settles in full if this caller dies
        progress["_invalidate"] = _invalidate
    t0 = time.time()
    deadline = t0 + timeout_s if timeout_s else None
    try:
        try:
            if backend == "dask":
                rows, info = _map_dask(run_dir, fn_spec, inputs, max_workers, deadline=deadline,
                                       allowance_s=allowance_s, progress=progress, peak_mb=hint, prior_ok=prior_ok)
            else:
                rows, info = _map_local(run_dir, fn_spec, inputs, max_workers, deadline=deadline,
                                        allowance_s=allowance_s, progress=progress, peak_mb=hint,
                                        prior_ok=prior_ok, on_note=_note, repo=REPO)
        finally:
            if end_beat:
                end_beat()                        # stopped before the ledger lock is taken to settle
        done = {r["i"] for r in rows}
        why = info.get("stopped") or "timed out"
        rows += [{"i": i, "ok": False, "kind": STOPPED, "error": why} for i in range(len(inputs)) if i not in done]
        rows.sort(key=lambda r: r["i"])
        wall = time.time() - t0
        # Chunks whose work is unaccounted for (lost worker, killed at the
        # deadline) are charged as busy for the whole call: over-, never under-charged.
        est = min(info.pop("unknown_chunks"), info["slots"]) * wall * info["cores_per_slot"]
        core_s = info.pop("core_s") + est
        if progress.get("remote_unconfirmed") and allowance_s is not None:
            core_s = max(core_s, allowance_s)    # stopped chunks not confirmed: charge the allowance
    except BaseException as e:
        if rid:
            if progress.get("remote_unconfirmed"):
                # Remote chunks that could not be confirmed stopped may run on up
                # to the call's allowance: charge all of it.
                used, how = allowance_s, "remote chunks not confirmed stopped; charged the allowance"
            elif progress.get("settled"):
                # Phase 42 α: the backend stopped every chunk and accounted for it
                # (what finished, what stopped chunks reported, a bound for the rest).
                used = min(allowance_s, progress.get("core_s", 0.0) + progress.get("settled_extra_s", 0.0))
                how = "charged work done and what its stopped chunks reported"
            else:
                # Interrupted before the backend could settle: every slot busy
                # since the start (capped at the reservation), never zero.
                used = min(allowance_s, progress.get("core_s", 0.0) + progress.get("slots", 0)
                           * progress.get("cores_per_slot", 0) * (time.time() - t0))
                how = "not settled; charged every slot as busy since the start"
            with _LedgerLock(run_dir) as led:
                led["reservations"].pop(rid, None)
                led["core_seconds"] = round(led["core_seconds"] + used, 1)
                led["calls"].append({"fn": fn_spec, "backend": backend, "task_core_s": round(used, 1),
                                     "n": len(inputs), "n_failed": len(inputs), "purpose": purpose, "out": out,
                                     "stopped": f"interrupted: {type(e).__name__}; {how}",
                                     "ts": time.strftime("%FT%T")})
                _drop_progress(progress)
        raise
    failed_by = {}
    for r in rows:
        if not r["ok"]:
            failed_by[r.get("kind") or MODEL] = failed_by.get(r.get("kind") or MODEL, 0) + 1
    summary = {**{k: v for k, v in info.items() if k not in ("slots", "cores_per_slot")},
               "n": len(rows), "n_ok": sum(r["ok"] for r in rows),
               "n_failed": sum(not r["ok"] for r in rows), "failed_by_kind": failed_by, "wall_s": round(wall, 1),
               "task_core_s": round(core_s, 1), "estimated_core_s": round(est, 1),
               "cap_outlook": progress.get("cap_outlook"),     # projected need against the allowance, if known
               # what the model calls themselves took: a charge far above it points at
               # retried or unconfirmed chunks (see "chunks")
               "model_s": round(_known_core_s(rows), 1)}
    if not ledger:
        return rows, summary
    with _LedgerLock(run_dir) as led:
        led["reservations"].pop(rid, None)
        _drop_progress(progress)
        led["core_seconds"] = round(led["core_seconds"] + core_s, 1)
        led["calls"].append({k: summary.get(k) for k in ("backend", "workers", "n", "n_failed", "failed_by_kind",
                                                         "wall_s", "task_core_s", "estimated_core_s", "model_s",
                                                         "cap_outlook",
                                                         "stopped", "breaks", "procs_final", "peak_mb")}
                            | {"fn": fn_spec, "purpose": purpose, "out": out, "ts": time.strftime("%FT%T")})
        if summary.get("peak_mb"):
            led.setdefault("mem_profile", {})[fn_spec] = {"peak_mb": summary["peak_mb"], "ts": time.strftime("%FT%T")}
    return rows, summary


def input_key(params) -> str:
    """Stable key of one parameter set (order of dict keys does not matter)."""
    return hashlib.sha1(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


_FP_SKIP_EXT = (".log", ".out", ".err", ".png", ".pdf", ".svg", ".jpg", ".jpeg", ".tmp", ".lock", ".pyc")
# scripts/pipeline.py's definition and records (models/pipeline.yaml, pipeline_state.json, pipeline_report.json):
# no model reads them, and the runner rewrites them when a step fails or its limit changes, which would discard the
# partial rows of the batch that step was running (Nigeria v2 full run, 2026-11-02).
_FP_RUNNER_FILES = ("pipeline.yaml", "pipeline_state.json", "pipeline_report.json")
# 2026-11-12 (owner): the top-level definitions of this module that only place, retry, account for or watch
# chunks. No row a reuse takes comes from them (only ok rows are reused, and a chunk's rows come from
# _remote_task / _call, which stay hashed), so code_fingerprint hashes this file with each of them replaced by
# a placeholder: PR #204 changed only these and still changed the fingerprint of every stored batch. A name
# not found is hashed whole (never fewer bytes); FPX1 checks every name exists. Add a definition here only if
# no row value can depend on it.
def _fp_ignored(fn: str, names, in_data: bool, runner: bool) -> bool:
    """Whether code_fingerprint leaves out file `fn` (`names`: the files of its directory; `in_data`: under the
    run's data/, where nothing is left out; `runner`: directly in the run's models/): logs, figures, summaries,
    the ledger, a batch's partial rows or detached-job record, the pipeline runner's own files. Another call's
    results file (its `.summary.json` is beside it) is never left out, whatever its extension."""
    if in_data or fn + ".summary.json" in names:
        return False
    return (fn.endswith(_FP_SKIP_EXT) or fn.endswith(".summary.json") or fn.endswith((PARTIAL_EXT, JOB_EXT))
            or fn.startswith("compute_ledger.json") or (runner and fn in _FP_RUNNER_FILES))


def _bundle_skips(fn: str, names, in_data: bool, runner: bool) -> bool:
    """Whether make_bundle leaves out file `fn` (arguments as `_fp_ignored`): only a file the fingerprint
    ignores, and of those only logs and the records this module writes beside a batch (summaries, partial
    rows, job records, the ledger). The other ignored files (figures, `.out`, `.err`, `.tmp`, the pipeline
    runner's files) are still shipped: a model that reads one found it on the workers before."""
    return _fp_ignored(fn, names, in_data, runner) and (
        fn.endswith((".log", ".summary.json", PARTIAL_EXT, JOB_EXT))
        or (runner and fn.startswith("compute_ledger.json")))     # the run's own ledger, where it is kept


# The fixed-name analysis outputs the v2 harness writes under models/ after its engine batch (toolkit/analysis run.py,
# stability.py, claims.py, estimates.py); with them a resume after ANALYZE looked stale (F5). Exact paths, skipped only
# in a v2 run (its spec, decision_spec.yaml, at the run root): a v1 run hashes everything, and every other file
# (decision_tests/<id>.yaml included: its names are not fixed) is hashed (Codex P2-9 on #161).
FP_HARNESS_OUTPUTS = ("models/allocation_robustness.yaml", "models/claims_spec.yaml", "models/claims_ledger.yaml")
# F6 Stage 2: sign-off records under data/, skipped in a v2 run (as FP_HARNESS_OUTPUTS). Exact paths, one reason each.
# A lead-signed preview and the owner-signed final run of one spec differ only in these, so the final run recomputed
# every stored batch. Everything a batch reads is still hashed; the self-test (RS4) fails if an engine-batch job
# module (toolkit/engines/**, toolkit/analysis/batch.py, trajectories.py) names one of them.
FP_SIGNOFF_RECORDS = (
    # toolkit/spec/geometry.py ARTIFACT, written by geometry.run() (stage_machine SIGN refreshes it): the geometry
    # check report, which copies the sign-off's signed_by / at and the owner's approvals (Codex r1 P2-1). Its other
    # content derives from the boundary, crosswalk and units files, which stay hashed; only the gate and the sign-off
    # sheet read it.
    "data/input_checks.json",
)
# Dependency manifests at the repo root: a pin change changes what the code computes (Codex P2-8).
FP_DEP_MANIFESTS = ("requirements.txt", "requirements-cloud.txt", "requirements-agent.txt")


def _v2_run(run_dir: str) -> bool:
    """A v2 run: a decision spec at the root and metadata.json recording pipeline v2 (Codex r1 P2-2: v1 can hold a
    spec too). A missing or unreadable record is not v2, so everything is hashed (fail closed)."""
    if not os.path.isfile(os.path.join(run_dir, "decision_spec.yaml")):
        return False
    try:
        return json.load(open(os.path.join(run_dir, "metadata.json"))).get("pipeline") == "v2"
    except (OSError, ValueError, AttributeError):
        return False


def _harness_output(run_dir: str, rel: str) -> bool:
    return rel.replace(os.sep, "/") in FP_HARNESS_OUTPUTS + FP_SIGNOFF_RECORDS and _v2_run(run_dir)


def runtime_versions() -> dict:
    """The orchestrating interpreter and the numerical packages it has, recorded with each batch (provenance)."""
    import platform
    from importlib import metadata
    got = {"python": platform.python_version()}
    for pkg in ("numpy", "scipy", "starsim", "sciris", "pandas"):
        try:
            got[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            pass
    return got


def fn_id(run_dir: str, fn_spec: str) -> str:
    """A function's identity across trees: run-relative for a function in the run dir, `repo:<path>` for one in the
    repo, else the spec as given. Each cloud launch extracts the code to a new temporary directory, so an absolute
    path never matched twice."""
    path, sep, name = fn_spec.partition("::")
    real = os.path.realpath(path if os.path.isabs(path) else os.path.join(run_dir, path))
    for base, tag in ((os.path.realpath(run_dir), ""), (os.path.realpath(REPO), "repo:")):
        if real.startswith(base + os.sep):
            return tag + os.path.relpath(real, base).replace(os.sep, "/") + sep + name
    return fn_spec


def code_fingerprint(run_dir: str, fn_spec: str, exclude=(), confined: bool = False, extra=None,
                     record: dict | None = None, extra_digest: str | None = None) -> str:
    """The reuse key of a batch's rows besides their params (W0-A), a hash of parts recorded with the batch
    (`record` gets key_parts): run_files, the run's models/ and data/ and the function's file (_run_files_digest);
    deps, the dependency manifests; data_set, the repo data read by path (FP_DATA_SET); extra, the caller's part
    (batch.py: the engine identity), or `extra_digest` as recorded (reuse_report, the run console). The repo code is
    not in it: it is the observed closure, recorded when the rows are computed and re-hashed when they are reused
    (_observed_check). The spec reaches a job through its params, the row's own key (input_key)."""
    run_dir = os.path.realpath(run_dir)
    parts = {"version": FP_KEY_VERSION, "run_files": _run_files_digest(run_dir, fn_spec, exclude, confined),
             "deps": _digest({m: _file_digest(os.path.join(REPO, m)) for m in FP_DEP_MANIFESTS
                              if os.path.isfile(os.path.join(REPO, m))}),
             "data_set": _files_digest(FP_DATA_SET, skip=run_dir)}
    if extra is not None or extra_digest:
        parts["extra"] = extra_digest or _digest(extra)
    if record is not None:
        record["key_parts"] = parts
    return "k3-" + _digest(parts)[:20]


FP_KEY_VERSION = 3
# W0-A: the repo data a batch can read by path, hashed whole into every key (the one list). "all": every file;
# "data": every file but code and docs (.py, .pyc, .md); the Python a job imports is its observed closure.
FP_DATA_SET = {"data": "all", "toolkit": "data"}
_FP_DATA_EXCLUDES = ("data/evals",)
_FP_CODE_EXT = (".py", ".pyc", ".md")


def _files_digest(spec: dict, skip: str | None = None) -> str:
    """sha256 over {repo-relative path: sha256} of the files `spec` names (_data_set_files)."""
    got = {}
    for rel in _data_set_files(spec, skip):
        try:
            got[rel] = _file_digest(os.path.join(REPO, rel))
        except OSError:
            got[rel] = "unreadable"
    return _digest(got)


def _data_set_files(spec: dict, skip: str | None = None) -> list[str]:
    """The repo-relative paths of the files `spec` names ({dir: "all" | "data"}), sorted; __pycache__, .git,
    _FP_DATA_EXCLUDES, and any `_truth*` component (eval_paths' hidden-material rule, also where a link leads)
    left out, linked directories followed (a loop stops), the launch stamp (_FP_REPO_SKIP) and the directory
    `skip` (the run dir, when it lies under one of them) skipped. The key hashes these files and make_bundle ships them."""
    got = []
    for root, kind in sorted(spec.items()):
        top = os.path.join(REPO, root)
        anc = {top: {os.path.realpath(top)}}
        for dp, dirs, fns in os.walk(top, followlinks=True):
            dirs[:] = sorted(
                d for d in dirs if d not in ("__pycache__", ".git") and not _hidden_path(os.path.join(dp, d))
                and os.path.relpath(os.path.join(dp, d), REPO).replace(os.sep, "/") not in _FP_DATA_EXCLUDES
                and os.path.realpath(os.path.join(dp, d)) not in anc[dp]
                and os.path.realpath(os.path.join(dp, d)) != (skip and os.path.realpath(skip)))
            for d in dirs:
                anc[os.path.join(dp, d)] = anc[dp] | {os.path.realpath(os.path.join(dp, d))}
            for f in fns:
                rel = os.path.relpath(os.path.join(dp, f), REPO).replace(os.sep, "/")
                if rel in _FP_REPO_SKIP or (kind == "data" and f.endswith(_FP_CODE_EXT)) \
                        or any(rel == ex or rel.startswith(ex + "/") for ex in _FP_DATA_EXCLUDES) \
                        or _hidden_path(os.path.join(dp, f)):
                    continue
                got.append(rel)
    return sorted(set(got))


def _hidden_path(path: str) -> bool:
    """A `_truth*` component in the path or in where it leads (eval_paths: hidden eval-suite material)."""
    return any(c.startswith("_truth") for p in (path, os.path.realpath(path)) for c in p.split(os.sep))


def _run_files_digest(run_dir: str, fn_spec: str, exclude=(), confined: bool = False) -> str:
    """Every file under the run's
    models/ and data/ (following symlinked directories) and the batch function's file, except a v2 run's fixed-name analysis outputs (FP_HARNESS_OUTPUTS) and sign-off records
    (FP_SIGNOFF_RECORDS) and (outside data/) logs,
    figures, summaries, the ledger, the
    pipeline runner's own files in models/ (_FP_RUNNER_FILES), and this call's own results file (`exclude`), and any batch's partial rows or
    detached-job record; files up to 64 MB by content, larger ones by
    size and mtime. Any change and earlier rows are not reused. The spec is not in it: what a spec sets reaches a
    job through its params, which are the row's own key (input_key).

    `confined` (the run console, which treats run artifacts as hostile; review round 3, P1-3): the run's files are
    read through no-follow directory and file fds (lib.safe_io), a link anywhere under models/ or data/ (the entries
    themselves included) or a function file outside the run dir and the repo raises ValueError, and nothing outside
    the run dir is read. On a tree with no links the result is byte-identical to the default, which is unchanged."""
    run_dir = os.path.realpath(run_dir)
    h = hashlib.sha256()
    path = fn_spec.partition("::")[0]
    fn_file = path if os.path.isabs(path) else os.path.join(run_dir, path)
    if confined:
        _confined_check(run_dir, path)
    skip = {os.path.realpath(x) for x in exclude}
    files = {fn_file}
    links = []
    for root in (os.path.join(run_dir, "models"), os.path.join(run_dir, "data")):
        # Every path a model can use is walked, aliases included (each under its
        # own name); only a loop (a dir that is its own ancestor) stops the walk.
        ancestors = {root: frozenset()}
        for dp, dirs, fns in os.walk(root, followlinks=True):
            real = os.path.realpath(dp)
            anc = ancestors.pop(dp, frozenset())
            if real in anc:
                dirs[:] = []
                continue
            dirs[:] = sorted(
                d for d in dirs
                if d not in ("__pycache__", ".git", "_truth")
                and not (root == os.path.join(run_dir, "models")
                         and os.path.realpath(dp) == os.path.realpath(os.path.join(run_dir, "models", "ext"))
                         and d.startswith("engine_"))
            )
            for d in dirs:
                ancestors[os.path.join(dp, d)] = anc | {real}
            for x in dirs + sorted(fns):
                if os.path.islink(os.path.join(dp, x)):
                    links.append(f"{os.path.join(dp, x)}->{os.readlink(os.path.join(dp, x))}")
            names = set(fns)
            in_data = root == os.path.join(run_dir, "data")
            runner = dp == os.path.join(run_dir, "models")
            in_run = root.startswith(run_dir + os.sep)
            for fn in fns:
                p = os.path.join(dp, fn)      # S1-8: harness-written analysis outputs (explicit list); the stamp
                if (_harness_output(run_dir, os.path.relpath(p, run_dir)) if in_run else
                        os.path.relpath(p, REPO).replace(os.sep, "/") in _FP_REPO_SKIP):
                    continue
                if _fp_ignored(fn, names, in_data, runner):
                    continue          # logs, figures, summaries, the ledger (never skipped under data/)
                files.add(os.path.join(dp, fn))
    for p in _built_engine_prod_files(run_dir, confined=confined):
        files.add(p)
    for ln in sorted(links):
        h.update(b"link:" + ln.encode() + b"\0")
    for f in sorted(files):
        if os.path.realpath(f) in skip:
            continue
        in_run = f.startswith(run_dir + os.sep)
        if confined and in_run:
            _hash_confined(h, run_dir, os.path.relpath(f, run_dir))
            continue
        try:
            st = os.stat(f)
        except OSError:
            continue
        rel = os.path.relpath(f, run_dir) if f.startswith(run_dir) else "repo:" + os.path.relpath(f, REPO)
        h.update(rel.encode() + b"\0")
        if st.st_size <= _FP_HASH_MB * 2**20:
            with open(f, "rb") as fh:
                for block in iter(lambda: fh.read(2**20), b""):
                    h.update(block)
        else:
            h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
        h.update(b"\0")
    return h.hexdigest()[:20]


def _confined_check(run_dir: str, fn_path: str) -> None:
    """code_fingerprint(confined=True): no link under the run's models/ or data/ (the entries themselves included),
    and the function file inside the run dir (relative, no "..") or inside the repo."""
    for sub in ("models", "data"):
        top = os.path.join(run_dir, sub)
        if os.path.islink(top):
            raise ValueError(f"{sub}/ is a link: not fingerprinted (confined)")
        for dp, dirs, fns in os.walk(top):
            for x in dirs + fns:
                if os.path.islink(os.path.join(dp, x)):
                    raise ValueError(f"{os.path.relpath(os.path.join(dp, x), run_dir)} is a link: not fingerprinted "
                                     "(confined)")
    if os.path.isabs(fn_path):
        if not os.path.realpath(fn_path).startswith(os.path.realpath(REPO) + os.sep):
            raise ValueError("the batch function's file is outside the run dir and the repo (confined)")
    elif not fn_path or ".." in fn_path.replace("\\", "/").split("/"):
        raise ValueError("the batch function's path leaves the run dir (confined)")


def _hash_confined(h, run_dir: str, rel: str) -> None:
    """The default loop's update for one run file, read through no-follow fds (a link raises ValueError)."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from lib.safe_io import open_nofollow, LinkRefused
    try:
        fd = open_nofollow(run_dir, rel)
    except FileNotFoundError:
        return
    except (LinkRefused, OSError) as e:
        raise ValueError(f"{rel}: {e} (confined)") from None
    try:
        st = os.fstat(fd)
        h.update(rel.encode() + b"\0")
        if st.st_size <= _FP_HASH_MB * 2**20:
            while True:
                block = os.read(fd, 2**20)
                if not block:
                    break
                h.update(block)
        else:
            h.update(f"{st.st_size}:{st.st_mtime_ns}".encode())
        h.update(b"\0")
    finally:
        os.close(fd)


def input_keys(inputs) -> list[str]:
    """One key per input: its params' hash and its occurrence among equal
    params, so repeated identical params (independent stochastic draws) stay
    separate rows."""
    seen, keys = {}, []
    for p in inputs:
        k = input_key(p)
        seen[k] = seen.get(k, 0) + 1
        keys.append(f"{k}#{seen[k] - 1}")
    return keys


PARTIAL_EXT, JOB_EXT = ".partial.jsonl", ".job.json"


def _out_files(out: str) -> tuple:
    """A batch's own files: never part of its code fingerprint."""
    return (out, out + ".tmp", out + ".summary.json", out + ".summary.json.tmp", out + PARTIAL_EXT, out + JOB_EXT,
            out + ".log")


def _same_fn(rec: dict, fn_spec: str, fid: str | None) -> bool:
    """The recorded function is this one: the same spec, or (S1-8) the same identity from another tree."""
    return rec.get("fn") == fn_spec or bool(fid and rec.get("fn_id") == fid)


def _reusable(out: str, fn_spec: str, fp: str, fid: str | None = None, run_dir: str | None = None,
              why: list | None = None, acc: dict | None = None, here: dict | None = None, confined: bool = False,
              trust: bool = False) -> dict:
    """key -> ok row from an earlier --out of the same function and code, and
    (Phase 42 α) from the rows an interrupted or killed run of it had written
    to `<out>.partial.jsonl` (its first line names the function and code). W0-A: a record is used only with the same
    key and an observed closure that still holds here (_observed_check); a row only when its observed set is one
    the record holds. Each refusal's reason goes to `why`, the observed record of what is kept to `acc`; `here`
    (this key's parts) names the parts that differ. `trust`: every ok row of the same function (reuse_report's count)."""
    got = {}
    why = [] if why is None else why

    def admit(rec, rows):
        ok_rows = {r["key"]: r for r in rows if isinstance(r, dict) and r.get("ok") and r.get("key")}
        if trust or not ok_rows:
            return ok_rows
        reason, a = record_check(rec, fp, run_dir, here, confined, rows)
        if reason:
            why.append(reason)
            return {}
        if acc is not None:
            _merge_acc(acc, a)
        return {k: r for k, r in ok_rows.items() if r.get("observed_id") in a["sets"]}
    try:
        prev = json.load(open(out + ".summary.json"))
        if _same_fn(prev, fn_spec, fid):
            body = open(out, "rb").read()
            rows = [json.loads(l) for l in body.decode().splitlines() if l.strip()]
            # Codex round 1 P1-1: a results file and a summary from two different writes (a torn snapshot) are
            # never trusted. A summary from before out_sha256 must at least count the rows it describes.
            want = prev.get("out_sha256")
            if (hashlib.sha256(body).hexdigest() != want) if want else (len(rows) != prev.get("n")):
                print(f"compute: {out}: results file does not match its summary ("
                      f"{'sha256' if want else 'row count'}): its rows are not reused", file=sys.stderr)
                rows = []
            got.update(admit(prev, rows))
    except (OSError, ValueError):
        pass
    try:
        with open(out + PARTIAL_EXT) as f:
            head = json.loads(f.readline() or "{}").get("_partial") or {}
            if _same_fn(head, fn_spec, fid):
                rows = []
                for line in f:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue                  # a line torn by a kill mid-write
                for k, r in admit(head, rows).items():
                    got.setdefault(k, r)
    except (OSError, ValueError, AttributeError):
        pass
    return got


# W0-A: the key parts code_fp hashes; the other key_parts entries are the observed record (not hashed: checked).
FP_BASE_PARTS = ("version", "run_files", "deps", "data_set", "extra")


def record_check(rec: dict, fp: str, run_dir: str | None, here: dict | None = None, confined: bool = False,
                 rows=()) -> tuple[str | None, dict]:
    """(why a stored batch record's rows are not reusable here or None, its observed record): it must hold an
    observed closure (else `no_observed_closure`), carry this key `fp` (else the parts that differ from `here`),
    and its closure must still hold (_observed_check). `rows`: a journal's rows, which carry their observed sets."""
    a, kp = _new_acc(), rec.get("key_parts") if isinstance(rec.get("key_parts"), dict) else {}
    _merge_parts(a, kp)
    _gather(a, rows)
    if not a["sets"]:
        return "no_observed_closure", a
    if rec.get("code_fp") != fp:
        here = here or {}
        return "key differs: " + ", ".join(sorted(k for k in FP_BASE_PARTS if here.get(k) != kp.get(k))), a
    return _observed_check(a, run_dir, confined), a


def _new_acc() -> dict:
    return {"files": {}, "sets": set(), "conflict": False, "child": False, "tree": None}


def _add_files(a: dict, files: dict) -> None:
    for k, v in files.items():
        if a["files"].setdefault(k, v) != v:
            a["conflict"] = True              # one path, two contents: edited while the batch ran


def _gather(a: dict, rows) -> None:
    """Add the observed sets rows carry (_observe); a set whose id is not its digest is left out."""
    for r in rows:
        if not isinstance(r, dict):
            continue
        obs = r.get("observed")
        if isinstance(obs, dict) and r.get("observed_id") == _digest(obs):
            _add_files(a, obs)
            a["sets"].add(r["observed_id"])
        a["child"] = a["child"] or bool(r.get("observed_child"))


def _merge_parts(a: dict, kp: dict) -> None:
    """Add a stored record's observed parts (_observed_parts)."""
    if isinstance(kp.get("observed"), dict) and isinstance(kp.get("observed_sets"), list):
        _add_files(a, kp["observed"])
        a["sets"].update(x for x in kp["observed_sets"] if isinstance(x, str))
    a["child"] = a["child"] or bool(kp.get("observed_child"))
    a["conflict"] = a["conflict"] or bool(kp.get("observed_conflict"))
    a["tree"] = a["tree"] or kp.get("tree")


def _merge_acc(a: dict, b: dict) -> None:
    _add_files(a, b["files"])
    a["sets"] |= b["sets"]
    a["child"], a["conflict"], a["tree"] = a["child"] or b["child"], a["conflict"] or b["conflict"], a["tree"] or b["tree"]


def _observed_parts(a: dict, tree: str) -> dict:
    """The observed record stored in key_parts: the union of the sets (files with sha256), their ids, and the
    whole-tree hash (checked only when a job had a child process)."""
    return {"observed": dict(sorted(a["files"].items())), "observed_sets": sorted(a["sets"]),
            "observed_child": a["child"], "observed_conflict": a["conflict"], "tree": tree}


def _here_digest(rel: str, run_dir: str | None, confined: bool) -> str | None:
    """sha256 here of a recorded observed path ("run:<rel>" under the run dir, else under the repo); None when it
    is missing, unreadable or not a plain relative path. `confined`: run files read with no link followed."""
    in_run = rel.startswith("run:")
    r = rel[4:] if in_run else rel
    if not r or os.path.isabs(r) or ".." in r.split("/") or (in_run and not run_dir):
        return None
    try:
        if in_run and confined:
            if HERE not in sys.path:
                sys.path.insert(0, HERE)
            from lib.safe_io import open_nofollow
            fd = open_nofollow(run_dir, r)
            with os.fdopen(fd, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        return _file_digest(os.path.join(run_dir if in_run else REPO, r))
    except (OSError, ValueError):
        return None


def _observed_check(a: dict, run_dir: str | None, confined: bool = False) -> str | None:
    """Why a stored observed closure does not hold here, or None: every recorded file re-hashes the same, and when
    a job had a child process the whole tree is unchanged."""
    if a["conflict"]:
        return "observed_conflict: a file had two contents while the batch ran"
    for rel, sha in sorted(a["files"].items()):
        now = _here_digest(rel, run_dir, confined)
        if now != sha:
            return f"observed file {'missing or unreadable' if now is None else 'changed'}: {rel}"
    if a["child"] and (not a["tree"] or a["tree"] != _tree_digest()):
        return "a job started a child process, and scripts/ or toolkit/ changed (whole-tree key)"
    return None


def _tree_digest() -> str:
    return _files_digest({x: "all" for x in REPO_CODE_DIRS})


def _stored_heads(out: str) -> list[dict]:
    """The function and code records of a stored batch: its summary, and the journal an interrupted run left."""
    heads = []
    try:
        heads.append(json.load(open(out + ".summary.json")))
    except (OSError, ValueError):
        pass
    try:
        with open(out + PARTIAL_EXT) as f:
            heads.append(json.loads(f.readline() or "{}").get("_partial"))
    except (OSError, ValueError, AttributeError):
        pass
    return [h for h in heads if isinstance(h, dict) and h.get("fn") and h.get("code_fp")]


def _here_spec(rec: dict) -> str:
    """A recorded function as a spec in this tree: from its identity (S1-8) when recorded, else as recorded."""
    fid = rec.get("fn_id") or ""
    if fid.startswith("repo:"):
        path, _, name = fid[len("repo:"):].partition("::")
        return os.path.join(REPO, path) + "::" + name
    return fid or rec["fn"]


REUSE_NOTE = ("cache eligibility, conditional on identical inputs: a stored row is reused only for the same params, "
              "and this check does not generate the next run's inputs; runtime_differs_here compares this "
              "interpreter's packages with the one that recorded the batch, not the workers'; an engine identity "
              "recorded in key_parts.extra is compared when the batch runs, not here (engine_checked)")


def reuse_report(run_dir: str) -> dict:
    """Would the code here reuse the stored rows under `run_dir`? For every stored batch (a `<out>.summary.json`, or
    the `<out>.partial.jsonl` an interrupted batch left): the ok rows stored, the rows run_map would keep (same
    function, same code fingerprint), and why not. Read-only and local: what a launcher runs from the tree it would
    ship, before launching (`compute.py reuse-check`, `cloud/run_cluster.py <config> reuse-check`)."""
    run_dir = os.path.realpath(run_dir)
    outs = set()
    for dp, dirs, fns in os.walk(run_dir):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git", "_truth"))
        for f in fns:
            for ext in (".summary.json", PARTIAL_EXT):
                if f.endswith(ext):
                    outs.add(os.path.join(dp, f[:-len(ext)]))
    batches = []
    for out in sorted(outs):
        heads = _stored_heads(out)
        if not heads:
            continue                     # not a compute batch: no function and code record
        stored = {}
        for h in heads:
            stored.update(_reusable(out, h["fn"], h["code_fp"], trust=True))
        spec = _here_spec(heads[0])
        path = spec.partition("::")[0]
        if not os.path.exists(path if os.path.isabs(path) else os.path.join(run_dir, path)):
            n, why = 0, f"function file not found here: {spec}"
        else:
            rec, why_l = {}, []
            kp = next((h["key_parts"] for h in heads if isinstance(h.get("key_parts"), dict)), {})
            # the caller's part (an engine identity) is taken as recorded: it is compared when the batch runs
            fp = code_fingerprint(run_dir, spec, exclude=_out_files(out), record=rec, extra_digest=kp.get("extra"))
            n = len(_reusable(out, spec, fp, fn_id(run_dir, spec), run_dir=run_dir, why=why_l,
                              here=rec.get("key_parts")))
            why = "" if n >= len(stored) else "; ".join(dict.fromkeys(why_l)) or "recorded under another function"
        rt = heads[0].get("runtime")
        batches.append({"out": os.path.relpath(out, run_dir), "fn": heads[0].get("fn_id") or heads[0]["fn"],
                        "stored": len(stored), "eligible": n, "why": why,
                        "engine_checked": "at run time" if "extra" in (heads[0].get("key_parts") or {}) else None,
                        "runtime_recorded": rt, "runtime_differs_here": bool(rt) and rt != runtime_versions()})
    return {"run_dir": run_dir, "stored": sum(b["stored"] for b in batches),
            "eligible": sum(b["eligible"] for b in batches), "note": REUSE_NOTE, "batches": batches}


def run_map(run_dir, fn_spec, inputs, out, fresh=False, **kw) -> tuple[list[dict], dict]:
    """The CLI's `map`: rows of an earlier run of the same command (same
    function, same code and data, same params) are kept and only the rest is
    computed, so rerunning a stopped or partly failed batch completes it.
    Writes --out and its summary; returns (rows, summary). Phase 42 α: one
    batch at a time per --out (`<out>.lock`); a second is refused."""
    lock = _out_lock(out, run_dir=run_dir)
    if lock is None:
        raise RuntimeError(f"another batch is writing {out}; wait for it (compute.py wait {out}) "
                           "or choose another --out")
    try:
        return _run_map(run_dir, fn_spec, inputs, out, fresh=fresh, **kw)
    finally:
        lock.close()


def _out_base(out: str, run_dir: str) -> tuple[int, str]:
    """(root fd, rel) for no-follow writes of `out` and its sidecars, anchored at the trusted run dir (`out` must lie
    in it): every component below the run dir is traversed with no link followed, so a planted analysis/batch link
    is refused, not resolved (lib.safe_io.anchored; no parent fallback). The caller closes the fd."""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from lib.safe_io import anchored
    return anchored(out, run_dir)


def _out_lock(out: str, block: bool = False, suffix: str = ".lock", run_dir: str | None = None):
    """An exclusive lock on `out` (an open file; closing releases it), or None if another process holds it. The lock
    file is opened for append with no link followed (never truncated)."""
    import fcntl
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from lib.safe_io import open_append_nofollow
    base, name = _out_base(out, run_dir)
    try:
        f = open_append_nofollow(base, name + suffix)
    finally:
        os.close(base)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    except OSError:
        f.close()
        return None
    return f


def _run_map(run_dir, fn_spec, inputs, out, fresh=False, **kw) -> tuple[list[dict], dict]:
    rel_out =os.path.relpath(os.path.realpath(out), os.path.realpath(run_dir))
    rec, extra = {}, kw.pop("key_extra", None)        # W0-A: the caller's key part (batch.py: the engine identity)
    fp = code_fingerprint(run_dir, fn_spec, exclude=_out_files(out), extra=extra, record=rec)
    fid = fn_id(run_dir, fn_spec)
    keys = input_keys(inputs)
    acc, why, tree = _new_acc(), [], _tree_digest()
    old = {} if fresh else _reusable(out, fn_spec, fp, fid, run_dir=run_dir, why=why, acc=acc,
                                     here=rec["key_parts"])
    if why:
        print(f"compute: {out}: stored rows not reused: {'; '.join(dict.fromkeys(why))}", file=sys.stderr)
    todo = [k for k, key in enumerate(keys) if key not in old]
    if todo:
        # Phase 42 α: rows are appended to <out>.partial.jsonl as they finish, so
        # an interrupted or killed batch keeps them (rerunning reuses them).
        # The rows kept so far are copied to a new journal that replaces the old
        # one only once complete (a kill during the copy loses nothing).
        from lib.safe_io import open_append_nofollow, write_nofollow
        base, name = _out_base(out, run_dir)
        journal = json.dumps({"_partial": {"fn": fn_spec, "fn_id": fid, "code_fp": fp, "n": len(inputs), "key_parts": {
            **rec["key_parts"], **_observed_parts(acc, tree)}}}) + "\n" + "".join(
            json.dumps({**r, "key": key}) + "\n" for key, r in old.items())
        try:
            write_nofollow(base, name + PARTIAL_EXT, journal.encode(), atomic=True)
            part = open_append_nofollow(base, name + PARTIAL_EXT)
        finally:
            os.close(base)

        def sink(got):
            for r in got:
                if 0 <= r.get("i", -1) < len(todo):
                    part.write(json.dumps({**r, "key": keys[todo[r["i"]]]}) + "\n")
            part.flush()
        try:
            new_rows, summary = map_fn(run_dir, fn_spec, [inputs[k] for k in todo],
                                       prior_ok=len(inputs) - len(todo), out=rel_out, on_rows=sink, **kw)
        finally:
            part.close()
    else:
        new_rows, summary = [], {"n": 0, "n_ok": 0, "n_failed": 0, "failed_by_kind": {}, "wall_s": 0.0,
                                 "task_core_s": 0.0, "stopped": None}
    by_pos = dict(zip(todo, new_rows))
    _gather(acc, new_rows)
    rows = []
    for k, key in enumerate(keys):
        r = dict(by_pos[k]) if k in by_pos else {**old[key], "reused": True}
        r["i"], r["key"] = k, key
        rows.append(r)
    failed_by = {}
    for r in rows:
        if not r["ok"]:
            failed_by[r.get("kind") or MODEL] = failed_by.get(r.get("kind") or MODEL, 0) + 1
    summary = {**summary, "n": len(rows), "n_ok": sum(r["ok"] for r in rows),
               "n_failed": sum(not r["ok"] for r in rows), "failed_by_kind": failed_by,
               "computed": len(todo), "reused": len(rows) - len(todo),
               "fn": fn_spec, "fn_id": fid, "code_fp": fp, "out": rel_out, "purpose": kw.get("purpose"),
               "runtime": runtime_versions(), "key_parts": {**rec["key_parts"], **_observed_parts(acc, tree)}}
    if why:
        summary["reuse_refused"] = list(dict.fromkeys(why))
    body = "".join(json.dumps(r) + "\n" for r in rows).encode()
    summary["out_sha256"] = hashlib.sha256(body).hexdigest()     # Codex round 1 P1-1: which results it describes
    if os.environ.get("MA_PIPELINE_RUN"):        # the pipeline run that computed it (scripts/pipeline.py)
        summary["pipeline_run"] = os.environ["MA_PIPELINE_RUN"]
    if os.environ.get("MA_COMPUTE_LAUNCH"):
        summary["launch"] = os.environ["MA_COMPUTE_LAUNCH"]
    from lib.safe_io import remove_nofollow, write_nofollow
    base, name = _out_base(out, run_dir)
    try:
        write_nofollow(base, name, body, atomic=True)
        write_nofollow(base, name + ".summary.json", json.dumps(summary, indent=1).encode(), atomic=True)
        try:
            remove_nofollow(base, name + PARTIAL_EXT)
        except OSError:
            pass
    finally:
        os.close(base)
    return rows, summary




def main() -> int:
    ap = argparse.ArgumentParser(description="Run a model function over many parameter sets")
    ap.add_argument("cmd", nargs="?", choices=["map", "reuse-check"])
    ap.add_argument("run_dir", nargs="?", help="map, reuse-check: the run directory")
    ap.add_argument("--expect-all", action="store_true",
                    help="reuse-check: exit 1 unless every stored row would be reused")
    ap.add_argument("--fn")
    ap.add_argument("--inputs")
    ap.add_argument("--out")
    ap.add_argument("--backend", default="auto", choices=["auto", "local", "dask"])
    ap.add_argument("--max-workers", type=int)
    ap.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    ap.add_argument("--timeout-s", type=float)
    ap.add_argument("--max-core-hours", type=float,
                    help="this call's allowance (default MA_COMPUTE_CALL_CORE_HOURS=50, within the run cap)")
    ap.add_argument("--purpose",
                    help="what this batch is for: the finding IDs it addresses (e.g. 'D-019, R-021') or the "
                         "plan item (e.g. 'plan: impact library'); recorded in the compute ledger")
    ap.add_argument("--fresh", action="store_true",
                    help="recompute every row (default: keep rows of an earlier run of the same command and code)")
    ap.add_argument("--no-ledger", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out-root", help=argparse.SUPPRESS)   # a remote chunk: the call dir its worker made for --out
    a = ap.parse_args()
    if a.cmd == "reuse-check":
        if not a.run_dir:
            ap.error("usage: compute.py reuse-check <run_dir> [--expect-all]")
        r = reuse_report(a.run_dir)
        for b in r["batches"]:
            print(f"{b['out']}: {b['eligible']} of {b['stored']} stored rows eligible for reuse"
                  + (f" ({b['why']})" if b["why"] else ""), file=sys.stderr)
        print(f"({REUSE_NOTE})", file=sys.stderr)
        print(json.dumps(r))
        return 1 if a.expect_all and r["eligible"] < r["stored"] else 0
    import signal

    def _on_term(*_):
        raise KeyboardInterrupt      # unwinds map_fn: the pool's groups are killed
    signal.signal(signal.SIGTERM, _on_term)
    gate = os.environ.get("MA_COMPUTE_GATE")
    if gate:
        gate = gate.replace("{pid}", str(os.getpid()))
        end = time.time() + 60
        while not os.path.exists(gate):
            if time.time() > end:
                return 3
            time.sleep(0.02)
        if os.path.exists(os.environ.get("MA_COMPUTE_CANCEL", "/nonexistent")):
            return 3          # cancelled before any work; the parent labels the rows
    for k in ("MA_COMPUTE_GATE", "MA_COMPUTE_CANCEL"):
        os.environ.pop(k, None)           # never inherited by the model's own subprocesses
    if a.cmd != "map" or not (a.run_dir and a.fn and a.inputs and a.out):
        ap.error("usage: compute.py map <run_dir> --fn <path.py>::<fn> --inputs <jsonl> --out <jsonl> --purpose <ids>")
    if not a.no_ledger and not (a.purpose or "").strip():
        ap.error("--purpose is required: name the finding IDs this batch addresses (e.g. --purpose 'D-019') "
                 "or the plan item it serves (e.g. --purpose 'plan: impact library'). It is recorded with the "
                 "batch's core-hours in models/compute_ledger.json.")
    if a.no_ledger:   # plain map, no reuse
        rows, summary = map_fn(a.run_dir, a.fn, _read_inputs(a.inputs), a.backend, a.max_workers,
                               a.max_tasks, a.timeout_s, ledger=False, max_core_hours=a.max_core_hours)
        from lib.safe_io import publish
        root = a.out_root or a.run_dir
        publish(a.out, "".join(json.dumps(r) + "\n" for r in rows), root)
        publish(a.out + ".summary.json", json.dumps(summary, indent=1), root)
    else:
        rows, summary = run_map(a.run_dir, a.fn, _read_inputs(a.inputs), a.out, fresh=a.fresh,
                                backend=a.backend, max_workers=a.max_workers, max_tasks=a.max_tasks,
                                timeout_s=a.timeout_s, max_core_hours=a.max_core_hours, purpose=a.purpose.strip())
    print(json.dumps({k: v for k, v in summary.items() if k not in ("bundle", "chunks")}))
    return 0 if summary["n_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
