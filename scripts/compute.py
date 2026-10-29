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

Phase 35, failures handled by the service (no agent troubleshooting):
memory is measured per process and concurrency lowered to fit; a process
death (out of memory) or a lost VM reruns the affected inputs at lower
concurrency; a model error repeating on most rows stops the batch early.
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


def _local_init(run_dir: str, fn_spec: str, init_dir: str | None = None):
    """Pool initializer. Each process records its model load time in
    `init_dir`, so loads are charged even in processes that get no chunk; a
    load that raises is recorded there too (`<pid>.err`), so a pool that breaks
    at start is reported as the model's load error, not as a crash."""
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
    process is retried once; a second death fails it as a model error."""
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
                                      initializer=_local_init, initargs=(run_dir, fn_spec, init_dir.name))

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
                ex, shrink = new_pool(cap), False
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
                ex, shrink = new_pool(cap), False
            if seen_peak and _mem_slots(seen_peak, avail, n) < cap:
                cap = _mem_slots(seen_peak, avail, n)
                shrink = True                     # drain, then rebuild the pool at `cap` processes
                rebudget()
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
                  "peak_mb": round(seen_peak, 1) or None}


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
                 chunk_allowance_s: float | None = None, call_id: str | None = None, repo: str | None = None,
                 peak_mb: float | None = None, prior_ok: int = 0, chunk_breaker: bool = True):
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

    def rows_with(msg, kind="stopped"):
        return [{"i": i, "ok": False, "kind": kind, "error": msg[:500], "worker": waddr} for i, _ in chunk]

    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        t0 = time.time()

        def failed(msg, kind="stopped"):
            return {"rows": rows_with(msg, kind), "core_s": round((time.time() - t0) * procs, 3)}
        if os.path.exists(os.path.join(calls, "cancel")):
            return {"rows": rows_with("cancelled"), "core_s": 0.0}
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
# Phase 38: compute VMs in priority order across Azure VM families with separate core
# quotas (8 vCPU, 64 GiB each). E7 attempt 7: workers were refused for most of the run at
# 344 of 350 standardEv5Family cores, a quota shared with the subscription's other users.
# Coiled tries a list in order for availability; fallback on a quota error is not yet
# observed (a 2026-10-27 test found Ev5 with room).
WORKER_VM_TYPES = ["Standard_E8s_v5", "Standard_E8as_v5", "Standard_E8ds_v5"]
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
              progress=None, peak_mb=None, prior_ok=0):
    """The allowance is split over the chunks that can run at once; each chunk
    enforces its share from when it starts (queueing behind other calls on a
    shared pool does not count). Whenever the call ends with chunks still
    out (cap, timeout, interrupt), those chunks are stopped on the workers.

    Phase 35: what one chunk learns about memory (its pool broke, or its
    measured peak allowed fewer processes) sets the processes for every chunk
    submitted after it. A chunk lost with its VM, or whose subprocess died, is
    resubmitted once with half the processes. A model error repeating across
    chunks stops the call (`_breaker`)."""
    import uuid
    import cloudpickle
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
        cur = [None]                                # (future, share) being processed
        learned = {"procs": None, "peak": peak_mb}

        def inflight():
            return [{"n": len(futs[f][0]), "share": held[f] or None, "started": started.get(f, time.time()),
                     "cores": cores_per_slot} for f in held]

        def procs_for(override=None):
            p = [int(x) for x in (env_procs, learned["procs"], override) if x]
            return min(p) if p else None            # None: the worker uses its own core count

        def submit(ch, attempt, override=None):
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
            f = c.submit(_remote_task, bfut, binfo["sha256"], run_dir, fn_spec, ch, deadline,
                         procs_for(override), share, call_id, REPO, learned["peak"],
                         prior_ok + sum(1 for r in rows if r.get("ok")), attempt != TAIL, pure=False, **placement)
            futs[f] = (ch, attempt, procs_for(override))
            held[f] = share or 0.0
            started[f] = time.time()
            return f
        futs, queue, started, errors = {}, [(ch, 0, None) for ch in chunks], {}, {}

        def halved(used, worker=None):
            return max(1, int(used or cpus.get(worker) or cores_per_slot) // 2)
        pending, rows, stop, lost = set(), [], None, 0
        try:
            while queue or pending:
                _note(progress, inflight(), call_id=call_id)
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
                    ch, attempt, used = futs[f]
                    cur[0] = (f, held.get(f))     # an interrupt mid-processing leaves it charged in `finally`
                    f_share = held.pop(f, None)
                    try:
                        got = f.result()
                        csum = got.get("summary") or {}
                        wk = got["rows"][0].get("worker") if got["rows"] else None
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
                        # Its work is unknown: charge the share it was allowed
                        # (chunks enforce their share), or estimate it at the end.
                        # charged as busy since submission, never above its share
                        busy = (time.time() - started.get(f, time.time())) * cores_per_slot
                        if f_share:
                            progress["core_s"] += min(f_share, busy)
                        else:
                            lost += 1
                        msg = f"chunk failed: {type(e).__name__}: {e}"[:500]
                        progress.setdefault("chunks", []).append(
                            {"n": len(ch), "attempt": attempt, "error": msg[:200],
                             "core_s": round(min(f_share, busy), 1) if f_share else None,
                             "wall_s": round(time.time() - started.get(f, time.time()), 1)})
                        errors.update({i: msg for i, _ in ch})
                        if attempt == 0 and (deadline is None or time.time() < deadline):
                            queue.insert(0, (ch, 1, halved(used)))
                            continue
                        rows += [{"i": i, "ok": False, "kind": INFRA, "error": errors[i]} for i, _ in ch]
                cur[0] = None
                if (pending or queue) and not stop:
                    stop = _breaker(rows, prior_ok=prior_ok)
                    if stop:
                        break
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
                    got_c = c.run(_cancel_call, call_id,
                                  workers=[w for w in c.scheduler_info()["workers"] if w not in exclude])
                    if any(v == -1 for v in (got_c or {}).values()):
                        progress["remote_unconfirmed"] = True     # a chunk could not be identified
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
            # On an autoscaling pool, scale-down only retires idle workers: no lost work.
            try:
                gone = [] if res else [w for w in workers if w not in c.scheduler_info()["workers"]]
            except Exception:  # noqa: BLE001
                gone = [] if res else list(workers)
            progress["gone_workers"] = len(gone)
    done_i = {r["i"] for r in rows}
    rows += [{"i": i, "ok": False, "kind": INFRA, "error": msg} for i, msg in errors.items() if i not in done_i]   # keep the real error
    return rows, {"backend": "dask", "workers": len(workers), "pool_target": pool, "slots": slots,
                  "cores_per_slot": cores_per_slot,
                  "core_s": progress["core_s"], "stopped": stop,
                  "unknown_chunks": lost + len(pending) + len(gone), "bundle": binfo,
                  "chunks": progress.get("chunks", []), "procs_final": learned["procs"],
                  "peak_mb": learned["peak"]}


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


def _note(progress: dict, out: list, extra_core_s: float = 0.0, **more) -> None:
    """Phase 42 α: persist what a ledgered call has done and what it has in
    flight (`out`: [{"n": tasks, "share": core-s or None, "started": t,
    "cores": cores}]), so a caller killed without settling (an agent's tool
    timeout, SIGKILL) is charged for work done, not its whole reservation.
    Written before new work is submitted. If a write fails the record is
    removed, so settlement falls back to charging the reservation."""
    path = progress.get("_file")
    if not path:
        return
    rec = {"t": time.time(), "core_s": round(float(progress.get("core_s") or 0) + extra_core_s, 1),
           "done_tasks": int(progress.get("done_tasks") or 0),
           "max_task_core_s": round(float(progress.get("max_task_core_s") or 0), 2),
           "remote_unconfirmed": bool(progress.get("remote_unconfirmed")), "out": out, **more}
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        progress["_file"] = None
        try:
            os.remove(path)
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


def _settle_dead(led, r, now=None) -> tuple[float, str, dict]:
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
    (a long task after short ones), so it is never what is charged."""
    now = time.time() if now is None else now
    held = float(r.get("core_s") or 0)
    path = r.get("progress")
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
    if out:
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
           f"<= {bound:.0f} core-s; {how}), not its reservation ({held:.0f} core-s)")
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
            got = c.run(_cancel_call, call_id, workers=[w for w in c.scheduler_info()["workers"] if w not in exclude])
        if any(v == -1 for v in (got or {}).values()):
            return False, "a remote chunk could not be identified (not signalled)"
        return True, "remote chunks cancelled"
    except Exception as e:  # noqa: BLE001 — cannot confirm: bounded by shares
        return False, f"remote cancel failed ({type(e).__name__})"


