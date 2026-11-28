#!/usr/bin/env python3
"""
Worker-side execution primitives for scripts/compute.py (strictly acyclic: zero imports from compute.py).

Runs model batches locally (spawned ProcessPoolExecutor) and inside remote Dask chunk subprocesses
(`compute_worker.py map <run_dir> --fn ... --inputs ... --out ... --no-ledger`).
Because worker processes only import compute_worker.py (never compute.py), Dask scheduling, ledger,
and CLI changes in compute.py never enter _observed_files() and never invalidate stored batches.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
from concurrent.futures.process import BrokenProcessPool
import hashlib
import importlib.util
import json
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DEFAULT_MAX_TASKS = 20000

MODEL, INFRA, STOPPED = "model", "infra", "stopped"
TAIL = 2                   # attempt marker: a chunk's breaker-stopped inputs rerun without the chunk breaker
BREAKER_MIN = 8            # same model error at least this often, and on half the finished rows
MEM_HEADROOM = 1.2         # a process may grow 20% past the largest peak seen
MEM_USABLE = 0.85          # share of the memory available at start that the pool may use
CAP_STOP = "stopped: compute cap reached"
_CALLS_DIR = "ma_compute_calls"     # under the temp dir: running-chunk pids and cancel marks per call
_FP_HASH_MB = 64           # files up to this size are fingerprinted by content, larger ones by size and mtime
_FP_REPO_SKIP = ("scripts/lib/code_commit.txt",)

_LOCAL_FN = None
_OBSERVED = {"run": None, "repo": None, "digests": {}, "sent": None, "opened": set(), "child": False, "hooked": False}
_CHILD_EVENTS = ("subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn", "os.exec", "os.fork", "os.forkpty",
                 "pty.spawn")


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


def _guard_registration():
    """A self-test's import-guard registration in this process (toolkit/import_guard.py: test-only toy engines), for
    the pool's workers; None in production. Read from the module only if this process already imported it (code
    registered it), never from the environment (follow-up to #133)."""
    g = sys.modules.get("toolkit.import_guard")
    return g.worker_registration() if g is not None else None


def _local_init(run_dir: str, fn_spec: str, init_dir: str | None = None, guard_reg=None, repo: str | None = None):
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
    if not os.environ.get("MA_COMPUTE_IN_CHUNK"):
        os.setpgrp()
    os.chdir(run_dir)
    _OBSERVED.update(run=run_dir, repo=repo or REPO)
    if not _OBSERVED["hooked"]:
        sys.addaudithook(_audit)
        _OBSERVED["hooked"] = True
    try:
        _LOCAL_FN = load_fn(run_dir, fn_spec)
        _observed_files()
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
        _observe(row)
        out.append(row)
    return out


def _audit(event: str, args) -> None:
    if event == "open":
        path, mode = args[0], args[1] if len(args) > 1 else None
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        if isinstance(path, (str, bytes)) and not any(c in str(mode or "") for c in "wax+") \
                and not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            _OBSERVED["opened"].add(os.path.abspath(os.fsdecode(path)))
    elif event in _CHILD_EVENTS:
        _OBSERVED["child"] = True


def _digest(x) -> str:
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


def _file_digest(f: str) -> str:
    """sha256 of a file's content, streamed."""
    h = hashlib.sha256()
    with open(f, "rb") as fh:
        for block in iter(lambda: fh.read(2**20), b""):
            h.update(block)
    return h.hexdigest()


def _observed_files() -> dict:
    """{path: sha256} of the repo files (relative to REPO) and run Python files ("run:<rel>") this process used. The
    path is kept as used (a linked module is re-checked through its link), else as resolved."""
    run, repo = _OBSERVED["run"] or os.getcwd(), _OBSERVED["repo"] or REPO
    roots = [(base + os.sep, tag) for d, tag in ((run, "run:"), (repo, ""))
             for base in dict.fromkeys((os.path.abspath(d), os.path.realpath(d)))]
    got, cache = {}, _OBSERVED["digests"]
    used = [getattr(m, "__file__", None) for m in list(sys.modules.values())] + sorted(_OBSERVED["opened"])
    for f in used:
        if not isinstance(f, str) or "__pycache__" in f or f.endswith(".pyc"):
            continue
        for path in (os.path.abspath(f), os.path.realpath(f)):
            hit = next(((b, t) for b, t in roots if path.startswith(b)), None)
            if hit is None:
                continue
            rel = hit[1] + os.path.relpath(path, hit[0][:-1]).replace(os.sep, "/")
            if (hit[1] and not rel.endswith(".py")) or rel in _FP_REPO_SKIP or os.path.isdir(path):
                break
            if path not in cache:
                try:
                    cache[path] = _file_digest(path)
                except (OSError, ValueError):
                    cache[path] = "unreadable"
            got[rel] = cache[path]
            break
    return got


def _had_child() -> bool:
    """This process started a child (seen by the audit hook, reaped, or still alive). Cheap, best effort."""
    if _OBSERVED["child"]:
        return True
    import resource
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    if ru.ru_utime + ru.ru_stime > 0:
        return True
    try:
        import psutil
        return bool(psutil.Process().children())
    except Exception:  # noqa: BLE001 -- psutil missing or refused: the reaped-child test above still applies
        return False


def _observe(row: dict) -> None:
    files = _observed_files()
    row["observed_id"] = _digest(files)
    if row["observed_id"] != _OBSERVED["sent"]:
        row["observed"] = files
        _OBSERVED["sent"] = row["observed_id"]
    if _had_child():
        row["observed_child"] = True


def _chunks(items: list, n_chunks: int) -> list:
    size = -(-len(items) // max(1, n_chunks))
    return [items[k:k + size] for k in range(0, len(items), size)] if items else []


def _budget_deadline(deadline, allowance_s, cores):
    """(deadline, reason): the earlier of the caller's deadline and the time
    at which `cores` busy cores would use up the core-second allowance."""
    if allowance_s is None:
        return deadline, "timed out"
    b = time.time() + allowance_s / max(1, cores)
    return (b, CAP_STOP) if deadline is None or b < deadline else (deadline, "timed out")


def _killpg(pgid, sig, direct=False):
    """Signal the group; with direct=True also the process itself."""
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


def _known_core_s(rows) -> float:
    return sum(r.get("seconds") or 0 for r in rows)


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
        return "proc:" + st[19]
    try:
        import psutil
        return f"psutil:{psutil.Process(int(pid)).create_time():.2f}"
    except ImportError:
        pass
    except Exception:  # noqa: BLE001 — the process is gone
        return None
    return _ps_start(pid)


def _ps_start(pid: int) -> str | None:
    """Start time from `ps` in a fixed environment (UTC, C locale)."""
    import subprocess
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "TZ": "UTC", "LC_ALL": "C", "LANG": "C"}
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                             timeout=10, env=env).stdout.strip()
        return "ps:" + " ".join(out.split()) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _pid_alive(pid, start: str | None = None) -> bool:
    """Is `pid` a running process? False when gone, defunct (zombie), or reused."""
    try:
        pid = int(pid)
        os.kill(pid, 0)
    except PermissionError:
        pass
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
                state = None
    if state in ("Z", "X", "x"):
        return False
    if start:
        now = _pid_start(pid)
        if now is not None and now.split(":")[0] == start.split(":")[0] and now != start:
            return False
    return True


