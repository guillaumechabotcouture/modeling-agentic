#!/usr/bin/env python3
"""Minimal-mount bubblewrap sandbox and verified-bytes worker for agent-built engines
(docs/design/agent-built-engines.md §3.2 & §5.1).

    python3 toolkit/engines/built/worker.py --self-test

The harness reads verified snapshot bytes ONCE, sends them to this worker inside an explicit
minimal-mount bubblewrap sandbox (`_bwrap_engine_cmd`: interpreter prefix, standard site-packages,
`contract.py`, `hooks.py`, `/proc`, `/dev`, private tmpfs — no repo, no home, no run directory, no
network), compiles each module from decoded UTF-8 verified source, enforces opaque unit/package IDs,
phased `TrackedParams`, `BalanceAnalyzer`, bounded output pipes, and harness-computed
`meta.age_shares`. Nothing here names a disease.
"""
from __future__ import annotations

import atexit
import base64
import copy
import datetime as _dt
import encodings
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import math
import os
import selectors
import shutil
import signal
import site
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import types
from collections.abc import Mapping

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))

DEFAULT_LIMITS = {
    "wall_s": 120,
    "cpu_s": 120,
    "memory_bytes": 4 * 1024 ** 3,
    "file_bytes": 64 * 1024 ** 2,
}
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 256 * 1024
UNSANDBOXED_MARKER = "engine_build/unsandboxed.json"
AGE_POP_ATTR_PRIORITY = (
    "age_population", "age_populations", "age_pop", "population_by_age", "pop_by_age",
    "age_shares", "age_weights", "age_distribution",
)
_AGE_POP_INCLUDE = ("pop", "share", "weight", "dist")
_AGE_POP_EXCLUDE = ("contact", "rate", "mort", "hazard", "eff")
_SANDBOX_CACHE: list[str | None] = []
_SANDBOX_DIAG: list[str] = []
_RO_MOUNT_CACHE: dict[tuple, list[str]] = {}
_SESSION_MAX_PER_PROC = 2
_SESSION_MAX_REQUESTS = {"closed_form": 2000, "starsim_module": 64}
_SESSION_POOL_LOCK = threading.Lock()
_SESSION_POOL: dict[tuple, "_WorkerSession"] = {}
_SESSION_POOL_PID: int = os.getpid()


class EngineUnsandboxed(RuntimeError):
    """Raised when no bubblewrap sandbox is available and the development override is not set."""


class WorkerError(RuntimeError):
    """Raised when the sandboxed engine worker fails or returns an invalid payload."""


def unsandboxed_finding(reason: str = "no bubblewrap sandbox available (development override)") -> dict:
    """HIGH non-declarable finding emitted when a run executes a built engine unsandboxed (BE35)."""
    return {
        "kind": "engine_unsandboxed",
        "severity": "HIGH",
        "stage": "ENGINE_ADMIT",
        "target_stage": "ENGINE",
        "key": "engine_sandbox",
        "claim": f"agent-built engine ran without the bubblewrap sandbox ({reason}); a run using the "
                 "unsandboxed development override cannot reach ACCEPT",
        "scope_declarable": False,
    }