def _drop_progress(progress: dict) -> None:
    path, progress["_file"] = progress.get("_file"), None
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def _live_reservations(led) -> float:
    """Core-seconds held by calls still running on this host. A call whose
    process is gone (or defunct) without settling is charged for its work
    done and a bound on its chunks still in flight (`_settle_dead`)."""
    host, total = os.uname().nodename, 0.0
    for rid, r in list(led["reservations"].items()):
        alive = r.get("host") != host
        if r.get("host") == host:
            try:
                alive = _pid_alive(int(r["pid"]), r.get("pid_start"))
            except (ValueError, KeyError, TypeError):
                alive = False
        if alive:
            total += float(r.get("core_s") or 0)
        else:
            charge, why, parts = _settle_dead(led, r)
            del led["reservations"][rid]
            led["core_seconds"] = round(led["core_seconds"] + charge, 1)
            led["calls"].append({"fn": r.get("fn"), "backend": r.get("backend"), "n": r.get("n"),
                                 "purpose": r.get("purpose"), "out": r.get("out"), "stopped": why,
                                 "task_core_s": round(charge, 1), **parts, "ts": time.strftime("%FT%T")})
            if r.get("progress"):
                try:
                    os.remove(r["progress"])
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
    progress = {"_rows": on_rows}
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
            pfile = os.path.join(run_dir, PROGRESS_DIR, hashlib.sha1(rid.encode()).hexdigest()[:16] + ".json")
            try:
                os.makedirs(os.path.dirname(pfile), exist_ok=True)
                progress["_file"] = pfile
                _note(progress, [])
            except OSError:
                progress["_file"] = None
            led["reservations"][rid] = {"core_s": allowance_s, "pid": os.getpid(),
                                        "pid_start": _pid_start(os.getpid()),
                                        "host": os.uname().nodename, "fn": fn_spec, "backend": backend,
                                        "n": len(inputs), "purpose": purpose, "out": out,
                                        "progress": progress.get("_file"), "started": round(time.time(), 1)}
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
        if backend == "dask":
            rows, info = _map_dask(run_dir, fn_spec, inputs, max_workers, deadline=deadline,
                                   allowance_s=allowance_s, progress=progress, peak_mb=hint, prior_ok=prior_ok)
        else:
            rows, info = _map_local(run_dir, fn_spec, inputs, max_workers, deadline=deadline,
                                    allowance_s=allowance_s, progress=progress, peak_mb=hint, prior_ok=prior_ok)
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
_FP_HASH_MB = 64           # files up to this size are fingerprinted by content, larger ones by size and mtime