def _same_process(pid: int, token: str | None) -> bool:
    """Is `pid` still the process recorded with start `token`?"""
    cur = _pid_start(pid)
    return bool(token and cur and cur == token)


def _stop_chunk(pid: int, grace_s: float = 5.0, token: str | None = None) -> bool:
    """SIGTERM a chunk subprocess's group (its handler kills its pool), then
    SIGKILL the group after the grace period. Returns whether it is stopped."""
    import signal
    if token is not None and not _same_process(pid, token):
        return not (_pid_alive(pid) or _group_alive(pid))
    _killpg(pid, signal.SIGTERM)
    end = time.time() + grace_s
    while time.time() < end and _group_alive(pid):
        time.sleep(0.1)
    if not _group_alive(pid):
        return True
    if token is not None and not _same_process(pid, token):
        return False
    _killpg(pid, signal.SIGKILL)
    end = time.time() + 5
    while time.time() < end and _group_alive(pid):
        time.sleep(0.05)
    return not _group_alive(pid)


def _claim(calls: str, token: str, who: str) -> tuple[bool, str]:
    """Claim a submission's start for `who` ("start" or "revoke")."""
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
    """Runs on a worker: claim a chunk's start so that it never starts."""
    import os, tempfile  # noqa: E401 — pickled by value
    calls = os.path.join(tempfile.gettempdir(), _CALLS_DIR, call_id)
    won, owner = _claim(calls, token, "revoke")
    return won or owner == "revoke"