def _safe_io():
    scripts = os.path.join(ROOT, "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from lib import safe_io as SIO
    return SIO


def record_unsandboxed(run_dir: str, *, action: str, engine_id: str | None = None,
                       reason: str = "no bubblewrap sandbox available (development override)") -> None:
    """Atomically persist a durable harness marker whenever a built engine runs unsandboxed (BE35)."""
    SIO = _safe_io()
    existing_actions: list[str] = []
    try:
        raw, _ = SIO.read_nofollow(run_dir, UNSANDBOXED_MARKER)
        prev = json.loads(raw.decode("utf-8"))
        if isinstance(prev, dict) and isinstance(prev.get("actions"), list):
            existing_actions = [str(a) for a in prev["actions"]]
    except (FileNotFoundError, SIO.LinkRefused, OSError, UnicodeDecodeError, ValueError):
        pass
    if action not in existing_actions:
        existing_actions.append(str(action))
    rec = {
        "unsandboxed": True,
        "engine_id": engine_id,
        "actions": existing_actions,
        "reason": str(reason),
        "platform": sys.platform,
        "recorded_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    SIO.write_nofollow(run_dir, UNSANDBOXED_MARKER, json.dumps(rec, indent=1, sort_keys=True).encode("utf-8"),
                       atomic=True)


def unsandboxed_findings(run_dir: str) -> list[dict]:
    """Gate view of durable unsandboxed built-engine execution markers (empty = sandboxed / unused)."""
    marker_path = os.path.join(run_dir, UNSANDBOXED_MARKER)
    if not os.path.lexists(marker_path):
        return []
    SIO = _safe_io()
    try:
        raw, st = SIO.read_nofollow(run_dir, UNSANDBOXED_MARKER)
        if st.st_nlink > 1:
            return [unsandboxed_finding("engine_build/unsandboxed.json has multiple hard links")]
        doc = json.loads(raw.decode("utf-8"))
    except (SIO.LinkRefused, OSError, UnicodeDecodeError, ValueError) as e:
        return [unsandboxed_finding(f"engine_build/unsandboxed.json unreadable or tampered ({e})")]
    if isinstance(doc, dict) and doc.get("unsandboxed") is False:
        return []
    reason = doc.get("reason") if isinstance(doc, dict) and doc.get("reason") else (
        "no bubblewrap sandbox available (development override)"
    )
    return [unsandboxed_finding(str(reason))]


def _ro_mount_prefixes() -> list[str]:
    """Minimal read-only runtime paths needed for Python + numpy/scipy/starsim without mounting `/`,
    the repository, any run directory, home/temp directories, or arbitrary `sys.path` entries."""
    cache_key = (
        sys.executable,
        sys.prefix,
        sys.base_prefix,
        os.environ.get("LD_LIBRARY_PATH") or "",
        os.environ.get("PYTHONHOME") or "",
    )
    cached = _RO_MOUNT_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)
    candidates: list[str] = []
    runtime_paths: list[str] = []
    repo_real = os.path.realpath(ROOT)
    forbidden = {"/", "/tmp", "/var", "/home", "/root", "/bin", "/sbin", "/lib", "/lib64", "/lib32", repo_real}

    def _safe_prefix(path: str) -> bool:
        return bool(path) and path not in (*forbidden, "/opt", "/usr", "/lib", "/lib64") and not path.startswith(
            repo_real + os.sep
        )

    def collect(path: str, *, runtime: bool = False) -> None:
        if not path or not os.path.exists(path):
            return
        norm = os.path.normpath(os.path.abspath(path))
        real = os.path.realpath(path)
        for target in dict.fromkeys((norm, real)):
            if target in forbidden:
                continue
            if target.startswith(repo_real + os.sep):
                rel = os.path.relpath(target, repo_real).replace(os.sep, "/")
                if not (rel in (".venv", "venv") or rel.startswith((".venv/", "venv/"))):
                    continue
            candidates.append(target)
            if runtime:
                runtime_paths.append(target)

    def collect_from_module_file(filepath: str | None, *, pkg_levels: int = 1) -> None:
        if not filepath or not os.path.exists(filepath):
            return
        for base_f in dict.fromkeys((os.path.abspath(filepath), os.path.realpath(filepath))):
            cur = os.path.dirname(base_f)
            collect(cur, runtime=True)
            for _ in range(pkg_levels):
                cur = os.path.dirname(cur)
                if _safe_prefix(cur):
                    collect(cur, runtime=True)
            if os.path.basename(cur) in ("lib", "lib64"):
                pref = os.path.dirname(cur)
                if _safe_prefix(pref):
                    collect(pref, runtime=True)

    # Standard OS runtime libs and dynamic-linker config (no secrets)
    for base in ("/usr", "/etc/ld.so.cache", "/etc/ld.so.conf", "/etc/ld.so.conf.d",
                 "/etc/alternatives", "/etc/ssl/certs"):
        if os.path.exists(base):
            collect(base)

    # Python interpreter prefixes & executable directories (never arbitrary sys.path entries)
    exe_abs = os.path.abspath(sys.executable)
    exe_real = os.path.realpath(sys.executable)
    exe_gp = os.path.dirname(os.path.dirname(exe_real))
    base_exe = getattr(sys, "_base_executable", "") or ""
    base_exe_real = os.path.realpath(base_exe) if base_exe and os.path.exists(base_exe) else ""
    base_exe_gp = os.path.dirname(os.path.dirname(base_exe_real)) if base_exe_real else ""
    for cand in (
        sys.prefix,
        sys.base_prefix,
        sys.exec_prefix,
        sys.base_exec_prefix,
        os.path.dirname(exe_abs),
        os.path.dirname(exe_real),
        exe_gp if _safe_prefix(exe_gp) else "",
        os.path.dirname(os.path.abspath(base_exe)) if base_exe else "",
        os.path.dirname(base_exe_real) if base_exe_real else "",
        base_exe_gp if _safe_prefix(base_exe_gp) else "",
    ):
        if cand:
            collect(cand, runtime=True)

    # Virtualenv pyvenv.cfg `home = ...` directory (and its parent prefix), if present
    for cfg_dir in dict.fromkeys((sys.prefix, os.path.dirname(os.path.dirname(exe_abs)))):
        cfg = os.path.join(cfg_dir, "pyvenv.cfg")
        if os.path.isfile(cfg):
            try:
                with open(cfg, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        k, sep, v = line.partition("=")
                        if sep and k.strip().lower() == "home":
                            home_dir = v.strip()
                            if home_dir and os.path.isdir(home_dir):
                                collect(home_dir, runtime=True)
                                home_parent = os.path.dirname(home_dir)
                                if _safe_prefix(home_parent):
                                    collect(home_parent, runtime=True)
                            break
            except OSError:
                pass

    # Authoritative stdlib & sysconfig roots (logical and realpath, covering symlinked conda/uv/coiled layouts)
    collect_from_module_file(getattr(encodings, "__file__", None), pkg_levels=2)
    collect_from_module_file(getattr(os, "__file__", None), pkg_levels=1)
    try:
        for sp in sysconfig.get_paths().values():
            if sp and os.path.exists(sp):
                collect(sp, runtime=True)
    except Exception:  # noqa: BLE001
        pass
    for var in ("LIBDIR", "LIBPL", "DESTLIB", "BINLIBDEST", "PREFIX", "EXEC_PREFIX", "base", "platbase"):
        val = sysconfig.get_config_var(var)
        if isinstance(val, str) and val and os.path.isdir(val):
            collect(val, runtime=True)

    # Official site-packages + symlink targets of core runtime & scientific libraries (never arbitrary sys.path)
    try:
        for sp in site.getsitepackages():
            if sp and os.path.isdir(sp):
                collect(sp, runtime=True)
    except AttributeError:
        pass
    for mod_name in ("math", "_json", "zlib", "numpy", "scipy", "yaml", "starsim"):
        try:
            spec = importlib.util.find_spec(mod_name)
        except (ImportError, ValueError, AttributeError):
            spec = None
        origin = getattr(spec, "origin", None) if spec is not None else None
        if origin and origin not in ("built-in", "frozen") and os.path.exists(origin):
            collect_from_module_file(origin, pkg_levels=2 if os.path.basename(origin) == "__init__.py" else 1)

    # Zipped stdlib archive (e.g., python314.zip) if present on sys.path (never arbitrary sys.path directories)
    for sp in sys.path:
        if sp and os.path.isfile(sp) and os.path.basename(sp).startswith("python") and sp.endswith(".zip"):
            collect(sp, runtime=True)

    # Shared-library & explicit Python home dirs needed by dynamic libpython / conda / setup-python
    for env_key in ("LD_LIBRARY_PATH", "PYTHONHOME"):
        for lp in (os.environ.get(env_key) or "").split(os.pathsep):
            lp = lp.strip()
            if lp and os.path.isdir(lp):
                collect(lp, runtime=True)

    # If the Python runtime lives under `/opt/<subroot>` (e.g. `/opt/coiled`, `/opt/conda`, `/opt/hostedtoolcache`),
    # mount `/opt/<subroot>` read-only so cross-directory conda/mamba/pixi symlinks inside it resolve cleanly.
    for rp in list(runtime_paths):
        if rp.startswith("/opt/"):
            parts = rp.split("/")
            if len(parts) >= 3 and parts[2]:
                sub = "/opt/" + parts[2]
                if os.path.isdir(sub) and sub != repo_real and not repo_real.startswith(sub + os.sep):
                    collect(sub)

    # Mount shallowest paths first so parents precede children; drop children covered by a mounted dir
    mounts: list[str] = []
    seen_dirs: list[str] = []
    seen: set[str] = set()
    for target in sorted(dict.fromkeys(candidates), key=lambda p: (p.count("/"), p)):
        if target in seen:
            continue
        if any(target.startswith(d.rstrip("/") + "/") for d in seen_dirs):
            continue
        seen.add(target)
        mounts.append(target)
        if os.path.isdir(target) and not os.path.islink(target):
            seen_dirs.append(target)
    _RO_MOUNT_CACHE[cache_key] = list(mounts)
    return mounts


def _ancestor_dirs(paths: list[str]) -> list[str]:
    """Return strict ancestor directories of `paths` (shallowest first) that bubblewrap would otherwise
    auto-synthesize as `0700` owned by root under `bwrap-sudo` (causing EACCES after `setpriv` drops uid)."""
    covered_dirs = {os.path.normpath(p).rstrip("/") for p in paths if p and os.path.isdir(p)}
    skip = {"/", "/dev", "/proc", "/tmp", "/bin", "/sbin", "/lib", "/lib64", "/lib32"} | {
        os.path.normpath(p).rstrip("/") for p in paths if p
    }
    ancs: set[str] = set()
    for p in paths:
        cur = os.path.dirname(os.path.normpath(p))
        while cur and cur != "/":
            if cur not in skip and not any(cur.startswith(d + "/") for d in covered_dirs):
                ancs.add(cur)
            nxt = os.path.dirname(cur)
            if nxt == cur:
                break
            cur = nxt
    return sorted(ancs, key=lambda d: (d.count("/"), d))


def _bwrap_engine_cmd(kind: str, envargs: list[str], child: list[str],
                      *, ro_prefixes: list[str] | None = None) -> list[str]:
    """Build the minimal-mount bubblewrap command (§5.1): NO `--ro-bind / /`.
    Mounts ONLY the interpreter/site-packages prefixes, `contract.py`, `hooks.py`, `worker.py`,
    `/proc`, `/dev`, and a private `--tmpfs /tmp`."""
    uid, gid = str(os.getuid()), str(os.getgid())
    if kind == "bwrap":
        ns = ["--unshare-all", "--uid", uid, "--gid", gid]
        drop: list[str] = []
    else:
        ns = ["--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
        drop = ["setpriv", "--reuid", uid, "--regid", gid, "--clear-groups", "--no-new-privs",
                "--inh-caps=-all", "--bounding-set=-all", "--"]

    mount_list = _ro_mount_prefixes() if ro_prefixes is None else list(ro_prefixes)
    binds: list[str] = []
    for top in ("/bin", "/sbin", "/lib", "/lib64", "/lib32"):
        if os.path.islink(top):
            binds += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            binds += ["--ro-bind", top, top]

    # Pre-create strict ancestor dirs with 0755 so bwrap-sudo + setpriv never hits 0700 root-owned parents
    for anc in _ancestor_dirs(mount_list):
        binds += ["--perms", "0755", "--dir", anc]

    for p in mount_list:
        binds += ["--ro-bind", p, p]

    # Harness files ONLY, mounted read-only under /harness using PEP 420 namespace packages
    # (never mounting the repo directory and never binding /dev/null onto regular files).
    contract_py = os.path.join(ROOT, "toolkit", "engines", "contract.py")
    hooks_py = os.path.join(ROOT, "toolkit", "engines", "hooks.py")
    worker_py = os.path.abspath(__file__)
    binds += [
        "--perms", "0755", "--tmpfs", "/harness",
        "--perms", "0755", "--dir", "/harness/toolkit",
        "--perms", "0755", "--dir", "/harness/toolkit/engines",
        "--ro-bind", contract_py, "/harness/toolkit/engines/contract.py",
        "--ro-bind", hooks_py, "/harness/toolkit/engines/hooks.py",
        "--ro-bind", worker_py, "/harness/worker.py",
        "--remount-ro", "/harness",
    ]
    return (["sudo", "-n"] if kind == "bwrap-sudo" else []) + [
        "bwrap",
        "--dev", "/dev",
        "--proc", "/proc",
        "--perms", "1777", "--tmpfs", "/tmp",
        *binds,
        *ns,
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        *envargs,
        "--chdir", "/tmp",
        *drop,
        *child,
    ]


def _minimal_env(hash_seed: str = "0", mounted_prefixes: list[str] | None = None) -> dict[str, str]:
    keep = {k: os.environ[k] for k in ("LANG", "LC_ALL", "TZ") if k in os.environ}
    raw_path = os.environ.get("PATH") or "/usr/bin:/bin"
    path_parts = [p for p in raw_path.split(os.pathsep) if p]
    for req_p in ("/usr/bin", "/bin"):
        if req_p not in path_parts:
            path_parts.append(req_p)
    keep["PATH"] = os.pathsep.join(path_parts)
    for env_key in ("LD_LIBRARY_PATH", "PYTHONHOME"):
        if os.environ.get(env_key):
            if mounted_prefixes is None:
                keep[env_key] = os.environ[env_key]
            else:
                ok_parts = []
                for lp in os.environ[env_key].split(os.pathsep):
                    lp_n = os.path.normpath(lp.strip()) if lp.strip() else ""
                    if lp_n and any(lp_n == m or lp_n.startswith(m.rstrip("/") + "/") for m in mounted_prefixes):
                        ok_parts.append(lp_n)
                if ok_parts:
                    keep[env_key] = os.pathsep.join(ok_parts)
    return dict(
        keep,
        HOME="/tmp",
        TMPDIR="/tmp",
        USER="sandbox",
        LOGNAME="sandbox",
        PYTHONHASHSEED=str(hash_seed),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
    )


def _probe_engine_bwrap(kind: str) -> tuple[bool, str]:
    prefixes = _ro_mount_prefixes()
    env = _minimal_env("0", mounted_prefixes=prefixes)
    envargs = [a for k, v in env.items() for a in ("--setenv", k, v)]
    cmd = _bwrap_engine_cmd(kind, envargs, [sys.executable, "-s", "-B", "/harness/worker.py", "_probe"],
                            ro_prefixes=prefixes)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        if r.returncode == 0 and r.stdout.strip() == "OK":
            return True, ""
        return False, (
            f"{kind}: rc={r.returncode} err={r.stderr.strip()[-350:]!r} out={r.stdout.strip()[-80:]!r} "
            f"exe={sys.executable!r} pre={sys.prefix!r} base={sys.base_prefix!r} "
            f"enc={getattr(encodings, '__file__', None)!r} mounts={prefixes!r}"
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"{kind}: {type(e).__name__}: {e}"


def engine_sandbox_kind() -> str | None:
    """Return 'bwrap', 'bwrap-sudo', or None (no minimal-mount bubblewrap sandbox available)."""
    if _SANDBOX_CACHE:
        return _SANDBOX_CACHE[0]
    kind: str | None = None
    diags: list[str] = []
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        ok_b, d_b = _probe_engine_bwrap("bwrap")
        if ok_b:
            kind = "bwrap"
        else:
            diags.append(d_b)
            if shutil.which("sudo") and shutil.which("setpriv"):
                ok_s, d_s = _probe_engine_bwrap("bwrap-sudo")
                if ok_s:
                    kind = "bwrap-sudo"
                else:
                    diags.append(d_s)
    elif sys.platform.startswith("linux"):
        diags.append("bwrap binary not on PATH")
    else:
        diags.append(f"platform={sys.platform}")
    _SANDBOX_CACHE.append(kind)
    _SANDBOX_DIAG[:] = diags
    return kind


def _run_bounded_subprocess(
    cmd: list[str],
    stdin_bytes: bytes,
    *,
    timeout_s: float,
    cwd: str | None,
    env: dict[str, str] | None,
    max_stdout: int,
    max_stderr: int,
) -> tuple[int, bytes, bytes]:
    """Run subprocess with hard parent-side bounds on stdout and stderr, killing child immediately
    on overflow or timeout so runaway worker output can never exhaust harness memory."""
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
    )
    out_buf = bytearray()
    err_buf = bytearray()
    overflow: list[tuple[str, int]] = []
    lock = threading.Lock()

    def _feed():
        try:
            assert proc.stdin is not None
            proc.stdin.write(stdin_bytes)
            proc.stdin.flush()
        except OSError:
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except OSError:
                pass

    def _drain(stream, buf: bytearray, cap: int, name: str):
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                if len(buf) + len(chunk) > cap:
                    with lock:
                        if not overflow:
                            overflow.append((name, cap))
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    break
                buf.extend(chunk)
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    t_in = threading.Thread(target=_feed, daemon=True)
    t_out = threading.Thread(target=_drain, args=(proc.stdout, out_buf, max_stdout, "stdout"), daemon=True)
    t_err = threading.Thread(target=_drain, args=(proc.stderr, err_buf, max_stderr, "stderr"), daemon=True)
    t_in.start()
    t_out.start()
    t_err.start()

    deadline = time.monotonic() + max(timeout_s, 0.1)
    timed_out = False
    try:
        rc = proc.wait(timeout=max(deadline - time.monotonic(), 0.1))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
        except OSError:
            pass
        rc = proc.wait()
    for t in (t_in, t_out, t_err):
        t.join(timeout=2.0)

    if timed_out:
        raise WorkerError(f"engine worker timed out after {timeout_s}s")
    if overflow:
        which, cap = overflow[0]
        raise WorkerError(f"engine worker exceeded {which} output limit ({cap} bytes)")
    return rc, bytes(out_buf), bytes(err_buf)


# --------------------------------------------------------------------------- in-worker runtime
class _TrackedParamsView(Mapping):
    """Shared-phase read-only view returned by `TrackedParams.copy()` so copying params in `build_sim`
    does not eagerly mark unread parameters as read in `build` (preserving BE5 phase attribution)."""

    def __init__(self, parent: "TrackedParams"):
        self._parent = parent

    def __getitem__(self, key):
        return self._parent[key]

    def get(self, key, default=None):
        return self._parent.get(key, default)

    def __contains__(self, key):
        return key in self._parent

    def __iter__(self):
        return iter(self._parent._data)

    def __len__(self):
        return len(self._parent._data)

    def copy(self):
        return _TrackedParamsView(self._parent)

    def __repr__(self):
        return f"TrackedParams({self._parent._data!r})"


class TrackedParams(Mapping):
    """Parameter mapping recording every parameter key accessed per execution phase (`build` vs
    `read_outcomes`). Subclasses `collections.abc.Mapping` (not `dict`) so `dict(params)` and
    `{**params}` invoke `__getitem__` and record reads rather than bypassing via C-level dict fast paths."""

    def __init__(self, initial: Mapping[str, float]):
        self._data: dict[str, float] = {str(k): float(v) for k, v in initial.items()}
        self._phase = "build"
        self.reads_by_phase: dict[str, set[str]] = {"build": set(), "read_outcomes": set()}
        self.reads: set[str] = set()

    def set_phase(self, phase: str) -> None:
        self._phase = phase
        self.reads_by_phase.setdefault(phase, set())

    def _record(self, key) -> None:
        sk = str(key)
        self.reads.add(sk)
        self.reads_by_phase[self._phase].add(sk)

    def __getitem__(self, key):
        val = self._data[key]
        self._record(key)
        return val

    def get(self, key, default=None):
        if key in self._data:
            self._record(key)
            return self._data[key]
        return default

    def __contains__(self, key):
        if key in self._data:
            self._record(key)
            return True
        return False

    def __iter__(self):
        return iter(self._data)

    def __len__(self):
        return len(self._data)

    def copy(self):
        return _TrackedParamsView(self)

    def __repr__(self):
        return f"TrackedParams({self._data!r})"


def _opaque_id(salt: str, raw_id: str) -> str:
    return hashlib.sha256(f"{salt}:{raw_id}".encode("utf-8")).hexdigest()[:16]


def _normalize_age_vec(age_groups: list[str], vec) -> dict[str, float] | None:
    if not (isinstance(vec, (list, tuple)) and len(vec) == len(age_groups)):
        return None
    try:
        fv = [float(x) for x in vec]
    except (TypeError, ValueError):
        return None
    tot = sum(fv)
    if tot > 0 and all(math.isfinite(x) and x >= 0 for x in fv):
        return {str(g): x / tot for g, x in zip(age_groups, fv)}
    return None


def _compute_age_shares(age_groups: list[str], unit_attrs: Mapping, override_shares: Mapping | None) -> dict[str, float]:
    """Harness-computed `meta.age_shares` — always overwrites whatever the model returned (BE14).
    Deterministic and independent of attribute dict insertion order; ignores non-population vectors."""
    if isinstance(override_shares, Mapping) and override_shares:
        vals = {str(g): float(override_shares.get(g, 0.0)) for g in age_groups}
        tot = sum(vals.values())
        if tot > 0 and all(math.isfinite(v) and v >= 0 for v in vals.values()):
            return {g: v / tot for g, v in vals.items()}
    attrs = unit_attrs or {}
    lower_map = {str(k).lower(): attrs[k] for k in sorted(attrs, key=str)}
    # 1. Exact priority population-vector attribute names
    for pname in AGE_POP_ATTR_PRIORITY:
        if pname in lower_map:
            norm = _normalize_age_vec(age_groups, lower_map[pname])
            if norm is not None:
                return norm
    # 2. Deterministic fallback: sorted keys containing 'age' + a population/share token, excluding non-population keys
    for k in sorted(lower_map):
        if ("age" in k and any(tok in k for tok in _AGE_POP_INCLUDE)
                and not any(tok in k for tok in _AGE_POP_EXCLUDE)):
            norm = _normalize_age_vec(age_groups, lower_map[k])
            if norm is not None:
                return norm
    n = max(len(age_groups), 1)
    return {str(g): 1.0 / n for g in age_groups}


class _SnapshotFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """In-memory loader resolving ONLY sibling `.py` modules from the verified snapshot bytes."""

    def __init__(self, sources: dict[str, bytes]):
        self._sources = {n[:-3]: b for n, b in sources.items() if n.endswith(".py")}
        self._pkg = "_built_snapshot"

    def find_spec(self, fullname, path=None, target=None):
        short = fullname[len(self._pkg) + 1:] if fullname.startswith(self._pkg + ".") else fullname
        if short in self._sources:
            return importlib.machinery.ModuleSpec(fullname, self, origin=f"<snapshot>/{short}.py")
        return None

    def create_module(self, spec):
        return types.ModuleType(spec.name)

    def exec_module(self, module):
        short = module.__name__[len(self._pkg) + 1:] if module.__name__.startswith(self._pkg + ".") else module.__name__
        src = self._sources[short]
        module.__package__ = self._pkg
        module.__file__ = f"<snapshot>/{short}.py"
        _exec_verified_bytes(src, module.__file__, module.__dict__)


def _exec_verified_bytes(src_bytes: bytes, filename: str, namespace: dict) -> None:
    """The ONE audited verified-bytes compile/exec site (scripts/import_audit.py, BE36).
    Decodes strict UTF-8 BEFORE compile so PEP-263 coding declarations can never diverge from S1-S4 AST."""
    src_text = src_bytes.decode("utf-8")
    code = compile(src_text, filename, "exec")
    exec(code, namespace)  # noqa: S102 -- verified snapshot bytes inside minimal-mount sandbox


def _load_model_from_bytes(files: dict[str, bytes], config: dict):
    """Compile verified snapshot bytes into isolated in-memory module namespace and construct Model.
    Keeps `_SnapshotFinder` active in `sys.meta_path` so method-local sibling imports resolve."""
    if "engine.py" not in files:
        raise WorkerError("snapshot has no engine.py")
    # Remove any stale snapshot finder/modules from an earlier load
    sys.meta_path[:] = [f for f in sys.meta_path if not isinstance(f, _SnapshotFinder)]
    for mname in [m for m in sys.modules if m == "_built_snapshot" or m.startswith("_built_snapshot.")]:
        sys.modules.pop(mname, None)
    pkg = types.ModuleType("_built_snapshot")
    pkg.__path__ = []
    sys.modules["_built_snapshot"] = pkg
    finder = _SnapshotFinder(files)
    sys.meta_path.insert(0, finder)
    mod = types.ModuleType("_built_snapshot.engine")
    mod.__package__ = "_built_snapshot"
    mod.__file__ = "<snapshot>/engine.py"
    sys.modules["_built_snapshot.engine"] = mod
    _exec_verified_bytes(files["engine.py"], mod.__file__, mod.__dict__)
    model_cls = getattr(mod, "Model", None)
    if not isinstance(model_cls, type):
        raise WorkerError("engine.py must define class Model")
    return model_cls(**(config or {}))


def _make_balance_analyzer(state_vars: Mapping):
    """Create a Starsim ss.Analyzer recording every declared compartment state per step."""
    import starsim as ss
    import numpy as np

    class _BalanceAnalyzer(ss.Analyzer):
        def __init__(self, sv):
            super().__init__(name="_harness_balance")
            self._sv = dict(sv)
            self.steps: list[dict[str, float]] = []
            self.min_elem: float | None = None

        def _resolve_module(self, mod_name: str):
            if mod_name == "people":
                return self.sim.people
            # Check Starsim 3.x module containers (ndict / Mapping) first for exact-case match
            for cname in ("diseases", "custom", "demographics", "connectors",
                          "networks", "interventions", "products", "analyzers"):
                cont = getattr(self.sim, cname, None)
                if isinstance(cont, Mapping) and mod_name in cont:
                    return cont[mod_name]
            get_mod = getattr(self.sim, "get_module", None)
            if callable(get_mod):
                try:
                    found = get_mod(mod_name, die=False, match_case=True)
                    if found is not None:
                        return found
                except Exception:  # noqa: BLE001
                    pass
            mods = getattr(self.sim, "modules", None)
            if isinstance(mods, Mapping) and mod_name in mods:
                return mods[mod_name]
            if mods is not None:
                try:
                    for m in mods:
                        if getattr(m, "name", None) == mod_name:
                            return m
                except TypeError:
                    pass
            return getattr(self.sim, mod_name, None)

        def _snap(self) -> dict[str, float]:
            row: dict[str, float] = {}
            for sname, loc in self._sv.items():
                mod_name, attr_name = loc
                target = self._resolve_module(str(mod_name))
                val = getattr(target, attr_name, None) if target is not None else None
                if val is None:
                    row[str(sname)] = float("nan")
                    self.min_elem = float("nan")
                else:
                    arr = np.asarray(val, dtype=float)
                    if arr.size:
                        mn = float(np.min(arr))
                        if self.min_elem is None or not math.isfinite(mn):
                            self.min_elem = mn
                        elif math.isfinite(self.min_elem) and mn < self.min_elem:
                            self.min_elem = mn
                    row[str(sname)] = float(np.sum(arr))
            return row

        def init_post(self):
            super().init_post()
            self.steps.append(self._snap())

        def step(self):
            self.steps.append(self._snap())

    return _BalanceAnalyzer(state_vars)


def _serialize_card(card) -> dict:
    return card.to_dict()


def _serialize_param(p) -> dict:
    return {
        "name": p.name, "value": float(p.value), "kind": p.kind, "units": p.units,
        "lo": float(p.lo) if p.lo is not None else None,
        "hi": float(p.hi) if p.hi is not None else None,
        "source": p.source, "verified": getattr(p, "verified", "unverified"),
    }


def _serialize_outcomes(out) -> dict:
    return {
        "per_capita": {str(k): {str(g): float(v) for g, v in m.items()} for k, m in out.per_capita.items()},
        "windows": {str(w): {str(m): float(v) for m, v in d.items()} for w, d in out.windows.items()},
        "cost_drivers": {str(k): float(v) for k, v in out.cost_drivers.items()},
        "meta": dict(out.meta or {}),
    }


def _run_one(model, kind: str, params_in: dict, unit_d: dict, pkg_d: dict, hor_d: dict, seed: int,
             id_salt: str, age_shares_override: dict | None) -> dict:
    from toolkit.engines.contract import Horizon, Outcomes, Package, UnitSpec, resolve_params

    resolved = resolve_params(model, params_in)
    card = model.describe()
    # Compute authoritative age_shares BEFORE invoking model code so in-place mutation cannot poison them
    raw_attrs = copy.deepcopy(unit_d.get("attributes") or {})
    auth_age_shares = _compute_age_shares(list(card.age_groups), raw_attrs, age_shares_override)
    opaque_u = UnitSpec(_opaque_id(id_salt, str(unit_d["id"])), copy.deepcopy(raw_attrs))
    opaque_p = Package(_opaque_id(id_salt, str(pkg_d["id"])), copy.deepcopy(pkg_d.get("components") or {}))
    hor = Horizon(float(hor_d["start"]), float(hor_d["stop"]), tuple(hor_d["window"]),
                  {str(k): list(v) for k, v in (hor_d.get("windows") or {}).items()})

    t_build = TrackedParams(resolved)
    balance_info: dict = {}

    if kind == "starsim_module":
        sim = model.build_sim(t_build, opaque_u, opaque_p, hor)
        if getattr(sim, "initialized", False):
            raise WorkerError("build_sim() returned an already-initialized Sim")
        sim.pars.rand_seed = int(seed)
        an = None
        if callable(getattr(model, "state_vars", None)):
            sv = model.state_vars()
            if sv:
                an = _make_balance_analyzer(sv)
                cur_an = getattr(sim.pars, "analyzers", None)
                if cur_an is None:
                    sim.pars.analyzers = [an]
                elif hasattr(cur_an, "append"):
                    cur_an.append(an)
                else:
                    sim.pars.analyzers = [cur_an, an]
        sim.run()
        t_build.set_phase("read_outcomes")
        out = model.read_outcomes(sim, opaque_u, opaque_p, hor)
        if an is not None:
            totals = [sum(step.values()) for step in an.steps] if an.steps else []
            mins = [min(step.values()) for step in an.steps if step]
            min_elem = an.min_elem if an.min_elem is not None else (min(mins) if mins else None)
            max_rel_drift = None
            if totals and totals[0] and math.isfinite(totals[0]):
                max_rel_drift = max(abs(t - totals[0]) / max(abs(totals[0]), 1e-12) for t in totals)
            balance_info = {
                "n_steps": len(an.steps),
                "initial_total": totals[0] if totals else None,
                "final_total": totals[-1] if totals else None,
                "max_rel_drift": max_rel_drift,
                "min_state": min_elem,
                "initial_states": dict(an.steps[0]) if an.steps else {},
                "final_states": dict(an.steps[-1]) if an.steps else {},
                "steps": an.steps[:200],
            }
    elif kind == "closed_form":
        out = model.evaluate(t_build, opaque_u, opaque_p, hor)
    else:
        raise WorkerError(f"unknown engine kind {kind!r}")

    if not isinstance(out, Outcomes):
        raise WorkerError(f"model did not return Outcomes (got {type(out).__name__})")
    # Always strip/overwrite harness-owned meta keys so model code can never forge them (BE14)
    meta = dict(out.meta or {})
    meta.pop("balance", None)
    meta.pop("param_reads", None)
    meta.pop("age_shares", None)
    meta["age_shares"] = auth_age_shares
    meta["param_reads"] = {
        "build": sorted(t_build.reads_by_phase["build"]),
        "read_outcomes": sorted(t_build.reads_by_phase["read_outcomes"]),
    }
    if balance_info:
        meta["balance"] = balance_info
    out = Outcomes(per_capita=out.per_capita, windows=out.windows, cost_drivers=out.cost_drivers, meta=meta)
    return _serialize_outcomes(out)


def _strip_cwd_from_sys_path() -> None:
    """Remove empty/cwd/temp entries from sys.path when running under `python -s -B`."""
    bad = {"", os.getcwd(), os.path.realpath(os.getcwd()), "/tmp", tempfile.gettempdir()}
    sys.path[:] = [p for p in sys.path if p not in bad and os.path.realpath(p) not in bad]


def _write_fd_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        n = os.write(fd, view[sent:])
        if n <= 0:
            raise OSError("short write to worker protocol fd")
        sent += n


def _apply_strict_rlimits(limits: Mapping) -> None:
    import resource
    for rname, key, extra in (("RLIMIT_AS", "memory_bytes", 0), ("RLIMIT_CPU", "cpu_s", 2),
                              ("RLIMIT_FSIZE", "file_bytes", 0)):
        r = getattr(resource, rname, None)
        if r is not None and key in limits:
            try:
                resource.setrlimit(r, (int(limits[key]), int(limits[key]) + extra))
            except (ValueError, OSError):
                pass


def _dispatch_action(model, kind: str, action: str, payload: Mapping, id_salt: str):
    if action == "introspect":
        return {
            "card": _serialize_card(model.describe()),
            "parameters": [_serialize_param(p) for p in model.parameters()],
        }
    if action == "describe":
        return _serialize_card(model.describe())
    if action == "parameters":
        return [_serialize_param(p) for p in model.parameters()]
    if action == "run":
        return _run_one(
            model, kind, payload.get("params") or {}, payload["unit"], payload["package"],
            payload["horizon"], int(payload.get("seed", 0)), id_salt,
            payload.get("age_shares"),
        )
    if action == "run_many":
        items = payload["items"]
        return [
            _run_one(
                model, kind, item.get("params") or {}, item["unit"], item["package"],
                item["horizon"], int(item.get("seed", 0)), id_salt,
                item.get("age_shares"),
            )
            for item in items
        ]
    raise WorkerError(f"unknown worker action {action!r}")


class _RequestTimeout(BaseException):
    """Raised inside a persistent worker by SIGALRM when a single request exceeds its wall timeout."""


class _StderrOverflow(BaseException):
    """Raised inside a persistent worker when stderr/stdout log output exceeds the per-request byte cap."""


class _BoundedStderr:
    """Line/byte-counted stderr proxy ensuring Python-level print() overflow in a persistent session
    fails closed deterministically alongside the parent's OS-level stderr pipe cap."""

    def __init__(self, underlying, cap: int):
        self._u = underlying
        self._cap = int(cap)
        self._n = 0
        self.overflowed = False

    def reset(self, cap: int) -> None:
        self._cap = int(cap)
        self._n = 0
        self.overflowed = False

    def write(self, s):
        if isinstance(s, bytes):
            nb = len(s)
        else:
            nb = len(str(s).encode("utf-8", errors="replace"))
        self._n += nb
        if self._n > self._cap:
            self.overflowed = True
            raise _StderrOverflow(f"engine worker exceeded stderr output limit ({self._cap} bytes)")
        return self._u.write(s)

    def flush(self):
        return self._u.flush()

    def isatty(self):
        return False


def _setup_worker_paths() -> None:
    _strip_cwd_from_sys_path()
    here_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(os.path.join(here_dir, "toolkit", "engines", "contract.py")):
        sys.path.insert(0, here_dir)
    else:
        sys.path.insert(0, ROOT)


def _worker_child() -> int:
    """One-shot subprocess entrypoint inside the minimal-mount sandbox (or dev override). Reads one
    JSON request from stdin, executes against verified bytes only, writes JSON response ONLY to the
    saved protocol fd (redirecting Python and C-level stdout to stderr)."""
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    _setup_worker_paths()
    try:
        req = json.loads(sys.stdin.read())
        limits = req.get("limits") or {}
        if req.get("strict_limits"):
            _apply_strict_rlimits(limits)
        files = {str(k): base64.b64decode(v) for k, v in req["files_b64"].items()}
        model = _load_model_from_bytes(files, req.get("config") or {})
        result = _dispatch_action(
            model, str(req["kind"]), str(req["action"]), req.get("payload") or {},
            str(req.get("id_salt") or "s0"),
        )
        _write_fd_all(proto_fd, json.dumps({"ok": True, "result": result}).encode("utf-8"))
        return 0
    except Exception as e:  # noqa: BLE001 -- structured error back to harness
        import traceback
        tb = traceback.format_exc().splitlines()[-6:]
        _write_fd_all(proto_fd, json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb}).encode("utf-8"))
        return 1
    finally:
        try:
            os.close(proto_fd)
        except OSError:
            pass


