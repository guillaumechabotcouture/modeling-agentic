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
         with the run). Each chunk is pinned to a free compute worker, so VMs
         that join mid-batch get work at once (Phase 45 δ). The run's
         `models/`, `data/` and the repo's `scripts/` and `toolkit/` are bundled once and unpacked on each worker at the SAME absolute
         paths, so model code with absolute paths works unchanged.
  local  otherwise: a process pool on this machine (cores - 1).

Guardrails: --max-tasks (default 20,000) per call; a per-call allowance
(--max-core-hours, default MA_COMPUTE_CALL_CORE_HOURS = 50) and a run-wide
cap (MA_COMPUTE_MAX_CORE_HOURS, default 200), both in core-hours and tracked
in `{run_dir}/models/compute_ledger.json`. A batch stops when its allowance
is used; --timeout-s stops it at a wall-clock limit.

Phase 35, failures handled by the service (no agent troubleshooting):
memory is measured per process and concurrency lowered to fit; a process
death (out of memory) reruns the affected inputs at lower concurrency; a chunk
lost with its VM is resubmitted up to MA_COMPUTE_INFRA_RETRIES (2) times per
input, and the autoscaler never retires a worker that is running one
(`BusyWorkerGuard`); a model error repeating on most rows stops the batch early.
Failed rows carry `kind`: model | infra | stopped. The CLI reuses rows of an
earlier run of the same command (same function, code, data and params), so
rerunning completes a stopped or partly failed batch (`--fresh` disables).
`gate_findings(run_dir)` reports results files with rows that have no result.

Phase 42 α, charging for work done: each ledgered call keeps a progress
record (`{run_dir}/.compute_progress/`: core-seconds accounted for, chunks in
flight with their share and start). A caller that dies without settling (an
agent's tool timeout kills it; a defunct/zombie pid counts as dead) is
charged that work plus a bound on its chunks still in flight, not its whole
reservation; no readable record charges the reservation, and the ledger row
says which. An interrupted call is charged what its stopped chunks reported.
Rows are appended to `<out>.partial.jsonl` as they finish, so rerunning a
killed batch keeps them. `map --detach` runs the batch in its own session
and returns at once; `compute.py status <out>` / `wait <out> --timeout-s S`
follow it (exit 0 finished, 1 finished with failed rows, 3 still running,
4 died or no job).
A recorded pid (pool process, registered chunk) is signalled only while its
current start token equals the saved one; otherwise it is left alone and the
call charged in full. Start tokens come from /proc, else psutil, else `ps` (run
in UTC and the C locale). Known limit: on a machine with neither /proc nor
psutil, tokens saved by a build before this normalisation differ from the new
ones for the same process, so a caller of that build still running across the
upgrade may be taken for dead. Linux VMs read /proc and are unaffected.
A reservation made on another host (a run resumed on a new cluster) is
judged by its record's heartbeat: a call refreshes the record every 30 s
from its own thread, and one not refreshed for 5 min is settled like a dead
local caller, except that nothing is signalled from here: its remote chunks
are bounded by their shares, local work in flight charges the reservation.
A record with no heartbeat (written before it) is settled after 5 min only
if it was kept in another run dir, otherwise after 24 h; no readable record
holds the reservation for 24 h, then charges it in full. Records are kept
relative to the run dir; an older absolute path into another run dir is
found by its `.compute_progress/<name>` suffix in this one.

Known limits: a process the model starts in its own session (setsid,
daemonizing) escapes the kill on a stop. Work redone by dask after a worker
dies is not seen directly; a worker that vanishes during a call is charged as
busy for the whole call instead. Remote chunks the client cannot confirm
stopped are charged the call's whole allowance. A function that raises fails only its
own row. Files over --max-file-mb (default 100) are left out of the bundle
and listed in the summary.

Usage:
    python3 scripts/compute.py map ... [--detach]
    python3 scripts/compute.py status|wait <out> [--timeout-s S]
    python3 scripts/compute.py --self-test
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
from concurrent.futures.process import BrokenProcessPool
import hashlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
# Repo code a run's model may import (Phase 42 ε: toolkit/, e.g. toolkit/alloc).
REPO_CODE_DIRS = ("scripts", "toolkit")
DEFAULT_MAX_TASKS = 20000
RUN_CAP_CORE_HOURS = "200"         # MA_COMPUTE_MAX_CORE_HOURS when unset: the run-wide cap
CORES_PER_WORKER = 8               # MA_COMPUTE_CORES_PER_WORKER when unset and no worker reports its cores
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
        return {"i": i, "ok": False, "kind": MODEL, "error": f"{type(e).__name__}: {e}"[:500],
                "trace": traceback.format_exc()[-1500:], "seconds": round(time.time() - t0, 3)}


# --------------------------------------------------------------------------- #
# Phase 35: failures the service handles itself
# --------------------------------------------------------------------------- #
# E7 attempt 7: a VM running 8 simulations at once ran out of memory; one dead
# process broke the whole process pool, so every simulation queued on that VM
# failed with it (1,544 of 1,920 rows, 2.9 h, 305 core-hours), and nothing
# stopped the batch. Failed rows now carry a kind:
#   model   the model raised, or its process died running that input ALONE
#           (needs more memory than the VM has, or crashes in native code):
#           only the model's author can fix these
#   infra   the machine failed (a pool broken by another process, a lost VM):
#           retried here at lower concurrency, never left to the agent
#   stopped cap, timeout, cancel, or the repeated-error breaker
# Memory is measured per process (peak RSS) and concurrency is lowered so the
# measured peak fits the VM; after a broken pool the in-flight inputs are rerun
# with half as many processes. A batch whose rows keep failing with the SAME
# model error stops early instead of running to the end.
MODEL, INFRA, STOPPED = "model", "infra", "stopped"
TAIL = 2                   # attempt marker: a chunk's breaker-stopped inputs rerun without the chunk breaker
BREAKER_MIN = 8            # same model error at least this often, and on half the finished rows
MEM_HEADROOM = 1.2         # a process may grow 20% past the largest peak seen
MEM_USABLE = 0.85          # share of the memory available at start that the pool may use
# 2026-11-02 (Nigeria v2 full run F3): two chunks lost when the autoscaler retired their workers were
# resubmitted once each, and both resubmissions were lost the same way: 16 rows infra, the analysis failed.
INFRA_RETRIES = 2          # MA_COMPUTE_INFRA_RETRIES when unset: resubmissions per input after its chunk was lost
NO_WORKER_WAIT_S = 1800    # MA_COMPUTE_NO_WORKER_WAIT_S when unset: how long a call waits with no compute worker


def _peak_mb() -> float:
    """Peak resident memory of this process plus its largest finished child, in MB."""
    import resource
    unit = 1 / 2**20 if sys.platform == "darwin" else 1 / 1024     # ru_maxrss: bytes on macOS, KiB on Linux
    return round((resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                  + resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss) * unit, 1)


def _avail_mb() -> float | None:
    """Memory available to new processes now (Linux MemAvailable; elsewhere half of RAM)."""
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) / 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**20 / 2
    except (ValueError, OSError, AttributeError):
        return None


def _mem_slots(peak_mb, avail_mb, cap: int) -> int:
    """Processes that fit: the measured peak (with headroom) into the usable memory, at most `cap`."""
    if not peak_mb or not avail_mb:
        return cap
    return max(1, min(cap, int(MEM_USABLE * avail_mb / (peak_mb * MEM_HEADROOM))))


def _kill_chunk_group_others() -> None:
    """Inside a remote chunk (one process group for the chunk, its pool and
    whatever the models started): kill every member except this process.
    Called between pools, so only what the dead pool left behind is hit."""
    if not os.environ.get("MA_COMPUTE_IN_CHUNK"):
        return                                    # locally each pool process leads its own group
    try:
        import psutil
    except ImportError:
        return
    import signal
    me, grp = os.getpid(), os.getpgrp()
    for proc in psutil.process_iter():
        try:
            if proc.pid != me and os.getpgid(proc.pid) == grp:
                os.kill(proc.pid, signal.SIGKILL)
        except (psutil.Error, OSError):
            pass


def _err_key(row) -> str:
    import re
    return re.sub(r"\d+", "N", str(row.get("error") or ""))[:80]


def _breaker(rows, min_n: int | None = None, prior_ok: int = 0) -> str | None:
    """A stop reason when one model error repeats: at least `min_n` rows and at
    least half of the finished rows (ok or model-failed, plus `prior_ok`
    successes the caller saw elsewhere in the batch). Otherwise None."""
    min_n = int(os.environ.get("MA_COMPUTE_BREAKER_MIN", BREAKER_MIN)) if min_n is None else min_n
    if min_n <= 0:
        return None
    finished = [r for r in rows if r.get("ok") or r.get("kind") == MODEL]
    counts = {}
    for r in finished:
        if not r.get("ok"):
            counts[_err_key(r)] = counts.get(_err_key(r), 0) + 1
    if not counts:
        return None
    key, c = max(counts.items(), key=lambda kv: kv[1])
    if c >= min_n and 2 * c >= len(finished) + prior_ok:
        err = next(r["error"] for r in finished if not r.get("ok") and _err_key(r) == key)
        return f"stopped: the same model error on {c} of {len(finished) + prior_ok} finished rows: {err[:300]}"
    return None


# --------------------------------------------------------------------------- #
# Local backend
# --------------------------------------------------------------------------- #

_LOCAL_FN = None


def _guard_registration():
    """A self-test's import-guard registration in this process (toolkit/import_guard.py: test-only toy engines), for
    the pool's workers; None in production. Read from the module only if this process already imported it (code
    registered it), never from the environment (follow-up to #133)."""
    g = sys.modules.get("toolkit.import_guard")
    return g.worker_registration() if g is not None else None


def _local_init(run_dir: str, fn_spec: str, init_dir: str | None = None, guard_reg=None):
    """Pool initializer. Each process records its model load time in
    `init_dir`, so loads are charged even in processes that get no chunk; a
    load that raises is recorded there too (`<pid>.err`), so a pool that breaks
    at start is reported as the model's load error, not as a crash. `guard_reg`:
    the parent self-test's import-guard registration, applied before the model loads."""
    global _LOCAL_FN
    t0 = time.time()
    if guard_reg:
        if REPO not in sys.path:
            sys.path.insert(0, REPO)
        from toolkit import import_guard
        import_guard.init_worker(guard_reg)
    # Own process group: whatever the model spawns (an external simulator)
    # is killed with this process. A child that starts its own session escapes.
    # Inside a remote chunk the whole chunk is already one group (killed as one).
    if not os.environ.get("MA_COMPUTE_IN_CHUNK"):
        os.setpgrp()
    os.chdir(run_dir)
    try:
        _LOCAL_FN = load_fn(run_dir, fn_spec)
    except BaseException as e:
        if init_dir:
            open(os.path.join(init_dir, f"{os.getpid()}.err"), "w").write(f"{type(e).__name__}: {e}"[:500])
        raise
    finally:
        if init_dir:
            open(os.path.join(init_dir, str(os.getpid())), "w").write(str(time.time() - t0))


def _local_chunk(chunk):
    out = []
    for i, params in chunk:
        row = _call(_LOCAL_FN, i, params)
        row["peak_mb"] = _peak_mb()
        out.append(row)
    return out


