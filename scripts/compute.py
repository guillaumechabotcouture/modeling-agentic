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
is used; --timeout-s stops it at a wall-clock limit. A function that raises fails only its
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


def _local_init(run_dir: str, fn_spec: str):
    global _LOCAL_FN
    os.chdir(run_dir)
    _LOCAL_FN = load_fn(run_dir, fn_spec)


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


def _kill_pool(ex):
    """SIGTERM the pool's processes, then SIGKILL any that ignore it, so
    shutdown() cannot hang on a model that traps signals."""
    procs = list((getattr(ex, "_processes", None) or {}).values())
    for proc in procs:
        proc.terminate()
    for proc in procs:
        proc.join(2)
        if proc.is_alive():
            proc.kill()
            proc.join(2)


def _map_local(run_dir, fn_spec, inputs, max_workers, deadline=None, allowance_s=None, progress=None):
    """Always a fresh spawn pool, even for one worker: the model's sibling
    modules and its chdir never leak into the caller or across runs."""
    import multiprocessing as mp
    progress = {} if progress is None else progress
    n = max(1, min(max_workers or max(1, (os.cpu_count() or 2) - 1), len(inputs) or 1))
    progress.update(slots=n, cores_per_slot=1, core_s=0.0)
    chunks = _chunks(list(enumerate(inputs)), n * 8)
    deadline, why = _budget_deadline(deadline, allowance_s, n)
    ex = cf.ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn"),
                                initializer=_local_init, initargs=(run_dir, fn_spec))
    futs = {ex.submit(_local_chunk, ch): ch for ch in chunks}
    pending, rows, stop, lost = set(futs), [], None, 0
    try:
        while pending:
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
        if pending:
            _kill_pool(ex)
        ex.shutdown(wait=True, cancel_futures=True)
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
            for dp, dirs, files in os.walk(root):
                dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "_truth")]
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