def _cancel_call(call_id: str) -> int:
    """On a worker (client.run): stop this call's running chunks and keep its
    queued ones from starting."""
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
                continue
            except OSError:
                unverified = True
                continue
            tok, cur = tok.strip(), _pid_start(pid)
            if tok and cur and cur == tok:
                if not _stop_chunk(pid, token=tok):
                    unverified = True
                n += 1
            elif tok and cur and cur.split(":")[0] == tok.split(":")[0]:
                continue
            elif _pid_alive(pid) or _group_alive(pid):
                unverified = True
    return -1 if unverified else n


def _no_symlink_path(path: str) -> bool:
    """True if no existing component of `path` is a symlink."""
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
    """Runs on a dask worker. Under a machine-wide lock: ensure the files on disk
    match `bundle`, then evaluate `chunk` in a subprocess running compute_worker.py."""
    import fcntl, io, json, os, shutil, signal, subprocess, sys, tarfile, tempfile, time  # noqa: E401 — pickled by value
    try:
        from distributed import get_worker
        waddr = get_worker().address
    except Exception:  # noqa: BLE001 — also called outside a worker (self-test)
        waddr = None
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
                    if os.path.islink("/" + m):
                        os.unlink("/" + m)
                tf.extractall("/", filter="data")
                os.makedirs(run_dir, exist_ok=True)
                json.dump({"sha": sha, "members": members}, open(current, "w"))
        with tempfile.TemporaryDirectory() as td:
            inp, out = os.path.join(td, "in.json"), os.path.join(td, "out.jsonl")
            json.dump([p for _, p in chunk], open(inp, "w"))
            script = os.path.join(repo or REPO, "scripts", "compute_worker.py")
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("MA_COMPUTE_SCHEDULER", "MA_COMPUTE_TLS", "AZ_", "AZURE_", "AWS_", "GOOGLE_",
                                        "GCP_", "OPENAI_", "ANTHROPIC_", "CLAUDE", "COILED_", "DASK_"))}
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["MA_COMPUTE_IN_CHUNK"] = "1"
            if peak_mb:
                env["MA_COMPUTE_PEAK_MB"] = str(peak_mb)
            env["MA_COMPUTE_PRIOR_OK"] = str(int(prior_ok or 0))
            if not chunk_breaker:
                env["MA_COMPUTE_BREAKER_MIN"] = "0"
            cancel_mark = os.path.join(calls, "cancel")
            cmd = [sys.executable, script, "map", run_dir, "--fn", fn_spec, "--inputs", inp,
                   "--out", out, "--out-root", td, "--backend", "local", "--max-workers", str(procs), "--no-ledger",
                   "--max-tasks", str(max(1, len(chunk)))]
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
                b = chunk_allowance_s + 60
                hard = b if hard is None else min(hard, b)
            errf = open(os.path.join(td, "chunk.err"), "w+")
            env["MA_COMPUTE_GATE"] = os.path.join(calls, "{pid}")
            env["MA_COMPUTE_CANCEL"] = cancel_mark
            proc = subprocess.Popen(cmd, cwd=run_dir, env=env, stdout=subprocess.DEVNULL, stderr=errf,
                                    start_new_session=True)
            pidf = os.path.join(calls, str(proc.pid))
            open(pidf + ".tmp", "w").write(f"{proc.pid} {_pid_start(proc.pid) or ''}")
            os.replace(pidf + ".tmp", pidf)
            if os.path.exists(os.path.join(calls, "cancel")):
                _stop_chunk(proc.pid, grace_s=0)
            try:
                proc.wait(timeout=hard)
            except subprocess.TimeoutExpired:
                _stop_chunk(proc.pid)
                proc.wait()
                return failed("timed out (chunk subprocess killed)")
            finally:
                _killpg(proc.pid, signal.SIGKILL)
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
                core_s = (time.time() - t0) * procs
    return {"rows": [{**row, "i": chunk[k][0], "worker": waddr} for k, row in enumerate(rows)], "core_s": core_s,
            "summary": csum if isinstance(csum, dict) else None, "stderr_tail": (err or "")[-300:]}


