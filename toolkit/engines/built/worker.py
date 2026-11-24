#!/usr/bin/env python3
"""Minimal-mount bubblewrap sandbox and verified-bytes worker for agent-built engines
(docs/design/agent-built-engines.md §3.2 & §5.1).

    python3 toolkit/engines/built/worker.py --self-test

The harness reads verified snapshot bytes ONCE, sends them to this worker inside an explicit
minimal-mount bubblewrap sandbox (`_bwrap_engine_cmd`: interpreter prefix, allowed site-packages,
`contract.py`, `hooks.py`, `/proc`, `/dev`, private tmpfs — no repo, no home, no run directory, no
network), compiles each module from those verified bytes, enforces opaque unit/package IDs,
`TrackedParams`, `BalanceAnalyzer`, and harness-computed `meta.age_shares`. Nothing here names a
disease.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.abc
import importlib.machinery
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
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
_SANDBOX_CACHE: list[str | None] = []


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
        "claim": f"agent-built engine ran without the bubblewrap sandbox ({reason}); a run using the "
                 "unsandboxed development override cannot reach ACCEPT",
        "scope_declarable": False,
    }


def _ro_mount_prefixes() -> list[str]:
    """Minimal read-only runtime paths needed for `python -I` + numpy/scipy/starsim without mounting `/`
    or the repository or the run directory or any home/temp directory."""
    mounts: list[str] = []
    seen: set[str] = set()
    repo_real = os.path.realpath(ROOT)

    def add_ro(path: str) -> None:
        if not path or not os.path.exists(path):
            return
        real = os.path.realpath(path)
        for target in dict.fromkeys((path, real)):
            if target in seen or target == "/":
                continue
            # Never mount the whole repo root or /tmp or /home directly
            if target in ("/tmp", "/var", "/home", "/root", repo_real):
                continue
            if any(target.startswith(m.rstrip("/") + "/") for m in seen if os.path.isdir(m)):
                continue
            seen.add(target)
            mounts.append(target)

    # Standard OS runtime libs
    for base in ("/usr", "/etc/ld.so.cache", "/etc/alternatives"):
        if os.path.exists(base):
            add_ro(base)
    # Python prefix & executable location (e.g. /opt/hostedtoolcache or venv, NOT its parent dirs)
    for cand in (sys.prefix, sys.base_prefix, os.path.dirname(os.path.abspath(sys.executable)),
                 os.path.dirname(os.path.realpath(sys.executable))):
        if cand:
            add_ro(cand)
    # Also ensure sys.path library directories outside the repo are mounted
    for sp in sys.path:
        if not sp or not os.path.isdir(sp):
            continue
        r = os.path.realpath(sp)
        if r == repo_real or r.startswith(repo_real + os.sep):
            # Allow a repo-local venv (e.g. .venv/lib/...) without mounting the repo itself
            rel = os.path.relpath(r, repo_real).replace(os.sep, "/")
            if not rel.startswith((".venv/", "venv/")):
                continue
        add_ro(r)
    return mounts


def _bwrap_engine_cmd(kind: str, envargs: list[str], child: list[str]) -> list[str]:
    """Build the minimal-mount bubblewrap command (§5.1): NO `--ro-bind / /`.
    Mounts ONLY the interpreter/library prefixes, `contract.py`, `hooks.py`, `worker.py`,
    `/proc`, `/dev`, and a private `--tmpfs /tmp`."""
    uid, gid = str(os.getuid()), str(os.getgid())
    if kind == "bwrap":
        ns = ["--unshare-all", "--uid", uid, "--gid", gid]
        drop: list[str] = []
    else:
        ns = ["--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
        drop = ["setpriv", "--reuid", uid, "--regid", gid, "--clear-groups", "--no-new-privs",
                "--inh-caps=-all", "--bounding-set=-all", "--"]

    binds: list[str] = []
    # Top-level OS links or directories (/bin, /lib, /lib64)
    for top in ("/bin", "/lib", "/lib64"):
        if os.path.islink(top):
            binds += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            binds += ["--ro-bind", top, top]

    for p in _ro_mount_prefixes():
        binds += ["--ro-bind", p, p]

    # Harness files ONLY, mounted under /harness (never mounting the repo directory)
    contract_py = os.path.join(ROOT, "toolkit", "engines", "contract.py")
    hooks_py = os.path.join(ROOT, "toolkit", "engines", "hooks.py")
    worker_py = os.path.abspath(__file__)
    binds += [
        "--tmpfs", "/harness",
        "--dir", "/harness/toolkit",
        "--dir", "/harness/toolkit/engines",
        "--ro-bind", "/dev/null", "/harness/toolkit/__init__.py",
        "--ro-bind", "/dev/null", "/harness/toolkit/engines/__init__.py",
        "--ro-bind", contract_py, "/harness/toolkit/engines/contract.py",
        "--ro-bind", hooks_py, "/harness/toolkit/engines/hooks.py",
        "--ro-bind", worker_py, "/harness/worker.py",
    ]
    return (["sudo", "-n"] if kind == "bwrap-sudo" else []) + [
        "bwrap",
        *binds,
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        *ns,
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        *envargs,
        "--chdir", "/tmp",
        *drop,
        *child,
    ]


def _minimal_env(hash_seed: str = "0") -> dict[str, str]:
    keep = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
    return dict(
        keep,
        HOME="/tmp",
        TMPDIR="/tmp",
        PYTHONHASHSEED=str(hash_seed),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONNOUSERSITE="1",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
    )


def _probe_engine_bwrap(kind: str) -> bool:
    env = _minimal_env("0")
    envargs = [a for k, v in env.items() for a in ("--setenv", k, v)]
    cmd = _bwrap_engine_cmd(kind, envargs, [sys.executable, "-I", "/harness/worker.py", "_probe"])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return r.returncode == 0 and r.stdout.strip() == "OK"
    except (OSError, subprocess.SubprocessError):
        return False


def engine_sandbox_kind() -> str | None:
    """Return 'bwrap', 'bwrap-sudo', or None (no minimal-mount bubblewrap sandbox available)."""
    if _SANDBOX_CACHE:
        return _SANDBOX_CACHE[0]
    kind: str | None = None
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        if _probe_engine_bwrap("bwrap"):
            kind = "bwrap"
        elif shutil.which("sudo") and shutil.which("setpriv") and _probe_engine_bwrap("bwrap-sudo"):
            kind = "bwrap-sudo"
    _SANDBOX_CACHE.append(kind)
    return kind


# --------------------------------------------------------------------------- in-worker runtime
class TrackedParams(dict):
    """Parameter dict recording every parameter key accessed by model code."""

    def __init__(self, initial: Mapping[str, float]):
        super().__init__({str(k): float(v) for k, v in initial.items()})
        self.reads: set[str] = set()

    def __getitem__(self, key):
        self.reads.add(str(key))
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in self:
            self.reads.add(str(key))
            return super().__getitem__(key)
        return default

    def __contains__(self, key):
        if super().__contains__(key):
            self.reads.add(str(key))
            return True
        return False

    def values(self):
        self.reads.update(self.keys())
        return super().values()

    def items(self):
        self.reads.update(self.keys())
        return super().items()

    def copy(self):
        self.reads.update(self.keys())
        return dict(self)


def _opaque_id(salt: str, raw_id: str) -> str:
    return hashlib.sha256(f"{salt}:{raw_id}".encode("utf-8")).hexdigest()[:16]


def _compute_age_shares(age_groups: list[str], unit_attrs: Mapping, override_shares: Mapping | None) -> dict[str, float]:
    """Harness-computed `meta.age_shares` — always overwrites whatever the model returned (BE14)."""
    if isinstance(override_shares, Mapping) and override_shares:
        vals = {str(g): float(override_shares.get(g, 0.0)) for g in age_groups}
        tot = sum(vals.values())
        if tot > 0 and all(math.isfinite(v) and v >= 0 for v in vals.values()):
            return {g: v / tot for g, v in vals.items()}
    # Check for an age population vector attribute whose length matches age_groups
    for k, v in (unit_attrs or {}).items():
        if isinstance(v, (list, tuple)) and len(v) == len(age_groups) and "age" in str(k).lower():
            try:
                fv = [float(x) for x in v]
            except (TypeError, ValueError):
                continue
            tot = sum(fv)
            if tot > 0 and all(math.isfinite(x) and x >= 0 for x in fv):
                return {str(g): x / tot for g, x in zip(age_groups, fv)}
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
    """The ONE audited verified-bytes compile/exec site (scripts/import_audit.py, BE36)."""
    code = compile(src_bytes, filename, "exec")
    exec(code, namespace)  # noqa: S102 -- verified snapshot bytes inside minimal-mount sandbox


def _load_model_from_bytes(files: dict[str, bytes], config: dict):
    """Compile verified snapshot bytes into isolated in-memory module namespace and construct Model."""
    if "engine.py" not in files:
        raise WorkerError("snapshot has no engine.py")
    pkg = types.ModuleType("_built_snapshot")
    pkg.__path__ = []
    sys.modules["_built_snapshot"] = pkg
    finder = _SnapshotFinder(files)
    sys.meta_path.insert(0, finder)
    try:
        mod = types.ModuleType("_built_snapshot.engine")
        mod.__package__ = "_built_snapshot"
        mod.__file__ = "<snapshot>/engine.py"
        sys.modules["_built_snapshot.engine"] = mod
        _exec_verified_bytes(files["engine.py"], mod.__file__, mod.__dict__)
    finally:
        if finder in sys.meta_path:
            sys.meta_path.remove(finder)
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
            super().__init__()
            self._sv = dict(sv)
            self.steps: list[dict[str, float]] = []

        def _snap(self) -> dict[str, float]:
            row: dict[str, float] = {}
            for sname, loc in self._sv.items():
                mod_name, attr_name = loc
                target = self.sim.people if mod_name == "people" else (
                    self.sim.diseases.get(mod_name) if hasattr(self.sim, "diseases") and mod_name in self.sim.diseases
                    else getattr(self.sim, mod_name, None)
                )
                val = getattr(target, attr_name, None) if target is not None else None
                if val is None:
                    row[str(sname)] = float("nan")
                else:
                    arr = np.asarray(val, dtype=float)
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
    opaque_u = UnitSpec(_opaque_id(id_salt, str(unit_d["id"])), dict(unit_d.get("attributes") or {}))
    opaque_p = Package(_opaque_id(id_salt, str(pkg_d["id"])), dict(pkg_d.get("components") or {}))
    hor = Horizon(float(hor_d["start"]), float(hor_d["stop"]), tuple(hor_d["window"]),
                  {str(k): list(v) for k, v in (hor_d.get("windows") or {}).items()})

    t_build = TrackedParams(resolved)
    balance_info: dict = {}
    outcome_reads: list[str] = []

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
                sim.pars.analyzers += [an]
        sim.run()
        out = model.read_outcomes(sim, opaque_u, opaque_p, hor)
        if an is not None:
            totals = [sum(step.values()) for step in an.steps] if an.steps else []
            mins = [min(step.values()) for step in an.steps if step]
            balance_info = {
                "n_steps": len(an.steps),
                "initial_total": totals[0] if totals else None,
                "final_total": totals[-1] if totals else None,
                "min_state": min(mins) if mins else None,
                "steps": an.steps[:200],
            }
    elif kind == "closed_form":
        out = model.evaluate(t_build, opaque_u, opaque_p, hor)
    else:
        raise WorkerError(f"unknown engine kind {kind!r}")

    if not isinstance(out, Outcomes):
        raise WorkerError(f"model did not return Outcomes (got {type(out).__name__})")
    # Always overwrite meta.age_shares from harness computation (BE14)
    meta = dict(out.meta or {})
    meta["age_shares"] = _compute_age_shares(list(card.age_groups), opaque_u.attributes, age_shares_override)
    meta["param_reads"] = {"build": sorted(t_build.reads), "read_outcomes": sorted(outcome_reads)}
    if balance_info:
        meta["balance"] = balance_info
    out = Outcomes(per_capita=out.per_capita, windows=out.windows, cost_drivers=out.cost_drivers, meta=meta)
    return _serialize_outcomes(out)


def _worker_child() -> int:
    """Subprocess entrypoint inside the minimal-mount sandbox (or dev override). Reads one JSON
    request from stdin, executes against verified bytes only, writes JSON response to stdout."""
    # Under `python -I`, PYTHONPATH is ignored; add this script's own directory (/harness or repo root)
    here_dir = os.path.dirname(os.path.abspath(__file__))
    if os.path.isfile(os.path.join(here_dir, "toolkit", "engines", "contract.py")):
        sys.path.insert(0, here_dir)
    else:
        sys.path.insert(0, ROOT)
    try:
        req = json.loads(sys.stdin.read())
        limits = req.get("limits") or {}
        if req.get("strict_limits"):
            import resource
            for rname, key, extra in (("RLIMIT_AS", "memory_bytes", 0), ("RLIMIT_CPU", "cpu_s", 2),
                                      ("RLIMIT_FSIZE", "file_bytes", 0)):
                r = getattr(resource, rname, None)
                if r is not None and key in limits:
                    try:
                        resource.setrlimit(r, (int(limits[key]), int(limits[key]) + extra))
                    except (ValueError, OSError):
                        pass
        files = {str(k): base64.b64decode(v) for k, v in req["files_b64"].items()}
        model = _load_model_from_bytes(files, req.get("config") or {})
        action = req["action"]
        if action == "describe":
            result = _serialize_card(model.describe())
        elif action == "parameters":
            result = [_serialize_param(p) for p in model.parameters()]
        elif action == "run":
            p = req["payload"]
            result = _run_one(
                model, str(req["kind"]), p.get("params") or {}, p["unit"], p["package"],
                p["horizon"], int(p.get("seed", 0)), str(req.get("id_salt") or "s0"),
                p.get("age_shares"),
            )
        elif action == "run_batch":
            p = req["payload"]
            results = []
            for item in p["items"]:
                results.append(_run_one(
                    model, str(req["kind"]), item.get("params") or {}, item["unit"], item["package"],
                    item["horizon"], int(item.get("seed", 0)), str(req.get("id_salt") or "s0"),
                    item.get("age_shares"),
                ))
            result = results
        else:
            raise WorkerError(f"unknown worker action {action!r}")
        sys.stdout.write(json.dumps({"ok": True, "result": result}))
        return 0
    except Exception as e:  # noqa: BLE001 -- structured error back to harness
        import traceback
        tb = traceback.format_exc().splitlines()[-6:]
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": tb}))
        return 1


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
) -> dict:
    """Execute `action` on verified snapshot bytes inside the minimal-mount bubblewrap sandbox.
    Raises `EngineUnsandboxed` when no sandbox is available unless `unsandboxed_ok=True`."""
    sb = engine_sandbox_kind() if _force_sandbox == "auto" else _force_sandbox
    if sb is None and not unsandboxed_ok:
        raise EngineUnsandboxed("engine_unsandboxed: no bubblewrap sandbox available (pass unsandboxed_ok=True "
                                "for local development only; unsandboxed runs cannot reach ACCEPT)")
    lim = dict(DEFAULT_LIMITS, **(limits or {}))
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
    env = _minimal_env(hash_seed=hash_seed)
    wall_s = float(lim.get("wall_s", 120))
    if sb is not None:
        envargs = [a for k, v in env.items() for a in ("--setenv", k, v)]
        cmd = _bwrap_engine_cmd(sb, envargs, [sys.executable, "-I", "/harness/worker.py", "_worker_child"])
        run_cwd = None
        run_env = None
    else:
        cmd = [sys.executable, "-I", os.path.abspath(__file__), "_worker_child"]
        run_cwd = tempfile.gettempdir()
        run_env = env
    try:
        proc = subprocess.run(cmd, input=json.dumps(req), capture_output=True, text=True,
                              timeout=wall_s, cwd=run_cwd, env=run_env)
    except subprocess.TimeoutExpired:
        raise WorkerError(f"engine worker timed out after {wall_s}s") from None
    try:
        resp = json.loads(proc.stdout)
    except ValueError:
        raise WorkerError(f"engine worker failed (rc={proc.returncode}): {(proc.stderr or proc.stdout)[-400:]}") from None
    if not resp.get("ok"):
        raise WorkerError(f"engine worker error: {resp.get('error')} | {' / '.join(resp.get('traceback') or [])}")
    return {"result": resp["result"], "sandbox": sb or "unsandboxed", "unsandboxed": sb is None}


def _self_test() -> int:
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # Toy closed-form engine that tries to lie about meta.age_shares (BE14)
    age_swap_py = (
        "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
        "class Model:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def describe(self):\n"
        "        return EngineCard(name='built:ag', version='0.1', mechanisms=['test'],\n"
        "                          components={'c': [0, 1]}, unit_attributes={'age_pop': 'vec'},\n"
        "                          outputs={'per_capita': ['cases'], 'windows': [], 'cost_drivers': []},\n"
        "                          age_groups=['young', 'old'], valid_ranges={})\n"
        "    def parameters(self):\n"
        "        return [ParamSpec('rate', 0.1, 'fixed', source='assumption')]\n"
        "    def evaluate(self, params, unit, package, horizon):\n"
        "        r = params['rate']\n"
        "        return Outcomes(per_capita={'cases': {'young': r, 'old': 2 * r}},\n"
        "                        windows={}, cost_drivers={},\n"
        "                        meta={'age_shares': {'young': 0.99, 'old': 0.01}, 'seen_uid': unit.id})\n"
    ).encode("utf-8")
    files = {"engine.py": age_swap_py, "card.yaml": b"name: built:ag\n", "ENGINE_NOTES.md": b"# notes\n"}

    # BE35: no sandbox fails closed with EngineUnsandboxed; with unsandboxed_ok=True records unsandboxed + HIGH finding
    try:
        run_in_worker(files, kind="closed_form", action="describe", unsandboxed_ok=False, _force_sandbox=None)
        be35_closed = False
    except EngineUnsandboxed as e:
        be35_closed = "engine_unsandboxed" in str(e)
    res_dev = run_in_worker(files, kind="closed_form", action="run", unsandboxed_ok=True, _force_sandbox=None,
                            payload={"params": {}, "unit": {"id": "real_unit_secret", "attributes": {"age_pop": [200, 800]}},
                                     "package": {"id": "pkg1", "components": {"c": 1}},
                                     "horizon": {"start": 0, "stop": 1, "window": [0, 1], "windows": {}}})
    f_un = unsandboxed_finding()
    ok(be35_closed and res_dev["unsandboxed"] is True and f_un["kind"] == "engine_unsandboxed"
       and f_un["severity"] == "HIGH" and f_un["scope_declarable"] is False,
       f"BE35 no sandbox fails closed with engine_unsandboxed; dev flag marks unsandboxed + HIGH non-declarable finding")

    # BE14: swapped meta.age_shares overwritten by harness from unit age_pop [200, 800] -> {young: 0.2, old: 0.8},
    # and unit.id seen by model is opaque (never 'real_unit_secret')
    sh = res_dev["result"]["meta"]["age_shares"]
    seen_uid = res_dev["result"]["meta"].get("seen_uid")
    ok(abs(sh["young"] - 0.2) < 1e-12 and abs(sh["old"] - 0.8) < 1e-12 and seen_uid != "real_unit_secret"
       and res_dev["result"]["meta"]["param_reads"]["build"] == ["rate"],
       f"BE14 swapped meta.age_shares overwritten by harness ({sh}), unit.id opaque ({seen_uid}), param_reads tracked")

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
        # On hosts without bwrap (macOS), verify _bwrap_engine_cmd mounts minimal set (no `--ro-bind / /`)
        cmd = _bwrap_engine_cmd("bwrap", [], [sys.executable, "-I", "/harness/worker.py", "_probe"])
        no_root_bind = not any(cmd[i:i + 3] == ["--ro-bind", "/", "/"] for i in range(len(cmd) - 2))
        ok(no_root_bind and "--tmpfs" in cmd and "/harness/toolkit/engines/contract.py" in cmd,
           "BE18 minimal-mount _bwrap_engine_cmd omits `--ro-bind / /` and mounts only runtime + contract/hooks/worker")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["_probe"]:
        sys.stdout.write("OK\n")
        sys.exit(0)
    if sys.argv[1:2] == ["_worker_child"]:
        sys.exit(_worker_child())
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