def _chunks(items: list, n_chunks: int) -> list:
    size = -(-len(items) // max(1, n_chunks))
    return [items[k:k + size] for k in range(0, len(items), size)] if items else []


CAP_STOP = "stopped: compute cap reached"


# v2 (Nigeria full run, 2026-11-02): scripts/pipeline.py's fixed 1-h limit killed an engine batch at 184 of 256
# design points. A pipeline step that runs the run's batches gets its wall-clock limit from the run cap instead.
STEP_WALL_MARGIN = 2.0              # workers join late, tasks do not pack perfectly, a lost VM reruns its inputs
STEP_WALL_OWN_S = 3600.0            # the step's own work besides the batches (what the fixed limit allowed)
STEP_WALL_CEILING_S = 24 * 3600.0


def step_wall_s(environ=None) -> float | None:
    """Wall-clock limit for a pipeline step that runs the run's engine batches: the time the cluster's target pool
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


def _map_local(run_dir, fn_spec, inputs, max_workers, deadline=None, allowance_s=None, progress=None,
               peak_mb=None, prior_ok=0):
    """Always a fresh spawn pool, even for one worker: the model's sibling
    modules and its chdir never leak into the caller or across runs.

    Phase 35: one input per submission, at most `cap` in flight, so the inputs
    running when a process dies are known. A broken pool is rebuilt with half
    as many processes and those inputs are rerun (not failed). `cap` also falls
    to what the measured peak memory per process (or the `peak_mb` hint)
    allows, and never rises again within the call (the allowance's wall-clock
    stop moves with it). An input whose process dies when the pool has one
    process is retried once; a second death fails it as a model error.

    v2 (spike lesson 7): the pool is also replaced after every
    `recycle_every()` finished inputs (drained, then rebuilt at the same size).
    The spike's pools broke at about 750 runs per VM from worker memory growth,
    and `max_tasks_per_child` deadlocked once every child had been recycled; a
    fresh pool per chunk fixed both."""
    import multiprocessing as mp
    import tempfile
    progress = {} if progress is None else progress
    n = max(1, min(max_workers or max(1, (os.cpu_count() or 2) - 1), len(inputs) or 1))
    progress.update(slots=n, cores_per_slot=1, core_s=0.0)
    user_deadline = deadline
    avail = _avail_mb()
    peak = float(peak_mb or os.environ.get("MA_COMPUTE_PEAK_MB") or 0) or None
    cap = _mem_slots(peak, avail, n)
    deadline, why = _budget_deadline(user_deadline, allowance_s, cap)
    init_dir = tempfile.TemporaryDirectory(prefix="ma_compute_init_")

    def new_pool(k):
        return cf.ProcessPoolExecutor(max_workers=k, mp_context=mp.get_context("spawn"),
                                      initializer=_local_init,
                                      initargs=(run_dir, fn_spec, init_dir.name, _guard_registration()))

    def load_error():
        for name in os.listdir(init_dir.name):
            if name.endswith(".err"):
                try:
                    return open(os.path.join(init_dir.name, name)).read()
                except OSError:
                    return "unreadable"
        return None

    todo = list(enumerate(inputs))
    running, rows, stop, lost, pids, breaks, seen_peak = {}, [], None, 0, set(), 0, 0.0
    alone = {}                                     # input -> times its process died with the pool at one process
    toks = {}                                      # pool pid -> start token (Phase 42 α: killed if its caller dies)
    shrink = False
    recycle, since, recycles = recycle_every(cap), 0, 0     # v2: fresh pool per chunk of finished inputs
    # successes elsewhere in the batch (other dask chunks, rows reused on resume)
    prior_ok = int(prior_ok or 0) + int(os.environ.get("MA_COMPUTE_PRIOR_OK") or 0)

    def init_core_s():
        # Model load time already spent by pool processes (added to core_s at the end).
        t = 0.0
        for name in os.listdir(init_dir.name):
            if not name.endswith(".err"):
                try:
                    t += float(open(os.path.join(init_dir.name, name)).read())
                except (OSError, ValueError):
                    pass
        return t

    def rebudget():
        # Fewer processes burn the allowance more slowly: move the cap's wall-clock stop.
        nonlocal deadline, why
        if allowance_s is not None:
            busy = max(cap, len(running))     # a shrinking pool still runs its in-flight inputs
            now = time.time()
            used = progress["core_s"] + init_core_s() + sum(now - t for _, t in running.values())   # in flight, loads
            deadline, why = _budget_deadline(user_deadline, max(0.0, allowance_s - used), busy)
    ex = new_pool(cap)
    try:
        while todo or running:
            broke = False
            if shrink and not running:            # idle processes keep their memory: rebuild at the new size
                ex.shutdown(wait=True)
                _kill_groups(pids, grace_s=0, direct=False)
                ex, shrink, since = new_pool(cap), False, 0
                rebudget()
            now = time.time()
            k = 0 if shrink else max(0, min(len(todo), cap - len(running)))

            def note(complete):
                for pid in set((getattr(ex, "_processes", None) or {}).keys()) - set(toks):
                    toks[pid] = _pid_start(pid)
                _note(progress, [{"n": 1, "started": t, "cores": 1} for _, t in running.values()]
                      + ([] if complete else [{"n": 1, "started": now, "cores": 1}] * k),
                      extra_core_s=init_core_s() + progress.get("unreturned_s", 0.0), pids=[[p_, t_] for p_, t_ in toks.items()],
                      pids_complete=complete)
            # Recorded before submitting, so a caller killed right after is charged for these
            # too; the pool may start processes for them, so their pids are not yet known.
            note(k == 0)
            try:
                while todo and len(running) < cap and not shrink:
                    f = ex.submit(_local_chunk, [todo[0]])
                    running[f] = (todo.pop(0), time.time())
            except BrokenProcessPool:             # a process died between a result and the next submission
                broke = True
            if k:
                note(True)
            pids |= set((getattr(ex, "_processes", None) or {}).keys())
            crashed = []
            if not broke:
                left = None if deadline is None else max(0.0, deadline - time.time())
                done, _ = cf.wait(set(running), timeout=left, return_when=cf.FIRST_COMPLETED)
                if not done:
                    stop = why
                    break
                for f in done:
                    # Stays in `running` until charged: an interrupt (or a BaseException
                    # from the result) leaves it to the in-flight charge in `finally`.
                    item, t_sub = running[f]
                    try:
                        got = f.result()
                    except BrokenProcessPool:
                        running.pop(f)
                        crashed.append((item, t_sub))
                        broke = True
                        continue
                    except Exception as e:  # noqa: BLE001 — e.g. an unpicklable result fails only its row
                        running.pop(f)
                        lost += 1
                        if progress.get("_file"):      # its time, in case this call never returns normally
                            progress["unreturned_s"] = progress.get("unreturned_s", 0.0) + time.time() - t_sub
                        rows.append({"i": item[0], "ok": False, "kind": INFRA,
                                     "error": f"chunk failed: {type(e).__name__}: {e}"[:500]})
                        continue
                    rows += got
                    since += len(got)
                    progress["core_s"] += _known_core_s(got)
                    running.pop(f)
                    _rows_done(progress, got)
                    seen_peak = max([seen_peak] + [r.get("peak_mb") or 0 for r in got])
            if broke:
                # Every input still in flight dies with the pool: collect them all.
                cf.wait(set(running), timeout=30)
                for f, (item, t_sub) in list(running.items()):
                    try:
                        got = f.result(timeout=0)
                        rows += got
                        progress["core_s"] += _known_core_s(got)
                        _rows_done(progress, got)
                    except Exception:  # noqa: BLE001 — broken with the pool
                        crashed.append((item, t_sub))
                running.clear()
                breaks += 1
                now = time.time()
                live = set((getattr(ex, "_processes", None) or {}).keys())
                # Their partial work is charged in full, not lost. It may overlap load time
                # also counted from init_dir: the ledger may overcharge, never undercharge.
                progress["core_s"] += sum(now - t for _, t in crashed)
                pids |= live
                _kill_groups(live, grace_s=0.5)
                ex.shutdown(wait=True, cancel_futures=True)
                _kill_groups(pids, grace_s=0, direct=False)
                _kill_chunk_group_others()        # what the dead pool's models started (inside a remote chunk)
                err = load_error()
                if err:
                    stop = f"stopped: the model failed to load: {err}"
                    rows += [{"i": it[0], "ok": False, "kind": MODEL, "error": f"the model failed to load: {err}"[:500]}
                             for it, _ in crashed]
                    break
                crashed.sort(key=lambda c: c[0][0])
                if cap == 1 and len(crashed) == 1:
                    i = crashed[0][0][0]
                    alone[i] = alone.get(i, 0) + 1
                    if alone[i] >= 2:
                        rows.append({"i": i, "ok": False, "kind": MODEL,
                                     "error": ("the process died twice running this input alone (out of memory or a "
                                               f"crash in native code); about {avail or 0:.0f} MB was available to the "
                                               f"pool, the largest process peak seen was {seen_peak:.0f} MB")})
                    else:
                        todo.insert(0, crashed[0][0])     # once could be the machine: one retry, still alone
                else:
                    todo = [it for it, _ in crashed] + todo
                    cap = max(1, cap // 2)
                rebudget()
                ex, shrink, since = new_pool(cap), False, 0
            if seen_peak and _mem_slots(seen_peak, avail, n) < cap:
                cap = _mem_slots(seen_peak, avail, n)
                shrink = True                     # drain, then rebuild the pool at `cap` processes
                rebudget()
            if recycle and since >= recycle and todo and not shrink:
                shrink, recycles = True, recycles + 1     # drain, then a fresh pool at the same size
            if todo or running:
                reason = _breaker(rows, prior_ok=prior_ok)
                if reason:
                    stop = reason
                    break
            if (todo or running) and allowance_s is not None and progress["core_s"] + init_core_s() >= allowance_s:
                stop = CAP_STOP
                break
    finally:
        live = set((getattr(ex, "_processes", None) or {}).keys())   # not yet reaped
        pids |= live
        # Phase 42 α: if this call is being interrupted, what it used is known:
        # rows finished, model loads, and the inputs in flight (killed now).
        in_flight = list(running.values())
        if running:
            _kill_groups(live)
            _kill_groups(pids - live, grace_s=0, direct=False)
        ex.shutdown(wait=True, cancel_futures=True)
        _kill_groups(pids, grace_s=0, direct=False)   # leftovers the model spawned (pool already reaped)
        t_end = time.time()                           # after the stop: its grace period counts
        extra = sum(t_end - t for _, t in in_flight) + init_core_s() + progress.get("unreturned_s", 0.0)
        progress.update(settled=True, settled_extra_s=extra)
    # Model load time of every pool process (a process still loading when it
    # was killed is covered by the unfinished-chunk estimate).
    for name in os.listdir(init_dir.name):
        if name.endswith(".err"):
            continue
        try:
            progress["core_s"] += float(open(os.path.join(init_dir.name, name)).read())
        except (OSError, ValueError):
            pass
    init_dir.cleanup()
    return rows, {"backend": "local", "workers": n, "slots": n, "cores_per_slot": 1, "core_s": progress["core_s"],
                  "stopped": stop, "unknown_chunks": lost + len(running), "breaks": breaks, "procs_final": cap,
                  "peak_mb": round(seen_peak, 1) or None, "pool_recycles": recycles}


def recycle_every(procs: int) -> int:
    """Finished inputs after which the local pool is replaced (v2, spike lesson 7): MA_COMPUTE_RECYCLE_EVERY,
    else 15 per process (the spike's chunk size, well below the ~750 runs per VM at which its pools broke);
    0 disables."""
    v = os.environ.get("MA_COMPUTE_RECYCLE_EVERY")
    try:
        return max(0, int(v)) if v not in (None, "") else 15 * max(1, int(procs))
    except ValueError:
        return 15 * max(1, int(procs))


# --------------------------------------------------------------------------- #
# Dask backend
# --------------------------------------------------------------------------- #

def make_bundle(run_dir: str, max_file_mb: float = DEFAULT_MAX_FILE_MB) -> tuple[bytes, dict]:
    """tar.gz of the run's models/ and data/ and the repo's scripts/ and toolkit/, with
    ABSOLUTE member paths so workers recreate the same layout."""
    run_dir = os.path.realpath(run_dir)
    skipped, n = [], 0
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
                 chunk_allowance_s: float | None = None, call_id: str | None = None, repo: str | None = None,
                 peak_mb: float | None = None, prior_ok: int = 0, chunk_breaker: bool = True,
                 token: str | None = None):
    """Runs on a dask worker. Under a machine-wide lock: make sure the files on
    disk are exactly THIS bundle (another call's bundle may have replaced them:
    its extra files are removed and no stale bytecode survives), then evaluate
    the chunk. The live run directory (has metadata.json) is never touched:
    there the model runs against the run's own files, and no path through a
    symlink is written or deleted. The chunk stops at `deadline` (wall clock)
    or once it has used `chunk_allowance_s` core-seconds, counted from when it
    starts running, not while it queues. Returns the rows and the core-seconds
    used, including work in crashed processes.

    Phase 45 δ: `token` names this submission. Under the lock, the chunk
    claims it (an O_EXCL file) before any work; if `_revoke_chunk` claimed it
    first, the chunk returns at once with no work done ("revoked")."""
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

    def rows_with(msg, kind="stopped"):
        return [{"i": i, "ok": False, "kind": kind, "error": msg[:500], "worker": waddr} for i, _ in chunk]

    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        t0 = time.time()

        def failed(msg, kind="stopped"):
            return {"rows": rows_with(msg, kind), "core_s": round((time.time() - t0) * procs, 3)}
        if os.path.exists(os.path.join(calls, "cancel")):
            return {"rows": rows_with("cancelled"), "core_s": 0.0}
        if token:
            won, owner = _claim(calls, token, "start")
            if not won and owner == "revoke":
                return {"rows": rows_with("revoked: moved to another worker"), "core_s": 0.0, "revoked": True}
            if not won:
                # An earlier attempt of this submission claimed its start (dask
                # replayed the task after a worker restart): that work is unknown,
                # so the caller treats the chunk as lost (charged, resubmitted once).
                return {"rows": rows_with("an earlier attempt of this chunk started; its work is unconfirmed",
                                          "infra"), "core_s": 0.0, "unconfirmed": True}
        live = os.path.exists(os.path.join(run_dir, "metadata.json"))
        if not live:
            tf = tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz")
            members = [m.name for m in tf.getmembers() if m.isfile()]
            dirs = {os.path.dirname("/" + m) for m in members} | {run_dir}
            if not all(_no_symlink_path(d) for d in dirs):
                return failed("refusing to stage the bundle: a target directory is a symlink", "model")
            if any(os.path.exists(os.path.join(d, "metadata.json")) for d in dirs if d.startswith(run_dir)):
                return failed("refusing to stage the bundle over a live run directory", "model")
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
            if peak_mb:
                env["MA_COMPUTE_PEAK_MB"] = str(peak_mb)    # Phase 35: start at a concurrency that fits
            env["MA_COMPUTE_PRIOR_OK"] = str(int(prior_ok or 0))   # the chunk's breaker sees the batch's successes
            if not chunk_breaker:
                env["MA_COMPUTE_BREAKER_MIN"] = "0"     # a rerun tail: only the batch-wide breaker applies
            cancel_mark = os.path.join(calls, "cancel")
            cmd = [sys.executable, script, "map", run_dir, "--fn", fn_spec, "--inputs", inp,
                   "--out", out, "--out-root", td, "--backend", "local", "--max-workers", str(procs), "--no-ledger",
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
                b = chunk_allowance_s + 60     # backstop only: memory may leave a single process running
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
            # pid and start token: _cancel_call signals it only while both still match
            open(pidf + ".tmp", "w").write(f"{proc.pid} {_pid_start(proc.pid) or ''}")
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
                return failed(f"chunk subprocess failed (rc {proc.returncode}): {(err or '')[-800:]}", "infra")
            rows = [json.loads(l) for l in open(out) if l.strip()]
            csum = None
            try:
                csum = json.load(open(out + ".summary.json"))
                core_s = float(csum["task_core_s"])
            except (OSError, ValueError, KeyError, TypeError):
                core_s = (time.time() - t0) * procs    # no summary: charge the chunk as fully busy
    return {"rows": [{**row, "i": chunk[k][0], "worker": waddr} for k, row in enumerate(rows)], "core_s": core_s,
            "summary": csum if isinstance(csum, dict) else None, "stderr_tail": (err or "")[-300:]}


def _claim(calls: str, token: str, who: str) -> tuple[bool, str]:
    """Claim a submission's start for `who` ("start" or "revoke"). Returns
    (won, owner): owner is whoever holds the claim, as recorded in the claim
    file. The file is written whole and then hard-linked into place, so the
    link succeeds for exactly one side and a reader never sees it half written."""
    import os, time  # noqa: E401 — pickled by value
    os.makedirs(calls, exist_ok=True)
    path = os.path.join(calls, f"claim-{token}")
    tmp = f"{path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    with open(tmp, "w") as fh:
        fh.write(who)
    try:
        os.link(tmp, path)
        return True, who
    except FileExistsError:
        with open(path) as fh:
            return False, fh.read()
    finally:
        os.unlink(tmp)


def _revoke_chunk(call_id: str, token: str) -> bool:
    """Runs on a worker (Phase 45 δ): claim a chunk's start so that it never
    starts. True: revoked, it does no work (asked again after a lost reply,
    the answer is the same). False: the chunk claimed its start first and is
    left alone. The claim is a file on the worker's disk; exactly one side wins."""
    import os, tempfile  # noqa: E401 — pickled by value
    calls = os.path.join(tempfile.gettempdir(), _CALLS_DIR, call_id)
    won, owner = _claim(calls, token, "revoke")
    return won or owner == "revoke"


def _same_process(pid: int, token: str | None) -> bool:
    """Is `pid` still the process recorded with start `token`? Only when its
    current token is read and equal; unknown on either side is not."""
    cur = _pid_start(pid)
    return bool(token and cur and cur == token)


def _stop_chunk(pid: int, grace_s: float = 5.0, token: str | None = None) -> bool:
    """SIGTERM a chunk subprocess's group (its handler kills its pool), then
    SIGKILL the group after the grace period. Returns whether it is stopped.

    `token`: a pid read from a registration (not our own unreaped child) is
    signalled only while its current start token still equals this one,
    checked before EVERY signal; once it no longer verifies, nothing more is
    sent and a group still alive counts as not stopped (unconfirmed)."""
    import signal
    if token is not None and not _same_process(pid, token):
        return not (_pid_alive(pid) or _group_alive(pid))
    _killpg(pid, signal.SIGTERM)       # group only: the chunk is its session leader
    end = time.time() + grace_s
    while time.time() < end and _group_alive(pid):
        time.sleep(0.1)
    if not _group_alive(pid):
        return True
    if token is not None and not _same_process(pid, token):
        return False                   # identity lost during the grace period: not signalled again
    _killpg(pid, signal.SIGKILL)
    end = time.time() + 5
    while time.time() < end and _group_alive(pid):   # SIGKILL is not instant; confirm
        time.sleep(0.05)
    return not _group_alive(pid)


def _cancel_call(call_id: str) -> int:
    """On a worker (client.run): stop this call's running chunks and keep its
    queued ones from starting. Returns how many chunks were stopped, or -1 if
    a registered chunk that may still run could not be identified (its start
    token unknown or unreadable): it is not signalled, and the caller treats
    the stop as unconfirmed."""
    import tempfile
    calls = os.path.join(tempfile.gettempdir(), _CALLS_DIR, call_id)
    os.makedirs(calls, exist_ok=True)
    open(os.path.join(calls, "cancel"), "w").write(str(time.time()))
    n, unverified = 0, False
    for name in os.listdir(calls):
        if name.isdigit():
            pid = int(name)
            try:
                _, _, tok = open(os.path.join(calls, name)).read().partition(" ")
            except FileNotFoundError:
                continue                          # unregistered as we looked: the chunk has ended
            except OSError:
                unverified = True                 # cannot read who it is: not signalled
                continue
            tok, cur = tok.strip(), _pid_start(pid)
            if tok and cur and cur == tok:
                if not _stop_chunk(pid, token=tok):
                    unverified = True
                n += 1
            elif tok and cur and cur.split(":")[0] == tok.split(":")[0]:
                continue                          # read the same way, different: pid reused, ours ended
            elif _pid_alive(pid) or _group_alive(pid):
                unverified = True                 # no token, unreadable, or read another way
    return -1 if unverified else n


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


def register_compute_tags(client, exclude: list[str] | tuple = (), resource: str = "compute") -> None:
    """Tag compute workers on an autoscaling cluster. Uploads this module first
    (as a worker plugin, so workers that join later get it too): the plugin is
    pickled by reference to `compute`, which cluster workers cannot otherwise
    import (Coiled check, Phase 30; same class of bug as PR #20)."""
    client.upload_file(os.path.abspath(__file__))
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
    some worker still holds `key` (the scattered bundle)}."""
    ts = dask_scheduler.tasks.get(key) if key is not None else None
    return {"workers": {a: [res is None or res in (ws.resources or {}), len(ws.processing),
                            getattr(ws.status, "name", str(ws.status)) == "running"]
                        for a, ws in dask_scheduler.workers.items()},
            "held": bool(ts is not None and ts.who_has)}


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
    addr = os.environ["MA_COMPUTE_SCHEDULER"]
    exclude = {w.strip() for w in os.environ.get("MA_COMPUTE_EXCLUDE_WORKERS", "").split(",") if w.strip()}
    bundle, binfo = make_bundle(run_dir)
    call_id = uuid.uuid4().hex
    t0_call = time.time()
    progress = {} if progress is None else progress
    with Client(addr, timeout="60s", security=_security()) as c:
        # Dask pickles functions by reference when it can, so the scheduler and
        # workers must be able to import this module: ship it to them.
        c.upload_file(os.path.abspath(__file__))
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
        bfut = (c.scatter(bundle, broadcast=False, hash=False) if res
                else c.scatter(bundle, workers=workers, broadcast=True))
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
            f = c.submit(_remote_task, bfut, binfo["sha256"], run_dir, fn_spec, ch, deadline,
                         procs_for(override), share, call_id, REPO, learned["peak"],
                         prior_ok + sum(1 for r in rows if r.get("ok")), attempt != TAIL, tok_,
                         pure=False, workers=[worker], allow_other_workers=False, **restrict)
            futs[f] = (ch, attempt, procs_for(override))
            pinned[f], toks[f] = worker, tok_
            held[f] = share or 0.0
            started[f] = time.time()
            return f
        futs, queue, started, errors = {}, [(ch, 0, None) for ch in chunks], {}, {}
        losses = {}                                 # input -> times its chunk was lost (chunk_lost)

        def halved(used, worker=None):
            return max(1, int(used or cpus.get(worker) or cores_per_slot) // 2)

        def chunk_lost(f, ch, attempt, used, f_share, msg):
            """A chunk whose result is lost (its worker died or left, or the task
            failed): charge it, then resubmit it on the remaining or new workers,
            up to `retries` times per input, the first time with half the processes."""
            nonlocal lost
            # Its work is unknown: charge the share it was allowed
            # (chunks enforce their share), or estimate it at the end.
            # charged as busy since submission, never above its share
            busy = (time.time() - started.get(f, time.time())) * cores_per_slot
            if f_share:
                progress["core_s"] += min(f_share, busy)
            else:
                lost += 1
            n_lost = 1 + max(losses.get(i, 0) for i, _ in ch)
            losses.update({i: n_lost for i, _ in ch})
            progress.setdefault("chunks", []).append(
                {"n": len(ch), "attempt": attempt, "losses": n_lost, "error": msg[:200], "worker": pinned.get(f),
                 "core_s": round(min(f_share, busy), 1) if f_share else None,
                 "wall_s": round(time.time() - started.get(f, time.time()), 1)})
            errors.update({i: msg for i, _ in ch})
            if n_lost <= retries and (deadline is None or time.time() < deadline):
                # A rerun tail stays one (no chunk breaker); a lost VM says nothing more about memory.
                queue.insert(0, (ch, TAIL if attempt == TAIL else 1, halved(used) if n_lost == 1 else used))
                progress["resubmitted"] = progress.get("resubmitted", 0) + 1
                return
            rows.extend({"i": i, "ok": False, "kind": INFRA, "error": errors[i]} for i, _ in ch)

        def view():
            """({address: [tagged, processing, accepting]} for the workers chunks
            may go to, every worker the scheduler knows, whether the bundle is
            still held), or None if unread."""
            try:
                v = c.run_on_scheduler(_sched_view, res, bfut.key)
                allw = v["workers"]
            except Exception:  # noqa: BLE001 — keep going on the last state; nothing is declared lost
                return None
            ok_w = {a: s for a, s in allw.items()
                    if a not in exclude and (s[0] if res else a in fixed)}
            return ok_w, allw, v["held"]

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
        no_w = None                                 # since when no compute worker has been taking work
        try:
            while queue or pending:
                _note(progress, inflight(), call_id=call_id)
                got_v, free = view(), []
                if got_v is not None:
                    ok_w, allw, held_b = got_v
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
                                   f"chunk failed: its worker {pinned[f]} left the cluster")
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
                            bfut = c.scatter(bundle, workers=list(ok_w), broadcast=not res, hash=False)
                            progress["bundle_rescatters"] = progress.get("bundle_rescatters", 0) + 1
                        except Exception:  # noqa: BLE001 — chunks then fail and are charged as lost
                            pass
                # A loss above may have used the rest of the allowance: stop now
                # rather than wait for a worker with demand still shown.
                if (queue or pending) and allowance_s is not None and progress["core_s"] >= allowance_s:
                    stop = CAP_STOP
                    break
                starved = allowance_s is not None and allowance_s - progress["core_s"] - sum(held.values()) <= 0
                while queue and free and len(pending) < slots and not starved:
                    f = submit(*queue[0], worker=free.pop(0))
                    if f is None:
                        starved = True
                        break
                    queue.pop(0)
                    pending.add(f)
                if res:
                    demand(0 if starved else min(len(queue), max(0, pool - len(pending))))
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
                    done, pending = wait(pending, timeout=poll_s if left is None else min(poll_s, left),
                                         return_when="FIRST_COMPLETED")
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
                        chunk_lost(f, ch, attempt, used, f_share, f"chunk failed: {type(e).__name__}: {e}"[:500])
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
                except (TimeoutError, cf.CancelledError):
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
                  "invariant_violation": (f"{len(absent)} inputs returned no row" if absent else None),
                  "chunks": progress.get("chunks", []), "procs_final": learned["procs"],
                  "peak_mb": learned["peak"], "busy_guard": guard, "resubmitted": progress.get("resubmitted", 0)}


# --------------------------------------------------------------------------- #
# Public entry
# --------------------------------------------------------------------------- #

def _known_core_s(rows) -> float:
    return sum(r.get("seconds") or 0 for r in rows)


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


def _proc_stat(pid: int) -> list | None:
    """Fields of /proc/<pid>/stat after the command name (Linux), or None."""
    try:
        with open(f"/proc/{int(pid)}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()
    except (OSError, IndexError, ValueError):
        return None


def _pid_start(pid: int) -> str | None:
    """When process `pid` started, as a token (None if unknown): a pid reused
    by a later process has a different token."""
    st = _proc_stat(pid)
    if st and len(st) > 19:
        return "proc:" + st[19]                   # field 22, starttime in clock ticks
    try:
        import psutil
        return f"psutil:{psutil.Process(int(pid)).create_time():.2f}"
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 — the process is gone
        return None
    return _ps_start(pid)


def _ps_start(pid: int) -> str | None:
    """Start time from `ps` in a fixed environment (UTC, C locale), so callers
    with different TZ or locale settings get the same token for one process."""
    import subprocess
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TZ": "UTC", "LC_ALL": "C", "LANG": "C"}
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=10, env=env).stdout.strip()
        return "ps:" + " ".join(out.split()) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _pid_alive(pid, start: str | None = None) -> bool:
    """Phase 42 α: is `pid` a running process? False when it is gone, when it
    is defunct (a zombie: `os.kill(pid, 0)` succeeds on one; E7 attempt 8 held
    a 400 core-hour reservation for 7+ hours on a zombie caller), or when its
    start token differs from `start` (the pid was reused)."""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except PermissionError:
        pass                                      # exists, owned by another user
    except (OSError, ValueError, TypeError, OverflowError):
        return False
    st = _proc_stat(pid)
    if st is not None:
        state = st[0] if st else None
    else:
        state = None
        try:
            import psutil
            try:
                state = "Z" if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE else "R"
            except psutil.NoSuchProcess:
                return False
            except psutil.Error:
                state = None
        except ImportError:
            import subprocess
            try:
                out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True,
                                     timeout=10).stdout.strip()
                if not out:
                    return False
                state = out[:1]
            except (OSError, subprocess.SubprocessError):
                state = None                      # cannot tell: os.kill said it exists
    if state in ("Z", "X", "x"):
        return False
    if start:
        now = _pid_start(pid)
        if now is not None and now.split(":")[0] == start.split(":")[0] and now != start:
            return False
    return True


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


def _note(progress: dict, out: list, extra_core_s: float = 0.0, **more) -> None:
    """Phase 42 α: persist what a ledgered call has done and what it has in
    flight (`out`: [{"n": tasks, "share": core-s or None, "started": t,
    "cores": cores}]), so a caller killed without settling (an agent's tool
    timeout, SIGKILL) is charged for work done, not its whole reservation.
    Written before new work is submitted. If a write fails the record is
    removed, so settlement falls back to charging the reservation."""
    if progress.get("_beat_err"):
        raise progress["_beat_err"]
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


def _task_seen(progress: dict, n: int, core_s: float) -> None:
    """Record `n` finished tasks that took `core_s` (the per-task bound for work in flight)."""
    if n > 0:
        progress["done_tasks"] = int(progress.get("done_tasks") or 0) + n
        progress["max_task_core_s"] = max(float(progress.get("max_task_core_s") or 0), float(core_s) / n)


def _rows_done(progress: dict, rows: list, core_s: float | None = None) -> None:
    """Finished rows: update the per-task bound and hand them to the row sink
    (`<out>.partial.jsonl`). A failing sink is switched off, never the batch."""
    timed = [r for r in rows if isinstance(r.get("seconds"), (int, float))]
    if core_s is None:
        _task_seen(progress, len(timed), _known_core_s(timed))
    else:                                         # a chunk's total (overheads included) over the rows that ran
        _task_seen(progress, len(timed) or len(rows), core_s)
    sink = progress.get("_rows")
    if sink and rows:
        try:
            sink(rows)
        except Exception as e:  # noqa: BLE001
            progress["_rows"] = None
            print(f"[compute] partial results not written ({type(e).__name__}: {e}); the batch continues",
                  file=sys.stderr, flush=True)


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
    bounded by their shares and local work in flight charges the reservation."""
    now = time.time() if now is None else now
    held = float(r.get("core_s") or 0)
    path, _ = _progress_path(r.get("progress"), run_dir)
    if not path:
        return held, "caller died; no progress record (reserved before Phase 42, or it could not be kept); charged its reservation", {}
    try:
        p = json.load(open(path))
        done = float(p["core_s"])
        out = list(p.get("out") or [])
        float(p["t"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        return held, f"caller died; progress record unreadable ({type(e).__name__}); charged its reservation", {}
    if p.get("remote_unconfirmed"):
        return held, "caller died; remote chunks not confirmed stopped; charged its reservation", {}
    if foreign:
        stopped, how = (False, "on another host: not stopped from here") if out else (True, "nothing in flight")
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
                bound += share
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
                         "reserved_core_s": round(held, 1)}


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
                                        allowance_s=allowance_s, progress=progress, peak_mb=hint, prior_ok=prior_ok)
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
_FP_HASH_MB = 64           # files up to this size are fingerprinted by content, larger ones by size and mtime
# F6 S1-8: the fingerprint covers code content only. The launch commit stamp changes with every commit (git archive
# fills it in, .gitattributes export-subst), so with it any launch commit invalidated every stored batch (F5, 2026-11-08).
_FP_REPO_SKIP = ("scripts/lib/code_commit.txt",)
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
    # toolkit/spec/geometry.py SIGNOFF_FILE, written by freeze() at sign-off and `approve`: who signed, when, the
    # owner's approvals, and a copy of the geometry declaration with sha256 hashes of the boundary, crosswalk and
    # units files. Those files themselves stay hashed under data/, and only the gate (geometry.check) reads the record.
    "data/geometry_signoff.json",
)
# Dependency manifests at the repo root: a pin change changes what the code computes (Codex P2-8).
FP_DEP_MANIFESTS = ("requirements.txt", "requirements-cloud.txt", "requirements-agent.txt")


def _harness_output(run_dir: str, rel: str) -> bool:
    return (rel.replace(os.sep, "/") in FP_HARNESS_OUTPUTS + FP_SIGNOFF_RECORDS
            and os.path.isfile(os.path.join(run_dir, "decision_spec.yaml")))


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