def code_fingerprint(run_dir: str, fn_spec: str, exclude=()) -> str:
    """What a result depends on besides its params: every file under the run's
    models/ and data/ (following symlinked directories) and the repo's
    scripts/, except (outside data/) logs, figures, summaries, the ledger, and
    this call's own results file (`exclude`), and any batch's partial rows or
    detached-job record; files up to 64 MB by content, larger ones by
    size and mtime. Any change and earlier rows are not reused."""
    run_dir = os.path.realpath(run_dir)
    h = hashlib.sha256()
    path = fn_spec.partition("::")[0]
    fn_file = path if os.path.isabs(path) else os.path.join(run_dir, path)
    skip = {os.path.realpath(x) for x in exclude}
    files = {fn_file}
    links = []
    for root in (os.path.join(run_dir, "models"), os.path.join(run_dir, "data"), os.path.join(REPO, "scripts")):
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
            for fn in fns:
                is_output = fn + ".summary.json" in names     # another call's results file, any extension
                if not in_data and not is_output and (fn.endswith(_FP_SKIP_EXT) or fn.endswith(".summary.json")
                                                      or fn.endswith((PARTIAL_EXT, JOB_EXT))
                                                      or fn.startswith("compute_ledger.json")):
                    continue          # logs, figures, summaries, the ledger (never skipped under data/)
                files.add(os.path.join(dp, fn))
    for ln in sorted(links):
        h.update(b"link:" + ln.encode() + b"\0")
    for f in sorted(files):
        if os.path.realpath(f) in skip:
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


