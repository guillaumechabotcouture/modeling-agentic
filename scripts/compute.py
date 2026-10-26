#!/usr/bin/env python3
"""
Phase 24 α — compute service: run a model function over many parameter sets.

E7 measured where a full run's wall time goes: 55-61% was the modeler waiting
on its own simulations (calibration grids, UQ draws, scenario sweeps) that ran
serially on one VM, polled in 10-minute sleep loops. Those batches are
embarrassingly parallel. This service fans them out.

    python3 scripts/compute.py map {run_dir} --fn models/outcome_fn.py::outcome_fn \\
        --inputs models/grid_points.jsonl --out models/grid_results.jsonl

  --inputs   JSONL, one parameter dict per line (or a JSON list)
  --out      JSONL, one row per input, in input order:
             {"i": 0, "ok": true, "result": {...}, "seconds": 3.1}
             {"i": 1, "ok": false, "error": "ValueError: ...", "seconds": 0.2}
  a summary is printed and written next to --out (`<out>.summary.json`)

Python API (used by scripts/propagate_uncertainty.py):

    from compute import map_fn
    rows = map_fn(run_dir, "models/outcome_fn.py::outcome_fn", list_of_params)

Backends (auto-selected):
  dask   when MA_COMPUTE_SCHEDULER is set (the launcher sets it on Coiled).
         Tasks go to the cluster's workers except those listed in
         MA_COMPUTE_EXCLUDE_WORKERS (the run VMs, whose single thread is busy
         with the run). The run's `models/`, `data/` and the repo's `scripts/`
         are bundled once and unpacked on each worker at the SAME absolute
         paths, so model code with absolute paths works unchanged.
  local  otherwise: a process pool on this machine (cores - 1).

Guardrails: --max-tasks (default 20,000) per call; a per-call allowance
(--max-core-hours, default MA_COMPUTE_CALL_CORE_HOURS = 50) and a run-wide
cap (MA_COMPUTE_MAX_CORE_HOURS, default 200), both in core-hours and tracked
in `{run_dir}/models/compute_ledger.json`. A batch stops when its allowance
is used; --timeout-s stops it at a wall-clock limit.

Known limits: a process the model starts in its own session (setsid,
daemonizing) escapes the kill on a stop. Work redone by dask after a worker
dies is not seen directly; a worker that vanishes during a call is charged as
busy for the whole call instead. Remote chunks the client cannot confirm
stopped are charged the call's whole allowance. A function that raises fails only its
own row. Files over --max-file-mb (default 100) are left out of the bundle
and listed in the summary.

Usage:
    python3 scripts/compute.py map ...
    python3 scripts/compute.py --self-test
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
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_MAX_TASKS = 20000
DEFAULT_MAX_FILE_MB = 100
LEDGER_REL = os.path.join("models", "compute_ledger.json")


# --------------------------------------------------------------------------- #
# Loading the function (same rules locally and on workers)
# --------------------------------------------------------------------------- #

def load_fn(run_dir: str, fn_spec: str):
    """`models/outcome_fn.py::outcome_fn` → callable. The run root and its
    models/ dir are put on sys.path so sibling imports work."""
    path, _, name = fn_spec.partition("::")
    if not name:
        raise ValueError(f"--fn must be <path.py>::<function>, got {fn_spec!r}")
    full = path if os.path.isabs(path) else os.path.join(run_dir, path)
    for p in (run_dir, os.path.dirname(full), os.path.join(REPO, "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    key = "ma_compute_" + hashlib.sha1(full.encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(key, full)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    fn = getattr(mod, name, None)
    if not callable(fn):
        raise ValueError(f"{fn_spec}: {name!r} is not a callable in {path}")
    return fn


def _jsonable(x):
    try:
        json.dumps(x)
        return x
    except (TypeError, ValueError):
        if isinstance(x, dict):
            return {str(k): _jsonable(v) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [_jsonable(v) for v in x]
        try:
            return float(x)
        except (TypeError, ValueError):
            return str(x)


def _call(fn, i: int, params: dict) -> dict:
    t0 = time.time()
    try:
        return {"i": i, "ok": True, "result": _jsonable(fn(params)), "seconds": round(time.time() - t0, 3)}
    except Exception as e:  # noqa: BLE001 — one failing row must not kill the batch
        return {"i": i, "ok": False, "error": f"{type(e).__name__}: {e}"[:500],
                "trace": traceback.format_exc()[-1500:], "seconds": round(time.time() - t0, 3)}


# --------------------------------------------------------------------------- #
# Local backend
# --------------------------------------------------------------------------- #

_LOCAL_FN = None


def _local_init(run_dir: str, fn_spec: str, init_dir: str | None = None):
    """Pool initializer. Each process records its model load time in
    `init_dir`, so loads are charged even in processes that get no chunk."""
    global _LOCAL_FN
    t0 = time.time()
    # Own process group: whatever the model spawns (an external simulator)
    # is killed with this process. A child that starts its own session escapes.
    # Inside a remote chunk the whole chunk is already one group (killed as one).
    if not os.environ.get("MA_COMPUTE_IN_CHUNK"):
        os.setpgrp()
    os.chdir(run_dir)
    try:
        _LOCAL_FN = load_fn(run_dir, fn_spec)
    finally:
        if init_dir:
            open(os.path.join(init_dir, str(os.getpid())), "w").write(str(time.time() - t0))


def _local_chunk(chunk):
    return [_call(_LOCAL_FN, i, params) for i, params in chunk]


def _chunks(items: list, n_chunks: int) -> list:
    size = -(-len(items) // max(1, n_chunks))
    return [items[k:k + size] for k in range(0, len(items), size)] if items else []


CAP_STOP = "stopped: compute cap reached"


def _budget_deadline(deadline, allowance_s, cores):
    """(deadline, reason): the earlier of the caller's deadline and the time
    at which `cores` busy cores would use up the core-second allowance."""
    if allowance_s is None:
        return deadline, "timed out"
    b = time.time() + allowance_s / max(1, cores)
    return (b, CAP_STOP) if deadline is None or b < deadline else (deadline, "timed out")


def _killpg(pgid, sig, direct=False):
    """Signal the group; with direct=True also the process itself (a pool
    worker stopped before _local_init has not made its group yet). Only pass
    direct=True for a process not yet reaped: a reaped pid can be reused."""
    for kill in (os.killpg, os.kill) if direct else (os.killpg,):
        try:
            kill(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _kill_groups(pids, grace_s=2.0, direct=True):
    """SIGTERM every pool process group, then SIGKILL whatever is left once
    one shared grace period ends (a model may trap SIGTERM)."""
    import signal
    for pid in pids:
        _killpg(pid, signal.SIGTERM, direct)
    end = time.time() + grace_s
    while time.time() < end and any(_group_alive(p, direct) for p in pids):
        time.sleep(0.05)
    for pid in pids:
        _killpg(pid, signal.SIGKILL, direct)


def _group_alive(pgid, direct=False) -> bool:
    for probe in (os.killpg, os.kill) if direct else (os.killpg,):
        try:
            probe(pgid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            pass
    return False


def _map_local(run_dir, fn_spec, inputs, max_workers, deadline=None, allowance_s=None, progress=None):
    """Always a fresh spawn pool, even for one worker: the model's sibling
    modules and its chdir never leak into the caller or across runs."""
    import multiprocessing as mp
    progress = {} if progress is None else progress
    n = max(1, min(max_workers or max(1, (os.cpu_count() or 2) - 1), len(inputs) or 1))
    progress.update(slots=n, cores_per_slot=1, core_s=0.0)
    chunks = _chunks(list(enumerate(inputs)), n * 8)
    deadline, why = _budget_deadline(deadline, allowance_s, n)
    import tempfile
    init_dir = tempfile.TemporaryDirectory(prefix="ma_compute_init_")
    ex = cf.ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn"),
                                initializer=_local_init, initargs=(run_dir, fn_spec, init_dir.name))
    futs = {ex.submit(_local_chunk, ch): ch for ch in chunks}
    pending, rows, stop, lost, pids = set(futs), [], None, 0, set()
    try:
        while pending:
            pids |= set((getattr(ex, "_processes", None) or {}).keys())
            left = None if deadline is None else max(0.0, deadline - time.time())
            done, pending = cf.wait(pending, timeout=left, return_when=cf.FIRST_COMPLETED)
            if not done:
                stop = why
                break
            for f in done:
                try:
                    got = f.result()
                    rows += got
                    progress["core_s"] += _known_core_s(got)
                except Exception as e:  # noqa: BLE001 — a crashed process fails only its chunk
                    lost += 1
                    rows += [{"i": i, "ok": False, "error": f"chunk failed: {type(e).__name__}: {e}"[:500]}
                             for i, _ in futs[f]]
            if pending and allowance_s is not None and progress["core_s"] >= allowance_s:
                stop = CAP_STOP
                break
    finally:
        live = set((getattr(ex, "_processes", None) or {}).keys())   # not yet reaped
        pids |= live
        if pending:
            _kill_groups(live)
            _kill_groups(pids - live, grace_s=0, direct=False)
        ex.shutdown(wait=True, cancel_futures=True)
        _kill_groups(pids, grace_s=0, direct=False)   # leftovers the model spawned (pool already reaped)
    # Model load time of every pool process (a process still loading when it
    # was killed is covered by the unfinished-chunk estimate).
    for name in os.listdir(init_dir.name):
        try:
            progress["core_s"] += float(open(os.path.join(init_dir.name, name)).read())
        except (OSError, ValueError):
            pass
    init_dir.cleanup()
    return rows, {"backend": "local", "workers": n, "slots": n, "cores_per_slot": 1, "core_s": progress["core_s"],
                  "stopped": stop, "unknown_chunks": lost + len(pending)}


# --------------------------------------------------------------------------- #
# Dask backend
# --------------------------------------------------------------------------- #

def make_bundle(run_dir: str, max_file_mb: float = DEFAULT_MAX_FILE_MB) -> tuple[bytes, dict]:
    """tar.gz of the run's models/ and data/ and the repo's scripts/, with
    ABSOLUTE member paths so workers recreate the same layout."""
    run_dir = os.path.realpath(run_dir)
    skipped, n = [], 0
    buf = io.BytesIO()
    # dereference: symlinked inputs are stored as files (workers extract with
    # the "data" filter, which rejects absolute links).
    with tarfile.open(fileobj=buf, mode="w:gz", dereference=True) as t:
        for root in (os.path.join(run_dir, "models"), os.path.join(run_dir, "data"), os.path.join(REPO, "scripts")):
            # Follow symlinked dirs; stop only at a loop (a dir that is its own
            # ancestor). Two names for one dir are both bundled: models use either.
            ancestors = {root: frozenset()}
            for dp, dirs, files in os.walk(root, followlinks=True):
                real = os.path.realpath(dp)
                anc = ancestors.pop(dp, frozenset())
                if real in anc:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "_truth")]
                for d in dirs:
                    ancestors[os.path.join(dp, d)] = anc | {real}
                for fn in files:
                    p = os.path.join(dp, fn)
                    try:
                        size = os.path.getsize(p)
                    except OSError:
                        continue
                    if size > max_file_mb * 2**20:
                        skipped.append((os.path.relpath(p, run_dir), round(size / 2**20, 1)))
                        continue
                    t.add(p, arcname=p.lstrip("/"))
                    n += 1
    data = buf.getvalue()
    return data, {"files": n, "bundle_mb": round(len(data) / 2**20, 1), "skipped_large": skipped,
                  "sha256": hashlib.sha256(data).hexdigest()}


_CALLS_DIR = "ma_compute_calls"     # under the temp dir: running-chunk pids and cancel marks per call


def _no_symlink_path(path: str) -> bool:
    """True if no existing component of `path` is a symlink (so writing or
    deleting there cannot be redirected into another directory)."""
    p = os.path.normpath(path)
    while p not in ("/", ""):
        if os.path.islink(p):
            return False
        p = os.path.dirname(p)
    return True


def _remote_task(bundle: bytes, sha: str, run_dir: str, fn_spec: str, chunk: list,
                 deadline: float | None = None, procs: int | None = None,
                 chunk_allowance_s: float | None = None, call_id: str | None = None, repo: str | None = None):
    """Runs on a dask worker. Under a machine-wide lock: make sure the files on
    disk are exactly THIS bundle (another call's bundle may have replaced them:
    its extra files are removed and no stale bytecode survives), then evaluate
    the chunk. The live run directory (has metadata.json) is never touched:
    there the model runs against the run's own files, and no path through a
    symlink is written or deleted. The chunk stops at `deadline` (wall clock)
    or once it has used `chunk_allowance_s` core-seconds, counted from when it
    starts running, not while it queues. Returns the rows and the core-seconds
    used, including work in crashed processes."""
    import fcntl, io, json, os, shutil, signal, subprocess, sys, tarfile, tempfile, time  # noqa: E401 — pickled by value
    try:
        from distributed import get_worker
        waddr = get_worker().address
    except Exception:  # noqa: BLE001 — also called outside a worker (self-test)
        waddr = None
    # One machine-wide lock: one chunk at a time per VM (each chunk already uses
    # its cores), and no bundle is replaced while a chunk is reading it.
    lock = os.path.join(tempfile.gettempdir(), "ma_compute.lock")
    procs = procs or int(os.environ.get("MA_COMPUTE_PROCS_PER_WORKER") or os.cpu_count() or 1)
    calls = os.path.join(tempfile.gettempdir(), _CALLS_DIR, call_id or "none")
    os.makedirs(calls, exist_ok=True)

    def rows_with(msg):
        return [{"i": i, "ok": False, "error": msg[:500], "worker": waddr} for i, _ in chunk]

    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        t0 = time.time()

        def failed(msg):
            return {"rows": rows_with(msg), "core_s": round((time.time() - t0) * procs, 3)}
        if os.path.exists(os.path.join(calls, "cancel")):
            return {"rows": rows_with("cancelled"), "core_s": 0.0}
        live = os.path.exists(os.path.join(run_dir, "metadata.json"))
        if not live:
            tf = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
            members = [m.name for m in tf.getmembers() if m.isfile()]
            dirs = {os.path.dirname("/" + m) for m in members} | {run_dir}
            if not all(_no_symlink_path(d) for d in dirs):
                return failed("refusing to stage the bundle: a target directory is a symlink")
            if any(os.path.exists(os.path.join(d, "metadata.json")) for d in dirs if d.startswith(run_dir)):
                return failed("refusing to stage the bundle over a live run directory")
            current = os.path.join(run_dir, ".ma_compute_bundle")
            try:
                prev = json.load(open(current))
            except (OSError, ValueError):
                prev = {}
            if prev.get("sha") != sha:
                for old in set(prev.get("members") or []) - set(members):
                    path = "/" + old
                    if _no_symlink_path(os.path.dirname(path)) and not os.path.islink(path):
                        try:
                            os.unlink(path)
                        except OSError:
                            pass
                for d in dirs:
                    pyc = os.path.join(d, "__pycache__")
                    if not os.path.islink(pyc):
                        shutil.rmtree(pyc, ignore_errors=True)
                for m in members:
                    if os.path.islink("/" + m):      # extraction would write through it
                        os.unlink("/" + m)
                tf.extractall("/", filter="data")
                os.makedirs(run_dir, exist_ok=True)
                json.dump({"sha": sha, "members": members}, open(current, "w"))
        # dask workers are daemonic (no multiprocessing children), so the chunk
        # runs in a subprocess using this module's local backend across the VM's
        # cores. MA_COMPUTE_PROCS_PER_WORKER caps processes for memory-heavy models.
        with tempfile.TemporaryDirectory() as td:
            inp, out = os.path.join(td, "in.json"), os.path.join(td, "out.jsonl")
            json.dump([p for _, p in chunk], open(inp, "w"))
            # The caller's repo (the bundle unpacks its scripts/ there); this
            # module's own location on a worker is wherever dask uploaded it.
            script = os.path.join(repo or REPO, "scripts", "compute.py")
            # Model code gets no credentials the worker may hold (cloud, model
            # APIs, the cluster's TLS files): same rule as agents/enforcement.py.
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("MA_COMPUTE_SCHEDULER", "MA_COMPUTE_TLS", "AZ_", "AZURE_", "AWS_", "GOOGLE_",
                                        "GCP_", "OPENAI_", "ANTHROPIC_", "CLAUDE", "COILED_", "DASK_"))}
            env["PYTHONDONTWRITEBYTECODE"] = "1"     # bytecode never outlives its bundle
            env["MA_COMPUTE_IN_CHUNK"] = "1"
            cancel_mark = os.path.join(calls, "cancel")
            cmd = [sys.executable, script, "map", run_dir, "--fn", fn_spec, "--inputs", inp,
                   "--out", out, "--backend", "local", "--max-workers", str(procs), "--no-ledger",
                   "--max-tasks", str(max(1, len(chunk)))]   # the caller already enforced its limit
            if chunk_allowance_s is not None:
                cmd += ["--max-core-hours", f"{chunk_allowance_s / 3600:.6f}"]
            left = None
            if deadline is not None:
                left = deadline - time.time()
                if left <= 0:
                    return {"rows": rows_with("timed out"), "core_s": 0.0}
                cmd += ["--timeout-s", f"{left:.1f}"]
            hard = None if left is None else left + 60
            if chunk_allowance_s is not None:
                b = chunk_allowance_s / max(1, min(procs, len(chunk))) + 60
                hard = b if hard is None else min(hard, b)
            # Own session: on a hard stop or a cancel the chunk is signalled as a
            # group; its SIGTERM handler kills its pool's groups.
            # Output to a file, not a pipe: a stray grandchild holding the pipe
            # open must not keep this wait from returning.
            errf = open(os.path.join(td, "chunk.err"), "w+")
            # The chunk does no work until it is registered (MA_COMPUTE_GATE) and
            # then re-checks the cancel mark itself, so once _cancel_call returns
            # no chunk of that call can start working.
            env["MA_COMPUTE_GATE"] = os.path.join(calls, "{pid}")
            env["MA_COMPUTE_CANCEL"] = cancel_mark
            proc = subprocess.Popen(cmd, cwd=run_dir, env=env, stdout=subprocess.DEVNULL, stderr=errf,
                                    start_new_session=True)
            pidf = os.path.join(calls, str(proc.pid))
            open(pidf + ".tmp", "w").write(str(proc.pid))
            os.replace(pidf + ".tmp", pidf)
            # _cancel_call writes its mark, then scans pids; this side writes its
            # pid, then checks the mark: one of the two always sees the other.
            if os.path.exists(os.path.join(calls, "cancel")):
                _stop_chunk(proc.pid, grace_s=0)
            try:
                proc.wait(timeout=hard)
            except subprocess.TimeoutExpired:
                _stop_chunk(proc.pid)
                proc.wait()
                return failed("timed out (chunk subprocess killed)")
            finally:
                _killpg(proc.pid, signal.SIGKILL)   # anything left in the chunk's group
                try:
                    os.unlink(pidf)
                except OSError:
                    pass
            errf.seek(0)
            err = errf.read()
            if not os.path.exists(out):
                if os.path.exists(cancel_mark):
                    return failed("cancelled")
                return failed(f"chunk subprocess failed (rc {proc.returncode}): {(err or '')[-800:]}")
            rows = [json.loads(l) for l in open(out) if l.strip()]
            try:
                core_s = float(json.load(open(out + ".summary.json"))["task_core_s"])
            except (OSError, ValueError, KeyError, TypeError):
                core_s = (time.time() - t0) * procs    # no summary: charge the chunk as fully busy
    return {"rows": [{**row, "i": chunk[k][0], "worker": waddr} for k, row in enumerate(rows)], "core_s": core_s}


def _stop_chunk(pid: int, grace_s: float = 5.0):
    """SIGTERM a chunk subprocess's group (its handler kills its pool), then
    SIGKILL the group after the grace period."""
    import signal
    _killpg(pid, signal.SIGTERM)       # group only: the chunk is its session leader
    end = time.time() + grace_s
    while time.time() < end and _group_alive(pid):
        time.sleep(0.1)
    _killpg(pid, signal.SIGKILL)
    end = time.time() + 5
    while time.time() < end and _group_alive(pid):   # SIGKILL is not instant; confirm
        time.sleep(0.05)


def _cancel_call(call_id: str) -> int:
    """On a worker (client.run): stop this call's running chunks and keep its
    queued ones from starting. Returns how many chunks were stopped."""
    import tempfile
    calls = os.path.join(tempfile.gettempdir(), _CALLS_DIR, call_id)
    os.makedirs(calls, exist_ok=True)
    open(os.path.join(calls, "cancel"), "w").write(str(time.time()))
    n = 0
    for name in os.listdir(calls):
        if name.isdigit():
            _stop_chunk(int(name))
            n += 1
    return n


try:
    from distributed.diagnostics.plugin import WorkerPlugin as _WorkerPlugin
except ImportError:          # local backend only
    _WorkerPlugin = object


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


# Launcher settings for an autoscaling pool (Coiled check, Phase 30). Coiled's
# adapt adds VMs only when queued work exceeds `target_duration` (default 3 min),
# and dask counts each worker thread as a parallel slot although a compute VM
# runs one chunk at a time on all its cores: with the defaults the pool stayed
# at 1 VM for a 45-min batch. Use as coiled.Cluster(worker_options=WORKER_OPTIONS)
# and cluster.adapt(..., **ADAPT_OPTIONS).
WORKER_OPTIONS = {"nthreads": 1}
ADAPT_OPTIONS = {"target_duration": "20s"}


def register_compute_tags(client, exclude: list[str] | tuple = (), resource: str = "compute") -> None:
    """Tag compute workers on an autoscaling cluster. Uploads this module first
    (as a worker plugin, so workers that join later get it too): the plugin is
    pickled by reference to `compute`, which cluster workers cannot otherwise
    import (Coiled check, Phase 30; same class of bug as PR #20)."""
    client.upload_file(os.path.abspath(__file__))
    client.register_plugin(ComputeTagPlugin(resource, exclude=list(exclude)), name=ComputeTagPlugin.name)


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


def _map_dask(run_dir, fn_spec, inputs, max_workers, chunks_per_worker=4, deadline=None, allowance_s=None,
              progress=None):
    """The allowance is split over the chunks that can run at once; each chunk
    enforces its share from when it starts (queueing behind other calls on a
    shared pool does not count). Whenever the call ends with chunks still
    out (cap, timeout, interrupt), those chunks are stopped on the workers."""
    import uuid
    import cloudpickle
    from distributed import Client, wait
    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    addr = os.environ["MA_COMPUTE_SCHEDULER"]
    exclude = {w.strip() for w in os.environ.get("MA_COMPUTE_EXCLUDE_WORKERS", "").split(",") if w.strip()}
    bundle, binfo = make_bundle(run_dir)
    call_id = uuid.uuid4().hex
    progress = {} if progress is None else progress
    with Client(addr, timeout="60s", security=_security()) as c:
        # Dask pickles functions by reference when it can, so the scheduler and
        # workers must be able to import this module: ship it to them.
        c.upload_file(os.path.abspath(__file__))
        winfo = c.scheduler_info()["workers"]
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
        cores_per_slot = max(procs.values()) if procs else int(env_procs or os.environ.get("MA_COMPUTE_CORES_PER_WORKER") or 8)
        pool = max(len(workers), target if res else 0, 1)        # the pool the batch may grow into
        chunks = _chunks(list(enumerate(inputs)), pool * chunks_per_worker)
        slots = max(1, min(pool, len(chunks)))   # _remote_task runs one chunk at a time per VM
        progress.update(slots=slots, cores_per_slot=cores_per_slot, core_s=0.0)
        bfut = (c.scatter(bundle, broadcast=False, hash=False) if res
                else c.scatter(bundle, workers=workers, broadcast=True))
        placement = ({"resources": {res: 1}} if res else {"workers": workers, "allow_other_workers": False})

        # At most `slots` chunks are out at once. Each gets a share of what is
        # left of the allowance after charges and the shares of chunks still
        # out, so the chunks together can never exceed the allowance.
        held = {}                                   # future -> its share

        def submit(ch, attempt):
            share = None
            if allowance_s is not None:
                free = allowance_s - progress["core_s"] - sum(held.values())
                share = free / max(1, slots - len(held))
                if share <= 0:
                    return None
            # A lost chunk is resubmitted once here (and the lost attempt
            # charged), rather than through dask retries, which would hide it.
            f = c.submit(_remote_task, bfut, binfo["sha256"], run_dir, fn_spec, ch, deadline,
                         int(env_procs) if env_procs else None,     # None: the worker uses its own core count
                         share, call_id, REPO, pure=False, **placement)
            futs[f] = (ch, attempt)
            held[f] = share or 0.0
            started[f] = time.time()
            return f
        futs, queue, started, errors = {}, [(ch, 0) for ch in chunks], {}, {}
        pending, rows, stop, lost = set(), [], None, 0
        try:
            while queue or pending:
                while queue and len(pending) < slots:
                    f = submit(*queue[0])
                    if f is None:
                        break
                    queue.pop(0)
                    pending.add(f)
                if not pending:
                    stop = CAP_STOP
                    break
                left = None if deadline is None else max(0.0, deadline - time.time())
                try:
                    done, pending = wait(pending, timeout=left, return_when="FIRST_COMPLETED")
                except TimeoutError:
                    stop = "timed out"
                    break
                for f in done:
                    ch, attempt = futs[f]
                    f_share = held.pop(f, None)
                    try:
                        got = f.result()
                        rows += got["rows"]
                        progress["core_s"] += float(got["core_s"])
                    except Exception as e:  # noqa: BLE001 — a lost worker fails only its chunk
                        # Its work is unknown: charge the share it was allowed
                        # (chunks enforce their share), or estimate it at the end.
                        # charged as busy since submission, never above its share
                        busy = (time.time() - started.get(f, time.time())) * cores_per_slot
                        if f_share:
                            progress["core_s"] += min(f_share, busy)
                        else:
                            lost += 1
                        msg = f"chunk failed: {type(e).__name__}: {e}"[:500]
                        errors.update({i: msg for i, _ in ch})
                        if attempt == 0 and (deadline is None or time.time() < deadline):
                            queue.insert(0, (ch, 1))
                            continue
                        rows += [{"i": i, "ok": False, "error": errors[i]} for i, _ in ch]
                if (pending or queue) and allowance_s is not None and progress["core_s"] >= allowance_s:
                    stop = CAP_STOP
                    break
        finally:
            if pending:
                # Stop the call's running chunks (and keep queued ones from starting),
                # THEN collect what they report: a stopped chunk returns the core-seconds
                # it actually used, a chunk that never started returns 0. Only chunks
                # that report nothing are estimated (E7 attempt 6: three concurrent
                # timed-out calls were each charged the whole pool, ~65 real core-hours
                # booked as 282, and the run cap stopped all further compute).
                try:
                    c.run(_cancel_call, call_id, workers=[w for w in c.scheduler_info()["workers"] if w not in exclude])
                except Exception:  # noqa: BLE001 — cannot confirm the remote stop
                    progress["remote_unconfirmed"] = True
                try:
                    wait(pending, timeout=float(os.environ.get("MA_COMPUTE_SETTLE_S", "120")))
                except TimeoutError:
                    pass
                for f in list(pending):
                    if f.done():
                        try:
                            progress["core_s"] += float(f.result()["core_s"])
                        except Exception:  # noqa: BLE001 — a failed settle stays unknown
                            continue
                        pending.discard(f)
                for f in pending:
                    f.cancel()
            # The scheduler silently reruns tasks from a worker that died; that
            # attempt's work is invisible here, so a vanished worker is charged
            # as busy for the whole call.
            # On an autoscaling pool, scale-down only retires idle workers: no lost work.
            try:
                gone = [] if res else [w for w in workers if w not in c.scheduler_info()["workers"]]
            except Exception:  # noqa: BLE001
                gone = [] if res else list(workers)
            progress["gone_workers"] = len(gone)
    done_i = {r["i"] for r in rows}
    rows += [{"i": i, "ok": False, "error": msg} for i, msg in errors.items() if i not in done_i]   # keep the real error
    return rows, {"backend": "dask", "workers": len(workers), "pool_target": pool, "slots": slots,
                  "cores_per_slot": cores_per_slot,
                  "core_s": progress["core_s"], "stopped": stop,
                  "unknown_chunks": lost + len(pending) + len(gone), "bundle": binfo}


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #

def _known_core_s(rows) -> float:
    return sum(r.get("seconds") or 0 for r in rows)


class _LedgerLock:
    """Exclusive lock on the run's compute ledger (POSIX advisory lock)."""

    def __init__(self, run_dir):
        os.makedirs(os.path.join(run_dir, "models"), exist_ok=True)
        self.path = os.path.join(run_dir, LEDGER_REL)
        self.lock = self.path + ".lock"

    def __enter__(self):
        import fcntl
        self.f = open(self.lock, "w")
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
        tmp = self.path + ".tmp"
        json.dump(self.led, open(tmp, "w"), indent=1)
        os.replace(tmp, self.path)
        self.f.close()
        return False


def _live_reservations(led) -> float:
    """Core-seconds held by calls still running on this host. A call whose
    process is gone without settling (killed caller) is charged its whole
    reservation: its remote chunks may still be running up to that allowance."""
    host, total = os.uname().nodename, 0.0
    for rid, r in list(led["reservations"].items()):
        alive = r.get("host") != host
        if r.get("host") == host:
            try:
                os.kill(int(r["pid"]), 0)
                alive = True
            except (OSError, ValueError, KeyError):
                alive = False
        if alive:
            total += float(r.get("core_s") or 0)
        else:
            del led["reservations"][rid]
            led["core_seconds"] = round(led["core_seconds"] + float(r.get("core_s") or 0), 1)
            led["calls"].append({"fn": r.get("fn"), "stopped": "caller died; charged its reservation",
                                 "task_core_s": r.get("core_s"), "ts": time.strftime("%FT%T")})
    return total


def map_fn(run_dir: str, fn_spec: str, inputs: list, backend: str = "auto",
           max_workers: int | None = None, max_tasks: int = DEFAULT_MAX_TASKS,
           timeout_s: float | None = None, ledger: bool = True,
           max_core_hours: float | None = None) -> tuple[list[dict], dict]:
    """Evaluate fn over inputs. Under the ledger, the call first reserves its
    allowance (`max_core_hours`, default MA_COMPUTE_CALL_CORE_HOURS = 50,
    never more than the run has left) and stops when it is used up;
    unfinished rows fail with "stopped: compute cap reached" or "timed out".
    A call that is interrupted is charged an upper bound of what it used."""
    run_dir = os.path.realpath(run_dir)
    if len(inputs) > max_tasks:
        raise ValueError(f"{len(inputs)} tasks exceeds --max-tasks {max_tasks}")
    allowance_s, rid = (None if max_core_hours is None else max_core_hours * 3600), None
    if ledger:
        cap_s = float(os.environ.get("MA_COMPUTE_MAX_CORE_HOURS", "200")) * 3600
        with _LedgerLock(run_dir) as led:
            held = _live_reservations(led)
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
            rid = f"{os.uname().nodename}:{os.getpid()}:{time.time():.6f}"
            led["reservations"][rid] = {"core_s": allowance_s, "pid": os.getpid(),
                                        "host": os.uname().nodename, "fn": fn_spec}
    if backend == "auto":
        backend = "dask" if os.environ.get("MA_COMPUTE_SCHEDULER") else "local"
    t0 = time.time()
    deadline = t0 + timeout_s if timeout_s else None
    progress = {}
    try:
        if backend == "dask":
            rows, info = _map_dask(run_dir, fn_spec, inputs, max_workers, deadline=deadline,
                                   allowance_s=allowance_s, progress=progress)
        else:
            rows, info = _map_local(run_dir, fn_spec, inputs, max_workers, deadline=deadline,
                                    allowance_s=allowance_s, progress=progress)
        done = {r["i"] for r in rows}
        why = info.get("stopped") or "timed out"
        rows += [{"i": i, "ok": False, "error": why} for i in range(len(inputs)) if i not in done]
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
            # Interrupted mid-batch: charge every slot as busy since the start
            # (capped at the reservation), never zero.
            used = progress.get("core_s", 0.0) + progress.get("slots", 0) * progress.get("cores_per_slot", 0) \
                * (time.time() - t0)
            # Remote chunks that could not be confirmed stopped may run on up
            # to the call's allowance: charge all of it.
            used = allowance_s if progress.get("remote_unconfirmed") else min(allowance_s, used)
            with _LedgerLock(run_dir) as led:
                led["reservations"].pop(rid, None)
                led["core_seconds"] = round(led["core_seconds"] + used, 1)
                led["calls"].append({"fn": fn_spec, "backend": backend, "task_core_s": round(used, 1),
                                     "stopped": f"interrupted: {type(e).__name__}", "ts": time.strftime("%FT%T")})
        raise
    summary = {**{k: v for k, v in info.items() if k not in ("slots", "cores_per_slot")},
               "n": len(rows), "n_ok": sum(r["ok"] for r in rows),
               "n_failed": sum(not r["ok"] for r in rows), "wall_s": round(wall, 1),
               "task_core_s": round(core_s, 1), "estimated_core_s": round(est, 1)}
    if not ledger:
        return rows, summary
    with _LedgerLock(run_dir) as led:
        led["reservations"].pop(rid, None)
        led["core_seconds"] = round(led["core_seconds"] + core_s, 1)
        led["calls"].append({k: summary.get(k) for k in ("backend", "workers", "n", "n_failed", "wall_s",
                                                         "task_core_s", "estimated_core_s", "stopped")}
                            | {"fn": fn_spec, "ts": time.strftime("%FT%T")})
    return rows, summary


def _read_inputs(path: str) -> list:
    txt = open(path).read().strip()
    if txt.startswith("["):
        return json.loads(txt)
    return [json.loads(l) for l in txt.splitlines() if l.strip()]


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _self_test() -> int:
    import shutil
    import subprocess as _sp
    import tempfile
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        os.makedirs(os.path.join(d, "data"))
        open(os.path.join(d, "data", "scale.txt"), "w").write("3\n")
        open(os.path.join(d, "models", "helper.py"), "w").write("def scale():\n    return float(open('data/scale.txt').read())\n")
        open(os.path.join(d, "models", "fn.py"), "w").write(
            "from helper import scale\n"
            "def f(p):\n"
            "    if p.get('boom'):\n        raise ValueError('bad draw')\n"
            "    return {'y': p['x'] * scale()}\n")
        inputs = [{"x": k} for k in range(40)] + [{"x": 0, "boom": True}]
        for workers in (1, 3):
            rows, s = map_fn(d, "models/fn.py::f", inputs, backend="local", max_workers=workers)
            ok([r["i"] for r in rows] == list(range(41)), f"C1 order kept (workers={workers})")
            ok(rows[5]["ok"] and rows[5]["result"] == {"y": 15.0}, f"C2 relative paths + sibling import (workers={workers}): {rows[5]}")
            ok(not rows[40]["ok"] and "bad draw" in rows[40]["error"] and s["n_failed"] == 1,
               f"C3 a failing row fails alone (workers={workers}): {rows[40]}")
        led = json.load(open(os.path.join(d, LEDGER_REL)))
        ok(len(led["calls"]) == 2, f"C4 ledger records each call: {led}")
        os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = "0"
        try:
            map_fn(d, "models/fn.py::f", inputs[:2], backend="local")
            ok(False, "C5 core-hour cap must refuse")
        except RuntimeError:
            pass
        finally:
            del os.environ["MA_COMPUTE_MAX_CORE_HOURS"]
        try:
            map_fn(d, "models/fn.py::f", inputs, backend="local", max_tasks=10)
            ok(False, "C6 max-tasks must refuse")
        except ValueError:
            pass
        os.symlink(os.path.join(d, "data", "scale.txt"), os.path.join(d, "data", "link.txt"))
        b, info = make_bundle(d)
        lk = [m for m in tarfile.open(fileobj=io.BytesIO(b)).getmembers() if m.name.endswith("data/link.txt")]
        ok(lk and lk[0].isfile(), "C7b a symlinked input is bundled as a file")
        os.remove(os.path.join(d, "data", "link.txt"))
        names = tarfile.open(fileobj=io.BytesIO(b)).getnames()
        ok(any(n.endswith("models/fn.py") and not n.startswith("/") for n in names)
           and any(n.endswith("scripts/compute.py") for n in names), f"C7 bundle has models/ and scripts/: {info}")
        open(os.path.join(d, "models", "slow.py"), "w").write(
            "import os, time\n"
            "def sleep(p):\n    time.sleep(p['s']); return {'ok': 1}\n"
            "def burn(p):\n    t = time.time()\n    while time.time() - t < p['s']: pass\n    return {'ok': 1}\n"
            "def die(p):\n    os._exit(3)\n")
        t0 = time.time()
        rows, s = map_fn(d, "models/fn.py::f", [], backend="local")
        ok(rows == [] and s["n"] == 0, f"C10 an empty batch is a no-op: {s}")
        rows, s = map_fn(d, "models/slow.py::sleep", [{"s": 5}] * 4, backend="local", max_workers=2, timeout_s=1.0)
        ok(time.time() - t0 < 4.5 and all(r["error"] == "timed out" for r in rows) and s["stopped"] == "timed out",
           f"C10 local timeout bounds the call ({time.time() - t0:.1f}s): {s}")
        t0 = time.time()
        rows, s = map_fn(d, "models/slow.py::sleep", [{"s": 3}] * 2, backend="local", max_workers=2, timeout_s=1e-6)
        ok(time.time() - t0 < 2.5 and all(not r["ok"] for r in rows),
           f"C10b a stop during pool startup does not wait for the model ({time.time() - t0:.2f}s)")
        cwd = os.getcwd()
        try:
            os.chdir(os.path.dirname(d))
            rows, _ = map_fn(os.path.basename(d), "models/fn.py::f", [{"x": 2}], backend="local")
            ok(rows[0].get("result") == {"y": 6.0} and os.path.realpath(os.getcwd()) == os.path.realpath(os.path.dirname(d)),
               f"C11 relative run_dir resolves; caller cwd unchanged: {rows[0]}")
        finally:
            os.chdir(cwd)
        ys = []
        for v in (1, 2):
            rd = os.path.join(d, f"iso{v}")
            os.makedirs(os.path.join(rd, "models"))
            open(os.path.join(rd, "models", "helper.py"), "w").write(f"V = {v}\n")
            open(os.path.join(rd, "models", "g.py"), "w").write("import helper\ndef g(p):\n    return helper.V\n")
            ys.append(map_fn(rd, "models/g.py::g", [{}], backend="local", max_workers=1)[0][0].get("result"))
        ok(ys == [1, 2], f"C12 each run gets its own sibling modules: {ys}")
        import threading
        rd = os.path.join(d, "led"); os.makedirs(os.path.join(rd, "models"))
        open(os.path.join(rd, "models", "slow.py"), "w").write(open(os.path.join(d, "models", "slow.py")).read())
        out = {}

        def call(tag, **kw):
            try:
                out[tag] = map_fn(rd, "models/slow.py::sleep", [{"s": 1.0}], backend="local", **kw)[1]
            except RuntimeError as e:
                out[tag] = str(e)
        os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = "1"
        try:
            ta = threading.Thread(target=call, args=("a",)); ta.start(); time.sleep(0.4)
            call("b"); ta.join()
            ok(isinstance(out["a"], dict) and "held by running calls" in str(out["b"]),
               f"C13 a running call's reservation blocks a concurrent call: {out}")
            ts = [threading.Thread(target=call, args=(k,), kwargs={"max_core_hours": 0.1}) for k in ("c", "e")]
            [t.start() for t in ts]; [t.join() for t in ts]
            led = json.load(open(os.path.join(rd, LEDGER_REL)))
            ok(all(isinstance(out[k], dict) for k in "ce") and len(led["calls"]) == 3 and not led["reservations"]
               and led["core_seconds"] >= 2.9, f"C13 concurrent calls both charged, no lost update: {led}")
            os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = str(1.5 / 3600)
            os.remove(os.path.join(rd, LEDGER_REL))
            t0 = time.time()
            rows, s = map_fn(rd, "models/slow.py::burn", [{"s": 0.4}] * 30, backend="local", max_workers=1)
            ok(s["stopped"] == CAP_STOP and s["n_ok"] < 30 and time.time() - t0 < 6
               and any(r.get("error") == CAP_STOP for r in rows), f"C14 the cap stops a batch midway: {s}")
            os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = "200"
            rows, s = map_fn(rd, "models/slow.py::die", [{}] * 2, backend="local", max_workers=1)
            ok(s["n_failed"] == 2 and s["estimated_core_s"] > 0 and s["task_core_s"] > 0,
               f"C15 a crashed chunk is still charged: {s}")
        finally:
            del os.environ["MA_COMPUTE_MAX_CORE_HOURS"]
        bad = os.path.join(d, "badled"); os.makedirs(os.path.join(bad, "models"))
        open(os.path.join(bad, LEDGER_REL), "w").write('{"core_seconds": 7')
        try:
            map_fn(bad, "models/fn.py::f", [{"x": 1}], backend="local")
            ok(False, "C15b a truncated ledger must refuse, not reset the budget")
        except RuntimeError:
            pass
        vd = os.path.realpath(os.path.join(d, "ver")); os.makedirs(os.path.join(vd, "models"))
        vfile, hfile = os.path.join(vd, "models", "v.py"), os.path.join(vd, "models", "oldhelper.py")

        def stage(body, helper):
            # same length, same mtime: only a fresh compile tells A from B
            open(vfile, "w").write(body); os.utime(vfile, (1_700_000_000, 1_700_000_000))
            if helper:
                open(hfile, "w").write("X = 73\n")
            elif os.path.exists(hfile):
                os.remove(hfile)
            return make_bundle(vd)
        ba, ia = stage("def v(p):\n    return 'A'\n", True)
        bb, ib = stage("def v(p):\n    return 'B'\n", False)
        bc, ic = stage("def v(p):\n    import oldhelper\n    return oldhelper.X\n", False)

        def remote(b_, i_):
            return _remote_task(b_, i_["sha256"], vd, "models/v.py::v", [(0, {})], procs=1)
        got = [remote(b_, i_)["rows"][0].get("result") for b_, i_ in ((ba, ia), (bb, ib), (ba, ia), (bb, ib))]
        ok(got == ["A", "B", "A", "B"], f"C16 a chunk runs its own bundle's code (same size+mtime): {got}")
        remote(ba, ia)
        r = remote(bc, ic)["rows"][0]
        ok(not r["ok"] and "oldhelper" in r.get("error", ""),
           f"C16b a file absent from the bundle is gone, not left over from an earlier one: {r}")
        open(vfile, "w").write("import os\ndef v(p):\n    os._exit(3)\n")
        bd, id_ = make_bundle(vd)
        r = remote(bd, id_)
        ok(not r["rows"][0]["ok"] and r["core_s"] > 0, f"C21 a remote chunk whose process crashed is charged: {r}")
        open(os.path.join(d, "models", "stubborn.py"), "w").write(
            "import signal, time\ndef f(p):\n    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    t = time.time()\n    while time.time() - t < 60: pass\n")
        t0 = time.time()
        rows, s = map_fn(d, "models/stubborn.py::f", [{}] * 2, backend="local", max_workers=2, timeout_s=1.0)
        ok(time.time() - t0 < 10 and all(r["error"] == "timed out" for r in rows),
           f"C18 a model ignoring SIGTERM still stops at the timeout ({time.time() - t0:.1f}s)")
        rd2 = os.path.join(d, "abort"); os.makedirs(os.path.join(rd2, "models"))
        real = globals()["_map_local"]

        def boom(*a, progress=None, **k):
            progress.update(slots=2, cores_per_slot=1, core_s=1.0)
            time.sleep(0.5)
            raise KeyboardInterrupt
        globals()["_map_local"] = boom
        try:
            map_fn(rd2, "models/fn.py::f", [{"x": 1}], backend="local")
            ok(False, "C19 interrupt must propagate")
        except KeyboardInterrupt:
            pass
        finally:
            globals()["_map_local"] = real
        led = json.load(open(os.path.join(rd2, LEDGER_REL)))
        ok(led["core_seconds"] >= 1.9 and not led["reservations"] and "interrupted" in led["calls"][-1]["stopped"],
           f"C19 an interrupted call is charged an upper bound, not zero: {led}")
        import subprocess as _sp
        dead = _sp.Popen([sys.executable, "-c", "pass"]); dead.wait()
        led["reservations"] = {"x": {"core_s": 500.0, "pid": dead.pid, "host": os.uname().nodename, "fn": "f"}}
        json.dump(led, open(os.path.join(rd2, LEDGER_REL), "w"))
        before = led["core_seconds"]
        map_fn(rd2, "models/fn.py::f", [], backend="local")
        led = json.load(open(os.path.join(rd2, LEDGER_REL)))
        ok(led["core_seconds"] >= before + 500 and not led["reservations"],
           f"C20 a dead caller's reservation is charged, not dropped: {led['core_seconds']} vs {before}")
        class _Sec:
            tls_ca_file = os.path.join(d, "w_ca.pem"); tls_client_cert = None; tls_client_key = None
            tls_worker_cert = os.path.join(d, "w_cert.pem"); tls_worker_key = os.path.join(d, "w_key.pem")
        class _W:
            security = _Sec()
            class scheduler:
                address = "tls://10.0.0.1:8786"
        for fn_ in ("w_ca.pem", "w_cert.pem", "w_key.pem"):
            open(os.path.join(d, fn_), "w").write("-----BEGIN X-----\n" + fn_ + "\n")
        tdir = os.path.join(d, "tls"); os.makedirs(tdir, mode=0o755); os.chmod(tdir, 0o755)
        open(os.path.join(tdir, "key.pem"), "w").write("old"); os.chmod(os.path.join(tdir, "key.pem"), 0o644)
        env = export_worker_env(tdir, worker=_W())
        mode = lambda p: os.stat(p).st_mode & 0o777  # noqa: E731
        ok(env["MA_COMPUTE_TLS_DIR"] == tdir and mode(tdir) == 0o700 and mode(os.path.join(tdir, "key.pem")) == 0o600
           and "w_key.pem" in open(os.path.join(tdir, "key.pem")).read()
           and "w_cert.pem" in open(os.path.join(tdir, "cert.pem")).read(),
           "C17 worker credentials exported 0600 in a 0700 dir, replacing an existing world-readable key")
        t0 = time.time()
        rows, s = map_fn(d, "models/stubborn.py::f", [{}] * 4, backend="local", max_workers=4, timeout_s=1.0)
        ok(time.time() - t0 < 5, f"C22 stubborn processes are killed together, not one by one ({time.time() - t0:.1f}s)")
        open(os.path.join(d, "models", "spawner.py"), "w").write(
            "import subprocess\ndef f(p):\n    return subprocess.Popen(['sleep', '60']).pid\n")
        rows, _ = map_fn(d, "models/spawner.py::f", [{}], backend="local")
        time.sleep(0.3)
        try:
            os.kill(rows[0]["result"], 0)
            alive = not open(f"/proc/{rows[0]['result']}/stat").read().split()[2] == "Z" if os.path.exists("/proc") else \
                _sp.run(["ps", "-o", "stat=", "-p", str(rows[0]["result"])], capture_output=True, text=True).stdout.strip()[:1] not in ("", "Z")
        except ProcessLookupError:
            alive = False
        ok(rows[0]["ok"] and not alive, f"C23 a process the model left running is killed with the batch: {rows[0]}")
        sd = os.path.realpath(os.path.join(d, "stage")); os.makedirs(os.path.join(sd, "models")); os.makedirs(os.path.join(sd, "data"))
        open(os.path.join(sd, "models", "v.py"), "w").write("def v(p):\n    return 1\n")
        open(os.path.join(sd, "data", "source.csv"), "w").write("a\n")
        b1, i1 = make_bundle(sd)
        os.remove(os.path.join(sd, "data", "source.csv"))
        b2, i2 = make_bundle(sd)
        _remote_task(b1, i1["sha256"], sd, "models/v.py::v", [(0, {})], procs=1)
        livedir = os.path.join(d, "live_run"); os.makedirs(os.path.join(livedir, "data"))
        open(os.path.join(livedir, "metadata.json"), "w").write("{}")
        open(os.path.join(livedir, "data", "source.csv"), "w").write("live\n")
        shutil.rmtree(os.path.join(sd, "data")); os.symlink(os.path.join(livedir, "data"), os.path.join(sd, "data"))
        r = _remote_task(b2, i2["sha256"], sd, "models/v.py::v", [(0, {})], procs=1)
        ok(os.path.exists(os.path.join(livedir, "data", "source.csv")),
           f"C24 staging never deletes through a symlink: {r['rows'][0]}")
        bl, il = make_bundle(livedir)   # a bundle whose target dirs run through the symlink
        os.makedirs(os.path.join(sd, "data", "x"), exist_ok=True)
        open(os.path.join(sd, "data", "x", "f.txt"), "w").write("1")
        b3, i3 = make_bundle(sd)
        r = _remote_task(b3, i3["sha256"], sd, "models/v.py::v", [(0, {})], procs=1)
        ok(not r["rows"][0]["ok"] and "symlink" in r["rows"][0]["error"],
           f"C24b staging refuses to write through a symlinked directory: {r['rows'][0]}")
        cdir = os.path.realpath(os.path.join(d, "cap")); os.makedirs(os.path.join(cdir, "models"))
        open(os.path.join(cdir, "models", "slow.py"), "w").write(open(os.path.join(d, "models", "slow.py")).read())
        bc_, ic_ = make_bundle(cdir)
        t0 = time.time()
        r = _remote_task(bc_, ic_["sha256"], cdir, "models/slow.py::burn", [(k, {"s": 0.4}) for k in range(20)],
                         procs=1, chunk_allowance_s=1.0)
        ok(time.time() - t0 < 8 and any(x.get("error") == CAP_STOP for x in r["rows"]) and r["core_s"] < 3,
           f"C25 a remote chunk enforces its allowance share: core_s={r['core_s']}")
        import threading as _th
        import uuid as _uuid
        res, cid = {}, "selftest-" + _uuid.uuid4().hex
        th = _th.Thread(target=lambda: res.update(r=_remote_task(bc_, ic_["sha256"], cdir, "models/slow.py::sleep",
                                                                 [(0, {"s": 60})], procs=1, call_id=cid)))
        th.start(); time.sleep(1.5)
        t0 = time.time(); n_stopped = _cancel_call(cid); th.join(15)
        later = _remote_task(bc_, ic_["sha256"], cdir, "models/slow.py::sleep", [(0, {"s": 60})], procs=1, call_id=cid)
        ok(n_stopped == 1 and not th.is_alive() and not res["r"]["rows"][0]["ok"]
           and later["rows"][0]["error"] == "cancelled", f"C26 cancel stops running and queued chunks: {n_stopped}, {res}")
        open(os.path.join(d, "models", "heavy.py"), "w").write("import time\ntime.sleep(1.0)\ndef f(p):\n    return 1\n")
        rows, s = map_fn(d, "models/heavy.py::f", [{}] * 2, backend="local", max_workers=1)
        ok(rows[0]["ok"] and s["task_core_s"] >= 0.9, f"C28 model load time is charged: {s}")
        rows, s = map_fn(d, "models/heavy.py::f", [{}] * 3, backend="local", max_workers=3)
        ok(s["task_core_s"] >= 2.7, f"C28b every process's model load is charged, chunk or not: {s}")
        ev = os.path.realpath(os.path.join(d, "envf")); os.makedirs(os.path.join(ev, "models"))
        open(os.path.join(ev, "models", "e.py"), "w").write(
            "import os\ndef f(p):\n    return [os.environ.get(k) for k in ('AZURE_SELFTEST', 'ANTHROPIC_API_KEY', 'OK_VAR')]\n")
        be_, ie_ = make_bundle(ev)
        saved = {k: os.environ.get(k) for k in ("AZURE_SELFTEST", "ANTHROPIC_API_KEY", "OK_VAR")}
        os.environ.update(AZURE_SELFTEST="secret-a", ANTHROPIC_API_KEY="secret-b", OK_VAR="kept")
        try:
            r = _remote_task(be_, ie_["sha256"], ev, "models/e.py::f", [(0, {})], procs=1)["rows"][0]
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        ok(r.get("result") == [None, None, "kept"], f"C32 model code on a worker gets no credentials: {r}")
        dl = os.path.join(d, "dlink"); os.makedirs(os.path.join(dl, "models"))
        ext = os.path.join(d, "ext_data"); os.makedirs(ext); open(os.path.join(ext, "t.csv"), "w").write("1\n")
        os.symlink(ext, os.path.join(dl, "data"))
        os.symlink(os.path.join(dl, "data"), os.path.join(ext, "loop"))      # a cycle
        bd_, idd = make_bundle(dl)
        names_ = tarfile.open(fileobj=io.BytesIO(bd_)).getnames()
        ok(any(n.endswith("dlink/data/t.csv") for n in names_), f"C7c a symlinked data dir is bundled (cycle-safe): {idd}")
        al = os.path.join(d, "alias"); os.makedirs(os.path.join(al, "data", "real")); os.makedirs(os.path.join(al, "models"))
        open(os.path.join(al, "data", "real", "value.txt"), "w").write("73\n")
        os.symlink(os.path.join(al, "data", "real"), os.path.join(al, "data", "a_alias"))
        na = tarfile.open(fileobj=io.BytesIO(make_bundle(al)[0])).getnames()
        ok(any(n.endswith("data/real/value.txt") for n in na) and any(n.endswith("data/a_alias/value.txt") for n in na),
           f"C7d both names of an aliased dir are bundled: {[n for n in na if 'value' in n]}")
        ld = os.path.realpath(os.path.join(d, "leaf")); os.makedirs(os.path.join(ld, "models")); os.makedirs(os.path.join(ld, "data"))
        open(os.path.join(ld, "models", "v.py"), "w").write("def v(p):\n    return 1\n")
        open(os.path.join(ld, "data", "input.csv"), "w").write("bundle\n")
        bl_, il_ = make_bundle(ld)
        victim = os.path.join(d, "victim_run"); os.makedirs(os.path.join(victim, "data"))
        open(os.path.join(victim, "metadata.json"), "w").write("{}")
        open(os.path.join(victim, "data", "input.csv"), "w").write("live\n")
        os.remove(os.path.join(ld, "data", "input.csv"))
        os.symlink(os.path.join(victim, "data", "input.csv"), os.path.join(ld, "data", "input.csv"))
        _remote_task(bl_, il_["sha256"], ld, "models/v.py::v", [(0, {})], procs=1)
        ok(open(os.path.join(victim, "data", "input.csv")).read() == "live\n"
           and not os.path.islink(os.path.join(ld, "data", "input.csv")),
           "C30 staging never writes through a symlinked file")
        open(os.path.join(d, "models", "envq.py"), "w").write(
            "import os\ndef f(p):\n    return [os.environ.get('MA_COMPUTE_GATE'), os.environ.get('MA_COMPUTE_CANCEL')]\n")
        ed = os.path.join(d, "envgate"); os.makedirs(ed)
        inp = os.path.join(ed, "in.json"); json.dump([{}], open(inp, "w")); out = os.path.join(ed, "out.jsonl")
        pe = _sp.Popen([sys.executable, os.path.abspath(__file__), "map", d, "--fn", "models/envq.py::f", "--inputs", inp,
                        "--out", out, "--backend", "local", "--no-ledger"],
                       env=dict(os.environ, MA_COMPUTE_GATE=os.path.join(ed, "{pid}"), MA_COMPUTE_CANCEL=os.path.join(ed, "cancel")))
        open(os.path.join(ed, str(pe.pid)), "w").write("1")
        pe.wait(60)
        got = json.loads(open(out).readline())["result"] if os.path.exists(out) else "no output"
        ok(got == [None, None], f"C31 the registration gate is not inherited by the model: {got}")
        gdir = os.path.join(d, "gate"); os.makedirs(gdir)
        open(os.path.join(gdir, "cancel"), "w").write("1")
        inp = os.path.join(gdir, "in.json"); json.dump([{"x": 1}], open(inp, "w"))
        out = os.path.join(gdir, "out.jsonl")
        envg = dict(os.environ, MA_COMPUTE_GATE=os.path.join(gdir, "{pid}"), MA_COMPUTE_CANCEL=os.path.join(gdir, "cancel"))
        pg = _sp.Popen([sys.executable, os.path.abspath(__file__), "map", d, "--fn", "models/fn.py::f", "--inputs", inp,
                        "--out", out, "--backend", "local", "--no-ledger"], env=envg)
        time.sleep(0.5)
        ok(pg.poll() is None and not os.path.exists(out), "C29 a chunk does no work before it is registered")
        open(os.path.join(gdir, str(pg.pid)), "w").write(str(pg.pid))
        ok(pg.wait(30) == 3 and not os.path.exists(out), "C29 a chunk registered after a cancel exits without working")
        # C8: dask backend against a local cluster, when distributed is installed
        try:
            from distributed import LocalCluster
        except ImportError:
            print("  (C8 skipped: distributed not installed)")
        else:
            with LocalCluster(n_workers=2, threads_per_worker=1, processes=True, dashboard_address=None) as lc:
                os.environ["MA_COMPUTE_SCHEDULER"] = lc.scheduler_address
                try:
                    rows, s = map_fn(d, "models/fn.py::f", inputs)
                    ok(s["backend"] == "dask" and rows[7]["result"] == {"y": 21.0} and s["n_failed"] == 1
                       and [r["i"] for r in rows] == list(range(41)), f"C8 dask backend: {s}")
                    w0 = list(lc.scheduler_info["workers"])[0]
                    os.environ["MA_COMPUTE_EXCLUDE_WORKERS"] = w0
                    rows, s = map_fn(d, "models/fn.py::f", inputs[:8])
                    used = {r.get("worker") for r in rows}
                    ok(s["workers"] == 1 and all(r["ok"] for r in rows) and w0 not in used and None not in used,
                       f"C9 no task ran on the excluded run worker: {used}")
                    os.environ["MA_COMPUTE_PROCS_PER_WORKER"] = "1"
                    rows, s = map_fn(d, "models/slow.py::sleep", [{"s": 1.5}] * 4, max_core_hours=2.0 / 3600)
                    # Not "exactly 1 done": model load time is charged too (Phase 29), so
                    # timing varies; the property is that the batch stops at the cap.
                    ok(s["n_ok"] < 4 and s["task_core_s"] <= 2.6 and s["stopped"] == CAP_STOP,
                       f"C27 later chunks get only what is left of the allowance: {s}")
                    # C35 (Phase 30): a timed-out batch is charged what actually ran, not the pool.
                    os.environ["MA_COMPUTE_PROCS_PER_WORKER"] = "1"
                    os.environ["MA_COMPUTE_EXCLUDE_WORKERS"] = w0
                    t0c = time.time()
                    rows3, s3 = map_fn(d, "models/slow.py::sleep", [{"s": 1.0}] * 40, timeout_s=2.5, ledger=False)
                    wall3 = time.time() - t0c
                    ok(s3["stopped"] == "timed out" and s3["estimated_core_s"] == 0
                       and s3["task_core_s"] <= 1 * wall3 + 1.0 and sum(r["ok"] for r in rows3) < 40,
                       f"C35 stopped chunks report their real use, queued ones nothing (no whole-pool estimate): {s3} wall {wall3:.1f}")
                    os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
                    # C34 (Phase 30): autoscaling pool. Untagged run worker; a worker
                    # added mid-batch must get work through the resource tag.
                    os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
                    from distributed import Client as _C2
                    import threading as _th2
                    with _C2(lc.scheduler_address) as _cc:
                        _cc.register_plugin(ComputeTagPlugin("compute", exclude=[w0]))
                    os.environ.update(MA_COMPUTE_RESOURCE="compute", MA_COMPUTE_TARGET_WORKERS="3",
                                      MA_COMPUTE_PROCS_PER_WORKER="1")
                    open(os.path.join(d, "models", "slowf.py"), "w").write(
                        "import time\ndef f(p):\n    time.sleep(0.4)\n    return p['x']\n")
                    box = {}
                    th = _th2.Thread(target=lambda: box.update(r=map_fn(d, "models/slowf.py::f",
                                                                         [{"x": k} for k in range(60)], ledger=False)))
                    th.start()
                    time.sleep(2.0)
                    lc.scale(3)                       # a new worker joins mid-batch
                    th.join(120)
                    rows2, s2 = box.get("r", ([], {}))
                    used2 = {r.get("worker") for r in rows2}
                    new_ws = set(lc.scheduler_info["workers"]) - {w0} - set(list(lc.scheduler_info["workers"])[:2])
                    ok(len(rows2) == 60 and all(r["ok"] for r in rows2) and w0 not in used2,
                       f"C34 tagged pool: every task done, none on the untagged run worker: {used2} {s2}")
                    ok(len(used2) >= 2 and s2.get("pool_target") == 3,
                       f"C34 a worker that joined mid-batch got work (pool target 3): {used2} {s2.get('pool_target')}")
                    for k in ("MA_COMPUTE_RESOURCE", "MA_COMPUTE_TARGET_WORKERS"):
                        os.environ.pop(k, None)
                finally:
                    os.environ.pop("MA_COMPUTE_SCHEDULER", None)
                    os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
                    os.environ.pop("MA_COMPUTE_PROCS_PER_WORKER", None)
        # C33: a scheduler and worker started outside this repo cannot import
        # this module (as on Coiled); the task must still run.
        try:
            import distributed  # noqa: F401
        except ImportError:
            pass
        else:
            iso = tempfile.mkdtemp(prefix="ma_iso_")
            sf = os.path.join(iso, "sched.json")
            envi = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH",)}
            dask_cli = os.path.join(os.path.dirname(sys.executable), "dask")
            procs_ = [_sp.Popen([dask_cli, "scheduler", "--scheduler-file", sf,
                                 "--host", "127.0.0.1", "--port", "0", "--no-dashboard"], cwd=iso, env=envi,
                                stdout=_sp.DEVNULL, stderr=open(os.path.join(iso, "sched.err"), "w"))]
            try:
                end = time.time() + 60
                while not os.path.exists(sf) and time.time() < end:
                    time.sleep(0.2)
                time.sleep(1.0)       # the file is written just before the scheduler listens
                procs_.append(_sp.Popen([dask_cli, "worker", "--scheduler-file", sf,
                                         "--nthreads", "1", "--no-dashboard"], cwd=iso, env=envi,
                                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
                os.environ["MA_COMPUTE_SCHEDULER"] = json.load(open(sf))["address"]
                from distributed import Client as _C
                with _C(os.environ["MA_COMPUTE_SCHEDULER"]) as cc:
                    cc.wait_for_workers(1, timeout=60)
                # Imported as a module, as propagate_uncertainty.py does (run as
                # __main__, dask would ship the functions by value and hide this).
                code = ("import json, sys; sys.path.insert(0, %r); import compute; "
                        "rows, s = compute.map_fn(%r, 'models/fn.py::f', [{'x': 0}, {'x': 1}, {'x': 2}], ledger=False); "
                        "print(json.dumps([r.get('result') or r.get('error') for r in rows]))") % (HERE, d)
                code_tag = ("import json, os, sys; sys.path.insert(0, %r); import compute; "
                            "from distributed import Client; "
                            "c = Client(os.environ['MA_COMPUTE_SCHEDULER']); compute.register_compute_tags(c, exclude=[]); "
                            "os.environ.update(MA_COMPUTE_RESOURCE='compute', MA_COMPUTE_TARGET_WORKERS='1'); "
                            "rows, s = compute.map_fn(%r, 'models/fn.py::f', [{'x': 0}, {'x': 1}], ledger=False); "
                            "print(json.dumps([r.get('result') or r.get('error') for r in rows]))") % (HERE, d)
                # C33b first, on a worker that has never received this module
                r2 = _sp.run([sys.executable, "-c", code_tag], capture_output=True, text=True, timeout=300,
                             env=dict(os.environ, PYTHONPATH=""))
                got2 = r2.stdout.strip().splitlines()[-1] if r2.stdout.strip() else r2.stderr[-400:]
                ok(got2 == json.dumps([{"y": 0.0}, {"y": 3.0}]),
                   f"C33b the compute-tag plugin works on a cluster that cannot import this module: {got2}")
                r = _sp.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=300,
                            env=dict(os.environ, PYTHONPATH=""))
                got = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-500:]
                ok(got == json.dumps([{"y": 0.0}, {"y": 3.0}, {"y": 6.0}]),
                   f"C33 runs on a cluster that cannot import this module: {got}")

            except Exception:
                print(open(os.path.join(iso, "sched.err")).read()[-3000:])
                raise
            finally:
                os.environ.pop("MA_COMPUTE_SCHEDULER", None)
                for pr in procs_:
                    pr.terminate()
                for pr in procs_:
                    pr.wait(10)
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a model function over many parameter sets")
    ap.add_argument("cmd", nargs="?", choices=["map"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--fn")
    ap.add_argument("--inputs")
    ap.add_argument("--out")
    ap.add_argument("--backend", default="auto", choices=["auto", "local", "dask"])
    ap.add_argument("--max-workers", type=int)
    ap.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    ap.add_argument("--timeout-s", type=float)
    ap.add_argument("--max-core-hours", type=float,
                    help="this call's allowance (default MA_COMPUTE_CALL_CORE_HOURS=50, within the run cap)")
    ap.add_argument("--no-ledger", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
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
        ap.error("usage: compute.py map <run_dir> --fn <path.py>::<fn> --inputs <jsonl> --out <jsonl>")
    rows, summary = map_fn(a.run_dir, a.fn, _read_inputs(a.inputs), a.backend, a.max_workers,
                           a.max_tasks, a.timeout_s, ledger=not a.no_ledger, max_core_hours=a.max_core_hours)
    with open(a.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    json.dump(summary, open(a.out + ".summary.json", "w"), indent=1)
    print(json.dumps({k: v for k, v in summary.items() if k != "bundle"}))
    return 0 if summary["n_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