def code_fingerprint(run_dir: str, fn_spec: str, exclude=(), confined: bool = False) -> str:
    """What a result depends on besides its params: every file under the run's
    models/ and data/ (following symlinked directories) and the repo's
    scripts/ and toolkit/ (code content only: never the commit stamp, F6 S1-8) and its dependency manifests
    (FP_DEP_MANIFESTS), except a v2 run's fixed-name analysis outputs (FP_HARNESS_OUTPUTS) and sign-off records
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
    for root in (os.path.join(run_dir, "models"), os.path.join(run_dir, "data"),
                 *(os.path.join(REPO, x) for x in REPO_CODE_DIRS)):
        # Every path a model can use is walked, aliases included (each under its
        # own name); only a loop (a dir that is its own ancestor) stops the walk.
        ancestors = {root: frozenset()}
        for dp, dirs, fns in os.walk(root, followlinks=True):
            real = os.path.realpath(dp)
            anc = ancestors.pop(dp, frozenset())
            if real in anc:
                dirs[:] = []
                continue
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git", "_truth"))
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
                is_output = fn + ".summary.json" in names     # another call's results file, any extension
                if not in_data and not is_output and (fn.endswith(_FP_SKIP_EXT) or fn.endswith(".summary.json")
                                                      or fn.endswith((PARTIAL_EXT, JOB_EXT))
                                                      or fn.startswith("compute_ledger.json")
                                                      or (runner and fn in _FP_RUNNER_FILES)):
                    continue          # logs, figures, summaries, the ledger (never skipped under data/)
                files.add(os.path.join(dp, fn))
    files.update(os.path.join(REPO, m) for m in FP_DEP_MANIFESTS)     # missing ones are skipped below
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


def _reusable(out: str, fn_spec: str, fp: str, fid: str | None = None) -> dict:
    """key -> ok row from an earlier --out of the same function and code, and
    (Phase 42 α) from the rows an interrupted or killed run of it had written
    to `<out>.partial.jsonl` (its first line names the function and code)."""
    got = {}
    try:
        prev = json.load(open(out + ".summary.json"))
        if _same_fn(prev, fn_spec, fid) and prev.get("code_fp") == fp:
            got.update({r["key"]: r for r in (json.loads(l) for l in open(out) if l.strip())
                        if isinstance(r, dict) and r.get("ok") and r.get("key")})
    except (OSError, ValueError):
        pass
    try:
        with open(out + PARTIAL_EXT) as f:
            head = json.loads(f.readline() or "{}").get("_partial") or {}
            if _same_fn(head, fn_spec, fid) and head.get("code_fp") == fp:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue                  # a line torn by a kill mid-write
                    if isinstance(r, dict) and r.get("ok") and r.get("key"):
                        got.setdefault(r["key"], r)
    except (OSError, ValueError, AttributeError):
        pass
    return got


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
              "interpreter's packages with the one that recorded the batch, not the workers'")


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
            stored.update(_reusable(out, h["fn"], h["code_fp"]))
        spec = _here_spec(heads[0])
        path = spec.partition("::")[0]
        if not os.path.exists(path if os.path.isabs(path) else os.path.join(run_dir, path)):
            n, why = 0, f"function file not found here: {spec}"
        else:
            fp = code_fingerprint(run_dir, spec, exclude=_out_files(out))
            n = len(_reusable(out, spec, fp, fn_id(run_dir, spec)))
            why = "" if n >= len(stored) else (
                "code fingerprint differs: code, data or a file under models/ changed"
                if fp not in {h["code_fp"] for h in heads} else "recorded under another function")
        rt = heads[0].get("runtime")
        batches.append({"out": os.path.relpath(out, run_dir), "fn": heads[0].get("fn_id") or heads[0]["fn"],
                        "stored": len(stored), "eligible": n, "why": why,
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
    fp = code_fingerprint(run_dir, fn_spec, exclude=_out_files(out))
    fid = fn_id(run_dir, fn_spec)
    keys = input_keys(inputs)
    old = {} if fresh else _reusable(out, fn_spec, fp, fid)
    todo = [k for k, key in enumerate(keys) if key not in old]
    if todo:
        # Phase 42 α: rows are appended to <out>.partial.jsonl as they finish, so
        # an interrupted or killed batch keeps them (rerunning reuses them).
        # The rows kept so far are copied to a new journal that replaces the old
        # one only once complete (a kill during the copy loses nothing).
        from lib.safe_io import open_append_nofollow, write_nofollow
        base, name = _out_base(out, run_dir)
        journal = json.dumps({"_partial": {"fn": fn_spec, "fn_id": fid, "code_fp": fp, "n": len(inputs)}}) + "\n" + "".join(
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
               "runtime": runtime_versions()}
    from lib.safe_io import remove_nofollow, write_nofollow
    base, name = _out_base(out, run_dir)
    try:
        write_nofollow(base, name, "".join(json.dumps(r) + "\n" for r in rows).encode(), atomic=True)
        write_nofollow(base, name + ".summary.json", json.dumps(summary, indent=1).encode(), atomic=True)
        try:
            remove_nofollow(base, name + PARTIAL_EXT)
        except OSError:
            pass
    finally:
        os.close(base)
    return rows, summary


def gate_findings(run_dir: str) -> list[dict]:
    """Phase 35: results files under models/ with rows that have no result
    (failed, or missing from the file against its summary's count). A file
    is left out when a newer results file of the same function has ok rows
    for all of its params (it was redone in full). Rows are never recovered
    piecemeal from other files: rerunning the same command completes a file
    in place. HIGH when more than 5% of a file's rows have no result (or the
    file is missing or unreadable), MEDIUM otherwise."""
    import collections
    import glob as _glob
    found = []
    for sp in _glob.glob(os.path.join(run_dir, "models", "**", "*.summary.json"), recursive=True):
        try:
            s = json.load(open(sp))
        except (OSError, ValueError):
            continue
        if not isinstance(s, dict) or "n_failed" not in s or "task_core_s" not in s:
            continue                       # not a compute summary
        out = sp[: -len(".summary.json")]
        if not os.path.exists(out):
            found.append((out, s, None, "missing (its summary is there)"))
            continue
        try:
            rows = [json.loads(l) for l in open(out) if l.strip()]
        except (OSError, ValueError) as e:
            found.append((out, s, None, f"unreadable ({e})"))
            continue
        found.append((out, s, rows, None))
    files = [(out, s.get("fn"), os.path.getmtime(out), {r.get("key") for r in rows if r.get("ok") and r.get("key")})
             for out, s, rows, _ in found if rows is not None]
    findings = []
    for out, s, rows, bad in sorted(found, key=lambda x: x[0]):
        rel = os.path.relpath(out, run_dir)
        if bad:
            findings.append({"kind": "compute_results_incomplete", "severity": "HIGH", "stage": "MODEL",
                             "target_stage": "MODEL", "scope_declarable": True,
                             "claim": f"{rel}: results file is {bad}; its rows cannot be checked."})
            continue
        mt = os.path.getmtime(out)
        n = max(len(rows), int(s.get("n") or 0))
        mine = {r.get("key") for r in rows if r.get("key")}
        if mine and len(mine) == len(rows) == n and any(
                o != out and fn == s.get("fn") and t >= mt and mine <= ok for o, fn, t, ok in files):
            continue                      # redone in full by a newer file
        left = [r for r in rows if not r.get("ok")]
        absent = n - len(rows)
        if not left and not absent:
            continue
        kinds = collections.Counter(r.get("kind") or MODEL for r in left)
        if absent:
            kinds["absent from the file"] = absent
        eg = ""
        if left:
            top_key = collections.Counter(_err_key(r) for r in left).most_common(1)[0][0]
            eg = "; e.g. " + str(next(r.get("error") for r in left if _err_key(r) == top_key))[:160]
        u = len(left) + absent
        findings.append({
            "kind": "compute_results_incomplete", "severity": "HIGH" if u / max(1, n) > 0.05 else "MEDIUM",
            "stage": "MODEL", "target_stage": "MODEL", "scope_declarable": True,
            "claim": (f"{rel}: {u} of {n} rows have no result "
                      f"({', '.join(f'{v} {k}' for k, v in sorted(kinds.items()))}{eg}). "
                      "Rerun the same compute.py map command: it computes only the missing rows, and machine "
                      "failures are retried automatically. Rows failing in the model itself need a model fix, or "
                      "an explicit exclusion justified in the write-up.")})
    return findings


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
           and any(n.endswith("scripts/compute.py") for n in names)
           and any(n.endswith("toolkit/alloc/costs.py") for n in names),
           f"C7 bundle has models/, scripts/ and toolkit/ (a run model may import toolkit.alloc): {info}")
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

        def call(tag, fn="models/slow.py::sleep", params=None, **kw):
            try:
                out[tag] = map_fn(rd, fn, [params or {"s": 1.0}], backend="local", **kw)[1]
            except RuntimeError as e:
                out[tag] = str(e)
        # a's task waits on a gate the test holds, so a's reservation is held
        # for certain while b is checked (not for "about a second").
        open(os.path.join(rd, "models", "gated13.py"), "w").write(
            "import os, time\ndef f(p):\n    end = time.time() + 300\n"
            "    while os.path.exists(p['gate']) and time.time() < end:\n        time.sleep(0.02)\n"
            "    time.sleep(1.0)\n    return {'ok': 1}\n")     # 1 s of work after release, as before
        gate13 = os.path.join(d, "gate13")
        open(gate13, "w").close()
        os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = "1"
        try:
            ta = threading.Thread(target=call, args=("a", "models/gated13.py::f", {"gate": gate13})); ta.start()
            end = time.time() + 60                # a's reservation is in the ledger
            while time.time() < end:
                try:
                    if json.load(open(os.path.join(rd, LEDGER_REL))).get("reservations"):
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(0.01)
            call("b")
            os.remove(gate13)                     # release a only after b's check
            ta.join()
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
            ok(s["n_failed"] == 2 and s["task_core_s"] > 0 and s["failed_by_kind"] == {"model": 2},
               f"C15 a crashed process is still charged, and an input that kills its process alone is a model error: {s}")
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
        end = time.time() + 10                    # a SIGKILL lands asynchronously: wait for it, bounded
        while time.time() < end and _pid_alive(rows[0]["result"]):
            time.sleep(0.02)
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
        # core_s and the cap-stopped rows show the share is enforced; the wall-clock
        # limit only catches a hang (it was 8 s and failed on a loaded machine).
        ok(time.time() - t0 < 30 and any(x.get("error") == CAP_STOP for x in r["rows"]) and r["core_s"] < 3,
           f"C25 a remote chunk enforces its allowance share: core_s={r['core_s']}")
        import threading as _th
        import uuid as _uuid
        res, cid = {}, "selftest-" + _uuid.uuid4().hex
        th = _th.Thread(target=lambda: res.update(r=_remote_task(bc_, ic_["sha256"], cdir, "models/slow.py::sleep",
                                                                 [(0, {"s": 60})], procs=1, call_id=cid)))
        th.start()
        # Readiness, not a fixed sleep (a loaded machine registered the chunk after 1.5 s):
        # wait until the chunk has registered its pid for this call.
        cdir_ = os.path.join(tempfile.gettempdir(), _CALLS_DIR, cid)
        end = time.time() + 60
        while time.time() < end and not any(n.isdigit() for n in (os.listdir(cdir_) if os.path.isdir(cdir_) else [])):
            time.sleep(0.02)
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
                # P35-D: on the dask path a pool broken inside a chunk heals there, and
                # a repeated model error stops the whole call.
                open(os.path.join(d, "models", "p35d.py"), "w").write(
                    "import os, time\n"
                    "def f(p):\n"
                    "    if p.get('crash') and os.environ.get('MA_COMPUTE_SELFTEST_ONCE') is None:\n"
                    "        flag = os.path.join(p['dir'], str(p['x']))\n"
                    "        if not os.path.exists(flag):\n"
                    "            open(flag, 'w').close(); os._exit(9)\n"
                    "    time.sleep(0.2)\n"
                    "    return {'x': p['x']}\n"
                    "def raises(p):\n    raise ValueError('diverged')\n")
                flags = tempfile.mkdtemp(prefix="ma_p35d_")
                code_d = ("import json, sys; sys.path.insert(0, %r); import compute; "
                          "ins = [{'x': k, 'dir': %r, 'crash': k in (2, 9)} for k in range(12)]; "
                          "rows, s = compute.map_fn(%r, 'models/p35d.py::f', ins, ledger=False, max_workers=1); "
                          "r2, s2 = compute.map_fn(%r, 'models/p35d.py::raises', [{}] * 40, ledger=False, max_workers=1); "
                          "print(json.dumps([s['n_failed'], [r['result']['x'] for r in rows if r['ok']], "
                          "s2['stopped'] or '', s2['failed_by_kind']]))") % (HERE, flags, d, d)
                r3 = _sp.run([sys.executable, "-c", code_d], capture_output=True, text=True, timeout=300,
                             env=dict(os.environ, PYTHONPATH="", MA_COMPUTE_PROCS_PER_WORKER="2"))
                try:
                    nf, xs, stop2, kinds2 = json.loads(r3.stdout.strip().splitlines()[-1])
                except (ValueError, IndexError):
                    nf, xs, stop2, kinds2 = None, None, "", {}
                ok(nf == 0 and xs == list(range(12)),
                   f"P35-D1 a process death inside a dask chunk is healed in the chunk: {r3.stdout[-300:]} {r3.stderr[-300:]}")
                ok("same model error" in stop2 and kinds2.get("stopped", 0) > 0,
                   f"P35-D2 a repeated model error stops a dask call: {stop2} {kinds2}")

            except Exception:
                print(open(os.path.join(iso, "sched.err")).read()[-3000:])
                raise
            finally:
                os.environ.pop("MA_COMPUTE_SCHEDULER", None)
                for pr in procs_:
                    pr.terminate()
                for pr in procs_:
                    pr.wait(10)
    _self_test_p35(ok)
    _self_test_p37(ok)
    _self_test_p42(ok)
    _self_test_p45d(ok)
    _self_test_sched(ok)
    try:
        _self_test_wall(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"SW: {type(e).__name__}: {e}")
    try:
        _self_test_xhost(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"XH: {type(e).__name__}: {e}")
    try:
        _self_test_infra(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"IR: {type(e).__name__}: {e}")
    try:
        _self_test_s18(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"RU: {type(e).__name__}: {e}")
    try:
        _self_test_signoff_reuse(ok)
    except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
        ok(False, f"RS: {type(e).__name__}: {e}")
    # CF-RW3 (round 4, P1-2): a batch's results, summary and journal never follow a planted link or .tmp
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as out_:
        os.makedirs(os.path.join(rd, "models"))
        open(os.path.join(rd, "models", "m.py"), "w").write("def f(p):\n    return {'y': p['x']}\n")
        victim = os.path.join(out_, "v")
        open(victim, "w").write("untouched")
        o = os.path.join(rd, "models", "r.jsonl")
        for suffix in (".tmp", ".summary.json.tmp", PARTIAL_EXT + ".tmp"):
            os.symlink(victim, o + suffix)
        try:
            run_map(rd, "models/m.py::f", [{"x": 1}], o, backend="local", max_workers=1)
        except (ValueError, OSError):
            pass
        ok(open(victim).read() == "untouched", "CF-RW3 run_map writes nothing through a planted .tmp link")
    # CF-RW1/2 (run console review round 3, P1-3): a confined fingerprint reads through no-follow fds and equals the
    # default one on a tree with no links; the default is unchanged
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as out:
        os.makedirs(os.path.join(rd, "models"))
        os.makedirs(os.path.join(rd, "data"))
        open(os.path.join(rd, "models", "m.py"), "w").write("def f(x):\n    return x\n")
        open(os.path.join(rd, "data", "d.csv"), "w").write("a\n1\n")
        try:
            same = code_fingerprint(rd, "models/m.py::f") == code_fingerprint(rd, "models/m.py::f", confined=True)
        except TypeError as e:
            same = e
        ok(same is True, f"CF-RW1 the confined fingerprint equals the default on a tree with no links ({same})")
        os.makedirs(os.path.join(out, "models"))
        open(os.path.join(out, "models", "m.py"), "w").write("SECRET = 1\n")
        import shutil
        shutil.rmtree(os.path.join(rd, "models"))
        os.symlink(os.path.join(out, "models"), os.path.join(rd, "models"))
        try:
            code_fingerprint(rd, "models/m.py::f", confined=True)
            r = "not refused"
        except ValueError:
            r = "refused"
        except TypeError as e:
            r = e
        ok(r == "refused" and isinstance(code_fingerprint(rd, "models/m.py::f"), str),
           f"CF-RW2 a linked models/ is refused by the confined fingerprint; the default still follows it ({r})")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_s18(ok) -> None:
    """F6 S1-8: the fingerprint covers code content only. The launch commit stamp, harness-written analysis outputs
    under models/ and the spec do not change it; code, data and any unknown file under models/ still do; a stored
    batch is found again from another tree, and `reuse_report` says how many stored rows would be reused."""
    import re as _re
    import shutil
    import subprocess as _sp
    import tempfile
    from unittest import mock
    g = globals()
    rep = g.get("reuse_report") or (lambda rd: {})
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        repo_a, rd = os.path.join(td, "repo_a"), os.path.join(td, "run")

        def w(root, rel, txt, mode="w"):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            open(os.path.join(root, rel), mode).write(txt)
        w(repo_a, "scripts/lib/code_commit.txt", "0" * 40 + "\n")
        w(repo_a, "scripts/lib/helper.py", "X = 1\n")
        w(repo_a, "toolkit/eng/m.py", "def f(p):\n    return {'y': 2 * p['x']}\n")
        w(rd, "models/fn.py", "def f(p):\n    return {'y': p['x']}\n")
        w(rd, "data/inputs/units.csv", "u,a\n1,2\n")
        with mock.patch.dict(g, {"REPO": repo_a}):
            def fp():
                return code_fingerprint(rd, "models/fn.py::f")
            fp0 = fp()
            w(repo_a, "scripts/lib/code_commit.txt", "1" * 40 + "\n")
            ok(fp() == fp0, "RU1 a new launch commit (scripts/lib/code_commit.txt, export-subst) keeps the fingerprint")
            w(repo_a, "scripts/lib/helper.py", "X = 2\n")
            fp1 = fp()
            ok(fp1 != fp0, "RU2 a change to repo code still changes the fingerprint")
            w(rd, "decision_spec.yaml", "spec: 1\n")       # a v2 run: its spec at the run root
            fp1 = fp()
            for rel in ("models/allocation_robustness.yaml", "models/claims_spec.yaml", "models/claims_ledger.yaml",
                        "decision_spec.yaml", "analysis/point.json"):
                w(rd, rel, "written: 1\n")
            ok(fp() == fp1, "RU3 a v2 run's fixed harness outputs under models/ and the spec keep the fingerprint")
            for rel in ("models/decision_tests/DT1.yaml", "models/decision_tests/custom.yaml", "data/input_checks.json"):
                w(rd, rel, "written: 2\n")
                fpx = fp()
                ok(fpx != fp1, f"RU3b (Codex P2-9) no wildcard: {rel} is hashed")
                fp1 = fpx
            for rel in ("models/notes.yaml", "models/decision_tests/helper.py", "models/ext/claims_spec.yaml"):
                w(rd, rel, "x = 1\n")
                fpx = fp()
                ok(fpx != fp1, f"RU4 fail closed: an unknown file under models/ ({rel}) still changes the fingerprint")
                fp1 = fpx
            w(rd, "data/inputs/units.csv", "u,a\n1,3\n")
            ok(fp() != fp1, "RU5 a changed input under data/ still changes the fingerprint")
            out = os.path.join(rd, "models", "batch.jsonl")
            _, s1 = run_map(rd, os.path.join(repo_a, "toolkit", "eng", "m.py") + "::f", [{"x": i} for i in range(3)],
                            out, backend="local", max_workers=1, ledger=False)
            ok(s1.get("computed") == 3, f"RU6 setup: the first batch computes 3 rows ({s1.get('computed')})")
            r = rep(rd)
            ok(r.get("stored") == 3 and r.get("eligible") == 3,
               f"RU7 reuse_report: every stored row would be reused with unchanged code ({r})")
        # each launch extracts its code to a new temporary directory, and a resume tree is a copy of the run
        repo_b, rd_b = os.path.join(td, "repo_b"), os.path.join(td, "run_b")
        shutil.copytree(repo_a, repo_b)
        shutil.copytree(rd, rd_b)
        w(repo_b, "scripts/lib/code_commit.txt", "2" * 40 + "\n")
        with mock.patch.dict(g, {"REPO": repo_b}):
            r = rep(rd_b)
            ok(r.get("stored") == 3 and r.get("eligible") == 3,
               f"RU8 another tree at another commit, the run dir moved: every stored row would be reused ({r})")
            _, s2 = run_map(rd_b, os.path.join(repo_b, "toolkit", "eng", "m.py") + "::f", [{"x": i} for i in range(4)],
                            os.path.join(rd_b, "models", "batch.jsonl"), backend="local", max_workers=1, ledger=False)
            ok(s2.get("computed") == 1 and s2.get("reused") == 3,
               f"RU9 run_map from another tree reuses the stored rows and computes only the new one: "
               f"{s2.get('computed')} computed, {s2.get('reused')} reused")
            w(repo_b, "toolkit/eng/m.py", "# changed\n", "a")
            r = rep(rd_b)
            ok(r.get("stored") == 4 and r.get("eligible") == 0 and all(b.get("why") for b in r.get("batches") or [{}]),
               f"RU10 changed engine code: nothing would be reused, and the report says why ({r})")
        # the CLI a launcher runs locally, from the tree it would ship
        rd_c = os.path.join(td, "run_c")
        w(rd_c, "models/fn.py", "def f(p):\n    return {'y': p['x']}\n")
        run_map(rd_c, "models/fn.py::f", [{"x": 1}, {"x": 2}], os.path.join(rd_c, "models", "b.jsonl"),
                backend="local", max_workers=1, ledger=False)
        cmd = [sys.executable, os.path.abspath(__file__), "reuse-check", rd_c]
        cli = _sp.run(cmd, capture_output=True, text=True, timeout=120)
        try:
            got = json.loads(cli.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            got = {}
        ok(cli.returncode == 0 and got.get("stored") == 2 and got.get("eligible") == 2 and "identical inputs" in got.get("note", ""),
           f"RU11 `compute.py reuse-check <run_dir>` prints the counts as JSON: rc={cli.returncode} {got} "
           f"{cli.stderr[-200:]}")
        w(rd_c, "models/fn.py", "# changed\n", "a")
        cli = _sp.run(cmd + ["--expect-all"], capture_output=True, text=True, timeout=120)
        ok(cli.returncode == 1, f"RU12 `reuse-check --expect-all` exits 1 when a stored row would be recomputed "
                                f"(rc={cli.returncode})")
    # Codex P2-9: exact paths only, each one a real harness output; a v1 run hashes everything
    hd = tuple(g.get("FP_HARNESS_OUTPUTS", ()))
    src = "".join(open(os.path.join(REPO, "toolkit", "analysis", f)).read()
                  for f in sorted(os.listdir(os.path.join(REPO, "toolkit", "analysis"))) if f.endswith(".py"))
    ok(hd and not g.get("FP_HARNESS_DIRS") and all("*" not in x and x.startswith("models/") and x in src for x in hd),
       f"RU13 the harness-output list is exact paths that toolkit/analysis writes, no wildcard: {hd}")
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        repo, rd = os.path.join(td, "repo"), os.path.join(td, "run")
        for root, rel, txt in ((repo, "scripts/x.py", "X = 1\n"), (repo, "requirements.txt", "starsim==3.0.0\n"),
                               (rd, "models/fn.py", "def f(p):\n    return {'y': open('models/claims_ledger.yaml').read()}\n"),
                               (rd, "models/claims_ledger.yaml", "a\n")):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            open(os.path.join(root, rel), "w").write(txt)
        with mock.patch.dict(g, {"REPO": repo}):
            fp0 = code_fingerprint(rd, "models/fn.py::f")
            open(os.path.join(rd, "models", "claims_ledger.yaml"), "w").write("b\n")
            fp1 = code_fingerprint(rd, "models/fn.py::f")
            ok(fp1 != fp0, "RU14 (Codex P2-9) a v1 function reading models/claims_ledger.yaml: the file is hashed")
            out = os.path.join(rd, "models", "o.jsonl")
            cwd = os.getcwd()
            os.chdir(rd)
            try:
                run_map(rd, "models/fn.py::f", [{"x": 1}], out, backend="local", max_workers=1, ledger=False)
                open(os.path.join(rd, "models", "claims_ledger.yaml"), "w").write("c\n")
                rows, s3 = run_map(rd, "models/fn.py::f", [{"x": 1}], out, backend="local", max_workers=1, ledger=False)
            finally:
                os.chdir(cwd)
            ok(s3.get("computed") == 1, f"RU14b the v1 batch reading that file is recomputed after it changes: {s3.get('computed')}")
            fp2 = code_fingerprint(rd, "models/fn.py::f")
            open(os.path.join(repo, "requirements.txt"), "w").write("starsim==3.1.0\n")
            ok(code_fingerprint(rd, "models/fn.py::f") != fp2,
               "RU15 (Codex P2-8) a dependency-only change (requirements.txt) changes the fingerprint")
            summ = json.load(open(out + ".summary.json"))
            ok((summ.get("runtime") or {}).get("python") == ".".join(map(str, sys.version_info[:3])),
               f"RU16 (Codex P2-8) a batch records the runtime versions it ran under: {summ.get('runtime')}")


def _engine_batch_reads(names, sources=None) -> list[str]:
    """Which engine-batch job modules mention a sign-off record (by path, file name or the module that owns it).
    `sources`: {label: text}; default, every module the v2 engine-batch functions run on a worker
    (toolkit/engines/**, toolkit/analysis/batch.py and trajectories.py)."""
    if sources is None:
        sources = {}
        roots = [os.path.join(REPO, "toolkit", "engines")]
        files = [os.path.join(REPO, "toolkit", "analysis", f) for f in ("batch.py", "trajectories.py")]
        for r in roots:
            for dp, _, fns in os.walk(r):
                files += [os.path.join(dp, f) for f in fns if f.endswith(".py")]
        sources = {os.path.relpath(f, REPO): open(f, encoding="utf-8").read() for f in sorted(files)}
    hits = []
    for label, text in sources.items():
        for n in names:
            base = os.path.basename(n)
            needles = (n, base, os.path.splitext(base)[0], "spec import geometry", "spec.geometry", "SIGNOFF_FILE")
            hits += [f"{label}: {x}" for x in needles if x in text]
    return hits


def _self_test_signoff_reuse(ok) -> None:
    """RS1-RS5 (F6 Stage 2): a lead-signed preview and the owner-signed final run of one spec differ only in their
    sign-off records; the engine-batch reuse fingerprint ignores exactly those (FP_SIGNOFF_RECORDS) in a v2 run."""
    import tempfile
    from unittest import mock
    g = globals()
    recs = tuple(g.get("FP_SIGNOFF_RECORDS", ()))
    ok(recs == ("data/geometry_signoff.json", "data/input_checks.json") and all("*" not in r and "?" not in r for r in recs),
       f"RS0 the sign-off exclusion is an exact, named list (no wildcard): {recs}")
    with tempfile.TemporaryDirectory() as td:
        td = os.path.realpath(td)
        repo = os.path.join(td, "repo")

        def w(root, rel, txt):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            open(os.path.join(root, rel), "w").write(txt)
        w(repo, "toolkit/eng/m.py", "def f(p):\n    return p\n")
        runs = {}
        for who in ("lead", "owner"):
            rd = os.path.join(td, f"run_{who}")
            w(rd, "decision_spec.yaml", "spec: 1\n")
            w(rd, "metadata.json", json.dumps({"pipeline": "v2"}))
            # Codex r1 P2-1: geometry.check copies the signer into the report it writes to data/input_checks.json
            w(rd, "data/input_checks.json", json.dumps({"geometry": {"signed_by": who, "findings": []}}))
            w(rd, "models/fn.py", "def f(p):\n    return p\n")
            w(rd, "data/inputs/units.csv", "u,a\n1,2\n")
            w(rd, "data/derived/rates.csv", "u,r\n1,0.5\n")
            w(rd, "data/geometry_signoff.json", json.dumps(
                {"signed_by": who, "at": f"2026-11-1{len(who)}T00:00:00Z", "declaration": {"enabled": False},
                 "approvals": {"x": {"approved_by": "owner"}} if who == "owner" else {}}))
            runs[who] = rd
        with mock.patch.dict(g, {"REPO": repo}):
            fl, fo = (code_fingerprint(runs[k], "models/fn.py::f") for k in ("lead", "owner"))
            ok(fl == fo, "RS1 lead- and owner-signed geometry records: same reuse fingerprint")
            ok(code_fingerprint(runs["owner"], "models/fn.py::f", confined=True) == fo,
               "RS1b the confined fingerprint ignores the same record")
            for rel in ("data/derived/rates.csv", "data/inputs/units.csv", "data/derivations_record.json",
                        "data/geometry_signoff.json.extra"):
                w(runs["owner"], rel, "changed\n")
                fx = code_fingerprint(runs["owner"], "models/fn.py::f")
                ok(fx != fo, f"RS2 any other file under data/ ({rel}) still changes the fingerprint")
                fo = fx
            # Codex r1 P2-2: a spec alone is not a v2 run; metadata.json must record pipeline v2
            w(runs["lead"], "metadata.json", json.dumps({"pipeline": "v1"}))
            f0 = code_fingerprint(runs["lead"], "models/fn.py::f")
            w(runs["lead"], "data/geometry_signoff.json", '{"signed_by": "someone else"}')
            ok(code_fingerprint(runs["lead"], "models/fn.py::f") != f0,
               "RS3b a run whose metadata records pipeline v1 hashes the record even with a decision_spec.yaml")
            w(runs["lead"], "metadata.json", json.dumps({"pipeline": "v2"}))
            os.remove(os.path.join(runs["lead"], "decision_spec.yaml"))     # a v1 run hashes everything
            f1 = code_fingerprint(runs["lead"], "models/fn.py::f")
            w(runs["lead"], "data/geometry_signoff.json", '{"signed_by": "owner"}')
            ok(code_fingerprint(runs["lead"], "models/fn.py::f") != f1,
               "RS3 a v1 run (no decision_spec.yaml): its own model may read the record, so it is hashed")
    hits = _engine_batch_reads(recs)
    ok(recs and not hits, f"RS4 no engine-batch job module reads an excluded sign-off record: {hits}")
    ok(bool(_engine_batch_reads(recs, {"planted.py": "json.load(open('data/geometry_signoff.json'))"})),
       "RS5 the RS4 scan finds a planted read of an excluded record (it is not vacuous)")


def _self_test_wall(ok) -> None:
    """SW1-SW5 (Nigeria v2 full run, 2026-11-02): the engine batch step's wall-clock limit, and the partial rows of a
    batch killed in a pipeline step staying reusable after the runner records the failure."""
    import tempfile
    cl = {"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": "8", "MA_COMPUTE_MAX_CORE_HOURS": "150"}
    ok(step_wall_s({}) is None and step_wall_s({"MA_COMPUTE_MAX_CORE_HOURS": "150"}) is None,
       "SW1 without a cluster there is no derived limit (the runner's default applies)")
    ok(step_wall_s(cl) == 20475 and step_wall_s(dict(cl, MA_COMPUTE_PROCS_PER_WORKER="4")) == 2 * 150 * 3600 / 32 + 3600,
       f"SW2 the run cap on the target pool, twice, plus an hour: {step_wall_s(cl)}")
    small = dict(cl, MA_COMPUTE_MAX_CORE_HOURS="20")
    ok(step_wall_s(dict(small, MA_COMPUTE_TARGET_WORKERS="")) == 2 * 20 * 3600 / 8 + 3600 > step_wall_s(small)
       and step_wall_s(dict(cl, MA_COMPUTE_MAX_CORE_HOURS="1e6")) == 24 * 3600,
       "SW3 an unknown pool counts as one worker; the limit has a 24-h ceiling")
    bad = []
    for k, v in (("MA_COMPUTE_MAX_CORE_HOURS", "lots"), ("MA_COMPUTE_MAX_CORE_HOURS", "-1"), ("MA_COMPUTE_MAX_CORE_HOURS", "inf"),
                 ("MA_COMPUTE_TARGET_WORKERS", "8.5"), ("MA_COMPUTE_PROCS_PER_WORKER", "0")):
        try:
            step_wall_s(dict(cl, **{k: v}))
            bad.append(f"{k}={v}")
        except ValueError:
            pass
    ok(not bad, f"SW4 unreadable budget values are refused (fail closed): accepted {bad}")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "fn.py"), "w").write("def f(p):\n    return p\n")
        fp0 = code_fingerprint(d, "models/fn.py::f")
        for rel in ("pipeline.yaml", "pipeline_state.json", "pipeline_report.json"):
            open(os.path.join(d, "models", rel), "w").write('{"steps": {"analyze": {"ok": false}}}\n')
        fp1 = code_fingerprint(d, "models/fn.py::f")
        open(os.path.join(d, "models", "helper.py"), "w").write("X = 1\n")
        ok(fp1 == fp0 and code_fingerprint(d, "models/fn.py::f") != fp1,
           "SW5 the pipeline runner's own definition and records are not part of a batch's fingerprint; model code is")


def _self_test_xhost(ok) -> None:
    """XH1-XH7 (Nigeria v2 full run, 2026-11-02): a caller on another host killed
    without settling (the analyze step's timeout) left a 120 core-hour
    reservation that a resumed run on a new cluster held forever, and its
    progress path pointed into the old checkout."""
    global PROGRESS_HEARTBEAT_S
    import subprocess as _sp
    import tempfile
    import threading
    now = time.time()
    other = "coiled-dask-old-worker"
    out = [{"n": 32, "share": 5000.0, "started": now - 4000, "cores": 8},
           {"n": 32, "share": 7000.0, "started": now - 3000, "cores": 8}]
    bound = 5000.0 + 7000.0 + 2 * 8 * SHARE_STOP_GRACE_S

    def rec(age, **kw):
        return {"t": now - age, "core_s": 1000.0, "done_tasks": 184, "max_task_core_s": 10.0,
                "remote_unconfirmed": False, "out": out, "call_id": "c-old", **kw}

    def setup(d, record, rel=None, old_root=None, **res):
        """A run dir whose ledger holds one reservation from `other`; the record
        is written at `.compute_progress/abc.json` (None: not written; str: raw)."""
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        pdir = os.path.join(d, PROGRESS_DIR)
        os.makedirs(pdir, exist_ok=True)
        pf = os.path.join(pdir, "abc.json")
        if record is not None:
            open(pf, "w").write(record if isinstance(record, str) else json.dumps(record))
        ref = rel if rel is not None else (os.path.join(old_root, PROGRESS_DIR, "abc.json") if old_root else pf)
        led = {"core_seconds": 0.0, "calls": [], "reservations": {"x": {
            "core_s": 432000.0, "pid": 1941, "pid_start": "proc:24313", "host": other,
            "fn": "/old/toolkit/analysis/batch.py::run_point", "backend": "dask", "n": 256,
            "progress": ref, "started": now - 5000, **res}}}
        return led, pf

    def settle(d, led):
        try:
            return _live_reservations(led, os.path.realpath(d), now=now), None
        except Exception as e:  # noqa: BLE001 -- before the change these cases fail here
            return None, f"{type(e).__name__}: {e}"

    # XH1 a stale cross-host record (heartbeat recorded, not refreshed past the
    # bound) is settled from its record: work done + its chunks' shares (they
    # cannot be cancelled from here), not the reservation; nothing is signalled here.
    with tempfile.TemporaryDirectory() as d:
        bystander = _sp.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            time.sleep(0.2)
            led, pf = setup(d, rec(PROGRESS_STALE_S + 60, hb_s=PROGRESS_HEARTBEAT_S,
                                   pids=[[bystander.pid, _pid_start(bystander.pid)]], pids_complete=True))
            held, err = settle(d, led)
            c = (led["calls"] or [{}])[0]
            ok(err is None and held == 0.0 and not led["reservations"]
               and abs(led["core_seconds"] - (1000.0 + bound)) < 1e-6 and "another host" in str(c.get("stopped"))
               and "charged work done" in str(c.get("stopped")) and not os.path.exists(pf)
               and bystander.poll() is None,
               f"XH1 a stale cross-host reservation is charged its record ({1000.0 + bound:.0f} core-s), "
               f"not held: held={held} err={err} led={led}")
        finally:
            bystander.kill()
            bystander.wait()
    # XH2 a fresh cross-host record stays held, untouched.
    with tempfile.TemporaryDirectory() as d:
        led, pf = setup(d, rec(30, hb_s=PROGRESS_HEARTBEAT_S))
        held, err = settle(d, led)
        ok(err is None and held == 432000.0 and "x" in led["reservations"] and not led["calls"]
           and os.path.exists(pf), f"XH2 a fresh cross-host reservation stays held: held={held} err={err} {led}")
    # XH3 a record without a heartbeat (written before it existed), in a run dir
    # that has not moved, cannot tell a long task from a dead caller: held
    # within the 24-h bound, settled past it.
    with tempfile.TemporaryDirectory() as d:
        led, pf = setup(d, rec(PROGRESS_STALE_S + 60))
        held, err = settle(d, led)
        ok(err is None and held == 432000.0 and "x" in led["reservations"],
           f"XH3a a legacy record not moved is held within the bound: held={held} err={err}")
        led, pf = setup(d, rec(PROGRESS_STALE_LEGACY_S + 60))
        held, err = settle(d, led)
        ok(err is None and held == 0.0 and abs(led["core_seconds"] - (1000.0 + bound)) < 1e-6,
           f"XH3b a legacy record past the 24-h bound is settled from its record: held={held} err={err} {led}")
    # XH4 the run dir moved (resumed on a new cluster, another checkout): the old
    # absolute path is resolved by its `.compute_progress/<name>` suffix in this run
    # dir; a legacy record that can no longer be refreshed here is settled once stale.
    with tempfile.TemporaryDirectory() as d:
        led, pf = setup(d, rec(PROGRESS_STALE_S + 60), old_root="/tmp/ab/F1/runs/R")
        held, err = settle(d, led)
        c = (led["calls"] or [{}])[0]
        ok(err is None and held == 0.0 and abs(led["core_seconds"] - (1000.0 + bound)) < 1e-6
           and c.get("done_core_s") == 1000.0 and not os.path.exists(pf),
           f"XH4 a moved run dir resolves the old progress path and settles from it: held={held} err={err} {led}")
        led, pf = setup(d, rec(60), old_root="/tmp/ab/F1/runs/R")
        held, err = settle(d, led)
        ok(err is None and held == 432000.0, f"XH4b a moved record still fresh is held: held={held} err={err}")
        # The same suffix resolution for a same-host caller that died.
        led, pf = setup(d, rec(60, hb_s=PROGRESS_HEARTBEAT_S), old_root="/tmp/ab/F1/runs/R",
                        host=os.uname().nodename, pid=2 ** 22 + 7, pid_start=None)
        led["reservations"]["x"]["backend"] = "dask"
        os.environ.pop("MA_COMPUTE_SCHEDULER", None)
        held, err = settle(d, led)
        ok(err is None and held == 0.0 and abs(led["core_seconds"] - (1000.0 + bound)) < 1e-6,
           f"XH4c a dead same-host caller's old absolute path resolves too: held={held} err={err} {led}")
    # XH5 fail closed: an unreadable (or missing) record from another host is held
    # within the 24-h bound from its reservation, then charged in full.
    for content, what in (("{not json", "unreadable"), (None, "missing")):
        with tempfile.TemporaryDirectory() as d:
            led, _ = setup(d, content)
            held, err = settle(d, led)
            ok(err is None and held == 432000.0, f"XH5a {what} record, reserved 1.4 h ago: held: {held} {err}")
            led, _ = setup(d, content, started=now - PROGRESS_STALE_LEGACY_S - 60)
            held, err = settle(d, led)
            c = (led["calls"] or [{}])[0]
            ok(err is None and held == 0.0 and led["core_seconds"] == 432000.0
               and "charged its reservation" in str(c.get("stopped")),
               f"XH5b {what} record past the bound: charged in full: held={held} err={err} {led}")
    # XH6 local work in flight on another host (no share, cannot be stopped from
    # here) charges the reservation.
    with tempfile.TemporaryDirectory() as d:
        led, _ = setup(d, rec(PROGRESS_STALE_S + 60, hb_s=PROGRESS_HEARTBEAT_S,
                              out=[{"n": 1, "started": now - 100, "cores": 1}], pids=[[1, "proc:1"]],
                              pids_complete=True), backend="local")
        held, err = settle(d, led)
        ok(err is None and held == 0.0 and led["core_seconds"] == 432000.0,
           f"XH6 cross-host local work in flight: the reservation is charged: held={held} err={err} {led}")
    # XH7 a running call records its progress path relative to the run dir and
    # refreshes the record's heartbeat while a task runs longer than it.
    saved = PROGRESS_HEARTBEAT_S
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "nap.py"), "w").write(
            "import time\ndef nap(p):\n    time.sleep(p['s']); return {'ok': 1}\n")
        seen = {}
        try:
            PROGRESS_HEARTBEAT_S = 0.2
            th = threading.Thread(target=lambda: map_fn(d, "models/nap.py::nap", [{"s": 2.0}], backend="local",
                                                        max_workers=1, max_core_hours=0.1))
            th.start()
            end = time.time() + 20
            while time.time() < end and th.is_alive():
                try:
                    r = next(iter(json.load(open(os.path.join(d, LEDGER_REL)))["reservations"].values()))
                    p = json.load(open(os.path.join(d, r["progress"])))
                    seen.setdefault("ref", r["progress"])
                    seen.setdefault("t", []).append(p["t"])
                    seen["hb"] = p.get("hb_s")
                except (OSError, ValueError, KeyError, StopIteration, TypeError):
                    pass
                time.sleep(0.1)
            th.join(30)
        finally:
            PROGRESS_HEARTBEAT_S = saved
        ts = sorted(set(seen.get("t", [])))
        ok(seen.get("ref") == os.path.join(PROGRESS_DIR, os.path.basename(str(seen.get("ref"))))
           and seen.get("hb") == 0.2 and len(ts) >= 4,
           f"XH7 the progress path is relative and the record is refreshed during a 2-s task: {seen.get('ref')} "
           f"hb={seen.get('hb')} {len(ts)} distinct t")


def _self_test_sched(ok) -> None:
    """Scheduler reads (PR #99's race, carried to compute.py; no network). distributed's
    Client.scheduler_info() returns {} (no "workers" key) whenever the client is not
    connected: before its first identity reply, while it reconnects, after a failed
    reconnect. Every read of the worker table here goes through worker_side's bounded
    reader: an unpopulated identity is waited out, and past the bound the call fails
    with SchedulerInfoUnavailable. It is never read as "no workers"."""
    import tempfile
    from unittest import mock

    class Stop(Exception):
        pass

    class FakeClient:
        def __init__(self, infos, status="running", stop_at_run=True):
            self.infos, self.status, self.ran, self.stop_at_run = list(infos), status, [], stop_at_run

        def __call__(self, *a, **kw):           # distributed.Client(addr, ...) returns this fake
            return self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def scheduler_info(self):
            return self.infos.pop(0) if len(self.infos) > 1 else self.infos[0]

        def upload_file(self, *a, **kw):
            pass

        def run(self, fn, *a, workers=None, **kw):
            self.ran.append(list(workers or []))
            if self.stop_at_run:
                raise Stop()
            return {w: 1 for w in workers or []}

    table = {"tls://10.0.0.5:1": {"resources": {}}, "tls://10.0.0.6:1": {"resources": {}},
             "tls://10.0.0.9:1": {"resources": {}}}
    two = ["tls://10.0.0.5:1", "tls://10.0.0.6:1"]
    wsm = _worker_side()
    sleeps = []
    env = {"MA_COMPUTE_SCHEDULER": "tls://sched:8786", "MA_COMPUTE_EXCLUDE_WORKERS": "tls://10.0.0.9:1"}
    with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env), \
            mock.patch.object(time, "sleep", sleeps.append):
        os.environ.pop("MA_COMPUTE_RESOURCE", None)
        for sub in ("models", "data"):
            os.makedirs(os.path.join(d, sub))
        # SR1 dispatch: two empty identities, then the table; the call goes on to the
        # table's workers (less the excluded run worker), after a doubling backoff.
        c = FakeClient([{}, {"id": "s"}, {"workers": table}])
        with mock.patch("distributed.Client", c):
            try:
                _map_dask(d, "models/fn.py::f", [{"x": 1}], 0)
                ok(False, "SR1 dispatch waits out an unpopulated scheduler identity")
            except Stop:
                ok(c.ran == [two] and len(sleeps) == 2 and sleeps[1] == 2 * sleeps[0],
                   f"SR1 dispatch waits out an unpopulated identity, then uses the table's workers: {c.ran} {sleeps}")
            except Exception as e:  # noqa: BLE001
                ok(False, f"SR1 dispatch waits out an unpopulated scheduler identity: {type(e).__name__}: {e}")
        # SR2 an identity that never populates fails the call, naming why; nothing is dispatched.
        sleeps.clear()
        c = FakeClient([{}], status="connecting")
        with mock.patch("distributed.Client", c):
            try:
                _map_dask(d, "models/fn.py::f", [{"x": 1}], 0)
                ok(False, "SR2 an identity that never populates fails the call")
            except wsm.SchedulerInfoUnavailable as e:
                ok(not c.ran and len(sleeps) == SCHED_ATTEMPTS - 1 and "no worker table" in str(e),
                   f"SR2 an identity that never populates fails the call after the bound, naming why: {e} {sleeps}")
            except Exception as e:  # noqa: BLE001
                ok(False, f"SR2 an unpopulated identity raises SchedulerInfoUnavailable, not {type(e).__name__}: {e}")
        # SR3 an empty table read from a connected scheduler is a real "no workers": refused as before.
        c = FakeClient([{"workers": {}}])
        with mock.patch("distributed.Client", c):
            try:
                _map_dask(d, "models/fn.py::f", [{"x": 1}], 0)
                ok(False, "SR3 a connected scheduler with no compute worker is refused")
            except RuntimeError as e:
                ok("no compute workers" in str(e) and not isinstance(e, wsm.SchedulerInfoUnavailable),
                   f"SR3 a connected scheduler with no compute worker is refused as before: {e}")
        # SR4 the remote cancel (settle and orphan paths) reads the table the same way.
        sleeps.clear()
        c = FakeClient([{}, {"workers": table}], stop_at_run=False)
        try:
            got = _cancel_remote(c, "call1", {"tls://10.0.0.9:1"})
        except Exception as e:  # noqa: BLE001
            got = f"{type(e).__name__}: {e}"
        ok(c.ran == [two] and got == {w: 1 for w in two} and len(sleeps) == 1,
           f"SR4 the remote cancel waits out an unpopulated identity, then reaches every worker: {c.ran} {got}")
        c = FakeClient([{}], stop_at_run=False)
        try:
            _cancel_remote(c, "call1", set())
            ok(False, "SR4b a cancel with no worker table raises instead of reaching no worker")
        except wsm.SchedulerInfoUnavailable:
            ok(not c.ran, "SR4b a cancel with no worker table raises instead of reaching no worker")
        except Exception as e:  # noqa: BLE001
            ok(False, f"SR4b a cancel with no worker table raises SchedulerInfoUnavailable, not {type(e).__name__}")
        p = {"call_id": "call1"}
        c = FakeClient([{}], stop_at_run=False)
        with mock.patch("distributed.Client", c):
            done, how = _stop_orphans(p, "dask")
        ok(not done and "SchedulerInfoUnavailable" in how and not c.ran,
           f"SR5 an orphan's cancel with no worker table is unconfirmed, naming why: {how}")
        c = FakeClient([{}, {"workers": table}], stop_at_run=False)
        with mock.patch("distributed.Client", c):
            done, how = _stop_orphans(p, "dask")
        ok(done and c.ran == [two], f"SR5b an orphan's cancel recovers once the table appears: {how} {c.ran}")
        # SR6 the vanished-worker check: an unpopulated identity is not "every worker
        # vanished"; past the bound it raises (the caller then charges every worker as busy).
        try:
            gone = _gone_workers(FakeClient([{}, {"workers": {"tls://10.0.0.5:1": {}}}]), two)
        except Exception as e:  # noqa: BLE001
            gone = f"{type(e).__name__}"
        ok(gone == ["tls://10.0.0.6:1"], f"SR6 the vanished-worker check waits out an unpopulated identity: {gone}")
        try:
            _gone_workers(FakeClient([{}]), two)
            ok(False, "SR6b a vanished-worker check with no table raises")
        except wsm.SchedulerInfoUnavailable:
            ok(True, "SR6b a vanished-worker check with no table raises")
        except Exception as e:  # noqa: BLE001
            ok(False, f"SR6b a vanished-worker check with no table raises SchedulerInfoUnavailable, not {type(e).__name__}")
    ok(_worker_side() is wsm and os.path.samefile(wsm.__file__, os.path.join(REPO, "cloud", "worker_side.py")),
       "SR7 the reader is the repo's cloud/worker_side.py, loaded once by path (a models/worker_side.py cannot shadow it)")


def _self_test_p45d(ok) -> None:
    """Phase 45 δ (A16): the chunk loop places chunks on free workers itself.

    E7 attempt 8 is emulated on a LocalCluster: an untagged run worker holds a
    task the scheduler expects to take 68 hours (set through
    `default-task-durations`, as the measured average of attempt 8's 34-hour
    pipeline task did), so the cluster's mean occupancy dwarfs any compute
    worker's queue and work stealing never moves chunks. Every wait is on an
    event (a file the model writes, a scheduler state), never a fixed sleep."""
    global _remote_task
    import tempfile
    try:
        import dask
        from distributed import Client, LocalCluster, WorkerPlugin
    except ImportError:
        print("  (P45D skipped: distributed not installed)")
        return

    class OwnTmp(WorkerPlugin):
        """_remote_task holds one machine-wide lock (one chunk per VM). Local
        workers share a machine, so each gets its own temp dir, as a VM has."""
        name = "ma-selftest-own-tmp"

        def __init__(self, base):
            self.base = base

        def setup(self, worker):
            import tempfile as tf_
            d_ = os.path.join(self.base, worker.address.rsplit(":", 1)[-1])
            os.makedirs(d_, exist_ok=True)
            os.environ["TMPDIR"] = d_
            tf_.tempdir = d_

    def state(dask_scheduler=None):
        s_ = dask_scheduler
        return {"unrunnable": sum(1 for ts in s_.unrunnable if ts.prefix.name == "remote_task"),
                "proc": {a: sum(1 for ts in ws.processing if ts.prefix.name == "remote_task")
                         for a, ws in s_.workers.items()},
                "target": s_.adaptive_target("20s"), "occ": s_.total_occupancy,
                "bundle": sorted({ws.address for ts in s_.tasks.values() if ts.prefix.name == "bytes"
                                  for ws in (ts.who_has or ())}),
                "running": {a: getattr(ws.status, "name", "") == "running" for a, ws in s_.workers.items()},
                "tagged": sorted(a for a, ws in s_.workers.items() if "compute" in (ws.resources or {})),
                "nproc": {a: len(ws.processing) for a, ws in s_.workers.items()},
                "bundle_keys": sorted(k for k, ts in s_.tasks.items() if ts.prefix.name == "bytes" and ts.who_has)}

    def idle_thread(dask_worker=None):
        return not dask_worker.state.executing

    def pause(dask_worker=None):
        """The worker stops taking new tasks, as a VM being retired does
        (closing_gracefully: unlike `paused`, the memory monitor does not undo it)."""
        from distributed.core import Status
        dask_worker.status = Status.closing_gracefully

    def unpause(dask_worker=None):
        from distributed.core import Status
        dask_worker.status = Status.running

    def die():                                     # after replying: the worker process exits
        import os as os_
        import threading as th_
        th_.Timer(0.2, os_._exit, (1,)).start()

    def kill(cc_, addrs):
        """Stop the workers taking work first, so no retry lands on a worker
        about to die; then end their processes."""
        cc_.run(pause, workers=sorted(addrs))
        until(lambda: not any(cc_.run_on_scheduler(state)["running"].get(a) for a in addrs), 30)
        cc_.run(die, workers=sorted(addrs))
        until(lambda: not set(addrs) & set(cc_.run_on_scheduler(state)["proc"]), 30)

    def until(pred, limit=60.0):
        end = time.time() + limit
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        return None

    keys = ("MA_COMPUTE_SCHEDULER", "MA_COMPUTE_RESOURCE", "MA_COMPUTE_TARGET_WORKERS", "MA_COMPUTE_PROCS_PER_WORKER",
            "MA_COMPUTE_EXCLUDE_WORKERS", "MA_COMPUTE_DISPATCH_S")
    saved = {k: os.environ.get(k) for k in keys}
    import threading
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as tmpb, \
            dask.config.set({"distributed.scheduler.default-task-durations": {"hog": "68h"},
                             "distributed.scheduler.active-memory-manager.start": False}):
        rd = os.path.realpath(rd)
        os.makedirs(os.path.join(rd, "models"))
        # A live run directory (metadata.json): chunks run against its files, so
        # the local "VMs" never stage one bundle into one shared path at once.
        open(os.path.join(rd, "metadata.json"), "w").write("{}")
        open(os.path.join(rd, "models", "a16.py"), "w").write(
            "import os, time\n"
            "def f(p):\n"
            "    d = p['dir']\n"
            "    if p.get('wait'):\n"
            "        open(os.path.join(d, 'started_%s_%d' % (p['tag'], p['k'])), 'w').close()\n"
            "        while not os.path.exists(os.path.join(d, p['wait'])):\n"
            "            time.sleep(0.02)\n"
            "    t0 = time.time()\n"
            "    time.sleep(p.get('s', 0))\n"
            "    open(os.path.join(d, 'span_%s_%d' % (p['tag'], p['k'])), 'w').write('%f %f' % (t0, time.time()))\n"
            "    return p['k']\n")
        with LocalCluster(n_workers=1, threads_per_worker=1, processes=True, dashboard_address=None) as lc, \
                Client(lc.scheduler_address) as cc:
            w_run = list(lc.scheduler_info["workers"])[0]
            try:
                hog = cc.submit(time.sleep, 600, key=f"hog-{os.urandom(8).hex()}", workers=[w_run], pure=False)
                cc.register_plugin(OwnTmp(tmpb))
                cc.register_plugin(ComputeTagPlugin("compute", exclude=[w_run]))
                lc.scale(2)
                cc.wait_for_workers(2, timeout=60)
                os.environ.update(MA_COMPUTE_SCHEDULER=lc.scheduler_address, MA_COMPUTE_RESOURCE="compute",
                                  MA_COMPUTE_TARGET_WORKERS="4", MA_COMPUTE_PROCS_PER_WORKER="1",
                                  MA_COMPUTE_EXCLUDE_WORKERS=w_run, MA_COMPUTE_DISPATCH_S="0.2")
                # 8 chunks of one input each, 1.5 s each; all wait for `go`.
                s_task = 1.5
                ins = [{"k": k, "dir": rd, "tag": "a", "wait": "go", "s": s_task} for k in range(8)]
                box, box2 = {}, {}

                def call(b, ins_):
                    try:
                        b["r"] = map_fn(rd, "models/a16.py::f", ins_, ledger=False)
                    except Exception as e:  # noqa: BLE001 — reported in the case's message
                        b["r"] = ([], {"error": f"{type(e).__name__}: {e}"})
                th = threading.Thread(target=call, args=(box, ins))
                th.start()
                started0 = until(lambda: os.path.exists(os.path.join(rd, "started_a_0")))
                # One chunk runs on the one compute worker; the other 3 slots of the
                # target show as unrunnable demand under the chunks' own prefix.
                st = until(lambda: (lambda v: v if v["unrunnable"] == 3 else None)(cc.run_on_scheduler(state)), 30)
                ok(started0 and st and st["target"] >= 4 and st["occ"] > 1e5
                   and sum(st["proc"].values()) == 1,
                   f"P45D-1 one chunk runs, 3 demand markers are visible to the autoscaler (target >= 4) under "
                   f"inflated occupancy: {st}")
                lc.scale(5)                        # 3 tagged workers join mid-batch
                cc.wait_for_workers(5, timeout=60)
                st2 = until(lambda: (lambda v: v if sum(1 for n in v["proc"].values() if n == 1) == 4
                                     and v["unrunnable"] == 0 else None)(cc.run_on_scheduler(state)), 30)
                ok(st2 is not None and not st2["proc"].get(w_run),
                   f"P45D-2 each joining worker got a chunk at once and the demand markers were withdrawn: {st2}")
                open(os.path.join(rd, "go"), "w").close()
                th.join(120)
                rows, s = box.get("r", ([], {}))
                used = {r.get("worker") for r in rows}
                spans = []
                for k in range(8):
                    try:
                        spans.append(tuple(map(float, open(os.path.join(rd, f"span_a_{k}")).read().split())))
                    except (OSError, ValueError):
                        pass
                wall = (max(e for _, e in spans) - min(b for b, _ in spans)) if len(spans) == 8 else None
                # Peak concurrency from the tasks' own spans (structure, not wall clock: a loaded runner stretched
                # the wall time past 0.6 x serial while the batch was parallel). With one process per worker,
                # overlapping spans ran on different workers at the same time.
                ev = sorted([(b, 1) for b, _ in spans] + [(e, -1) for _, e in spans], key=lambda x: (x[0], x[1]))
                peak, cur = 0, 0
                for _, step in ev:
                    cur += step
                    peak = max(peak, cur)
                serial = 8 * s_task
                ok(len(rows) == 8 and all(r["ok"] for r in rows) and [r["result"] for r in rows] == list(range(8))
                   and w_run not in used and len(used) >= 4 and s.get("workers") == len(used)
                   and s.get("workers_seen") == 4,
                   f"P45D-3 every chunk done on {len(used)} compute workers, none on the run worker, "
                   f"and the ledger's worker count is the workers used: {used} {s}")
                ok(len(spans) == 8 and peak >= 2,
                   f"P45D-4 the batch ran in parallel: {peak} of its 8 tasks ran at once on separate workers "
                   f"(wall {wall} s against {serial} s serial, information only)")
                left = cc.run_on_scheduler(state)
                ok(left["unrunnable"] == 0 and not any(left["proc"].values()),
                   f"P45D-5 no demand marker or chunk outlives the call: {left}")
                # A worker that dies under its pinned chunk: the chunk cannot run
                # anywhere else, so the loop sees the worker gone, charges the chunk,
                # and resubmits it once to another worker. The compute workers that
                # hold the scattered bundle die too: when no copy is left (dask cannot
                # recompute scattered data and cancels the chunk), it is scattered again.
                ins2 = [{"k": 0, "dir": rd, "tag": "b", "wait": "go2", "s": 0.1}]
                th2 = threading.Thread(target=call, args=(box2, ins2))
                th2.start()
                until(lambda: os.path.exists(os.path.join(rd, "started_b_0")))
                victim = until(lambda: next((a for a, n in cc.run_on_scheduler(state)["proc"].items() if n), None), 30)
                holders = set(cc.run_on_scheduler(state)["bundle"])
                dying = ({victim} | holders) - {w_run}
                kill(cc, dying)
                open(os.path.join(rd, "go2"), "w").close()
                th2.join(120)
                rows2, s2 = box2.get("r", ([], {}))
                lost = [ch for ch in s2.get("chunks", []) if ch.get("error")]
                ok(len(rows2) == 1 and rows2[0]["ok"] and rows2[0].get("worker") not in dying
                   and len(lost) == 1 and lost[0]["attempt"] == 0 and lost[0]["worker"] == victim
                   and s2.get("estimated_core_s", 0) > 0,
                   f"P45D-6 a chunk whose worker left is charged and resubmitted once elsewhere: {rows2} {s2}")
                ok(s2.get("bundle_rescatters") == (0 if w_run in holders else 1),
                   f"P45D-7 a bundle whose every copy was lost is scattered again (once): holders {holders}, "
                   f"run worker {w_run}, {s2.get('bundle_rescatters')}")
                # The same with the bundle kept: a second chunk runs on another worker
                # first, which then holds a copy (the active memory manager is off
                # here, so the copy stays); only the blocked chunk's worker dies, and
                # the loop finds it gone from the scheduler.
                ins3 = [{"k": 0, "dir": rd, "tag": "c", "wait": "go3", "s": 0.1},
                        {"k": 1, "dir": rd, "tag": "c", "s": 0.1}]
                box3 = {}
                th3 = threading.Thread(target=call, args=(box3, ins3))
                th3.start()
                until(lambda: os.path.exists(os.path.join(rd, "started_c_0"))
                      and os.path.exists(os.path.join(rd, "span_c_1")))
                victim3 = until(lambda: (lambda pr: next(a for a, n in pr.items() if n) if sum(pr.values()) == 1
                                         and len(box3) == 0 else None)(cc.run_on_scheduler(state)["proc"]), 30)
                kept = until(lambda: (lambda b: b if set(b) - {victim3} else None)(cc.run_on_scheduler(state)["bundle"]),
                             30)
                ok(kept is not None, f"P45D-8 setup: the bundle has a copy off the victim: {kept}")
                kill(cc, {victim3})
                open(os.path.join(rd, "go3"), "w").close()
                th3.join(120)
                rows3, s3 = box3.get("r", ([], {}))
                lost3 = [ch for ch in s3.get("chunks", []) if ch.get("error")]
                ok(len(rows3) == 2 and all(r["ok"] for r in rows3) and rows3[0].get("worker") != victim3
                   and len(lost3) == 1
                   and "left the cluster" in lost3[0]["error"] and lost3[0]["worker"] == victim3
                   and s3.get("bundle_rescatters") == 0,
                   f"P45D-8 a pinned chunk whose worker left the scheduler is resubmitted elsewhere: {rows3} {s3}")
                # P45D-9/10: a chunk pinned to a worker it cannot start on moves, and
                # only while it has not started. Every compute worker's one-chunk lock
                # is held by the test, so the chunk is submitted but cannot claim its
                # start; then the others are released and its worker either gets a
                # second task (another call picked it from the same snapshot) or stops
                # taking work (closing gracefully).
                import fcntl

                def hold_locks():
                    fs = {}
                    for a in cc.run_on_scheduler(state)["tagged"]:
                        d_ = os.path.join(tmpb, a.rsplit(":", 1)[-1])
                        os.makedirs(d_, exist_ok=True)
                        fh = open(os.path.join(d_, "ma_compute.lock"), "w")
                        fcntl.flock(fh, fcntl.LOCK_EX)
                        fs[a] = fh
                    return fs

                def blocker(path):
                    import os as os_
                    import time as t_
                    while not os_.path.exists(path):
                        t_.sleep(0.02)
                    return 1
                for tag, how in (("d", "collision"), ("e", "closing")):
                    until(lambda: len(cc.run_on_scheduler(state)["tagged"]) >= 3, 60)
                    locks = hold_locks()
                    bx = {}
                    thx = threading.Thread(target=call, args=(bx, [{"k": 0, "dir": rd, "tag": tag, "s": 0.1}]))
                    thx.start()
                    w_x = until(lambda: next((a for a, n in cc.run_on_scheduler(state)["proc"].items() if n), None), 30)
                    for a, fh in locks.items():
                        if a != w_x:
                            fh.close()             # every other worker can run it now
                    foreign = None
                    if how == "collision":
                        foreign = cc.submit(blocker, os.path.join(rd, f"release_{tag}"), workers=[w_x],
                                            allow_other_workers=False, pure=False)
                    else:
                        cc.run(pause, workers=[w_x])
                    thx.join(120)
                    locks[w_x].close()
                    open(os.path.join(rd, f"release_{tag}"), "w").close()
                    if foreign is not None:
                        foreign.result(timeout=60)
                    else:
                        cc.run(unpause, workers=[w_x])
                    # the revoked submission has returned (its thread is free)
                    until(lambda: cc.run(idle_thread, workers=[w_x]).get(w_x), 30)
                    rx, sx = bx.get("r", ([], {}))
                    ran = [f_ for f_ in os.listdir(rd) if f_.startswith(f"span_{tag}_")]
                    ok(not thx.is_alive() and len(rx) == 1 and rx[0]["ok"] and rx[0].get("worker") != w_x
                       and sx.get("moved", 0) >= 1 and len(ran) == 1 and not [c_ for c_ in sx.get("chunks", [])
                                                                             if c_.get("error")],
                       f"P45D-{9 if how == 'collision' else 10} an unstarted chunk on a worker it cannot start on "
                       f"({how}) moves to another, and runs once: {rx} {sx}")
                    # The revoked first submission, released now, does no work.
                    ok(len([f_ for f_ in os.listdir(rd) if f_.startswith(f"span_{tag}_")]) == 1,
                       f"P45D-{9 if how == 'collision' else 10}b the revoked submission did no work")
                # P45D-12/13: the start claim records who holds it. A revoke asked
                # again gives the same answer; a replayed submission finds its own
                # earlier start claim and reports unconfirmed work, never "revoked".
                import uuid as uuid_
                cid = uuid_.uuid4().hex
                cdir = os.path.join(tempfile.gettempdir(), _CALLS_DIR, cid)
                r1, r2 = _revoke_chunk(cid, "x"), _revoke_chunk(cid, "x")
                c1 = _claim(cdir, "x", "start")
                c2 = _claim(cdir, "y", "start")
                r3, r4 = _revoke_chunk(cid, "y"), _revoke_chunk(cid, "y")
                ok(r1 and r2 and c1 == (False, "revoke") and c2 == (True, "start") and not r3 and not r4,
                   f"P45D-12 claim owner recorded; a repeated revoke keeps its answer: {r1, r2, c1, c2, r3, r4}")
                b13, i13 = make_bundle(rd)
                one = [(0, {"k": 0, "dir": rd, "tag": "g", "s": 0})]
                _claim(cdir, "z", "start")              # an earlier attempt of submission z
                g1 = _remote_task(b13, i13["sha256"], rd, "models/a16.py::f", one, procs=1, call_id=cid, token="z")
                g2 = _remote_task(b13, i13["sha256"], rd, "models/a16.py::f", one, procs=1, call_id=cid, token="x")
                ok(g1.get("unconfirmed") and not g1.get("revoked") and g2.get("revoked") and not g2.get("unconfirmed")
                   and not os.path.exists(os.path.join(rd, "span_g_0")),
                   f"P45D-13 a replay reports unconfirmed work, only a revoke claim reports revoked: {g1} {g2}")
                # P45D-14..16: the client side, with a stand-in chunk task whose first
                # call returns (14) "revoked" for a chunk the client never revoked (the
                # revoke's reply was lost), (15) "unconfirmed", (16) no rows at all.
                real_task = _remote_task

                def first_then_real(kind, flag):
                    def _remote_task_stub(bundle, sha, run_dir, fn_spec, chunk, *a, **k):
                        import os as os_
                        try:
                            os_.close(os_.open(flag, os_.O_CREAT | os_.O_EXCL | os_.O_WRONLY))
                        except FileExistsError:
                            return real_task(bundle, sha, run_dir, fn_spec, chunk, *a, **k)
                        if kind == "norows":
                            return {"rows": [], "core_s": 0.0}
                        return {"rows": [{"i": i, "ok": False, "kind": "infra", "error": kind} for i, _ in chunk],
                                "core_s": 0.0, kind: True}
                    return _remote_task_stub
                outs = {}
                try:
                    for kind in ("revoked", "unconfirmed", "norows"):
                        _remote_task = first_then_real(kind, os.path.join(rd, f"stub_{kind}"))
                        outs[kind] = map_fn(rd, "models/a16.py::f",
                                            [{"k": k, "dir": rd, "tag": f"h{kind}", "s": 0} for k in range(2)],
                                            ledger=False)
                finally:
                    _remote_task = real_task
                rv, sv = outs.get("revoked", ([], {}))
                ok(len(rv) == 2 and all(r["ok"] for r in rv) and sv.get("moved") == 1 and not sv.get("invariant_violation"),
                   f"P45D-14 a revoked result with no replacement is placed again, not dropped: {rv} {sv}")
                ru, su = outs.get("unconfirmed", ([], {}))
                lost_u = [c_ for c_ in su.get("chunks", []) if "unconfirmed" in str(c_.get("error"))]
                ok(len(ru) == 2 and all(r["ok"] for r in ru) and len(lost_u) == 1 and lost_u[0]["attempt"] == 0
                   and su.get("estimated_core_s", 0) > 0,
                   f"P45D-15 unconfirmed work is a lost chunk: charged, resubmitted once: {ru} {su}")
                rn, sn = outs.get("norows", ([], {}))
                ok(len(rn) == 2 and sum(r["ok"] for r in rn) == 1 and sn.get("invariant_violation")
                   and [r for r in rn if not r["ok"]][0]["kind"] == INFRA,
                   f"P45D-16 an input that returned no row fails loudly: {rn} {sn}")
                hog.cancel()
                # P45D-11: a loss that uses the rest of the allowance ends the call at once
                # (no worker free, so before this the loop slept with demand shown).
                # Every compute worker but one runs a blocker; the chunk's worker then
                # dies, and no new compute worker can join (the tag plugin is removed).
                until(lambda: len(cc.run_on_scheduler(state)["tagged"]) >= 3, 60)
                tagged = cc.run_on_scheduler(state)["tagged"]
                stop_b = os.path.join(rd, "release_f")
                blockers = [cc.submit(blocker, stop_b, workers=[a], allow_other_workers=False, pure=False,
                                      resources={"compute": 1}) for a in tagged[1:]]
                until(lambda: all(cc.run_on_scheduler(state)["nproc"].get(a) for a in tagged[1:]), 30)
                os.environ["MA_COMPUTE_PROCS_PER_WORKER"] = "64"   # a loss is charged 64 cores x its time
                bf = {}
                thf = threading.Thread(target=lambda: bf.update(r=map_fn(
                    rd, "models/a16.py::f", [{"k": 0, "dir": rd, "tag": "f", "wait": "go_f", "s": 0.1}],
                    ledger=False, max_core_hours=30.0 / 3600)))
                thf.start()
                until(lambda: os.path.exists(os.path.join(rd, "started_f_0")))
                # The run worker fetches a copy of the bundle, so the loss below is seen
                # as the worker leaving (the path that skipped the allowance check),
                # not as a cancelled chunk.
                from distributed import Future
                keep = [cc.submit(len, Future(k, client=cc), workers=[w_run], pure=False)
                        for k in cc.run_on_scheduler(state)["bundle_keys"]]
                until(lambda: w_run in cc.run_on_scheduler(state)["bundle"], 30)
                # Share 30 core-s: the chunk itself would stop at 30 s; charged as a loss
                # it is 64 cores x its time since submission, over 30 after 1 s.
                t_f = time.time()
                until(lambda: time.time() - t_f > 1.0, 5)
                cc.unregister_worker_plugin(ComputeTagPlugin.name)
                kill(cc, {tagged[0]})
                thf.join(60)
                os.environ["MA_COMPUTE_PROCS_PER_WORKER"] = "1"
                rf, sf = bf.get("r", ([], {}))
                left_f = cc.run_on_scheduler(state)
                open(stop_b, "w").close()
                open(os.path.join(rd, "go_f"), "w").close()
                ok(not thf.is_alive() and sf.get("stopped") == CAP_STOP and left_f["unrunnable"] == 0,
                   f"P45D-11 a loss that uses the allowance stops the call, with no demand left: "
                   f"alive={thf.is_alive()} {sf.get('stopped')} {left_f['unrunnable']}")
                ok(any("left the cluster" in str(c_.get("error")) for c_ in sf.get("chunks", [])),
                   f"P45D-11b the loss was seen as the worker leaving: {sf.get('chunks')}")
                for b_ in blockers + keep:
                    b_.cancel()
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


def _self_test_infra(ok) -> None:
    """IR1-IR6 (Nigeria v2 full run, 2026-11-02): at the tail of a 256-point batch the autoscaler
    retired workers that were still running chunks. Each lost chunk was resubmitted once, both
    resubmissions were lost the same way, and 16 rows came back infra; the analysis then failed
    closed. A LocalCluster of 2 workers; every wait is on an event, never a fixed sleep."""
    global _remote_task
    import tempfile
    import threading
    try:
        import dask
        from distributed import Client, LocalCluster, WorkerPlugin
    except ImportError:
        print("  (IR skipped: distributed not installed)")
        return

    class OwnTmp(WorkerPlugin):
        """One temp dir per local worker, as each VM has (the one-chunk lock lives there)."""
        name = "ma-selftest-own-tmp"

        def __init__(self, base):
            self.base = base

        def setup(self, worker):
            import tempfile as tf_
            d_ = os.path.join(self.base, worker.address.rsplit(":", 1)[-1])
            os.makedirs(d_, exist_ok=True)
            os.environ["TMPDIR"] = d_
            tf_.tempdir = d_

    def until(pred, limit=60.0):
        end = time.time() + limit
        while time.time() < end:
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        return None

    def nproc(dask_scheduler=None):
        return {a: len(ws.processing) for a, ws in dask_scheduler.workers.items()}

    def unrunnable(dask_scheduler=None):
        return len(dask_scheduler.unrunnable)

    def pick(target=0, attribute="address", dask_scheduler=None):
        """What the autoscaler would retire to reach `target` (Coiled asks by name)."""
        return dask_scheduler.workers_to_close(target=target, attribute=attribute)

    def names(dask_scheduler=None):
        return {a: ws.name for a, ws in dask_scheduler.workers.items()}

    def guards(dask_scheduler=None):
        return sorted(dask_scheduler.plugins)

    def blocker(path):
        import os as os_
        import time as t_
        while not os_.path.exists(path):
            t_.sleep(0.02)
        return 1

    def calls(rd, tag, k=None):
        return [f for f in os.listdir(rd) if f.startswith(f"call_{tag}_" + ("" if k is None else f"{k}_"))]

    def in_thread(box, *a, **kw):
        def go():
            try:
                box["r"] = map_fn(*a, **kw)
            except Exception as e:  # noqa: BLE001 — reported in the case's message
                box["r"] = ([], {"error": f"{type(e).__name__}: {e}"})
        th = threading.Thread(target=go, daemon=True)     # a hang fails the case, not the whole run
        th.start()
        return th

    keys = ("MA_COMPUTE_SCHEDULER", "MA_COMPUTE_RESOURCE", "MA_COMPUTE_TARGET_WORKERS", "MA_COMPUTE_PROCS_PER_WORKER",
            "MA_COMPUTE_EXCLUDE_WORKERS", "MA_COMPUTE_DISPATCH_S", "MA_COMPUTE_NO_WORKER_WAIT_S")
    saved = {k: os.environ.get(k) for k in keys}
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as tmpb, \
            dask.config.set({"distributed.scheduler.active-memory-manager.start": False}):
        rd = os.path.realpath(rd)
        os.makedirs(os.path.join(rd, "models"))
        open(os.path.join(rd, "metadata.json"), "w").write("{}")      # live run dir: no bundle staging
        open(os.path.join(rd, "models", "ir.py"), "w").write(
            "import os, time\n"
            "def f(p):\n"
            "    d = p['dir']\n"
            "    open(os.path.join(d, 'call_%s_%d_%d' % (p['tag'], p['k'], os.getpid())), 'w').close()\n"
            "    if p.get('raise'):\n"
            "        raise ValueError('bad input %d' % p['k'])\n"
            "    if p.get('wait'):\n"
            "        open(os.path.join(d, 'started_%s_%d' % (p['tag'], p['k'])), 'w').close()\n"
            "        while not os.path.exists(os.path.join(d, p['wait'])):\n"
            "            time.sleep(0.02)\n"
            "    return p['k']\n")
        with LocalCluster(n_workers=2, threads_per_worker=1, processes=True, dashboard_address=None) as lc, \
                Client(lc.scheduler_address) as cc:
            try:
                cc.register_plugin(OwnTmp(tmpb))
                cc.register_plugin(ComputeTagPlugin("compute"))
                os.environ.update(MA_COMPUTE_SCHEDULER=lc.scheduler_address, MA_COMPUTE_RESOURCE="compute",
                                  MA_COMPUTE_TARGET_WORKERS="2", MA_COMPUTE_PROCS_PER_WORKER="1",
                                  MA_COMPUTE_DISPATCH_S="0.2")
                os.environ.pop("MA_COMPUTE_EXCLUDE_WORKERS", None)
                w = sorted(cc.run_on_scheduler(nproc))
                # IR1: the hazard. Asked for fewer workers than are busy (Coiled's adapt passes
                # its target to the scheduler), the scheduler picks a busy worker to retire.
                stop_1 = os.path.join(rd, "release_1")
                blk = cc.submit(blocker, stop_1, workers=[w[0]], allow_other_workers=False, pure=False)
                until(lambda: cc.run_on_scheduler(nproc).get(w[0]), 30)
                before = cc.run_on_scheduler(pick)
                # IR2: model errors are never retried: each input runs once and its row is a
                # model failure; no chunk is recorded as lost. The call also installs the guard.
                open(stop_1, "w").close()
                blk.result(timeout=60)
                b2 = {}
                in_thread(b2, rd, "models/ir.py::f", [{"k": k, "dir": rd, "tag": "m", "raise": True}
                                                      for k in range(2)], ledger=False).join(120)
                r2, s2 = b2.get("r", ([], {}))
                ok(len(r2) == 2 and all(not r["ok"] and r.get("kind") == MODEL for r in r2)
                   and len(calls(rd, "m")) == 2 and not [c_ for c_ in s2.get("chunks", []) if c_.get("error")],
                   f"IR2 a model error is not retried: each input ran once ({sorted(calls(rd, 'm'))}): {r2} {s2}")
                stop_1b = os.path.join(rd, "release_1b")
                blk = cc.submit(blocker, stop_1b, workers=[w[0]], allow_other_workers=False, pure=False)
                until(lambda: cc.run_on_scheduler(nproc).get(w[0]), 30)
                after = cc.run_on_scheduler(pick)
                nm = cc.run_on_scheduler(names)
                after_n = cc.run_on_scheduler(pick, 0, "name")
                guard = BusyWorkerGuard.name
                ok(w[0] in before and guard in cc.run_on_scheduler(guards)
                   and w[0] not in after and w[1] in after and nm[w[0]] not in after_n and nm[w[1]] in after_n,
                   f"IR1 a busy worker is never chosen to close once a compute call has run (without the guard "
                   f"{before}, with it {after}, by name {after_n}; busy {w[0]})")
                open(stop_1b, "w").close()
                blk.result(timeout=60)
                if guard in cc.run_on_scheduler(guards):
                    cc.unregister_scheduler_plugin(guard)
                register_compute_tags(cc)
                ok(guard in cc.run_on_scheduler(guards),
                   f"IR1b the launcher's registration installs the guard too: {cc.run_on_scheduler(guards)}")
                # IR3: a worker retired under a running chunk (what adapt's scale-down does when it
                # picks one): the chunk is resubmitted and finishes on the remaining worker.
                b3 = {}
                th3 = in_thread(b3, rd, "models/ir.py::f", [{"k": k, "dir": rd, "tag": "r", "wait": "go_r"}
                                                            for k in range(2)], ledger=False)
                until(lambda: os.path.exists(os.path.join(rd, "started_r_0"))
                      and os.path.exists(os.path.join(rd, "started_r_1")), 60)
                pr = cc.run_on_scheduler(nproc)
                victim = next((a for a in sorted(pr) if pr[a]), None)
                cc.retire_workers([victim], close_workers=True, remove=True)
                until(lambda: victim not in cc.run_on_scheduler(nproc), 30)
                open(os.path.join(rd, "go_r"), "w").close()
                th3.join(120)
                r3, s3 = b3.get("r", ([], {}))
                lost3 = [c_ for c_ in s3.get("chunks", []) if c_.get("error")]
                ok(len(r3) == 2 and all(r["ok"] for r in r3) and all(r.get("worker") != victim for r in r3)
                   and len(lost3) == 1 and "left the cluster" in lost3[0]["error"] and lost3[0]["worker"] == victim
                   and s3.get("estimated_core_s", 0) > 0 and not s3.get("failed_by_kind"),
                   f"IR3 a chunk whose worker was retired under it is charged and finished on the remaining "
                   f"worker within the call: {r3} {s3}")
                # IR4/IR5: the retries are bounded per input (INFRA_RETRIES = 2): a chunk lost twice
                # still finishes; one lost three times is an infra row, after exactly three attempts.
                real_task = _remote_task

                def lossy(n_loss, tag):
                    def _remote_task_stub(bundle, sha, run_dir, fn_spec, chunk, *a, **k):
                        import os as os_
                        marks = [f_ for f_ in os_.listdir(run_dir) if f_.startswith(f"loss_{tag}_")]
                        if len(marks) < n_loss:
                            open(os_.path.join(run_dir, f"loss_{tag}_{len(marks)}"), "w").close()
                            return {"rows": [{"i": i, "ok": False, "kind": "infra",
                                              "error": "chunk failed: its worker left the cluster"} for i, _ in chunk],
                                    "core_s": 0.0, "unconfirmed": True}
                        return real_task(bundle, sha, run_dir, fn_spec, chunk, *a, **k)
                    return _remote_task_stub
                outs = {}
                try:
                    for n_loss in (2, 3):
                        _remote_task = lossy(n_loss, f"b{n_loss}")
                        outs[n_loss] = map_fn(rd, "models/ir.py::f", [{"k": 0, "dir": rd, "tag": f"b{n_loss}"}],
                                              ledger=False)
                finally:
                    _remote_task = real_task
                (r4, s4), (r5, s5) = outs.get(2, ([], {})), outs.get(3, ([], {}))
                lost4 = [c_ for c_ in s4.get("chunks", []) if c_.get("error")]
                lost5 = [c_ for c_ in s5.get("chunks", []) if c_.get("error")]
                marks5 = [f_ for f_ in os.listdir(rd) if f_.startswith("loss_b3_")]
                ok(len(r4) == 1 and r4[0]["ok"] and len(lost4) == 2 and len(calls(rd, "b2")) == 1,
                   f"IR4 a chunk lost twice is resubmitted twice and finishes: {r4} {lost4}")
                ok(len(r5) == 1 and not r5[0]["ok"] and r5[0].get("kind") == INFRA and len(lost5) == 3
                   and len(marks5) == 3 and not calls(rd, "b3") and s5.get("failed_by_kind") == {INFRA: 1},
                   f"IR5 a chunk lost three times is an infra row after three attempts, no more: {r5} {lost5} {marks5}")
                # IR6: no compute worker at all (none carries the resource): the call waits for the
                # pool within MA_COMPUTE_NO_WORKER_WAIT_S, then gives up with infra rows and no demand left.
                os.environ.update(MA_COMPUTE_RESOURCE="ma-selftest-none", MA_COMPUTE_NO_WORKER_WAIT_S="1")
                b6 = {}
                t6 = time.time()
                th6 = in_thread(b6, rd, "models/ir.py::f", [{"k": k, "dir": rd, "tag": "n"} for k in range(3)],
                                ledger=False)
                th6.join(60)
                r6, s6 = b6.get("r", ([], {}))
                ok(not th6.is_alive() and len(r6) == 3 and all(r.get("kind") == INFRA for r in r6)
                   and str(s6.get("stopped") or "").startswith("no compute worker")
                   and time.time() - t6 >= 1.0 and cc.run_on_scheduler(unrunnable) == 0,
                   f"IR6 with no compute worker the call waits its bound, then returns infra rows and withdraws "
                   f"its demand: alive={th6.is_alive()} {r6} {s6.get('stopped')}")
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v


def _self_test_p42(ok) -> None:
    """Phase 42 α: callers that die or are interrupted are charged for work
    done; zombies are dead; detached batches outlive their caller."""
    import signal
    import subprocess as _sp
    import tempfile
    env = {k: v for k, v in os.environ.items() if not k.startswith("MA_COMPUTE_")}
    host = os.uname().nodename

    def ledger(d):
        return json.load(open(os.path.join(d, LEDGER_REL)))

    def wait_rows(path, k, limit=60):
        end = time.time() + limit
        while time.time() < end:
            try:
                if sum(1 for _ in open(path)) - 1 >= k:
                    return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    def gone(pid, limit=30):
        """A SIGKILL is delivered asynchronously: wait until the process is dead
        or defunct (not reaped: the settling call must see a zombie)."""
        end = time.time() + limit
        while _pid_alive(pid) and time.time() < end:
            time.sleep(0.01)

    def wait_ready(run_dir, done, limit=60, pids=True, gate=None, blocked=0):
        """A state the test controls, not timing: the call's progress record
        shows `done` finished tasks, work in flight and (`pids`) its pool pids
        recorded; with a `gate`, also `blocked` tasks waiting on it. Gated tasks
        cannot finish, so nothing more is submitted or recorded: the state is
        frozen while the test kills."""
        import glob as _g
        end, last = time.time() + limit, None
        while time.time() < end:
            held_n = len(_g.glob(gate + ".started.*")) if gate else 0
            for f in _g.glob(os.path.join(run_dir, PROGRESS_DIR, "*.json")):
                try:
                    rec = json.load(open(f))
                except (OSError, ValueError):
                    continue
                last = rec
                seen_pids.extend(rec.get("pids") or [])
                if (int(rec.get("done_tasks") or 0) >= done and rec.get("out") and held_n >= blocked
                        and (rec.get("pids_complete") or not pids)):
                    return True, rec
            time.sleep(0.01)
        return False, last

    GATED = ("import os, time\n"
             "def f(p):\n"
             "    if p.get('gate') and p['k'] >= p.get('free', 0):\n"
             "        open(p['gate'] + '.started.%d' % p['k'], 'w').close()\n"
             "        end = time.time() + 300\n"
             "        while os.path.exists(p['gate']) and time.time() < end:\n"
             "            time.sleep(0.02)\n"
             "    time.sleep(p['s'])\n"
             "    return {'k': p['k']}\n")

    def gated_inputs(path, gate, n, free, s):
        """Inputs k < free run freely; the rest wait while `gate` exists (it is created here)."""
        open(gate, "w").close()
        gates.append(gate)
        open(path, "w").write("".join(json.dumps({"s": s, "k": k, "free": free, "gate": gate}) + "\n"
                                      for k in range(n)))

    # Everything these tests start is stopped on the way out, pass or fail:
    # callers (`procs`), the pool processes their records named, detached jobs.
    procs, seen_pids, gates = [], [], []

    def track(p):
        procs.append(p)
        return p

    def cleanup(run_dir):
        import glob as _g
        for g in gates:                           # gated tasks return
            try:
                os.remove(g)
            except OSError:
                pass
        gates.clear()
        for f in _g.glob(os.path.join(run_dir, "models", "*" + JOB_EXT)):
            try:
                job = json.load(open(f))
                tok, cur = job.get("pid_start"), _pid_start(int(job["pid"]))
                if tok and cur and tok == cur:    # signalled only when identified (pid reuse)
                    _killpg(int(job["pid"]), signal.SIGKILL, direct=True)
            except (OSError, ValueError, KeyError, TypeError):
                pass
        for f in _g.glob(os.path.join(run_dir, PROGRESS_DIR, "*.json")):
            try:
                seen_pids.extend(json.load(open(f)).get("pids") or [])
            except (OSError, ValueError, AttributeError):
                pass
        if seen_pids:
            _stop_orphans({"pids": [list(x) for x in {tuple(x) for x in seen_pids}], "pids_complete": True}, "local")
        for p in procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL) if os.getpgid(p.pid) == p.pid else p.kill()
                except OSError:
                    pass
            try:
                p.wait(10)
            except _sp.TimeoutExpired:
                pass
        procs.clear()
        seen_pids.clear()

    import contextlib

    @contextlib.contextmanager
    def run_dir_cleaned():
        with tempfile.TemporaryDirectory() as dd:
            try:
                yield dd
            finally:
                cleanup(dd)

    with run_dir_cleaned() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "slow.py"), "w").write(
            "import time\ndef sleep(p):\n    time.sleep(p['s']); return {'k': p['k']}\n")
        inp = os.path.join(d, "models", "in.jsonl")
        open(inp, "w").write("".join(json.dumps({"s": 1.0, "k": k}) + "\n" for k in range(8)))
        out = os.path.join(d, "models", "out.jsonl")
        cmd = [sys.executable, os.path.abspath(__file__), "map", d, "--fn", "models/slow.py::sleep", "--inputs", inp,
               "--out", out, "--backend", "local", "--max-workers", "2", "--max-core-hours", "1", "--purpose", "P42"]

        # P42-1 (bug: os.kill(pid, 0) succeeds on a defunct process, so a zombie
        # caller's reservation was held forever): a real zombie child is dead.
        z = _sp.Popen([sys.executable, "-c", "pass"])
        end = time.time() + 30                    # until it has exited (not reaped: a zombie)
        while time.time() < end and (_proc_stat(z.pid) or ["?"])[0] != "Z" and _sp.run(
                ["ps", "-o", "stat=", "-p", str(z.pid)], capture_output=True, text=True).stdout.strip()[:1] != "Z":
            time.sleep(0.02)
        try:
            os.kill(z.pid, 0)
            kill_ok = True
        except OSError:
            kill_ok = False
        ok(kill_ok and not _pid_alive(z.pid) and _pid_alive(os.getpid(), _pid_start(os.getpid())),
           f"P42-1 a zombie is dead though os.kill(pid, 0) succeeds on it ({kill_ok}); this process is alive")
        tok = _pid_start(os.getpid())
        if tok:
            ok(not _pid_alive(os.getpid(), tok + "9"), "P42-1b a reused pid (start token differs) is dead")
        # P42-1c (bug: `ps lstart` depends on TZ and locale, so a caller with another
        # TZ saw a live caller's token differ and settled it): the ps token is the
        # same whatever the caller's TZ.
        toks_tz = set()
        for tz in ("UTC", "America/Los_Angeles", "Asia/Kolkata"):
            r = _sp.run([sys.executable, "-c", "import sys; sys.path.insert(0, %r); import compute; "
                         "print(compute._ps_start(%d))" % (HERE, os.getpid())],
                        env=dict(env, TZ=tz, LC_ALL="fr_FR.UTF-8"), capture_output=True, text=True, timeout=60)
            toks_tz.add(r.stdout.strip())
        ok(len(toks_tz) == 1 and next(iter(toks_tz)).startswith("ps:") and next(iter(toks_tz)) == _ps_start(os.getpid()),
           f"P42-1c the ps start token does not depend on the caller's TZ or locale: {toks_tz}")
        # P42-3d (bug: a pid whose identity cannot be verified was signalled): a live
        # process recorded with a different or no start token is never signalled,
        # and the stop is unconfirmed (charged in full).
        bystander = track(_sp.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True))
        end = time.time() + 30
        while _pid_start(bystander.pid) is None and time.time() < end:
            time.sleep(0.02)
        for saved in ("psutil:1.00", "proc:1", "ps:Thu Jan  1 00:00:00 1970", None):
            stopped3d, how3d = _stop_orphans({"pids": [[bystander.pid, saved]], "pids_complete": True}, "local")
            ok(bystander.poll() is None and (saved is not None or not stopped3d),
               f"P42-3d a pid recorded with token {saved!r} is not signalled ({stopped3d}, {how3d})")
        bystander.kill()
        bystander.wait()

        # P42-3e (bug: _stop_chunk sent SIGKILL after the SIGTERM wait without
        # re-checking identity): a chunk ignoring SIGTERM whose identity no longer
        # verifies after the grace period is not SIGKILLed, and is reported unstopped.
        stub = track(_sp.Popen([sys.executable, "-c", "import signal, time; "
                                "signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); "
                                "time.sleep(120)"], start_new_session=True, stdout=_sp.PIPE, text=True))
        stub.stdout.readline()                    # its SIGTERM handler is installed
        tok3e = _pid_start(stub.pid)
        real_start, calls3e = _pid_start, []

        def flaky_start(pid):                     # identity verifies once, then reads as another process
            calls3e.append(pid)
            return real_start(pid) if len(calls3e) == 1 else (tok3e or "x:") + "-reused"
        globals()["_pid_start"] = flaky_start
        try:
            res3e = _stop_chunk(stub.pid, grace_s=0.3, token=tok3e)
        finally:
            globals()["_pid_start"] = real_start
        ok(tok3e and res3e is False and stub.poll() is None,
           f"P42-3e no SIGKILL once identity is lost during the grace period ({res3e}, alive {stub.poll() is None})")
        # P42-3f (bug: _cancel_call skipped unreadable registrations and tokens read
        # another way, reporting the call cancelled): both are unconfirmed (-1), not signalled.
        import uuid as _uuid
        for what in ("other kind", "unreadable"):
            cid = "selftest-p42-" + _uuid.uuid4().hex
            cdir_ = os.path.join(tempfile.gettempdir(), _CALLS_DIR, cid)
            os.makedirs(cdir_)
            reg = os.path.join(cdir_, str(stub.pid))
            cur = _pid_start(stub.pid) or ""
            other = "proc:1" if not cur.startswith("proc:") else "psutil:1.00"
            open(reg, "w").write(f"{stub.pid} {other}")
            if what == "unreadable":
                os.chmod(reg, 0)
            try:
                got3f = _cancel_call(cid)
            finally:
                os.chmod(reg, 0o600)
                import shutil as _sh
                _sh.rmtree(cdir_, ignore_errors=True)
            readable = what != "unreadable" or os.geteuid() != 0     # root reads a 000 file
            ok(not readable or (got3f == -1 and stub.poll() is None),
               f"P42-3f a registration with {what} token is unconfirmed and not signalled: {got3f}")
        stub.kill()
        stub.wait()

        # P42-2 (bug: a dead caller was charged its whole reservation): a zombie
        # caller with a progress record is charged its work, and released.
        pf = os.path.join(d, "rec.json")
        json.dump({"t": time.time(), "core_s": 100.0, "done_tasks": 10, "max_task_core_s": 10.0, "out": []}, open(pf, "w"))
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        json.dump({"core_seconds": 0.0, "calls": [], "reservations": {"z": {
            "core_s": 1440000.0, "pid": z.pid, "host": host, "fn": "f", "progress": pf}}},
            open(os.path.join(d, LEDGER_REL), "w"))
        map_fn(d, "models/slow.py::sleep", [], backend="local")
        led = ledger(d)
        ok(not led["reservations"] and abs(led["core_seconds"] - 100.0) < 1e-6
           and "charged work done" in led["calls"][0]["stopped"] and not os.path.exists(pf),
           f"P42-2 a zombie caller is settled for its 100 core-s of work, not 1,440,000: {led}")
        z.wait()
        # P42-3 (bug: in-flight chunks were charged the allowance): remote chunks
        # whose stop cannot be confirmed (no scheduler) are bounded by their shares,
        # which they enforce; not by observed task times (not an upper bound).
        now = time.time()
        rec = {"t": now - 30, "core_s": 100.0, "done_tasks": 20, "max_task_core_s": 3.0, "call_id": "c1",
               "out": [{"n": 10, "share": 5000.0, "started": now - 50, "cores": 4},
                       {"n": 2, "share": 20.0, "started": now - 40, "cores": 4}]}
        json.dump(rec, open(pf, "w"))
        hist = {"calls": []}
        os.environ.pop("MA_COMPUTE_SCHEDULER", None)
        charge, why, parts = _settle_dead(hist, {"core_s": 9000.0, "progress": pf, "fn": "f", "backend": "dask"},
                                          now=now)
        ok(abs(charge - 5240.0) < 1e-6 and "no scheduler" in why and parts.get("inflight_estimate_core_s") == 36.0,
           f"P42-3 unconfirmed remote chunks are charged their shares, the task-time estimate is only recorded: "
           f"{charge} ({why}) {parts}")
        # P42-3b a dead local caller's leftover pool process is killed, then bounded by its time out.
        orphan = track(_sp.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True))
        time.sleep(0.3)
        rec = {"t": now, "core_s": 100.0, "out": [{"n": 1, "started": time.time() - 10, "cores": 1}],
               "pids": [[orphan.pid, _pid_start(orphan.pid)]], "pids_complete": True}
        json.dump(rec, open(pf, "w"))
        charge, why, _ = _settle_dead(hist, {"core_s": 9000.0, "progress": pf, "fn": "f", "backend": "local"})
        dead_after = orphan.poll() is not None or orphan.wait(5) is not None
        ok(dead_after and 110.0 <= charge <= 120.0 and "pool processes stopped" in why,
           f"P42-3b the orphaned pool process is killed and charged its time out (~10 s): {charge} ({why})")
        # P42-3c killed while the pool was starting processes (pids unknown): local work
        # in flight cannot be stopped or bounded, so the reservation is charged.
        rec["pids_complete"] = False
        json.dump(rec, open(pf, "w"))
        charge, why, _ = _settle_dead(hist, {"core_s": 9000.0, "progress": pf, "fn": "f", "backend": "local"})
        ok(charge == 9000.0 and "not stopped" in why, f"P42-3c pids not recorded: reservation charged: {charge} {why}")
        # P42-4 fail closed (bug: an unreadable record must not hand out a discount).
        for content, what in ((None, "no record"), ("{not json", "unreadable"),
                              (json.dumps({**rec, "remote_unconfirmed": True}), "not confirmed")):
            r = {"core_s": 9000.0, "fn": "f"}
            if content is not None:
                open(pf, "w").write(content)
                r["progress"] = pf
            charge, why, _ = _settle_dead(hist, r, now=now)
            ok(charge == 9000.0 and "charged its reservation" in why,
               f"P42-4 fail closed ({what}): the reservation is charged and the row says why: {charge} {why}")

        # P42-5..7 use gated tasks: tasks k >= free wait on a gate file the test
        # holds, so the batch's state is frozen (nothing finishes, nothing more is
        # submitted or recorded) while the test kills, interrupts or checks it.
        open(os.path.join(d, "models", "gated.py"), "w").write(GATED)

        def gcmd(inputs, *extra):
            return [sys.executable, os.path.abspath(__file__), "map", d, "--fn", "models/gated.py::f",
                    "--inputs", inputs, "--out", out, "--backend", "local", "--max-workers", "2",
                    "--max-core-hours", "1", "--purpose", "P42", *extra]

        # P42-5 a caller killed mid-batch (SIGKILL to its group, as a tool timeout
        # does), left a zombie: the next call charges its work, not 3,600 core-s;
        # rerunning the command keeps the rows it had finished.
        os.remove(os.path.join(d, LEDGER_REL))
        g5, in5 = os.path.join(d, "gate5"), os.path.join(d, "models", "in5.jsonl")
        gated_inputs(in5, g5, 8, free=4, s=0.3)
        t0 = time.time()
        p = track(_sp.Popen(gcmd(in5), env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True))
        got, rec5 = wait_ready(d, 4, gate=g5, blocked=2)
        os.killpg(p.pid, signal.SIGKILL)
        el = time.time() - t0
        gone(p.pid)
        map_fn(d, "models/slow.py::sleep", [], backend="local")
        led = ledger(d)
        c = (led["calls"] or [{}])[0]
        ok(got, f"P42-5 setup: 4 tasks done and 2 held at the gate before the kill (last record {rec5})")
        ok(got and not led["reservations"] and "caller died; charged work done" in str(c.get("stopped"))
           and 1.0 <= c["task_core_s"] <= 2 * el + 15 and c.get("purpose") == "P42",
           f"P42-5 a killed caller is charged its work ({c.get('task_core_s')} core-s in {el:.1f}s), not 3,600: {c}")
        p.wait()
        os.remove(g5)
        r = _sp.run(gcmd(in5), env=env, capture_output=True, text=True, timeout=120)
        s = json.load(open(out + ".summary.json")) if os.path.exists(out + ".summary.json") else {}
        ok(r.returncode == 0 and s.get("reused", 0) >= 4 and s.get("n_ok") == 8 and not os.path.exists(out + PARTIAL_EXT),
           f"P42-5b rerunning after the kill keeps the rows finished before it: {s} {r.stderr[-300:]}")

        # P42-5c only the caller is killed: its pool processes live on (held at the
        # gate), and the settling call stops them before bounding their work by time.
        os.remove(os.path.join(d, LEDGER_REL))
        g5c, in5c = os.path.join(d, "gate5c"), os.path.join(d, "models", "in5c.jsonl")
        gated_inputs(in5c, g5c, 8, free=2, s=0.3)
        t0 = time.time()
        p = track(_sp.Popen(gcmd(in5c), env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
        got, rec5c = wait_ready(d, 2, gate=g5c, blocked=2)
        p.kill()
        gone(p.pid)
        map_fn(d, "models/slow.py::sleep", [], backend="local")
        el = time.time() - t0
        c = ledger(d)["calls"][0]
        p.wait()
        ok(got, f"P42-5c setup: 2 tasks done and 2 held at the gate before the kill (last record {rec5c})")
        ok("pool processes stopped" in str(c.get("stopped")) and 0.5 <= c["task_core_s"] <= 2 * el + 15,
           f"P42-5c a killed caller's surviving pool is stopped and its work bounded ({el:.1f}s): {c}")

        # P42-6 an interrupted call (SIGTERM) is charged what ran, settled by the backend.
        os.remove(os.path.join(d, LEDGER_REL))
        g6, in6 = os.path.join(d, "gate6"), os.path.join(d, "models", "in6.jsonl")
        gated_inputs(in6, g6, 8, free=2, s=0.3)
        t0 = time.time()
        p = track(_sp.Popen(gcmd(in6), env=env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL))
        got, rec6 = wait_ready(d, 2, gate=g6, blocked=2)
        p.send_signal(signal.SIGTERM)
        p.wait(60)
        el = time.time() - t0
        c = ledger(d)["calls"][-1]
        ok(got, f"P42-6 setup: 2 tasks done and 2 held at the gate before the interrupt (last record {rec6})")
        ok("interrupted: KeyboardInterrupt; charged work done" in c["stopped"] and 0.5 <= c["task_core_s"] <= 2 * el + 15,
           f"P42-6 an interrupted call is charged what ran ({c['task_core_s']} core-s in {el:.1f}s): {c}")

        # P42-7 a detached batch outlives its caller (killed with its whole group);
        # status reports it running and a second batch is refused while every
        # task is held at the gate; once released, wait reports it finished.
        for f in (out, out + ".summary.json"):
            if os.path.exists(f):
                os.remove(f)
        g7, in7 = os.path.join(d, "gate7"), os.path.join(d, "models", "in7.jsonl")
        gated_inputs(in7, g7, 8, free=0, s=0.2)
        cmd7 = gcmd(in7)
        wrapper = _sp.Popen(["/bin/sh", "-c", " ".join(_sp.list2cmdline([x]) for x in cmd7 + ["--detach"]) + "; sleep 60"],
                            env=env, stdout=_sp.PIPE, text=True, start_new_session=True)
        track(wrapper)
        line = wrapper.stdout.readline()
        os.killpg(wrapper.pid, signal.SIGKILL)
        wrapper.wait()
        try:
            det = json.loads(line)
        except ValueError:
            det = {}
        st = _sp.run([sys.executable, os.path.abspath(__file__), "status", out], env=env, capture_output=True, text=True)
        second = _sp.run(cmd7 + ["--detach"], env=env, capture_output=True, text=True, timeout=60)
        os.remove(g7)                              # release the batch only after both checks ran
        ok(second.returncode == 4 and '"detached": false' in second.stdout,
           f"P42-7d a second batch on the same --out is refused while the first runs: {second.stdout[-200:]}")
        w = _sp.run([sys.executable, os.path.abspath(__file__), "wait", out, "--timeout-s", "90"], env=env,
                    capture_output=True, text=True, timeout=150)
        try:
            ws = json.loads(w.stdout)
        except ValueError:
            ws = {}
        ok(det.get("detached") and st.returncode == 3 and '"running"' in st.stdout,
           f"P42-7 --detach returns at once and status reports the batch running: {line!r} {st.stdout[-200:]}")
        ok(w.returncode == 0 and ws.get("state") == FINISHED and (ws.get("summary") or {}).get("n_ok") == 8,
           f"P42-7b the detached batch finished after its caller's group was killed: {w.stdout[-300:]} {w.stderr[-200:]}")
        ok(_sp.run([sys.executable, os.path.abspath(__file__), "status", os.path.join(d, "none.jsonl")],
                   env=env, capture_output=True).returncode == 4, "P42-7c status of an unknown batch exits 4")

    # P42-8 (dask): an interrupted call on a pool still scaling up is charged its
    # chunks' reports, not every target slot as busy since the start.
    try:
        from distributed import LocalCluster
    except ImportError:
        print("  (P42-8 skipped: distributed not installed)")
        return
    with run_dir_cleaned() as d, \
            LocalCluster(n_workers=1, threads_per_worker=1, processes=True, dashboard_address=None) as lc:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "slow.py"), "w").write(
            "import time\ndef sleep(p):\n    time.sleep(p['s']); return {'k': p['k']}\n")
        # SIGINT is restored to raise KeyboardInterrupt: a child of a background job
        # (`cmd &`) inherits SIGINT ignored, and the interrupt was then lost.
        code = ("import json, os, signal, sys; signal.signal(signal.SIGINT, signal.default_int_handler); "
                "sys.path.insert(0, %r); import compute; "
                "from distributed import Client; c = Client(os.environ['MA_COMPUTE_SCHEDULER']); "
                "compute.register_compute_tags(c, exclude=[]); c.close(); "
                "compute.map_fn(%r, 'models/slow.py::sleep', [{'s': 1.0, 'k': k} for k in range(80)], "
                "max_core_hours=1.0, purpose='P42-8')") % (HERE, d)
        envd = dict(env, MA_COMPUTE_SCHEDULER=lc.scheduler_address, MA_COMPUTE_RESOURCE="compute",
                    MA_COMPUTE_TARGET_WORKERS="6", MA_COMPUTE_PROCS_PER_WORKER="1", MA_COMPUTE_SETTLE_S="30")
        t0 = time.time()
        p = track(_sp.Popen([sys.executable, "-c", code], env=envd, stdout=_sp.DEVNULL, stderr=_sp.PIPE, text=True))
        # State, not timing: a chunk has finished and others are in flight.
        got8, rec8 = wait_ready(d, 1, limit=120, pids=False)
        p.send_signal(signal.SIGINT)
        err = p.communicate(timeout=120)[1]
        el = time.time() - t0
        try:
            c = ledger(d)["calls"][-1]
        except (OSError, ValueError, IndexError):
            c = {}
        ok(got8, f"P42-8 setup: a dask chunk finished before the interrupt (last record {rec8}) {err[-300:]}")
        ok("charged work done" in str(c.get("stopped")) and 0 < c.get("task_core_s", 0) <= el + 5,
           f"P42-8 an interrupted dask call on 1 of 6 target workers is charged ~1 worker's time "
           f"({c.get('task_core_s')} core-s in {el:.1f}s; before: about 6 x elapsed): {c} {err[-300:]}")


def _self_test_p37(ok) -> None:
    """Phase 37: each batch records what it is for."""
    import subprocess as _sp
    import tempfile
    global _map_local
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "fn.py"), "w").write("def f(p):\n    return {'y': p['x'] * 2}\n")
        inp = os.path.join(d, "models", "in.jsonl")
        open(inp, "w").write("".join(json.dumps({"x": k}) + "\n" for k in range(3)))
        out = os.path.join(d, "models", "out.jsonl")
        base = [sys.executable, os.path.abspath(__file__), "map", d, "--fn", "models/fn.py::f", "--inputs", inp,
                "--out", out, "--backend", "local", "--max-workers", "1"]
        env = {k: v for k, v in os.environ.items() if k != "MA_COMPUTE_SCHEDULER"}
        r = _sp.run(base + ["--purpose", "  "], capture_output=True, text=True, env=env, timeout=120)
        ok(r.returncode == 2 and "--purpose is required" in r.stderr and not os.path.exists(out)
           and not os.path.exists(os.path.join(d, LEDGER_REL)),
           f"P37-1 a batch without a purpose is refused before any work: {r.returncode} {r.stderr[-200:]}")
        r = _sp.run(base + ["--purpose", "D-019, R-021"], capture_output=True, text=True, env=env, timeout=120)
        led = json.load(open(os.path.join(d, LEDGER_REL))) if os.path.exists(os.path.join(d, LEDGER_REL)) else {}
        call = (led.get("calls") or [{}])[-1]
        summ = json.load(open(out + ".summary.json")) if os.path.exists(out + ".summary.json") else {}
        ok(r.returncode == 0 and call.get("purpose") == "D-019, R-021" and call.get("out") == "models/out.jsonl"
           and summ.get("purpose") == "D-019, R-021",
           f"P37-2 the purpose and results file are recorded with the call and its summary: {call} {r.stderr[-300:]}")
        saved = _map_local

        def _boom(*a, **k):
            raise KeyboardInterrupt
        _map_local = _boom
        try:
            map_fn(d, "models/fn.py::f", [{"x": 1}, {"x": 2}], backend="local", purpose="P-009", out="models/o2.jsonl")
        except KeyboardInterrupt:
            pass
        finally:
            _map_local = saved
        call = json.load(open(os.path.join(d, LEDGER_REL)))["calls"][-1]
        ok(call.get("purpose") == "P-009" and call.get("n") == 2 and call.get("n_failed") == 2
           and str(call.get("stopped", "")).startswith("interrupted"),
           f"P37-3 an interrupted call keeps its purpose and counts its rows as failed: {call}")


def _self_test_p35(ok) -> None:
    """Phase 35: a broken pool heals, memory sets concurrency, repeated model
    errors stop a batch, reruns complete only missing rows, the gate sees gaps."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "models"))
        # "oom": an input with crash=True kills its process when 2+ run at once
        # (a file per running input stands in for memory pressure); alone it is fine.
        # It waits up to 5 s for a neighbour before deciding it is alone: on a slow
        # runner the pool's processes start one by one, and a fixed 0.3 s check let
        # the input run alone, so no pool broke (CI failures, 2026-10-27).
        open(os.path.join(d, "models", "oom.py"), "w").write(
            "import os, time, glob\n"
            "def f(p):\n"
            "    me = os.path.join(p['dir'], str(os.getpid()))\n"
            "    open(me, 'w').close()\n"
            "    try:\n"
            "        def alive(f):\n"
            "            try:\n                os.kill(int(os.path.basename(f)), 0); return True\n"
            "            except OSError:\n                return False\n"
            "        end = time.time() + (5.0 if p.get('crash') else 0.3)\n"
            "        while time.time() < end:\n"
            "            if p.get('crash') and sum(map(alive, glob.glob(os.path.join(p['dir'], '*')))) > 1:\n"
            "                os._exit(9)\n"
            "            time.sleep(0.02)\n"
            "        return {'x': p['x']}\n"
            "    finally:\n"
            "        os.path.exists(me) and os.remove(me)\n"
            "def always(p):\n    os._exit(9)\n"
            "def raises(p):\n    raise ValueError(f\"no convergence at x={p['x']}\")\n"
            "def some(p):\n    if p['x'] % 10 == 0:\n        raise ValueError('bad draw')\n    return {'x': p['x']}\n"
            "def big(p):\n    b = bytearray(p['mb'] * 2**20)\n    return {'n': len(b)}\n")
        busy = os.path.join(d, "busy"); os.makedirs(busy)
        inputs = [{"x": k, "dir": busy, "crash": k in (3, 11)} for k in range(16)]
        rows, s = map_fn(d, "models/oom.py::f", inputs, backend="local", max_workers=4)
        ok(s["n_failed"] == 0 and [r["result"]["x"] for r in rows] == list(range(16)) and s["breaks"] >= 1
           and s["procs_final"] < 4,
           f"P35-1 a pool broken by one process reruns its in-flight inputs at lower concurrency, none lost: {s}")
        os.environ["MA_COMPUTE_RECYCLE_EVERY"] = "4"
        try:
            rows, s = map_fn(d, "models/oom.py::f", [{"x": k, "dir": busy} for k in range(12)], backend="local",
                             max_workers=2)
        finally:
            os.environ.pop("MA_COMPUTE_RECYCLE_EVERY", None)
        ok(s["n_failed"] == 0 and [r["result"]["x"] for r in rows] == list(range(12)) and s.get("pool_recycles", 0) >= 2
           and recycle_every(8) == 120,
           f"P35-1b v2 lesson 7: the pool is replaced after every chunk of finished inputs, none lost: {s}")
        rows, s = map_fn(d, "models/oom.py::always", [{"x": k} for k in range(3)], backend="local", max_workers=2)
        ok(s["failed_by_kind"] == {"model": 3} and all("alone" in r["error"] for r in rows),
           f"P35-2 an input that kills its process alone fails as a model error: {s}")
        t0 = time.time()
        rows, s = map_fn(d, "models/oom.py::raises", [{"x": k} for k in range(200)], backend="local", max_workers=2)
        ok(s["stopped"] and "same model error" in s["stopped"] and s["failed_by_kind"].get("model", 0) < 40
           and s["failed_by_kind"].get("stopped", 0) > 150 and time.time() - t0 < 30,
           f"P35-3 a repeated model error stops the batch early: {s.get('stopped')} {s['failed_by_kind']}")
        rows, s = map_fn(d, "models/oom.py::some", [{"x": k} for k in range(60)], backend="local", max_workers=2)
        ok(not s["stopped"] and s["n_failed"] == 6, f"P35-4 a minority of failing draws does not trip the breaker: {s}")
        rows, s = map_fn(d, "models/oom.py::big", [{"mb": 200}] * 4, backend="local", max_workers=4)
        ok(s["peak_mb"] and s["peak_mb"] >= 200, f"P35-5 peak memory per process is measured: {s}")
        ok(_mem_slots(5000, 64000, 8) == 8, "P35-6a ample memory keeps every core")
        ok(_mem_slots(9000, 64000, 8) == 5 and _mem_slots(60000, 64000, 8) == 1 and _mem_slots(None, 64000, 8) == 8,
           f"P35-6 concurrency fits the measured peak: {_mem_slots(9000, 64000, 8)}")
        led = json.load(open(os.path.join(d, LEDGER_REL)))
        ok((led.get("mem_profile") or {}).get("models/oom.py::big", {}).get("peak_mb", 0) >= 200,
           f"P35-7 the ledger keeps each function's last peak as the next call's starting point: {led.get('mem_profile')}")
        # resume: a partly failed batch rerun computes only the missing rows
        open(os.path.join(d, "models", "flaky.py"), "w").write(
            "import os\n"
            "def f(p):\n"
            "    if p['x'] in (2, 5) and os.path.exists(os.path.join(os.path.dirname(__file__), '..', 'ctl', 'FAIL')):\n"
            "        raise RuntimeError('transient')\n"
            "    return {'y': p['x'] * 2}\n")
        os.makedirs(os.path.join(d, "ctl"))      # outside models/ and data/: not part of the fingerprint
        FAIL = os.path.join(d, "ctl", "FAIL")
        open(FAIL, "w").close()
        out = os.path.join(d, "models", "flaky_results.jsonl")
        ins = [{"x": k} for k in range(8)]
        rows, s = run_map(d, "models/flaky.py::f", ins, out, backend="local", max_workers=2)
        ok(s["n_failed"] == 2 and s["computed"] == 8, f"P35-8 first run: two rows fail: {s}")
        g = gate_findings(d)
        ok(len(g) == 1 and g[0]["severity"] == "HIGH" and "2 of 8" in g[0]["claim"],
           f"P35-9 the gate reports a results file with missing rows: {g}")
        os.remove(FAIL)
        rows, s = run_map(d, "models/flaky.py::f", ins, out, backend="local", max_workers=2)
        ok(s["n_failed"] == 0 and s["computed"] == 2 and s["reused"] == 6 and [r["result"]["y"] for r in rows]
           == [0, 2, 4, 6, 8, 10, 12, 14], f"P35-10 a rerun computes only the missing rows: {s}")
        ok(gate_findings(d) == [], f"P35-11 a completed file clears the gate: {gate_findings(d)}")
        open(os.path.join(d, "models", "flaky.py"), "a").write("# changed\n")
        rows, s = run_map(d, "models/flaky.py::f", ins, out, backend="local", max_workers=2)
        ok(s["computed"] == 8, f"P35-12 changed model code: nothing is reused: {s}")
        rows, s = run_map(d, "models/flaky.py::f", ins[::-1] + [{"x": 99}], out, backend="local", max_workers=2)
        ok(s["computed"] == 1 and rows[0]["result"] == {"y": 14} and rows[0]["i"] == 0,
           f"P35-13 reuse is by params, not position: {s}")
        rows, s = run_map(d, "models/flaky.py::f", ins, out, fresh=True, backend="local", max_workers=2)
        ok(s["computed"] == 8, f"P35-14 --fresh recomputes everything: {s}")
        # a failed attempt file covered by a newer file of the same function
        open(FAIL, "w").close()
        a1 = os.path.join(d, "models", "lib_attempt1.jsonl")
        run_map(d, "models/flaky.py::f", ins, a1, backend="local", max_workers=2)
        ok(any("lib_attempt1" in f["claim"] for f in gate_findings(d)), "P35-15 an attempt file with gaps is reported")
        os.remove(FAIL)
        time.sleep(0.05)
        run_map(d, "models/flaky.py::f", [ins[2], ins[5]], os.path.join(d, "models", "lib_retry.jsonl"),
                backend="local", max_workers=2)
        ok(any("lib_attempt1" in f["claim"] for f in gate_findings(d)),
           "P35-16a rows are not recovered piecemeal from another file")
        run_map(d, "models/flaky.py::f", ins, os.path.join(d, "models", "lib_full.jsonl"), backend="local", max_workers=2)
        ok(not any("lib_attempt1" in f["claim"] for f in gate_findings(d)),
           f"P35-16 a file redone in full by a newer file is not reported: {gate_findings(d)}")
        open(os.path.join(d, "models", "x.jsonl"), "w").write("{not json\n")
        json.dump({"n_failed": 0, "task_core_s": 1}, open(os.path.join(d, "models", "x.jsonl.summary.json"), "w"))
        ok(any("x.jsonl" in f["claim"] and f["severity"] == "HIGH" for f in gate_findings(d)),
           "P35-17 an unreadable results file fails closed")
        ok(_breaker([{"ok": False, "kind": INFRA, "error": "e"}] * 20) is None,
           "P35-18 machine failures never trip the model-error breaker")
        errs = [{"ok": False, "kind": MODEL, "error": "ValueError: x"}] * 8
        ok(_breaker(errs) and _breaker(errs, prior_ok=300) is None,
           "P35-19 a chunk's breaker counts the batch's successes elsewhere")
        open(os.path.join(d, "models", "draw.py"), "w").write(
            "import random\ndef f(p):\n    return {'u': random.random()}\n")
        dout = os.path.join(d, "models", "draws.jsonl")
        rows, s = run_map(d, "models/draw.py::f", [{}] * 5, dout, backend="local", max_workers=2)
        rows2, s2 = run_map(d, "models/draw.py::f", [{}] * 6, dout, backend="local", max_workers=2)
        us = [r["result"]["u"] for r in rows2]
        ok(len(set(us)) == 6 and s2["computed"] == 1 and us[:5] == [r["result"]["u"] for r in rows],
           f"P35-20 identical params are separate draws, kept and reused one for one: {s2.get('computed')}")
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        dfile = os.path.join(d, "data", "tbl.csv")
        open(dfile, "w").write("a\n1\n"); os.utime(dfile, (1_700_000_000, 1_700_000_000))
        fp1 = code_fingerprint(d, "models/draw.py::f")
        open(dfile, "w").write("a\n2\n"); os.utime(dfile, (1_700_000_000, 1_700_000_000))
        ok(code_fingerprint(d, "models/draw.py::f") != fp1,
           "P35-21 same-size data with the same mtime but new content changes the fingerprint")
        open(os.path.join(d, "models", "run.log"), "w").write("progress\n")
        fp2 = code_fingerprint(d, "models/draw.py::f")
        open(os.path.join(d, "models", "run.log"), "a").write("more\n")
        ok(code_fingerprint(d, "models/draw.py::f") == fp2, "P35-22 logs do not change the fingerprint")
        tr = os.path.join(d, "models", "trunc.jsonl")
        open(tr, "w").write(json.dumps({"i": 0, "ok": True, "key": "a#0", "result": 1}) + "\n")
        json.dump({"n": 100, "n_failed": 0, "task_core_s": 1, "fn": "models/t.py::f"}, open(tr + ".summary.json", "w"))
        ok(any("trunc.jsonl" in f["claim"] and "99 of 100" in f["claim"] and f["severity"] == "HIGH"
               for f in gate_findings(d)), f"P35-23 rows absent from a file count: {gate_findings(d)}")
        os.remove(tr)
        ok(any("trunc.jsonl" in f["claim"] and "missing" in f["claim"] for f in gate_findings(d)),
           "P35-24 a results file missing behind its summary is reported")
        os.remove(tr + ".summary.json")
        dup = os.path.join(d, "models", "dup.jsonl")
        with open(dup, "w") as f:
            f.write(json.dumps({"i": 0, "ok": True, "key": "z#0", "result": 1}) + "\n")
            f.write(json.dumps({"i": 1, "ok": False, "key": "z#1", "kind": "model", "error": "x"}) + "\n")
        json.dump({"n": 2, "n_failed": 1, "task_core_s": 1, "fn": "models/t.py::f"},
                  open(dup + ".summary.json", "w"))
        ok(any("dup.jsonl" in f["claim"] for f in gate_findings(d)),
           "P35-25 a file's own ok rows never hide its failed rows")
        fpa = code_fingerprint(d, "models/draw.py::f", exclude=(dout,))
        fpb = code_fingerprint(d, "models/draw.py::f")
        ok(fpa != fpb, "P35-26 another call's results file is part of the fingerprint (a model may read it)")
        uo = os.path.join(d, "models", "upstream.out")
        open(uo, "w").write("1\n"); json.dump({"n_failed": 0, "task_core_s": 0}, open(uo + ".summary.json", "w"))
        fpc = code_fingerprint(d, "models/draw.py::f")
        open(uo, "w").write("2\n")
        ok(code_fingerprint(d, "models/draw.py::f") != fpc, "P35-26b a results file counts whatever its extension")
        os.remove(uo); os.remove(uo + ".summary.json")
        big = os.path.join(d, "models", "bigfail.jsonl")
        with open(big, "w") as f:
            for k in range(20000):
                f.write(json.dumps({"i": k, "ok": False, "key": f"b#{k}", "kind": "stopped", "error": f"stopped {k}"}) + "\n")
        json.dump({"n": 20000, "n_failed": 20000, "task_core_s": 1, "fn": "models/b.py::f"}, open(big + ".summary.json", "w"))
        t0 = time.time()
        g = gate_findings(d)
        ok(time.time() - t0 < 5 and any("bigfail" in f["claim"] for f in g),
           f"P35-30 a large failed file is reported quickly ({time.time() - t0:.1f}s)")
        os.remove(big); os.remove(big + ".summary.json")
        open(os.path.join(d, "models", "brk.py"), "w").write(
            "def f(p):\n    if p['x'] >= 100:\n        raise ValueError('persistent')\n    return {'x': p['x']}\n")
        bo = os.path.join(d, "models", "brk.jsonl")
        run_map(d, "models/brk.py::f", [{"x": k} for k in range(100)], bo, backend="local", max_workers=1)
        rows, s = run_map(d, "models/brk.py::f", [{"x": k} for k in range(110)], bo, backend="local", max_workers=1)
        ok(not s.get("stopped") and s["failed_by_kind"] == {"model": 10},
           f"P35-27 rows reused on resume count as successes for the breaker: {s.get('stopped')} {s['failed_by_kind']}")
        tr2 = os.path.join(d, "models", "partial.jsonl")
        open(tr2, "w").write(json.dumps({"i": 0, "ok": True, "key": "q#0", "result": 1}) + "\n")
        json.dump({"n": 100, "n_failed": 0, "task_core_s": 1, "fn": "models/q.py::f"}, open(tr2 + ".summary.json", "w"))
        time.sleep(0.05)
        nw = os.path.join(d, "models", "partial_new.jsonl")
        open(nw, "w").write(json.dumps({"i": 0, "ok": True, "key": "q#0", "result": 1}) + "\n")
        json.dump({"n": 1, "n_failed": 0, "task_core_s": 1, "fn": "models/q.py::f"}, open(nw + ".summary.json", "w"))
        ok(any("partial.jsonl" in f["claim"] and "99 of 100" in f["claim"] for f in gate_findings(d)),
           "P35-28 a truncated file is not superseded by a newer file holding only its few rows")
        rows, s = map_fn(d, "models/oom.py::big", [{"mb": 300}] * 6, backend="local", max_workers=4)
        os.environ["MA_COMPUTE_PEAK_MB"] = "1"
        try:
            import unittest.mock as _m
            with _m.patch(__name__ + "._avail_mb", return_value=900.0):
                rows, s = map_fn(d, "models/oom.py::big", [{"mb": 300}] * 6, backend="local", max_workers=4,
                                 ledger=False)
        finally:
            del os.environ["MA_COMPUTE_PEAK_MB"]
        ok(s["n_failed"] == 0 and s["procs_final"] < 4,
           f"P35-29 concurrency falls to what measured memory allows, the pool shrinks with it: {s}")
        open(os.path.join(d, "models", "var.py"), "w").write(
            "import time\ndef f(p):\n    t = time.time()\n    while time.time() - t < p['s']: pass\n    return {}\n")
        try:
            with _m.patch(__name__ + "._avail_mb", return_value=60.0):   # one ~70 MB process: no hint, 4 at start
                t0 = time.time()
                rows, s = map_fn(d, "models/var.py::f", [{"s": 0.2}] + [{"s": 8.0}] * 3 + [{"s": 8.0}] * 4,
                                 backend="local", max_workers=4, max_core_hours=4 / 3600)
                el = time.time() - t0
        finally:
            pass
        bo2 = os.path.join(d, "data", "baseline.out")
        open(bo2, "w").write("1")
        fpo = code_fingerprint(d, "models/draw.py::f")
        open(bo2, "w").write("2")
        ok(code_fingerprint(d, "models/draw.py::f") != fpo, "P35-34 every file under data/ counts, whatever its extension")
        dres = os.path.join(d, "data", "res.jsonl")
        run_map(d, "models/brk.py::f", [{"x": k} for k in range(5)], dres, backend="local", max_workers=1)
        _r, s35 = run_map(d, "models/brk.py::f", [{"x": k} for k in range(5)], dres, backend="local", max_workers=1)
        ok(s35["computed"] == 0, f"P35-35 a results file under data/ resumes (its own summary is not a dependency): {s35}")
        sd2 = os.path.join(d, "data", "sl")
        for nm, v in (("a", "1"), ("b", "2")):
            os.makedirs(os.path.join(sd2, nm), exist_ok=True)
            open(os.path.join(sd2, nm, "value.csv"), "w").write(v)
        os.symlink("a", os.path.join(sd2, "current"))
        fps = code_fingerprint(d, "models/draw.py::f")
        os.remove(os.path.join(sd2, "current")); os.symlink("b", os.path.join(sd2, "current"))
        ok(code_fingerprint(d, "models/draw.py::f") != fps, "P35-32 retargeting a symlinked data dir changes the fingerprint")
        ok(s["stopped"] == CAP_STOP and el < 5.0,   # without the fix: about 8 s
           f"P35-31 a shrinking pool's allowance stop counts the inputs still running ({el:.1f}s): {s.get('stopped')}")


RUNNING, FINISHED, DIED, NO_JOB = "running", "finished", "died", "no detached job"
_STATUS_EXIT = {FINISHED: 0, RUNNING: 3, DIED: 4, NO_JOB: 4}


def job_status(out: str) -> dict:
    """Phase 42 α: state of the detached batch writing `out`: running,
    finished (its summary was written after it started), died (gone without
    a summary: rerun the same command; finished rows are kept), or no
    detached job. Rows done are counted from `<out>.partial.jsonl`."""
    try:
        job = json.load(open(out + JOB_EXT))
        pid, t0 = int(job["pid"]), float(job["started"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        return {"state": NO_JOB, "out": out, "error": f"{out + JOB_EXT}: {type(e).__name__}"}
    st = {"out": out, "pid": pid, "n": job.get("n"), "elapsed_s": round(time.time() - t0, 1), "log": job.get("log")}
    if _pid_alive(pid, job.get("pid_start")):
        rows = 0
        try:
            with open(out + PARTIAL_EXT) as f:
                rows = max(0, sum(1 for _ in f) - 1)
        except OSError:
            pass
        return {"state": RUNNING, "rows_done": rows, **st}
    try:
        fresh = os.path.getmtime(out + ".summary.json") >= t0 - 1
        summ = json.load(open(out + ".summary.json")) if fresh else None
    except (OSError, ValueError):
        summ = None
    if isinstance(summ, dict):
        return {"state": FINISHED, **st, "summary": {k: v for k, v in summ.items() if k not in ("bundle", "chunks")}}
    tail = ""
    try:
        tail = open(job.get("log") or "").read()[-1500:]
    except OSError:
        pass
    return {"state": DIED, **st, "log_tail": tail,
            "next": "the batch stopped without finishing; read the log, then rerun the same map command "
                    "(rows already finished are kept)"}


def _detach(argv: list, out: str, n: int, run_dir: str | None) -> int:
    """Start this same `map` command in its own session (an agent's tool
    timeout or a killed shell does not reach it) and return at once. Every
    file it writes (launch lock, lock, log, job record) is anchored at the
    trusted run dir, with no link followed below it."""
    import subprocess
    launch = _out_lock(out, block=True, suffix=".launch.lock", run_dir=run_dir)   # one launch at a time per --out
    try:
        return _detach_locked(argv, out, n, subprocess, run_dir)
    finally:
        launch.close()


def _detach_locked(argv, out, n, subprocess, run_dir) -> int:
    st = job_status(out)
    busy = _out_lock(out, run_dir=run_dir)
    if busy is not None:
        busy.close()
    if st["state"] == RUNNING or busy is None:
        print(json.dumps({"detached": False, "error": "a batch is already writing this --out", **st}))
        return 4
    log = out + ".log"
    t0 = time.time()
    from lib.safe_io import publish, open_append_nofollow
    base, rel = _out_base(log, run_dir)
    publish(log, b"", run_dir)                                   # a fresh log file, no link followed
    # the child's stdout is the checked append primitive's own descriptor: opened with no link followed and refused
    # when the file has other hard links (a swap after the fresh file was published is not appended to)
    try:
        lf = open_append_nofollow(base, rel)
    finally:
        os.close(base)
    with lf:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + argv, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    job = {"pid": p.pid, "pid_start": _pid_start(p.pid), "started": t0, "n": n, "log": log, "argv": argv}
    publish(out + JOB_EXT, json.dumps(job, indent=1), run_dir)
    print(json.dumps({"detached": True, "pid": p.pid, "out": out, "log": log,
                      "next": f"python3 scripts/compute.py wait {out} --timeout-s 540  "
                              "(repeat while it exits 3 = still running)"}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a model function over many parameter sets")
    ap.add_argument("cmd", nargs="?", choices=["map", "status", "wait", "reuse-check"])
    ap.add_argument("run_dir", nargs="?", help="map, reuse-check: the run directory; status/wait: the batch's --out file")
    ap.add_argument("--expect-all", action="store_true",
                    help="reuse-check: exit 1 unless every stored row would be reused")
    ap.add_argument("--detach", action="store_true",
                    help="run the batch in its own session and return at once; follow it with "
                         "`compute.py wait <out>` (an agent tool timeout cannot kill it)")
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
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
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
    if a.cmd in ("status", "wait"):
        if not a.run_dir:
            ap.error(f"usage: compute.py {a.cmd} <out.jsonl> [--timeout-s S]")
        end = time.time() + (a.timeout_s if a.timeout_s is not None else 540)
        st = job_status(a.run_dir)
        while a.cmd == "wait" and st["state"] == RUNNING and time.time() < end:
            time.sleep(min(5.0, max(0.1, end - time.time())))
            st = job_status(a.run_dir)
        print(json.dumps(st))
        code = _STATUS_EXIT[st["state"]]
        if st["state"] == FINISHED and (st.get("summary") or {}).get("n_failed"):
            code = 1
        return code
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
    if a.detach:
        if a.no_ledger:
            ap.error("--detach is for ledgered batches")
        return _detach([x for x in sys.argv[1:] if x != "--detach"], a.out, len(_read_inputs(a.inputs)), a.run_dir)
    if a.no_ledger:   # a remote chunk: plain map, no reuse (the caller already chose the rows)
        rows, summary = map_fn(a.run_dir, a.fn, _read_inputs(a.inputs), a.backend, a.max_workers,
                               a.max_tasks, a.timeout_s, ledger=False, max_core_hours=a.max_core_hours)
        from lib.safe_io import publish   # anchored at the trusted root: --out-root (the worker's call dir) or the run dir
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