def _reusable(out: str, fn_spec: str, fp: str) -> dict:
    """key -> ok row from an earlier --out of the same function and code, and
    (Phase 42 α) from the rows an interrupted or killed run of it had written
    to `<out>.partial.jsonl` (its first line names the function and code)."""
    got = {}
    try:
        prev = json.load(open(out + ".summary.json"))
        if prev.get("fn") == fn_spec and prev.get("code_fp") == fp:
            got.update({r["key"]: r for r in (json.loads(l) for l in open(out) if l.strip())
                        if isinstance(r, dict) and r.get("ok") and r.get("key")})
    except (OSError, ValueError):
        pass
    try:
        with open(out + PARTIAL_EXT) as f:
            head = json.loads(f.readline() or "{}").get("_partial") or {}
            if head.get("fn") == fn_spec and head.get("code_fp") == fp:
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


def run_map(run_dir, fn_spec, inputs, out, fresh=False, **kw) -> tuple[list[dict], dict]:
    """The CLI's `map`: rows of an earlier run of the same command (same
    function, same code and data, same params) are kept and only the rest is
    computed, so rerunning a stopped or partly failed batch completes it.
    Writes --out and its summary; returns (rows, summary). Phase 42 α: one
    batch at a time per --out (`<out>.lock`); a second is refused."""
    lock = _out_lock(out)
    if lock is None:
        raise RuntimeError(f"another batch is writing {out}; wait for it (compute.py wait {out}) "
                           "or choose another --out")
    try:
        return _run_map(run_dir, fn_spec, inputs, out, fresh=fresh, **kw)
    finally:
        lock.close()