def _task_seen(progress: dict, n: int, core_s: float) -> None:
    """Record `n` finished tasks that took `core_s` (the per-task bound for work in flight)."""
    if n > 0:
        progress["done_tasks"] = int(progress.get("done_tasks") or 0) + n
        progress["max_task_core_s"] = max(float(progress.get("max_task_core_s") or 0), float(core_s) / n)


def _rows_done(progress: dict, rows: list, core_s: float | None = None) -> None:
    """Finished rows: update the per-task bound and hand them to the row sink."""
    timed = [r for r in rows if isinstance(r.get("seconds"), (int, float))]
    if core_s is None:
        _task_seen(progress, len(timed), _known_core_s(timed))
    else:
        _task_seen(progress, len(timed) or len(rows), core_s)
    sink = progress.get("_rows")
    if sink and rows:
        try:
            sink(rows)
        except Exception as e:  # noqa: BLE001
            progress["_rows"] = None
            print(f"[compute] partial results not written ({type(e).__name__}: {e}); the batch continues",
                  file=sys.stderr, flush=True)


def recycle_every(procs: int) -> int:
    """Finished inputs after which the local pool is replaced."""
    v = os.environ.get("MA_COMPUTE_RECYCLE_EVERY")
    try:
        return max(0, int(v)) if v not in (None, "") else 15 * max(1, int(procs))
    except ValueError:
        return 15 * max(1, int(procs))