def _worker_session() -> int:
    """Persistent NDJSON sandboxed worker loop. Compiles verified snapshot bytes ONCE per session
    and serves sequential requests over stdin/proto_fd with per-request SIGALRM timeouts, stderr
    byte caps, and resource usage reporting for proactive parent-side recycling."""
    import resource
    proto_fd = os.dup(1)
    os.dup2(2, 1)
    bounded_err = _BoundedStderr(sys.stderr, MAX_STDERR_BYTES)
    sys.stderr = bounded_err
    sys.stdout = bounded_err
    _setup_worker_paths()

    def _send(obj: dict) -> None:
        _write_fd_all(proto_fd, (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8"))

    def _on_alarm(_sig, _frame):
        raise _RequestTimeout("request timed out inside worker")

    signal.signal(signal.SIGALRM, _on_alarm)
    try:
        init_line = sys.stdin.buffer.readline()
        if not init_line:
            return 0
        init = json.loads(init_line.decode("utf-8"))
        limits = init.get("limits") or {}
        init_err_cap = int(init.get("max_stderr", MAX_STDERR_BYTES))
        bounded_err.reset(init_err_cap)
        if init.get("strict_limits"):
            _apply_strict_rlimits(limits)
        files = {str(k): base64.b64decode(v) for k, v in init["files_b64"].items()}
        kind = str(init["kind"])
        default_salt = str(init.get("id_salt") or "s0")
        model = _load_model_from_bytes(files, init.get("config") or {})
        _send({"ok": True, "ready": True, "pid": os.getpid()})

        n_reqs = 0
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return 0
            req = json.loads(line.decode("utf-8"))
            action = str(req.get("action") or "")
            if action == "shutdown":
                _send({"ok": True, "shutdown": True})
                return 0
            req_err_cap = int(req.get("max_stderr", init_err_cap))
            bounded_err.reset(req_err_cap)
            timeout_s = float(req.get("timeout_s") or 0.0)
            if timeout_s > 0.0:
                signal.setitimer(signal.ITIMER_REAL, timeout_s)
            try:
                result = _dispatch_action(
                    model, kind, action, req.get("payload") or {},
                    str(req.get("id_salt") or default_salt),
                )
                n_reqs += 1
                ru = resource.getrusage(resource.RUSAGE_SELF)
                rss_scale = 1 if sys.platform == "darwin" else 1024
                stats = {
                    "cpu_s": float(ru.ru_utime + ru.ru_stime),
                    "rss_bytes": int(ru.ru_maxrss) * rss_scale,
                    "requests": n_reqs,
                    "pid": os.getpid(),
                }
                _send({"ok": True, "result": result, "stats": stats})
            except _RequestTimeout:
                _send({"ok": False, "error": f"engine worker timed out after {timeout_s}s", "fatal": True})
                return 1
            except _StderrOverflow as e:
                _send({"ok": False, "error": str(e), "fatal": True})
                return 1
            except Exception as e:  # noqa: BLE001 -- structured error back to harness
                import traceback
                tb = traceback.format_exc().splitlines()[-6:]
                _send({"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb, "fatal": False})
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0.0)
                try:
                    bounded_err.flush()
                except OSError:
                    pass
    except _StderrOverflow as e:
        try:
            _send({"ok": False, "error": str(e), "fatal": True})
        except OSError:
            pass
        return 1
    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc().splitlines()[-6:]
        try:
            _send({"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb, "fatal": True})
        except OSError:
            pass
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        try:
            os.close(proto_fd)
        except OSError:
            pass


class _SessionDead(RuntimeError):
    """Internal signal that a pooled worker session exited before handling a new request."""


class _WorkerSession:
    """Persistent sandboxed worker process speaking bounded NDJSON over stdin/stdout."""

    def __init__(
        self,
        cmd: list[str],
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        init_req: dict,
        init_timeout_s: float,
        max_stdout: int,
        max_stderr: int,
        limits: Mapping,
        kind: str,
    ):
        self._lock = threading.Lock()
        self._closed = False
        self._dead = False
        self._limits = dict(limits)
        self._kind = str(kind)
        self._out_buf = bytearray()
        self._err_lock = threading.Lock()
        self._err_req_bytes = 0
        self._err_tail = bytearray()
        self._err_overflow = False
        self._max_stderr = int(max_stderr)
        self.worker_pid: int | None = None
        self.requests_served = 0

        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._err_thread.start()
        try:
            self._write_line(init_req)
            resp = self._read_json_line(timeout_s=init_timeout_s, max_bytes=max_stdout)
            if not resp.get("ok") or not resp.get("ready"):
                err = resp.get("error") or "worker session failed to initialize"
                tb = " / ".join(resp.get("traceback") or [])
                raise WorkerError(f"engine worker error: {err}" + (f" | {tb}" if tb else ""))
            self.worker_pid = resp.get("pid")
        except Exception:
            self.close()
            raise

    def _drain_stderr(self) -> None:
        stream = self.proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                kill_now = False
                with self._err_lock:
                    self._err_req_bytes += len(chunk)
                    self._err_tail.extend(chunk)
                    if len(self._err_tail) > 4096:
                        del self._err_tail[:-4096]
                    if self._err_req_bytes > self._max_stderr:
                        self._err_overflow = True
                        kill_now = True
                if kill_now:
                    self._dead = True
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
                    break
        except OSError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _stderr_tail_text(self) -> str:
        with self._err_lock:
            return bytes(self._err_tail).decode("utf-8", errors="replace")

    def is_alive(self) -> bool:
        return not self._closed and not self._dead and self.proc.poll() is None

    def _write_line(self, obj: Mapping) -> None:
        assert self.proc.stdin is not None
        raw = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            self.proc.stdin.write(raw)
            self.proc.stdin.flush()
        except OSError as e:
            self._dead = True
            raise _SessionDead(f"worker stdin closed: {e}") from None

    def _read_json_line(self, *, timeout_s: float, max_bytes: int) -> dict:
        assert self.proc.stdout is not None
        out_fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + max(timeout_s, 0.1)
        sel = selectors.DefaultSelector()
        try:
            sel.register(out_fd, selectors.EVENT_READ)
            while True:
                with self._err_lock:
                    if self._err_overflow:
                        cap = self._max_stderr
                        self._dead = True
                        raise WorkerError(f"engine worker exceeded stderr output limit ({cap} bytes)")
                nl = self._out_buf.find(b"\n")
                if nl >= 0:
                    line = bytes(self._out_buf[:nl])
                    del self._out_buf[:nl + 1]
                    break
                if len(self._out_buf) > max_bytes:
                    self._dead = True
                    raise WorkerError(f"engine worker exceeded stdout output limit ({max_bytes} bytes)")
                rem = deadline - time.monotonic()
                if rem <= 0.0:
                    self._dead = True
                    raise WorkerError(f"engine worker timed out after {timeout_s}s")
                events = sel.select(timeout=min(0.2, rem))
                if not events:
                    continue
                chunk = os.read(out_fd, 65536)
                if not chunk:
                    self._dead = True
                    with self._err_lock:
                        if self._err_overflow:
                            raise WorkerError(
                                f"engine worker exceeded stderr output limit ({self._max_stderr} bytes)"
                            )
                    rc = self.proc.poll()
                    tail = self._stderr_tail_text()[-400:]
                    raise WorkerError(f"engine worker failed (rc={rc}): {tail}")
                self._out_buf.extend(chunk)
                if len(self._out_buf) > max_bytes and b"\n" not in self._out_buf:
                    self._dead = True
                    raise WorkerError(f"engine worker exceeded stdout output limit ({max_bytes} bytes)")
        finally:
            sel.close()
        with self._err_lock:
            if self._err_overflow:
                self._dead = True
                raise WorkerError(f"engine worker exceeded stderr output limit ({self._max_stderr} bytes)")
        try:
            return json.loads(line.decode("utf-8", errors="replace"))
        except ValueError:
            self._dead = True
            raise WorkerError(f"engine worker returned invalid JSON: {line[-200:]!r}") from None

    def request(
        self,
        action: str,
        payload: Mapping | None,
        *,
        id_salt: str,
        timeout_s: float,
        max_stdout: int,
        max_stderr: int,
    ) -> tuple[object, bool]:
        """Send one request on this session. Returns `(result, should_recycle)`."""
        with self._lock:
            if not self.is_alive():
                raise _SessionDead("worker session is no longer alive")
            with self._err_lock:
                self._err_req_bytes = 0
                self._err_overflow = False
                self._max_stderr = int(max_stderr)
                self._err_tail.clear()
            self._write_line({
                "action": str(action),
                "payload": dict(payload or {}),
                "id_salt": str(id_salt),
                "timeout_s": float(timeout_s),
                "max_stderr": int(max_stderr),
            })
            try:
                resp = self._read_json_line(timeout_s=timeout_s, max_bytes=max_stdout)
            except WorkerError:
                self.close()
                raise
            if not resp.get("ok"):
                err = resp.get("error") or "unknown worker error"
                tb = " / ".join(resp.get("traceback") or [])
                if resp.get("fatal"):
                    self.close()
                    if "exceeded" in str(err) and "output limit" in str(err):
                        raise WorkerError(str(err))
                    if "timed out" in str(err):
                        raise WorkerError(str(err))
                raise WorkerError(f"engine worker error: {err}" + (f" | {tb}" if tb else ""))
            self.requests_served += 1
            stats = resp.get("stats") or {}
            cpu_lim = float(self._limits.get("cpu_s", DEFAULT_LIMITS["cpu_s"]))
            mem_lim = float(self._limits.get("memory_bytes", DEFAULT_LIMITS["memory_bytes"]))
            max_reqs = int(self._limits.get(
                "session_max_requests", _SESSION_MAX_REQUESTS.get(self._kind, 64),
            ))
            should_recycle = (
                float(stats.get("cpu_s", 0.0)) > 0.75 * cpu_lim
                or float(stats.get("rss_bytes", 0.0)) > 0.5 * mem_lim
                or int(stats.get("requests", self.requests_served)) >= max_reqs
            )
            return resp["result"], should_recycle

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._dead = True
        try:
            if self.proc.stdin is not None:
                if self.proc.poll() is None:
                    try:
                        self.proc.stdin.write(b'{"action":"shutdown"}\n')
                        self.proc.stdin.flush()
                    except OSError:
                        pass
                self.proc.stdin.close()
        except OSError:
            pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.terminate()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    try:
                        self.proc.kill()
                    except OSError:
                        pass
                    try:
                        self.proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
        for stream in (self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def close_worker_sessions() -> None:
    """Close and evict all pooled persistent worker sessions for the current process."""
    global _SESSION_POOL_PID
    with _SESSION_POOL_LOCK:
        pid = os.getpid()
        if pid != _SESSION_POOL_PID:
            _SESSION_POOL.clear()
            _SESSION_POOL_PID = pid
            return
        sessions = list(_SESSION_POOL.values())
        _SESSION_POOL.clear()
    for sess in sessions:
        sess.close()


atexit.register(close_worker_sessions)


def _files_digest(verified_files: Mapping[str, bytes]) -> str:
    h = hashlib.sha256()
    for k in sorted(verified_files):
        h.update(str(k).encode("utf-8") + b"\0")
        h.update(bytes(verified_files[k]))
    return h.hexdigest()


def _run_in_session(
    verified_files: Mapping[str, bytes],
    *,
    sb: str | None,
    kind: str,
    action: str,
    config: Mapping | None,
    payload: Mapping | None,
    lim: dict,
    hash_seed: str,
    id_salt: str,
    max_stdout: int,
    max_stderr: int,
) -> dict:
    global _SESSION_POOL_PID
    wall_s = float(lim.get("wall_s", 120))
    cfg = dict(config or {})
    cfg_sha = hashlib.sha256(json.dumps(cfg, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    lim_key = tuple(sorted((str(k), str(v)) for k, v in lim.items()))
    pid = os.getpid()
    pool_key = (pid, _files_digest(verified_files), str(kind), cfg_sha, str(hash_seed), str(id_salt), lim_key, sb or "unsandboxed")

    def _spawn_cmd() -> tuple[list[str], str | None, dict[str, str] | None]:
        if sb is not None:
            prefixes = _ro_mount_prefixes()
            env = _minimal_env(hash_seed=hash_seed, mounted_prefixes=prefixes)
            envargs = [a for k, v in env.items() for a in ("--setenv", k, v)]
            cmd = _bwrap_engine_cmd(
                sb, envargs, [sys.executable, "-s", "-B", "/harness/worker.py", "_worker_session"],
                ro_prefixes=prefixes,
            )
            return cmd, None, None
        env = _minimal_env(hash_seed=hash_seed)
        return [sys.executable, "-s", "-B", os.path.abspath(__file__), "_worker_session"], tempfile.gettempdir(), env

    def _acquire() -> _WorkerSession:
        global _SESSION_POOL_PID
        evicted: list[_WorkerSession] = []
        with _SESSION_POOL_LOCK:
            if pid != _SESSION_POOL_PID:
                _SESSION_POOL.clear()
                _SESSION_POOL_PID = pid
            existing = _SESSION_POOL.get(pool_key)
            if existing is not None:
                if existing.is_alive():
                    return existing
                _SESSION_POOL.pop(pool_key, None)
                evicted.append(existing)
        for old in evicted:
            old.close()
        cmd, run_cwd, run_env = _spawn_cmd()
        init_req = {
            "files_b64": {str(k): base64.b64encode(bytes(v)).decode("ascii") for k, v in verified_files.items()},
            "kind": str(kind),
            "config": cfg,
            "limits": lim,
            "strict_limits": sb is not None,
            "id_salt": str(id_salt),
            "max_stderr": int(max_stderr),
        }
        sess = _WorkerSession(
            cmd, cwd=run_cwd, env=run_env, init_req=init_req,
            init_timeout_s=min(wall_s, 60.0), max_stdout=max_stdout, max_stderr=max_stderr,
            limits=lim, kind=str(kind),
        )
        to_close: list[_WorkerSession] = []
        with _SESSION_POOL_LOCK:
            if pid != _SESSION_POOL_PID:
                _SESSION_POOL.clear()
                _SESSION_POOL_PID = pid
            while len(_SESSION_POOL) >= _SESSION_MAX_PER_PROC:
                old_k = next(iter(_SESSION_POOL))
                to_close.append(_SESSION_POOL.pop(old_k))
            prev = _SESSION_POOL.get(pool_key)
            if prev is not None and prev.is_alive():
                to_close.append(sess)
                sess = prev
            else:
                if prev is not None:
                    to_close.append(prev)
                _SESSION_POOL[pool_key] = sess
        for old in to_close:
            old.close()
        return sess

    def _evict(sess: _WorkerSession) -> None:
        with _SESSION_POOL_LOCK:
            if _SESSION_POOL.get(pool_key) is sess:
                _SESSION_POOL.pop(pool_key, None)
        sess.close()

    for attempt in range(2):
        sess = _acquire()
        try:
            result, should_recycle = sess.request(
                action, payload, id_salt=id_salt, timeout_s=wall_s,
                max_stdout=max_stdout, max_stderr=max_stderr,
            )
            if should_recycle:
                _evict(sess)
            return {"result": result, "sandbox": sb or "unsandboxed", "unsandboxed": sb is None}
        except _SessionDead:
            _evict(sess)
            if attempt == 1:
                raise WorkerError("engine worker session died unexpectedly") from None
        except WorkerError:
            if not sess.is_alive():
                _evict(sess)
            raise
    raise WorkerError("engine worker session failed")


def run_in_worker(
    verified_files: Mapping[str, bytes],
    *,
    kind: str,
    action: str,
    config: Mapping | None = None,
    payload: Mapping | None = None,
    unsandboxed_ok: bool = False,
    limits: Mapping | None = None,
    hash_seed: str = "0",
    id_salt: str = "s0",
    _force_sandbox: str | None = "auto",
    use_session: bool = False,
) -> dict:
    """Execute `action` on verified snapshot bytes inside the minimal-mount bubblewrap sandbox.
    Raises `EngineUnsandboxed` when no sandbox is available unless `unsandboxed_ok=True`.
    When `use_session=True`, reuses a persistent sandboxed NDJSON worker session pooled per process
    (used post-admission by `BuiltEngine` and during A10 calibration; admission checks A1-A9 & A11
    keep `use_session=False` for fresh-process isolation)."""
    sb = engine_sandbox_kind() if _force_sandbox == "auto" else _force_sandbox
    if sb is None and not unsandboxed_ok:
        diag = f" [probe: {'; '.join(_SANDBOX_DIAG)}]" if _force_sandbox == "auto" and _SANDBOX_DIAG else ""
        raise EngineUnsandboxed("engine_unsandboxed: no bubblewrap sandbox available (pass unsandboxed_ok=True "
                                f"for local development only; unsandboxed runs cannot reach ACCEPT){diag}")
    lim = dict(DEFAULT_LIMITS, **(limits or {}))
    file_cap = int(lim.get("file_bytes", DEFAULT_LIMITS["file_bytes"]))
    max_stdout = min(MAX_STDOUT_BYTES, int(lim.get("max_stdout_bytes", file_cap)))
    max_stderr = min(MAX_STDERR_BYTES, int(lim.get("max_stderr_bytes", file_cap)))
    if use_session:
        return _run_in_session(
            verified_files, sb=sb, kind=kind, action=action, config=config, payload=payload,
            lim=lim, hash_seed=hash_seed, id_salt=id_salt, max_stdout=max_stdout, max_stderr=max_stderr,
        )
    req = {
        "files_b64": {str(k): base64.b64encode(bytes(v)).decode("ascii") for k, v in verified_files.items()},
        "kind": str(kind),
        "action": str(action),
        "config": dict(config or {}),
        "payload": dict(payload or {}),
        "limits": lim,
        "strict_limits": sb is not None,
        "id_salt": str(id_salt),
    }
    wall_s = float(lim.get("wall_s", 120))
    if sb is not None:
        prefixes = _ro_mount_prefixes()
        env = _minimal_env(hash_seed=hash_seed, mounted_prefixes=prefixes)
        envargs = [a for k, v in env.items() for a in ("--setenv", k, v)]
        cmd = _bwrap_engine_cmd(sb, envargs, [sys.executable, "-s", "-B", "/harness/worker.py", "_worker_child"],
                                ro_prefixes=prefixes)
        run_cwd = None
        run_env = None
    else:
        env = _minimal_env(hash_seed=hash_seed)
        cmd = [sys.executable, "-s", "-B", os.path.abspath(__file__), "_worker_child"]
        run_cwd = tempfile.gettempdir()
        run_env = env
    rc, out_b, err_b = _run_bounded_subprocess(
        cmd,
        json.dumps(req).encode("utf-8"),
        timeout_s=wall_s,
        cwd=run_cwd,
        env=run_env,
        max_stdout=max_stdout,
        max_stderr=max_stderr,
    )
    out_s = out_b.decode("utf-8", errors="replace")
    err_s = err_b.decode("utf-8", errors="replace")
    try:
        resp = json.loads(out_s)
    except ValueError:
        raise WorkerError(f"engine worker failed (rc={rc}): {(err_s or out_s)[-400:]}") from None
    if not resp.get("ok"):
        raise WorkerError(f"engine worker error: {resp.get('error')} | {' / '.join(resp.get('traceback') or [])}")
    return {"result": resp["result"], "sandbox": sb or "unsandboxed", "unsandboxed": sb is None}


def _self_test() -> int:
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # Toy closed-form engine: mutates age_pop in place, prints to stdout, uses lazy sibling import, tries to lie about age_shares
    age_swap_py = (
        "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
        "class Model:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def describe(self):\n"
        "        return EngineCard(name='built:ag', version='0.1', mechanisms=['test'],\n"
        "                          components={'c': [0, 1]}, unit_attributes={'age_pop': 'vec', 'age_contact': 'vec', 'age_population': 'vec'},\n"
        "                          outputs={'per_capita': ['cases'], 'windows': [], 'cost_drivers': []},\n"
        "                          age_groups=['young', 'old'], valid_ranges={})\n"
        "    def parameters(self):\n"
        "        return [ParamSpec('rate', 0.1, 'fixed', source='assumption')]\n"
        "    def evaluate(self, params, unit, package, horizon):\n"
        "        from .helper import MULT\n"
        "        print('model diagnostic log on stdout')\n"
        "        if 'age_pop' in unit.attributes:\n"
        "            unit.attributes['age_pop'].reverse()\n"
        "        r = params['rate'] * MULT\n"
        "        return Outcomes(per_capita={'cases': {'young': r, 'old': 2 * r}},\n"
        "                        windows={}, cost_drivers={},\n"
        "                        meta={'age_shares': {'young': 0.99, 'old': 0.01}, 'seen_uid': unit.id,\n"
        "                              'hval': hash('probe_key')})\n"
    ).encode("utf-8")
    files = {
        "engine.py": age_swap_py,
        "helper.py": b"MULT = 1.0\n",
        "card.yaml": b"name: built:ag\n",
        "ENGINE_NOTES.md": b"# notes\n",
    }

    # BE35: no sandbox fails closed with EngineUnsandboxed; with unsandboxed_ok=True records unsandboxed + HIGH finding
    try:
        run_in_worker(files, kind="closed_form", action="describe", unsandboxed_ok=False, _force_sandbox=None)
        be35_closed = False
    except EngineUnsandboxed as e:
        be35_closed = "engine_unsandboxed" in str(e)
    base_payload = {
        "params": {},
        "unit": {"id": "real_unit_secret", "attributes": {"age_pop": [200, 800]}},
        "package": {"id": "pkg1", "components": {"c": 1}},
        "horizon": {"start": 0, "stop": 1, "window": [0, 1], "windows": {}},
    }
    res_dev = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None,
                            payload=base_payload, hash_seed="0")
    f_un = unsandboxed_finding()
    ok(be35_closed and res_dev["unsandboxed"] is True and f_un["kind"] == "engine_unsandboxed"
       and f_un["severity"] == "HIGH" and f_un["scope_declarable"] is False,
       "BE35 no sandbox fails closed with engine_unsandboxed; dev flag marks unsandboxed + HIGH non-declarable finding")

    # BE14 + P1-12 + P2-2 + P2-3: pre-run age_shares resist in-place mutation, lazy sibling import & print work
    sh = res_dev["result"]["meta"]["age_shares"]
    seen_uid = res_dev["result"]["meta"].get("seen_uid")
    ok(abs(sh["young"] - 0.2) < 1e-12 and abs(sh["old"] - 0.8) < 1e-12 and seen_uid != "real_unit_secret"
       and res_dev["result"]["meta"]["param_reads"]["build"] == ["rate"],
       f"BE14 pre-run age_shares resist in-place reverse ({sh}), opaque id ({seen_uid}), lazy sibling import & stdout log")

    # P1-13: age_shares independent of dict insertion order and ignores age_contact
    p_a = {**base_payload, "unit": {"id": "u", "attributes": {"age_contact": [1, 1], "age_population": [10, 90]}}}
    p_b = {**base_payload, "unit": {"id": "u", "attributes": {"age_population": [10, 90], "age_contact": [1, 1]}}}
    r_a = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None, payload=p_a)
    r_b = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None, payload=p_b)
    sh_a, sh_b = r_a["result"]["meta"]["age_shares"], r_b["result"]["meta"]["age_shares"]
    ok(abs(sh_a["young"] - 0.1) < 1e-12 and sh_a == sh_b,
       f"BE14b age_shares deterministic across key order and ignores age_contact ({sh_a})")

    # P2-4: PYTHONHASHSEED honored across worker processes
    res_s0 = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None,
                           payload=base_payload, hash_seed="0")
    res_s42 = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None,
                            payload=base_payload, hash_seed="42")
    ok(res_dev["result"]["meta"]["hval"] == res_s0["result"]["meta"]["hval"]
       and res_s0["result"]["meta"]["hval"] != res_s42["result"]["meta"]["hval"],
       "BE14c worker honors PYTHONHASHSEED (same seed -> equal hash, different seed -> distinct hash)")

    # P1-14 + P1-15: phased param_reads captures read_outcomes-only reads; forged meta.balance stripped when no analyzer ran
    ss_mock_py = (
        "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
        "class _Pars:\n"
        "    def __init__(self):\n"
        "        self.rand_seed = 0\n"
        "        self.analyzers = []\n"
        "class _Sim:\n"
        "    def __init__(self):\n"
        "        self.initialized = False\n"
        "        self.pars = _Pars()\n"
        "    def run(self):\n"
        "        self.initialized = True\n"
        "class Model:\n"
        "    def describe(self):\n"
        "        return EngineCard(name='built:ss', version='0.1', mechanisms=['t'],\n"
        "                          components={'c': [0, 1]}, unit_attributes={'x': 'x'},\n"
        "                          outputs={'per_capita': ['cases'], 'windows': [], 'cost_drivers': []},\n"
        "                          age_groups=['all'], valid_ranges={})\n"
        "    def parameters(self):\n"
        "        return [ParamSpec('rate', 0.2, 'fixed', source='assumption')]\n"
        "    def state_vars(self):\n"
        "        return {}\n"
        "    def build_sim(self, params, unit, package, horizon):\n"
        "        self._p = params\n"
        "        return _Sim()\n"
        "    def read_outcomes(self, sim, unit, package, horizon):\n"
        "        r = self._p['rate']\n"
        "        return Outcomes(per_capita={'cases': {'all': r}}, windows={}, cost_drivers={},\n"
        "                        meta={'balance': {'forged': True}})\n"
    ).encode("utf-8")
    res_ss = run_in_worker(
        {"engine.py": ss_mock_py, "card.yaml": b"name: built:ss\n", "ENGINE_NOTES.md": b"# n\n"},
        kind="starsim_module", action="run", unsandboxed_ok=True, _force_sandbox=None,
        payload={"params": {}, "unit": {"id": "u", "attributes": {"x": 1}},
                 "package": {"id": "p", "components": {"c": 1}},
                 "horizon": {"start": 0, "stop": 1, "window": [0, 1], "windows": {}}},
    )
    ss_meta = res_ss["result"]["meta"]
    ok(ss_meta["param_reads"] == {"build": [], "read_outcomes": ["rate"]} and "balance" not in ss_meta,
       f"BE14d read_outcomes-only param reads attributed to read_outcomes and forged meta.balance stripped ({ss_meta})")

    # P1-16: decoded UTF-8 compiled (never reinterpreted via latin-1 coding comment)
    coding_py = (
        "# coding: latin-1\n"
        "from toolkit.engines.contract import EngineCard, Outcomes\n"
        "class Model:\n"
        "    def describe(self):\n"
        "        return EngineCard(name='built:c', version='0.1', mechanisms=['café'],\n"
        "                          components={'c': [0]}, unit_attributes={'x': 'x'},\n"
        "                          outputs={'per_capita': ['y'], 'windows': [], 'cost_drivers': []},\n"
        "                          age_groups=['all'], valid_ranges={})\n"
        "    def parameters(self):\n"
        "        return []\n"
    ).encode("utf-8")
    res_cod = run_in_worker(
        {"engine.py": coding_py, "card.yaml": b"name: built:c\n", "ENGINE_NOTES.md": b"# n\n"},
        kind="closed_form", action="describe", unsandboxed_ok=True, _force_sandbox=None,
    )
    ok(res_cod["result"]["mechanisms"] == ["café"],
       f"BE14e worker compiles decoded UTF-8 source without latin-1 reinterpretation ({res_cod['result']['mechanisms']})")

    # P1-17: bounded stderr/stdout kills runaway output worker under file_bytes cap
    noisy_py = (
        "from toolkit.engines.contract import EngineCard\n"
        "class Model:\n"
        "    def describe(self):\n"
        "        print('X' * 8192)\n"
        "        return EngineCard(name='built:n', version='0.1', mechanisms=['t'],\n"
        "                          components={'c': [0]}, unit_attributes={'x': 'x'},\n"
        "                          outputs={'per_capita': ['y'], 'windows': [], 'cost_drivers': []},\n"
        "                          age_groups=['all'], valid_ranges={})\n"
    ).encode("utf-8")
    try:
        run_in_worker(
            {"engine.py": noisy_py, "card.yaml": b"name: built:n\n", "ENGINE_NOTES.md": b"# n\n"},
            kind="closed_form", action="describe", unsandboxed_ok=True, _force_sandbox=None,
            limits={"file_bytes": 1024},
        )
        capped = False
    except WorkerError as e:
        capped = "exceeded" in str(e) and "output limit" in str(e)
    ok(capped, "BE18b parent bounds worker stderr/stdout pipe memory and kills child on overflow")

    # P1-9: _bwrap_engine_cmd never mounts arbitrary directories appended to sys.path
    with tempfile.TemporaryDirectory() as ext_dir:
        sys.path.append(ext_dir)
        try:
            cmd_ext = _bwrap_engine_cmd("bwrap", [], [sys.executable, "-s", "-B", "/harness/worker.py", "_probe"])
        finally:
            sys.path.remove(ext_dir)
        ext_real = os.path.realpath(ext_dir)
        ok(ext_dir not in cmd_ext and ext_real not in cmd_ext,
           "BE18c _bwrap_engine_cmd ignores arbitrary sys.path directories")

    # BE18d: stdlib (encodings, os, sysconfig stdlib) covered by _ro_mount_prefixes() and --tmpfs /tmp precedes binds
    ro_pref = _ro_mount_prefixes()
    def _covered(p: str) -> bool:
        if not p:
            return True
        pn = os.path.normpath(os.path.abspath(p))
        return any(pn == m or pn.startswith(m.rstrip("/") + "/") for m in ro_pref)
    enc_f = getattr(encodings, "__file__", "") or ""
    os_f = getattr(os, "__file__", "") or ""
    stdlib_d = sysconfig.get_path("stdlib") or ""
    cmd_ord = _bwrap_engine_cmd("bwrap", [], [sys.executable, "-s", "-B", "/harness/worker.py", "_probe"],
                                ro_prefixes=ro_pref)
    tmp_idx = cmd_ord.index("--tmpfs")
    bind_idx = cmd_ord.index("--ro-bind")
    ok(all(_covered(p) for p in (enc_f, os.path.realpath(enc_f), os_f, os.path.realpath(os_f),
                                 stdlib_d, os.path.realpath(stdlib_d)))
       and tmp_idx < bind_idx,
       f"BE18d _ro_mount_prefixes covers encodings/os/stdlib (logical+realpath) and --tmpfs /tmp precedes --ro-bind")

    # BE18e: bwrap-sudo pre-creates ancestor dirs (e.g. /opt, /etc, /etc/ssl) with 0755 and /tmp with 1777
    cmd_perm = _bwrap_engine_cmd("bwrap-sudo", [], [sys.executable, "-s", "-B", "/harness/worker.py", "_probe"],
                                 ro_prefixes=["/usr", "/opt/runtime", "/etc/ld.so.cache", "/etc/ssl/certs"])
    def _seq(lst: list[str], sub: list[str]) -> int:
        for i in range(len(lst) - len(sub) + 1):
            if lst[i:i + len(sub)] == sub:
                return i
        return -1
    i_tmp = _seq(cmd_perm, ["--perms", "1777", "--tmpfs", "/tmp"])
    i_opt_d = _seq(cmd_perm, ["--perms", "0755", "--dir", "/opt"])
    i_opt_b = _seq(cmd_perm, ["--ro-bind", "/opt/runtime", "/opt/runtime"])
    i_etc_d = _seq(cmd_perm, ["--perms", "0755", "--dir", "/etc"])
    i_ssl_d = _seq(cmd_perm, ["--perms", "0755", "--dir", "/etc/ssl"])
    i_ssl_b = _seq(cmd_perm, ["--ro-bind", "/etc/ssl/certs", "/etc/ssl/certs"])
    i_har = _seq(cmd_perm, ["--perms", "0755", "--tmpfs", "/harness"])
    ok(i_tmp >= 0 and 0 <= i_opt_d < i_opt_b and 0 <= i_etc_d < i_ssl_d < i_ssl_b and i_har >= 0,
       f"BE18e bwrap-sudo pre-creates ancestor dirs 0755 before --ro-bind and /tmp 1777 ({i_tmp}, {i_opt_d}<{i_opt_b}, {i_etc_d}<{i_ssl_d}<{i_ssl_b})")

    # BE18: in bubblewrap sandbox (Linux CI / Coiled VMs), sentinel file outside mount set cannot be read
    sb = engine_sandbox_kind()
    if sb is not None:
        with tempfile.TemporaryDirectory() as td:
            sentinel = os.path.join(td, "secret_sentinel.txt")
            open(sentinel, "w").write("TOP_SECRET_SENTINEL")
            probe_py = (
                "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
                "class Model:\n"
                "    def describe(self):\n"
                "        return EngineCard(name='built:sb', version='0.1', mechanisms=['t'],\n"
                "                          components={'c': [0]}, unit_attributes={'x': 'x'},\n"
                "                          outputs={'per_capita': ['y'], 'windows': [], 'cost_drivers': []},\n"
                "                          age_groups=['all'], valid_ranges={})\n"
                "    def parameters(self):\n"
                "        return []\n"
                "    def evaluate(self, params, unit, package, horizon):\n"
                f"        p = {sentinel!r}\n"
                "        try:\n"
                "            data = open(p).read()\n"
                "        except OSError:\n"
                "            data = 'BLOCKED'\n"
                "        return Outcomes(per_capita={'y': {'all': 1.0 if data == 'BLOCKED' else 0.0}},\n"
                "                        windows={}, cost_drivers={}, meta={'probe': data})\n"
            ).encode("utf-8")
            sb_files = {"engine.py": probe_py, "card.yaml": b"name: built:sb\n", "ENGINE_NOTES.md": b"# n\n"}
            out_sb = run_in_worker(sb_files, kind="closed_form", action="run", unsandboxed_ok=False,
                                   payload={"params": {}, "unit": {"id": "u", "attributes": {"x": 1}},
                                            "package": {"id": "p", "components": {"c": 0}},
                                            "horizon": {"start": 0, "stop": 1, "window": [0, 1], "windows": {}}})
            ok(out_sb["unsandboxed"] is False and out_sb["result"]["meta"]["probe"] == "BLOCKED",
               f"BE18 minimal-mount {sb} sandbox blocks reading sentinel file outside mount set ({out_sb['result']['meta']})")
    else:
        if os.environ.get("MA_REQUIRE_SANDBOX") == "1":
            ok(False, f"BE18 MA_REQUIRE_SANDBOX=1 but engine_sandbox_kind() is None (diag={_SANDBOX_DIAG})")
        else:
            cmd = _bwrap_engine_cmd("bwrap", [], [sys.executable, "-s", "-B", "/harness/worker.py", "_probe"])
            no_root_bind = not any(cmd[i:i + 3] == ["--ro-bind", "/", "/"] for i in range(len(cmd) - 2))
            no_devnull = "/dev/null" not in cmd
            ok(no_root_bind and no_devnull and "--tmpfs" in cmd and "/harness/toolkit/engines/contract.py" in cmd,
               "BE18 minimal-mount _bwrap_engine_cmd omits `--ro-bind / /` and `/dev/null` bind and mounts only runtime + contract/hooks/worker")

    # BE_TP1: TrackedParams tracks dict(params)/{**params} and lazy shared-phase .copy() (no BE5 bypass)
    tp1 = TrackedParams({"a": 1.0, "b": 2.0})
    d_copy = dict(tp1)
    tp2 = TrackedParams({"a": 1.0, "b": 2.0})
    u_copy = {**tp2}
    tp3 = TrackedParams({"a": 1.0, "b": 2.0})
    lazy_view = tp3.copy()
    _ = lazy_view["a"]
    tp3.set_phase("read_outcomes")
    _ = lazy_view["b"]
    ok(d_copy == {"a": 1.0, "b": 2.0} and tp1.reads_by_phase["build"] == {"a", "b"}
       and u_copy == {"a": 1.0, "b": 2.0} and tp2.reads_by_phase["build"] == {"a", "b"}
       and tp3.reads_by_phase["build"] == {"a"} and tp3.reads_by_phase["read_outcomes"] == {"b"},
       f"BE_TP1 TrackedParams records dict(p)/{{**p}} and preserves phase attribution across .copy() ({tp3.reads_by_phase})")

    # BE_SESS1: persistent worker session reuses worker, recycles on max_requests, enforces stderr cap, and matches one-shot
    close_worker_sessions()
    s_r1 = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None,
                         payload=base_payload, hash_seed="0", use_session=True)
    pool_after_1 = list(_SESSION_POOL.values())
    pid1 = pool_after_1[0].worker_pid if pool_after_1 else None
    s_r2 = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None,
                         payload=base_payload, hash_seed="0", use_session=True)
    pool_after_2 = list(_SESSION_POOL.values())
    pid2 = pool_after_2[0].worker_pid if pool_after_2 else None
    # Recycle after session_max_requests=2
    _ = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None,
                      payload=base_payload, hash_seed="0", limits={"session_max_requests": 2}, use_session=True)
    _ = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None,
                      payload=base_payload, hash_seed="0", limits={"session_max_requests": 2}, use_session=True)
    recycled_empty = not any(
        s._limits.get("session_max_requests") == 2 for s in _SESSION_POOL.values()
    )
    # Stderr cap enforced in persistent session too
    try:
        run_in_worker(
            {"engine.py": noisy_py, "card.yaml": b"name: built:n\n", "ENGINE_NOTES.md": b"# n\n"},
            kind="closed_form", action="describe", unsandboxed_ok=True, _force_sandbox=None,
            limits={"file_bytes": 1024}, use_session=True,
        )
        sess_capped = False
    except WorkerError as e:
        sess_capped = "exceeded" in str(e) and "output limit" in str(e)
    close_worker_sessions()
    ok(pid1 is not None and pid1 == pid2 and s_r1["result"] == res_dev["result"] and s_r2["result"] == res_dev["result"]
       and recycled_empty and sess_capped and not _SESSION_POOL,
       f"BE_SESS1 persistent worker session reuses pid ({pid1}=={pid2}), matches one-shot, recycles on cap, and enforces stderr cap")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["_probe"]:
        _strip_cwd_from_sys_path()
        here_dir = os.path.dirname(os.path.abspath(__file__))
        if os.path.isfile(os.path.join(here_dir, "toolkit", "engines", "contract.py")):
            sys.path.insert(0, here_dir)
        from toolkit.engines.contract import EngineCard  # noqa: F401
        sys.stdout.write("OK\n")
        sys.exit(0)
    if sys.argv[1:2] == ["_worker_child"]:
        sys.exit(_worker_child())
    if sys.argv[1:2] == ["_worker_session"]:
        sys.exit(_worker_session())
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