def _remote_task(bundle: bytes, sha: str, run_dir: str, fn_spec: str, chunk: list,
                 deadline: float | None = None, procs: int | None = None):
    """Runs on a dask worker. Under a machine-wide lock: make sure the files on
    disk are exactly THIS bundle (another call's bundle may have replaced them:
    its extra files are removed and no stale bytecode survives), then evaluate
    the chunk. The live run directory (has metadata.json) is never touched:
    there the model runs against the run's own files. Returns the rows and the
    core-seconds the chunk used, including work in crashed processes."""
    import fcntl, io, json, os, shutil, signal, subprocess, sys, tarfile, tempfile, time  # noqa: E401 — pickled by value
    try:
        from distributed import get_worker
        waddr = get_worker().address
    except Exception:  # noqa: BLE001 — also called outside a worker (self-test)
        waddr = None
    # One machine-wide lock: one chunk at a time per VM (each chunk already uses
    # its cores), and no bundle is replaced while a chunk is reading it.
    lock = os.path.join(tempfile.gettempdir(), "ma_compute.lock")
    procs = procs or int(os.environ.get("MA_COMPUTE_PROCS_PER_WORKER") or max(1, (os.cpu_count() or 2) - 1))
    t0 = time.time()

    def failed(msg):
        return {"rows": [{"i": i, "ok": False, "error": msg[:500], "worker": waddr} for i, _ in chunk],
                "core_s": round((time.time() - t0) * procs, 3)}

    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        live = os.path.exists(os.path.join(run_dir, "metadata.json"))
        current = os.path.join(run_dir, ".ma_compute_bundle")
        try:
            prev = json.load(open(current))
        except (OSError, ValueError):
            prev = {}
        if not live and prev.get("sha") != sha:
            tf = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
            members = [m.name for m in tf.getmembers() if m.isfile()]
            for old in set(prev.get("members") or []) - set(members):
                try:
                    os.unlink("/" + old)
                except OSError:
                    pass
            for d in {os.path.dirname("/" + m) for m in members}:
                shutil.rmtree(os.path.join(d, "__pycache__"), ignore_errors=True)
            tf.extractall("/", filter="data")
            os.makedirs(run_dir, exist_ok=True)
            json.dump({"sha": sha, "members": members}, open(current, "w"))
        # dask workers are daemonic (no multiprocessing children), so the chunk
        # runs in a subprocess using this module's local backend across the VM's
        # cores. MA_COMPUTE_PROCS_PER_WORKER caps processes for memory-heavy models.
        with tempfile.TemporaryDirectory() as td:
            inp, out = os.path.join(td, "in.json"), os.path.join(td, "out.jsonl")
            json.dump([p for _, p in chunk], open(inp, "w"))
            script = os.path.join(REPO, "scripts", "compute.py")
            env = {k: v for k, v in os.environ.items() if not k.startswith("MA_COMPUTE_SCHEDULER")}
            env["PYTHONDONTWRITEBYTECODE"] = "1"     # bytecode never outlives its bundle
            cmd = [sys.executable, script, "map", run_dir, "--fn", fn_spec, "--inputs", inp,
                   "--out", out, "--backend", "local", "--max-workers", str(procs), "--no-ledger"]
            left = None
            if deadline is not None:
                left = deadline - time.time()
                if left <= 0:
                    return {"rows": [{"i": i, "ok": False, "error": "timed out", "worker": waddr} for i, _ in chunk],
                            "core_s": 0.0}
                cmd += ["--timeout-s", f"{left:.1f}"]
            # Own process group: on a hard timeout the whole tree (the chunk's
            # pool and anything the model spawned) is killed.
            proc = subprocess.Popen(cmd, cwd=run_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, start_new_session=True)
            try:
                _, err = proc.communicate(timeout=None if left is None else left + 60)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                return failed("timed out (chunk subprocess killed)")
            if not os.path.exists(out):
                return failed(f"chunk subprocess failed (rc {proc.returncode}): {(err or '')[-800:]}")
            rows = [json.loads(l) for l in open(out) if l.strip()]
            try:
                core_s = float(json.load(open(out + ".summary.json"))["task_core_s"])
            except (OSError, ValueError, KeyError, TypeError):
                core_s = (time.time() - t0) * procs    # no summary: charge the chunk as fully busy
    return {"rows": [{**row, "i": chunk[k][0], "worker": waddr} for k, row in enumerate(rows)], "core_s": core_s}


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
    import cloudpickle
    from distributed import Client, wait
    cloudpickle.register_pickle_by_value(sys.modules[__name__])
    addr = os.environ["MA_COMPUTE_SCHEDULER"]
    exclude = {w.strip() for w in os.environ.get("MA_COMPUTE_EXCLUDE_WORKERS", "").split(",") if w.strip()}
    bundle, binfo = make_bundle(run_dir)
    with Client(addr, timeout="60s", security=_security()) as c:
        winfo = c.scheduler_info()["workers"]
        workers = [w for w in winfo if w not in exclude]
        if max_workers:
            workers = workers[:max_workers]
        if not workers:
            raise RuntimeError("no compute workers available (all are excluded run workers)")
        env_procs = os.environ.get("MA_COMPUTE_PROCS_PER_WORKER")
        cpus = c.run(os.cpu_count, workers=workers)
        procs = {w: int(env_procs) if env_procs else (cpus.get(w) or 1) for w in workers}   # compute VMs run nothing else
        slots = len(workers)          # _remote_task runs one chunk at a time per VM
        cores_per_slot = max(procs.values())
        progress = {} if progress is None else progress
        progress.update(slots=slots, cores_per_slot=cores_per_slot, core_s=0.0)
        deadline, why = _budget_deadline(deadline, allowance_s, slots * cores_per_slot)
        bfut = c.scatter(bundle, workers=workers, broadcast=True)
        chunks = _chunks(list(enumerate(inputs)), len(workers) * chunks_per_worker)
        def submit(ch):
            # No dask retries: a failed attempt's work would go uncharged. A lost
            # chunk is resubmitted once here, and the lost attempt is charged.
            return c.submit(_remote_task, bfut, binfo["sha256"], run_dir, fn_spec, ch, deadline, cores_per_slot,
                            workers=workers, allow_other_workers=False, pure=False)
        futs = {submit(ch): (ch, 0) for ch in chunks}
        pending, rows, stop, lost = set(futs), [], None, 0
        while pending:
            left = None if deadline is None else max(0.0, deadline - time.time())
            try:
                done, pending = wait(pending, timeout=left, return_when="FIRST_COMPLETED")
            except TimeoutError:
                stop = why
                break
            for f in done:
                ch, attempt = futs[f]
                try:
                    got = f.result()
                    rows += got["rows"]
                    progress["core_s"] += float(got["core_s"])
                except Exception as e:  # noqa: BLE001 — a lost worker fails only its chunk
                    lost += 1
                    if attempt == 0 and (deadline is None or time.time() < deadline):
                        g = submit(ch)
                        futs[g] = (ch, 1)
                        pending.add(g)
                        continue
                    rows += [{"i": i, "ok": False, "error": f"chunk failed: {type(e).__name__}: {e}"[:500]}
                             for i, _ in ch]
            if pending and allowance_s is not None and progress["core_s"] >= allowance_s:
                stop = CAP_STOP
                break
        for f in pending:
            f.cancel()   # running chunks stop themselves at `deadline` (their subprocess timeout)
    return rows, {"backend": "dask", "workers": len(workers), "slots": slots, "cores_per_slot": cores_per_slot,
                  "core_s": progress["core_s"], "stopped": stop, "unknown_chunks": lost + len(pending), "bundle": binfo}


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
    allowance_s, rid = None, None
    if ledger:
        cap_s = float(os.environ.get("MA_COMPUTE_MAX_CORE_HOURS", "200")) * 3600
        with _LedgerLock(run_dir) as led:
            held = _live_reservations(led)
            remaining = cap_s - led["core_seconds"] - held
            if remaining <= 0:
                raise RuntimeError(
                    f"run compute cap reached: {led['core_seconds'] / 3600:.1f} used"
                    + (f" + {held / 3600:.1f} held by running calls" if held else "")
                    + f" of {cap_s / 3600:g} core-hours")
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
    except BaseException as e:
        if rid:
            # Interrupted mid-batch: charge every slot as busy since the start
            # (capped at the reservation), never zero.
            used = progress.get("core_s", 0.0) + progress.get("slots", 0) * progress.get("cores_per_slot", 0) \
                * (time.time() - t0)
            used = min(allowance_s, used)
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
                finally:
                    os.environ.pop("MA_COMPUTE_SCHEDULER", None)
                    os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
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