def _map_local(run_dir, fn_spec, inputs, max_workers, deadline=None, allowance_s=None, progress=None,
               peak_mb=None, prior_ok=0, on_note=None, repo=None):
    """Always a fresh spawn pool, even for one worker."""
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
    eff_repo = repo or REPO

    def new_pool(k):
        return cf.ProcessPoolExecutor(max_workers=k, mp_context=mp.get_context("spawn"),
                                      initializer=_local_init,
                                      initargs=(run_dir, fn_spec, init_dir.name, _guard_registration(), eff_repo))

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
    alone = {}
    toks = {}
    shrink = False
    recycle, since, recycles = recycle_every(cap), 0, 0
    prior_ok = int(prior_ok or 0) + int(os.environ.get("MA_COMPUTE_PRIOR_OK") or 0)

    def init_core_s():
        t = 0.0
        for name in os.listdir(init_dir.name):
            if not name.endswith(".err"):
                try:
                    t += float(open(os.path.join(init_dir.name, name)).read())
                except (OSError, ValueError):
                    pass
        return t

    def rebudget():
        nonlocal deadline, why
        if allowance_s is not None:
            busy = max(cap, len(running))
            now = time.time()
            used = progress["core_s"] + init_core_s() + sum(now - t for _, t in running.values())
            deadline, why = _budget_deadline(user_deadline, max(0.0, allowance_s - used), busy)
    ex = new_pool(cap)
    try:
        while todo or running:
            broke = False
            if shrink and not running:
                ex.shutdown(wait=True)
                _kill_groups(pids, grace_s=0, direct=False)
                ex, shrink, since = new_pool(cap), False, 0
                rebudget()
            now = time.time()
            k = 0 if shrink else max(0, min(len(todo), cap - len(running)))

            def note(complete):
                for pid in set((getattr(ex, "_processes", None) or {}).keys()) - set(toks):
                    toks[pid] = _pid_start(pid)
                if on_note is not None:
                    on_note(progress, [{"n": 1, "started": t, "cores": 1} for _, t in running.values()]
                            + ([] if complete else [{"n": 1, "started": now, "cores": 1}] * k),
                            extra_core_s=init_core_s() + progress.get("unreturned_s", 0.0),
                            pids=[[p_, t_] for p_, t_ in toks.items()],
                            pids_complete=complete)
            note(k == 0)
            try:
                while todo and len(running) < cap and not shrink:
                    f = ex.submit(_local_chunk, [todo[0]])
                    running[f] = (todo.pop(0), time.time())
            except BrokenProcessPool:
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
                        if progress.get("_file"):
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
                progress["core_s"] += sum(now - t for _, t in crashed)
                pids |= live
                _kill_groups(live, grace_s=0.5)
                ex.shutdown(wait=True, cancel_futures=True)
                _kill_groups(pids, grace_s=0, direct=False)
                _kill_chunk_group_others()
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
                        todo.insert(0, crashed[0][0])
                else:
                    todo = [it for it, _ in crashed] + todo
                    cap = max(1, cap // 2)
                rebudget()
                ex, shrink, since = new_pool(cap), False, 0
            if seen_peak and _mem_slots(seen_peak, avail, n) < cap:
                cap = _mem_slots(seen_peak, avail, n)
                shrink = True
                rebudget()
            if recycle and since >= recycle and todo and not shrink:
                shrink, recycles = True, recycles + 1
            if todo or running:
                reason = _breaker(rows, prior_ok=prior_ok)
                if reason:
                    stop = reason
                    break
            if (todo or running) and allowance_s is not None and progress["core_s"] + init_core_s() >= allowance_s:
                stop = CAP_STOP
                break
    finally:
        live = set((getattr(ex, "_processes", None) or {}).keys())
        pids |= live
        in_flight = list(running.values())
        if running:
            _kill_groups(live)
            _kill_groups(pids - live, grace_s=0, direct=False)
        ex.shutdown(wait=True, cancel_futures=True)
        _kill_groups(pids, grace_s=0, direct=False)
        t_end = time.time()
        extra = sum(t_end - t for _, t in in_flight) + init_core_s() + progress.get("unreturned_s", 0.0)
        progress.update(settled=True, settled_extra_s=extra)
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


def _read_inputs(path: str) -> list:
    txt = open(path).read().strip()
    if txt.startswith("["):
        return json.loads(txt)
    return [json.loads(l) for l in txt.splitlines() if l.strip()]


def _self_test() -> int:
    import tempfile
    import uuid
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    ok(not any(isinstance(v, type(sys)) and getattr(v, "__name__", "") == "compute"
               for v in globals().values()) and "compute" not in sys.modules,
       "CW1 compute_worker has zero imports from compute.py")
    ok(_mem_slots(5000, 64000, 8) == 8 and _mem_slots(9000, 64000, 8) == 5, "CW2 _mem_slots")
    ok(_chunks(list(range(5)), 2) == [[0, 1, 2], [3, 4]], "CW3 _chunks")
    ok(_breaker([{"ok": False, "kind": INFRA, "error": "e"}] * 20) is None, "CW4 _breaker ignores infra")
    errs = [{"ok": False, "kind": MODEL, "error": "ValueError: x"}] * 8
    ok(_breaker(errs) is not None and _breaker(errs, prior_ok=300) is None, "CW5 _breaker checks prior_ok")

    cid = "cw-selftest-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        os.makedirs(os.path.join(d, "models"))
        fn_path = os.path.join(d, "models", "m.py")
        open(fn_path, "w").write("def f(p):\n    return {'y': p['x'] * 2}\n")
        ok(_file_digest(fn_path) == hashlib.sha256(open(fn_path, "rb").read()).hexdigest(), "CW6 _file_digest raw sha256")
        rows, info = _map_local(d, "models/m.py::f", [{"x": 1}, {"x": 2}], max_workers=1)
        ok(len(rows) == 2 and rows[0]["result"] == {"y": 2} and info["stopped"] is None, f"CW7 _map_local: {info}")
        cdir = os.path.join(tempfile.gettempdir(), _CALLS_DIR, cid)
        r1 = _revoke_chunk(cid, "t1")
        c1 = _claim(cdir, "t1", "start")
        ok(r1 and c1 == (False, "revoke"), f"CW8 _claim/_revoke_chunk: {r1} {c1}")
        import shutil
        shutil.rmtree(cdir, ignore_errors=True)
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compute worker chunk runner")
    ap.add_argument("cmd", nargs="?", choices=["map"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--fn")
    ap.add_argument("--inputs")
    ap.add_argument("--out")
    ap.add_argument("--backend", default="local", choices=["local"])
    ap.add_argument("--max-workers", type=int)
    ap.add_argument("--max-tasks", type=int, default=DEFAULT_MAX_TASKS)
    ap.add_argument("--timeout-s", type=float)
    ap.add_argument("--max-core-hours", type=float)
    ap.add_argument("--no-ledger", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out-root", help=argparse.SUPPRESS)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    import signal

    def _on_term(*_):
        raise KeyboardInterrupt
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
            return 3
    for k in ("MA_COMPUTE_GATE", "MA_COMPUTE_CANCEL"):
        os.environ.pop(k, None)
    if a.cmd != "map" or not (a.run_dir and a.fn and a.inputs and a.out):
        ap.error("usage: compute_worker.py map <run_dir> --fn <path.py>::<fn> --inputs <jsonl> --out <jsonl> --no-ledger")
    if not a.no_ledger:
        ap.error("compute_worker.py requires --no-ledger; use scripts/compute.py for ledgered batches")
    run_dir = os.path.realpath(a.run_dir)
    inputs = _read_inputs(a.inputs)
    if len(inputs) > a.max_tasks:
        raise ValueError(f"{len(inputs)} tasks exceeds --max-tasks {a.max_tasks}")
    allowance_s = None if a.max_core_hours is None else a.max_core_hours * 3600
    t0 = time.time()
    deadline = t0 + a.timeout_s if a.timeout_s else None
    rows, info = _map_local(run_dir, a.fn, inputs, a.max_workers, deadline=deadline, allowance_s=allowance_s)
    done = {r["i"] for r in rows}
    why = info.get("stopped") or "timed out"
    rows += [{"i": i, "ok": False, "kind": STOPPED, "error": why} for i in range(len(inputs)) if i not in done]
    rows.sort(key=lambda r: r["i"])
    wall = time.time() - t0
    est = min(info.pop("unknown_chunks"), info["slots"]) * wall * info["cores_per_slot"]
    core_s = info.pop("core_s") + est
    failed_by = {}
    for r in rows:
        if not r["ok"]:
            failed_by[r.get("kind") or MODEL] = failed_by.get(r.get("kind") or MODEL, 0) + 1
    summary = {**{k: v for k, v in info.items() if k not in ("slots", "cores_per_slot")},
               "n": len(rows), "n_ok": sum(r["ok"] for r in rows),
               "n_failed": sum(not r["ok"] for r in rows), "failed_by_kind": failed_by, "wall_s": round(wall, 1),
               "task_core_s": round(core_s, 1), "estimated_core_s": round(est, 1),
               "cap_outlook": None, "model_s": round(_known_core_s(rows), 1)}
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from lib.safe_io import publish
    root = a.out_root or run_dir
    publish(a.out, "".join(json.dumps(r) + "\n" for r in rows), root)
    publish(a.out + ".summary.json", json.dumps(summary, indent=1), root)
    print(json.dumps({k: v for k, v in summary.items() if k not in ("bundle", "chunks")}))
    return 0 if summary["n_failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