def _out_lock(out: str, block: bool = False, suffix: str = ".lock"):
    """An exclusive lock on `out` (an open file; closing releases it), or None if another process holds it."""
    import fcntl
    f = open(out + suffix, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
    except OSError:
        f.close()
        return None
    return f


def _run_map(run_dir, fn_spec, inputs, out, fresh=False, **kw) -> tuple[list[dict], dict]:
    rel_out =os.path.relpath(os.path.realpath(out), os.path.realpath(run_dir))
    fp = code_fingerprint(run_dir, fn_spec, exclude=(out, out + ".tmp", out + ".summary.json",
                                                     out + ".summary.json.tmp", out + PARTIAL_EXT,
                                                     out + JOB_EXT, out + ".log"))
    keys = input_keys(inputs)
    old = {} if fresh else _reusable(out, fn_spec, fp)
    todo = [k for k, key in enumerate(keys) if key not in old]
    if todo:
        # Phase 42 α: rows are appended to <out>.partial.jsonl as they finish, so
        # an interrupted or killed batch keeps them (rerunning reuses them).
        # The rows kept so far are copied to a new journal that replaces the old
        # one only once complete (a kill during the copy loses nothing).
        with open(out + PARTIAL_EXT + ".tmp", "w") as f:
            f.write(json.dumps({"_partial": {"fn": fn_spec, "code_fp": fp, "n": len(inputs)}}) + "\n")
            for key, r in old.items():
                f.write(json.dumps({**r, "key": key}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(out + PARTIAL_EXT + ".tmp", out + PARTIAL_EXT)
        part = open(out + PARTIAL_EXT, "a")

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
               "fn": fn_spec, "code_fp": fp, "out": rel_out, "purpose": kw.get("purpose")}
    tmp = out + ".tmp"
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    os.replace(tmp, out)
    json.dump(summary, open(out + ".summary.json.tmp", "w"), indent=1)
    os.replace(out + ".summary.json.tmp", out + ".summary.json")
    try:
        os.remove(out + PARTIAL_EXT)
    except OSError:
        pass
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
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


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


def _detach(argv: list, out: str, n: int) -> int:
    """Start this same `map` command in its own session (an agent's tool
    timeout or a killed shell does not reach it) and return at once."""
    import subprocess
    launch = _out_lock(out, block=True, suffix=".launch.lock")    # one launch at a time per --out
    try:
        return _detach_locked(argv, out, n, subprocess)
    finally:
        launch.close()


def _detach_locked(argv, out, n, subprocess) -> int:
    st = job_status(out)
    busy = _out_lock(out)
    if busy is not None:
        busy.close()
    if st["state"] == RUNNING or busy is None:
        print(json.dumps({"detached": False, "error": "a batch is already writing this --out", **st}))
        return 4
    log = out + ".log"
    t0 = time.time()
    with open(log, "w") as lf:
        p = subprocess.Popen([sys.executable, os.path.abspath(__file__)] + argv, stdin=subprocess.DEVNULL,
                             stdout=lf, stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
    job = {"pid": p.pid, "pid_start": _pid_start(p.pid), "started": t0, "n": n, "log": log, "argv": argv}
    tmp = out + JOB_EXT + ".tmp"
    json.dump(job, open(tmp, "w"), indent=1)
    os.replace(tmp, out + JOB_EXT)
    print(json.dumps({"detached": True, "pid": p.pid, "out": out, "log": log,
                      "next": f"python3 scripts/compute.py wait {out} --timeout-s 540  "
                              "(repeat while it exits 3 = still running)"}))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Run a model function over many parameter sets")
    ap.add_argument("cmd", nargs="?", choices=["map", "status", "wait"])
    ap.add_argument("run_dir", nargs="?", help="map: the run directory; status/wait: the batch's --out file")
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
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
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
        return _detach([x for x in sys.argv[1:] if x != "--detach"], a.out, len(_read_inputs(a.inputs)))
    if a.no_ledger:   # a remote chunk: plain map, no reuse (the caller already chose the rows)
        rows, summary = map_fn(a.run_dir, a.fn, _read_inputs(a.inputs), a.backend, a.max_workers,
                               a.max_tasks, a.timeout_s, ledger=False, max_core_hours=a.max_core_hours)
        with open(a.out, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        json.dump(summary, open(a.out + ".summary.json", "w"), indent=1)
    else:
        rows, summary = run_map(a.run_dir, a.fn, _read_inputs(a.inputs), a.out, fresh=a.fresh,
                                backend=a.backend, max_workers=a.max_workers, max_tasks=a.max_tasks,
                                timeout_s=a.timeout_s, max_core_hours=a.max_core_hours, purpose=a.purpose.strip())
    print(json.dumps({k: v for k, v in summary.items() if k not in ("bundle", "chunks")}))
    return 0 if summary["n_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
