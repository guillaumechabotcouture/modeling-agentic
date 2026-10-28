#!/usr/bin/env python3
"""S1: the run's declared build pipeline (`models/pipeline.yaml`), run by the harness.

    python3 scripts/pipeline.py validate <run_dir>
    python3 scripts/pipeline.py run <run_dir> --dry-run          # print the plan
    python3 scripts/pipeline.py run <run_dir> [--budget-s N]     # the harness run
    python3 scripts/pipeline.py run <run_dir> <step> --scratch   # try one step in a copy
    python3 scripts/pipeline.py --self-test

E7 attempt 7: the modeler wrote new driver scripts every round
(`run_all_r2.sh`, `run_r4_chain.sh`), and figures and numbers drifted from
the code that should have produced them. Here the modeler declares each step
once and the harness runs them, like `make` (docs/design/stage-machine.md §3.2):

    steps:
      - id: library
        run: "compute: models/library.py::run_cell"   # through scripts/compute.py
        inputs_jsonl: models/lib_inputs.jsonl
        out: models/lib_results.jsonl
        purpose: "plan: impact library"
        inputs: [models/library.py, models/model.py]
        outputs: [models/lib_results.jsonl]
      - id: allocate
        run: python3 models/scenarios.py          # a shell command, run from the run dir
        inputs: [models/lib_results.jsonl, data/units.csv]
        outputs: [allocation.csv]
      - id: figures
        run: python3 models/figures.py
        inputs: [allocation.csv]
        outputs: ["figures/*.png"]

Step keys: id, run, inputs, outputs, and optionally purpose, timeout_s; a
compute step (`run: "compute: <path.py>::<fn>"`) also needs inputs_jsonl, out
(one of its outputs) and purpose. Anything else is a validation error. A
step's input that matches another step's output is an edge of the DAG.
Validation fails closed: unknown keys, duplicate ids, an output claimed by two
steps, a cycle (a step reading its own output included), a path outside the
run directory, or an output the harness or a critic already owns.

Incremental: a step is stale when it never ran, last failed, its definition
changed, the hash of its inputs or code changed (code = every file named on
its command line plus the run-local modules a Python script imports,
recursively), or its outputs are missing or differ from what it last wrote
(a hand edit). Everything downstream of a stale step is stale. Hashes are in
`models/pipeline_state.json`; each run writes `models/pipeline_report.json`.

Owned outputs: declared outputs are protected artifacts
(agents/integrity.py). Only the harness's run (`harness_build`, with the
run's ArtifactIntegrity) may write them; it marks the outputs of the steps it
ran as harness-written and nothing else. Agents try changes with `--scratch`:
the step runs in a temporary copy of the run directory, and nothing in the
run directory changes.

Like scripts/derived_rebuild.py: commands run from the run directory, without
cloud credentials, each in its own process group (killed on timeout), within
an optional total time budget.

Known limits: a script's undeclared data reads (an `open()` of a file not in
`inputs`) and dynamic imports are not hashed; declare them. A shell script's
own callees are not followed. Scratch runs of compute steps are charged to the
copy's compute ledger, not the run's.
"""
from __future__ import annotations

import argparse
import ast
import fnmatch
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import nullcontext

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SPEC = os.path.join("models", "pipeline.yaml")
STATE = os.path.join("models", "pipeline_state.json")
REPORT = os.path.join("models", "pipeline_report.json")
# The runner's own records: written by the harness run only (protected in
# agents/enforcement.py SCRIPT_OUTPUTS).
OWN_FILES = ("models/pipeline_state.json", "models/pipeline_state.json.tmp",
             "models/pipeline_report.json", "models/pipeline_report.json.tmp")
STEP_KEYS = {"id", "run", "inputs", "outputs", "purpose", "timeout_s", "inputs_jsonl", "out"}
COMPUTE_KEYS = {"inputs_jsonl", "out"}
TOP_KEYS = {"steps", "version"}
DEFAULT_TIMEOUT_S = 3600
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_GLOB = re.compile(r"[*?\[]")


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

_ENF = []


def _enforcement():
    """agents/enforcement.py loaded by path (importing the agents package pulls in the SDK)."""
    if _ENF:
        return _ENF[0]
    import importlib.util
    p = os.path.join(REPO, "agents", "enforcement.py")
    spec = importlib.util.spec_from_file_location("_ma_enforcement", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _ENF.append(mod)
    return mod


def _reserved_patterns() -> tuple[str, ...]:
    """Files another owner writes: the harness and gate scripts, the critics, this runner."""
    try:
        outs = tuple(_enforcement().SCRIPT_OUTPUTS)
    except Exception:  # noqa: BLE001 — fail closed: the families we know
        outs = ("report.md", "metadata.json", "artifact_integrity.json", "final_gate.json", "*.yaml")
    return outs + ("critique_*.yaml", "critique_*.md", "fix_check_*.yaml", SPEC) + OWN_FILES


def script_env(environ=None) -> dict:
    env = dict(os.environ if environ is None else environ)
    try:
        pre = tuple(_enforcement().CLOUD_ENV_PREFIXES)
    except Exception:  # noqa: BLE001 — fail closed: strip the known families
        pre = ("AZ_", "AZURE_", "OPENAI_", "AWS_", "GOOGLE_")
    return {k: v for k, v in env.items() if not k.startswith(pre)}


def _sha(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _inside(path: str, root: str) -> bool:
    try:
        r = os.path.realpath(root)
        return os.path.commonpath([os.path.realpath(path), r]) == r
    except ValueError:
        return False


def _matches(run_dir: str, pat: str) -> list[str]:
    """Run-relative files matching a declared path or glob (inside the run dir only)."""
    out = []
    for p in glob.glob(os.path.join(run_dir, pat), recursive=True):
        if os.path.isfile(p) and _inside(p, run_dir):
            out.append(os.path.relpath(p, run_dir).replace(os.sep, "/"))
    return sorted(out)


def _overlap(a: str, b: str, run_dir: str | None = None) -> bool:
    """Two declared paths/globs can name the same file."""
    if a == b or fnmatch.fnmatchcase(a, b) or fnmatch.fnmatchcase(b, a):
        return True
    return bool(run_dir) and bool(set(_matches(run_dir, a)) & set(_matches(run_dir, b)))


def _compute_fn(step: dict) -> str | None:
    run = step.get("run")
    if isinstance(run, str) and run.strip().startswith("compute:"):
        return run.strip()[len("compute:"):].strip()
    return None


def step_outputs(step: dict) -> list[str]:
    """Declared outputs plus the compute service's summary of a compute step's results file."""
    outs = list(step.get("outputs") or [])
    if _compute_fn(step) and step.get("out"):
        s = step["out"] + ".summary.json"
        if s not in outs:
            outs.append(s)
    return outs


def step_inputs(step: dict) -> list[str]:
    ins = list(step.get("inputs") or [])
    if _compute_fn(step):
        for x in (step.get("inputs_jsonl"), (_compute_fn(step) or "").partition("::")[0]):
            if x and x not in ins:
                ins.append(x)
    return ins


# --------------------------------------------------------------------------- #
# Loading and validation
# --------------------------------------------------------------------------- #

def _check_path(p, run_dir: str, what: str) -> str | None:
    if not isinstance(p, str) or not p.strip():
        return f"{what}: {p!r} is not a non-empty path string"
    if os.path.isabs(p) or p.startswith("~"):
        return f"{what}: {p!r} is absolute; paths are relative to the run directory"
    parts = p.replace("\\", "/").split("/")
    if ".." in parts:
        return f"{what}: {p!r} leaves the run directory"
    if os.path.normpath(p) != p.rstrip("/") and not p.startswith("./"):
        return f"{what}: {p!r} is not a normalized path (write it as {os.path.normpath(p)!r})"
    # The deepest non-glob parent, and every existing match, must resolve inside the run dir (symlinks).
    fixed = []
    for part in parts:
        if _GLOB.search(part):
            break
        fixed.append(part)
    if fixed and not _inside(os.path.join(run_dir, *fixed), run_dir):
        return f"{what}: {p!r} resolves outside the run directory (symlink)"
    for m in glob.glob(os.path.join(run_dir, p), recursive=True):
        if not _inside(m, run_dir):
            return f"{what}: {p!r} matches {m}, outside the run directory (symlink)"
    return None


def _normalize(step: dict) -> dict:
    s = dict(step)
    run = s.get("run")
    if isinstance(run, dict) and set(run) == {"compute"}:
        s["run"] = f"compute: {run['compute']}"     # `run: {compute: path::fn}` is the same step
    for k in ("inputs", "outputs"):
        if isinstance(s.get(k), str):
            s[k] = [s[k]]
    for k in ("inputs", "outputs"):
        if isinstance(s.get(k), list):
            s[k] = [x[2:] if isinstance(x, str) and x.startswith("./") else x for x in s[k]]
    return s


def load(run_dir: str) -> tuple[list[dict], list[str]]:
    """(steps in topological order, errors). Any error: no steps (fail closed)."""
    import yaml
    run_dir = os.path.realpath(run_dir)
    p = os.path.join(run_dir, SPEC)
    if not os.path.isfile(p):
        return [], [f"{SPEC} not found"]
    try:
        with open(p) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        return [], [f"{SPEC} does not parse: {type(e).__name__}: {str(e)[:300]}"]
    if not isinstance(doc, dict):
        return [], [f"{SPEC} must be a mapping with a `steps` list"]
    errs = [f"unknown top-level key {k!r} (allowed: {sorted(TOP_KEYS)})" for k in doc if k not in TOP_KEYS]
    raw = doc.get("steps")
    if not isinstance(raw, list) or not raw:
        return [], errs + ["`steps` must be a non-empty list"]
    steps, seen = [], set()
    reserved = _reserved_patterns()
    for n, st in enumerate(raw):
        where = f"step {n + 1}"
        if not isinstance(st, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        st = _normalize(st)
        sid = st.get("id")
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            errs.append(f"{where}: `id` must match {_ID.pattern}")
        else:
            where = f"step {sid!r}"
            if sid in seen:
                errs.append(f"{where}: duplicate id")
            seen.add(sid)
        for k in st:
            if k not in STEP_KEYS:
                errs.append(f"{where}: unknown key {k!r} (allowed: {sorted(STEP_KEYS)})")
        if not isinstance(st.get("run"), str) or not st["run"].strip():
            errs.append(f"{where}: `run` must be a shell command or `compute: <path.py>::<fn>`")
        for k in ("inputs", "outputs"):
            if not isinstance(st.get(k), list) or not all(isinstance(x, str) for x in st[k]):
                errs.append(f"{where}: `{k}` must be a list of paths")
                st[k] = []
        if not st["outputs"]:
            errs.append(f"{where}: declares no outputs")
        if "timeout_s" in st and (not isinstance(st["timeout_s"], (int, float)) or isinstance(st["timeout_s"], bool)
                                  or st["timeout_s"] <= 0):
            errs.append(f"{where}: `timeout_s` must be a positive number")
        if "purpose" in st and (not isinstance(st["purpose"], str) or not st["purpose"].strip()):
            errs.append(f"{where}: `purpose` must be a non-empty string")
        fn = _compute_fn(st)
        if fn is not None:
            if not re.fullmatch(r"[^:\s]+\.py::[A-Za-z_]\w*", fn):
                errs.append(f"{where}: compute step must name `<path.py>::<fn>`, got {fn!r}")
            else:
                e = _check_path(fn.partition("::")[0], run_dir, f"{where} compute function")
                if e:
                    errs.append(e)
            for k in sorted(COMPUTE_KEYS | {"purpose"}):
                if not isinstance(st.get(k), str) or not st[k].strip():
                    errs.append(f"{where}: a compute step needs `{k}`")
            if isinstance(st.get("out"), str) and st["out"] not in st["outputs"]:
                errs.append(f"{where}: `out` ({st['out']}) must be one of its `outputs`")
            for k in ("inputs_jsonl", "out"):
                if isinstance(st.get(k), str) and _GLOB.search(st[k]):
                    errs.append(f"{where}: `{k}` must be a file, not a glob")
        else:
            for k in sorted(COMPUTE_KEYS):
                if k in st:
                    errs.append(f"{where}: `{k}` is only for compute steps")
        for x in st["inputs"] + st["outputs"] + [st[k] for k in ("inputs_jsonl", "out") if isinstance(st.get(k), str)]:
            e = _check_path(x, run_dir, where)
            if e:
                errs.append(e)
        for o in step_outputs(st):
            if not isinstance(o, str):
                continue
            bad = [r for r in reserved if _overlap(o, r)] + \
                  [m for m in _matches(run_dir, o) if any(fnmatch.fnmatchcase(m, r) for r in reserved)]
            if bad:
                errs.append(f"{where}: output {o!r} is owned elsewhere ({', '.join(sorted(set(bad))[:3])}); "
                            "pipeline steps may not write harness, gate or critique files")
        steps.append(st)
    if errs:
        return [], errs
    # Outputs claimed twice (two steps, or twice in one step's overlapping globs is fine).
    for i, a in enumerate(steps):
        for b in steps[i + 1:]:
            for oa in step_outputs(a):
                for ob in step_outputs(b):
                    if _overlap(oa, ob, run_dir):
                        errs.append(f"output {oa!r} of step {a['id']!r} and {ob!r} of step {b['id']!r} "
                                    "can name the same file: each file has one producing step")
    if errs:
        return [], errs
    deps = {s["id"]: set() for s in steps}
    for s in steps:
        for i in step_inputs(s):
            for t in steps:
                if any(_overlap(i, o, run_dir) for o in step_outputs(t)):
                    deps[s["id"]].add(t["id"])
    order, state = [], {}

    def visit(sid, path):
        if state.get(sid) == 2:
            return
        if state.get(sid) == 1:
            cyc = path[path.index(sid):] + [sid]
            errs.append("cycle: " + " -> ".join(cyc))
            return
        state[sid] = 1
        for d in sorted(deps[sid], key=[s["id"] for s in steps].index):
            visit(d, path + [sid])
            if errs:
                return
        state[sid] = 2
        order.append(sid)

    for s in steps:
        visit(s["id"], [])
        if errs:
            return [], errs
    by_id = {s["id"]: s for s in steps}
    out = []
    for sid in order:
        s = dict(by_id[sid])
        s["_deps"] = sorted(deps[sid])
        out.append(s)
    return out, []


def declared_outputs(run_dir: str) -> list[str]:
    """Output paths/globs pipeline.yaml declares, for the integrity record. Lenient on
    purpose (fail closed): a pipeline that does not validate still protects every
    output it names inside the run dir, except files another owner already holds."""
    import yaml
    try:
        with open(os.path.join(run_dir, SPEC)) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return []
    raw = doc.get("steps") if isinstance(doc, dict) else None
    reserved = _reserved_patterns()
    pats = []
    for st in raw if isinstance(raw, list) else []:
        if not isinstance(st, dict):
            continue
        st = _normalize(st)
        if not isinstance(st.get("outputs"), list):
            st["outputs"] = []
        for o in step_outputs(st):
            if isinstance(o, str) and o.strip() and _check_path(o, run_dir, "") is None \
                    and not any(_overlap(o, r) for r in reserved) and o not in pats:
                pats.append(o)
    return pats


# --------------------------------------------------------------------------- #
# Hashing and planning
# --------------------------------------------------------------------------- #

def _local_imports(py: str, run_dir: str, seen: set) -> None:
    """Run-local modules a Python file imports (next to it, or under models/), recursively."""
    if py in seen or not os.path.isfile(py):
        return
    seen.add(py)
    try:
        tree = ast.parse(open(py, encoding="utf-8", errors="replace").read())
    except (SyntaxError, ValueError, OSError):
        return
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level:
            names.update(a.name for a in node.names)
    roots = [os.path.dirname(py), os.path.join(run_dir, "models"), run_dir]
    for name in names:
        rel = name.replace(".", os.sep)
        for root in roots:
            for cand in (os.path.join(root, rel + ".py"), os.path.join(root, rel, "__init__.py")):
                if os.path.isfile(cand) and _inside(cand, run_dir):
                    _local_imports(os.path.realpath(cand), run_dir, seen)


def code_files(step: dict, run_dir: str) -> list[str]:
    """Files the step's command runs: every existing file named on the command line inside
    the run dir (any .py outside it too), plus the run-local modules Python files import."""
    run_dir = os.path.realpath(run_dir)
    fn = _compute_fn(step)
    if fn is not None:
        toks = [fn.partition("::")[0]]
    else:
        try:
            toks = shlex.split(step["run"], comments=True)
        except ValueError:
            toks = step["run"].split()
    named, py = set(), set()
    for t in toks:
        if not t or t.startswith("-"):
            continue
        t = t.split("=", 1)[1] if re.match(r"^\w+=", t) else t
        p = os.path.realpath(os.path.join(run_dir, os.path.expanduser(t)))
        if not os.path.isfile(p):
            continue
        if _inside(p, run_dir) or p.endswith(".py"):
            named.add(p)
            if p.endswith(".py") and _inside(p, run_dir):
                _local_imports(p, run_dir, py)
    return sorted(named | py)


def _rel(p: str, run_dir: str) -> str:
    return os.path.relpath(p, run_dir).replace(os.sep, "/") if _inside(p, run_dir) else p


def fingerprint(step: dict, run_dir: str) -> dict:
    """What a step's result depends on: its definition, its input files and its code."""
    spec = {k: v for k, v in step.items() if not k.startswith("_")}
    ins, missing = {}, []
    for pat in step_inputs(step):
        m = _matches(run_dir, pat)
        if not m:
            missing.append(pat)
        for rel in m:
            ins[rel] = _sha(os.path.join(run_dir, rel))
    code = {_rel(p, run_dir): _sha(p) for p in code_files(step, run_dir)}
    key = hashlib.sha256(json.dumps({"spec": spec, "inputs": ins, "code": code}, sort_keys=True).encode()).hexdigest()
    return {"key": key, "inputs": ins, "code": code, "missing_inputs": missing}


def output_hashes(step: dict, run_dir: str) -> dict:
    return {rel: _sha(os.path.join(run_dir, rel)) for pat in step_outputs(step) for rel in _matches(run_dir, pat)}


def _missing_outputs(step: dict, run_dir: str) -> list[str]:
    return [o for o in step_outputs(step) if not _matches(run_dir, o)]


def _load_state(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, STATE)) as f:
            s = json.load(f)
        return s if isinstance(s, dict) and isinstance(s.get("steps"), dict) else {"steps": {}}
    except (OSError, ValueError):
        return {"steps": {}}


def _write_json(run_dir: str, rel: str, obj) -> str:
    p = os.path.join(run_dir, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p + ".tmp", "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
    os.replace(p + ".tmp", p)
    return rel.replace(os.sep, "/")


def _why_stale(step: dict, run_dir: str, rec: dict | None) -> str | None:
    if not isinstance(rec, dict):
        return "never run"
    if not rec.get("ok"):
        return "last run failed"
    fp = fingerprint(step, run_dir)
    if fp["missing_inputs"]:
        return f"inputs missing: {', '.join(fp['missing_inputs'])}"
    if fp["key"] != rec.get("key"):
        changed = sorted({k for k in set(fp["inputs"]) | set(rec.get("inputs") or {})
                          if fp["inputs"].get(k) != (rec.get("inputs") or {}).get(k)}
                         | {k for k in set(fp["code"]) | set(rec.get("code") or {})
                            if fp["code"].get(k) != (rec.get("code") or {}).get(k)})
        return f"changed: {', '.join(changed[:5])}" if changed else "step definition changed"
    miss = _missing_outputs(step, run_dir)
    if miss:
        return f"outputs missing: {', '.join(miss)}"
    if output_hashes(step, run_dir) != rec.get("outputs"):
        return "outputs differ from what the step last wrote"
    return None


def plan(run_dir: str, steps: list[dict] | None = None) -> list[dict]:
    """Steps to rerun, in run order: [{id, why}]. Raises ValueError if the pipeline is invalid."""
    run_dir = os.path.realpath(run_dir)
    if steps is None:
        steps, errs = load(run_dir)
        if errs:
            raise ValueError("; ".join(errs))
    state = _load_state(run_dir)["steps"]
    stale: dict[str, str] = {}
    for s in steps:
        up = [d for d in s["_deps"] if d in stale]
        why = f"downstream of {', '.join(up)}" if up else _why_stale(s, run_dir, state.get(s["id"]))
        if why:
            stale[s["id"]] = why
    return [{"id": s["id"], "why": stale[s["id"]]} for s in steps if s["id"] in stale]


# --------------------------------------------------------------------------- #
# Running
# --------------------------------------------------------------------------- #

def _run_cmd(cmd, cwd, timeout_s, env, shell=False):
    """Own process group; on timeout the whole group is killed (as derived_rebuild)."""
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                         text=True, env=env, start_new_session=True, shell=shell)
    try:
        _out, err = p.communicate(timeout=timeout_s)
        return p.returncode, (err or "")[-800:]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return None, f"timed out after {timeout_s:.0f}s (process group killed)"


def _exec_step(step: dict, run_dir: str, timeout_s: float, python: str | None) -> tuple[int | None, str]:
    env = script_env()
    # A fresh bytecode cache per step: Python trusts a .pyc whose source has the same
    # size and mtime second, so an edit made within a second of the last run would
    # otherwise run the old code (seen in this file's self-test).
    cache = tempfile.mkdtemp(prefix="pipeline_pyc_")
    env["PYTHONPYCACHEPREFIX"] = cache
    try:
        fn = _compute_fn(step)
        if fn is not None:
            cmd = [python or sys.executable, os.path.join(HERE, "compute.py"), "map", run_dir, "--fn", fn,
                   "--inputs", os.path.join(run_dir, step["inputs_jsonl"]), "--out", os.path.join(run_dir, step["out"]),
                   "--purpose", step["purpose"].strip(), "--timeout-s", str(max(1.0, timeout_s))]
            return _run_cmd(cmd, run_dir, timeout_s + 60, env)
        return _run_cmd(step["run"], run_dir, timeout_s, env, shell=True)
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def _snapshot(step: dict, run_dir: str) -> dict:
    snap = {}
    for pat in step_outputs(step):
        for rel in _matches(run_dir, pat):
            try:
                snap[rel] = os.stat(os.path.join(run_dir, rel)).st_mtime_ns
            except OSError:
                pass
    return snap


def _run_one(step: dict, run_dir: str, timeout_s: float, python: str | None) -> dict:
    """Run a step and check what it wrote. Returns its report row (without bookkeeping)."""
    fp = fingerprint(step, run_dir)
    if fp["missing_inputs"]:
        return {"returncode": None, "seconds": 0.0, "ok": False,
                "error": f"inputs missing: {', '.join(fp['missing_inputs'])}", "written": [], "removed": []}
    before = _snapshot(step, run_dir)
    s0 = time.time()
    try:
        rc, err = _exec_step(step, run_dir, timeout_s, python)
    except OSError as e:
        rc, err = None, f"{type(e).__name__}: {e}"
    row = {"returncode": rc, "seconds": round(time.time() - s0, 1), "ok": rc == 0, "written": [], "removed": []}
    after = _snapshot(step, run_dir)
    row["written"] = sorted(r for r, m in after.items() if before.get(r) != m)
    if rc != 0:
        row["error"] = err
        return row
    problems = []
    for pat in step_outputs(step):
        hits = [r for r in after if fnmatch.fnmatchcase(r, pat) or r == pat]
        fresh = [r for r in hits if r in row["written"]]
        if not fresh:
            problems.append(f"{pat}: not written by this run" if hits else f"{pat}: not produced")
        elif _GLOB.search(pat):
            # Files matching a glob output that this run did not rewrite are leftovers of an
            # earlier version of the step: removed, so no stale file passes as current.
            for r in hits:
                if r not in row["written"]:
                    try:
                        os.remove(os.path.join(run_dir, r))
                        row["removed"].append(r)
                    except OSError as e:
                        problems.append(f"{r}: stale leftover could not be removed ({e})")
    if problems:
        row["ok"] = False
        row["error"] = "declared outputs: " + "; ".join(problems)
    return row


def run(run_dir: str, budget_s: float | None = None, integrity=None, python: str | None = None,
        only: str | None = None, force: bool = False) -> dict:
    """Run the stale steps in topological order and record their hashes.

    A failed step stops everything downstream of it (their records are left
    unchanged, so they stay stale). With `integrity` (the run's
    ArtifactIntegrity) the run holds a harness grant for the outputs of the
    steps it runs, and marks each file those steps wrote as harness-written.
    `only` + `force` run one step regardless of staleness (scratch mode)."""
    run_dir = os.path.realpath(run_dir)
    t0 = time.time()
    steps, errs = load(run_dir)
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "errors": errs, "plan": [], "steps": [], "ok": not errs}
    if errs:
        _finish(run_dir, report, t0, integrity, [])
        return report
    if only is not None:
        if only not in {s["id"] for s in steps}:
            report.update(errors=[f"no step {only!r}"], ok=False)
            _finish(run_dir, report, t0, integrity, [])
            return report
        report["plan"] = [{"id": only, "why": "requested"}] if force else [p for p in plan(run_dir, steps) if p["id"] == only]
    else:
        report["plan"] = plan(run_dir, steps)
    todo = {p["id"] for p in report["plan"]}
    by_id = {s["id"]: s for s in steps}
    grant = [o for s in steps if s["id"] in todo for o in step_outputs(s)] + list(OWN_FILES)
    ctx = integrity.harness_pipeline(grant) if integrity is not None else nullcontext()
    with ctx:
        state = _load_state(run_dir)
        failed: set[str] = set()
        produced: list[str] = []
        for s in steps:
            if s["id"] not in todo:
                continue
            row = {"id": s["id"], "why": next(p["why"] for p in report["plan"] if p["id"] == s["id"])}
            up = [d for d in s["_deps"] if d in failed]
            left = None if budget_s is None else budget_s - (time.time() - t0)
            if up:
                row.update(status="skipped", error=f"upstream step failed: {', '.join(up)}")
                failed.add(s["id"])
            elif left is not None and left < 5:
                row.update(status="not_started", error="pipeline time budget used")
                failed.add(s["id"])
            else:
                tmo = float(s.get("timeout_s") or DEFAULT_TIMEOUT_S)
                res = _run_one(s, run_dir, tmo if left is None else min(tmo, left), python)
                row.update(res, status="ok" if res["ok"] else "failed")
                produced += res["written"]
                if res["ok"]:
                    fp = fingerprint(s, run_dir)
                    state["steps"][s["id"]] = {"ok": True, "key": fp["key"], "inputs": fp["inputs"],
                                               "code": fp["code"], "outputs": output_hashes(s, run_dir),
                                               "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                else:
                    failed.add(s["id"])
                    state["steps"][s["id"]] = {"ok": False, "at": time.strftime("%Y-%m-%dT%H:%M:%S")}
                produced.append(_write_json(run_dir, STATE, state))
            report["steps"].append(row)
        report["ok"] = not failed
        _finish(run_dir, report, t0, integrity, produced, by_id)
    return report


def _finish(run_dir, report, t0, integrity, produced, by_id=None):
    report["seconds"] = round(time.time() - t0, 1)
    produced = list(produced)
    try:
        produced.append(_write_json(run_dir, REPORT, report))
    except OSError:
        pass
    if integrity is not None:
        # Only the files the steps that ran wrote, and the runner's own records it
        # wrote this time: never other files (that would accept an agent's edit).
        for rel in sorted(set(produced)):
            integrity.harness_wrote(rel)


def harness_build(run_dir: str, integrity, budget_s: float | None = None) -> dict:
    """HOOK POINT (S1): the harness's pipeline run. Call it with the run's
    ArtifactIntegrity wherever the harness rebuilds (after a modeler/analyst
    stage, before a gate). Requires the integrity record: without it the
    outputs would be flagged as unexplained changes."""
    if integrity is None:
        raise ValueError("harness_build needs the run's ArtifactIntegrity")
    return run(run_dir, budget_s=budget_s, integrity=integrity)


def scratch(run_dir: str, step_id: str, python: str | None = None, keep: bool = True) -> dict:
    """Run one step in a temporary copy of the run dir. Nothing in the run dir changes."""
    run_dir = os.path.realpath(run_dir)
    dst = os.path.realpath(tempfile.mkdtemp(prefix="pipeline_scratch_"))
    copy = os.path.join(dst, os.path.basename(run_dir))
    shutil.copytree(run_dir, copy, symlinks=True)
    # A symlink in the copy that points back into the real run dir would let the step
    # write there: re-point it at the same place in the copy.
    for dp, dirs, fns in os.walk(copy):
        for x in dirs + fns:
            p = os.path.join(dp, x)
            if os.path.islink(p):
                tgt = os.path.realpath(p)
                if tgt == run_dir or tgt.startswith(run_dir + os.sep):
                    os.remove(p)
                    os.symlink(os.path.join(copy, os.path.relpath(tgt, run_dir)), p)
    rep = run(copy, python=python, only=step_id, force=True)
    rep["scratch_dir"] = copy
    if not keep:
        shutil.rmtree(dst, ignore_errors=True)
    return rep


def summary_line(rep: dict) -> str:
    if rep.get("errors"):
        return "pipeline: invalid: " + "; ".join(rep["errors"][:5])
    ran = [r for r in rep["steps"] if r.get("status") == "ok"]
    bad = [r for r in rep["steps"] if r.get("status") != "ok"]
    return (f"pipeline: {len(ran)} step(s) rebuilt" + (f" ({', '.join(r['id'] for r in ran)})" if ran else "")
            + (f"; {len(bad)} not rebuilt: " + ", ".join(f"{r['id']} ({r.get('status')})" for r in bad) if bad else "")
            + f" [{rep.get('seconds', 0)}s]")


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #

def _tree_hash(d: str) -> dict:
    out = {}
    for dp, _dirs, fns in os.walk(d):
        for fn in fns:
            p = os.path.join(dp, fn)
            out[os.path.relpath(p, d)] = (_sha(p), os.stat(p).st_mtime_ns)
    return out


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def w(d, rel, txt):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(txt)

    def spec(d, steps):
        import yaml
        w(d, SPEC, yaml.safe_dump({"steps": steps}, sort_keys=False))

    py = shlex.quote(sys.executable)
    three = [
        {"id": "a", "run": f"{py} models/a.py", "inputs": ["data/x.csv"], "outputs": ["models/a.out"]},
        {"id": "b", "run": f"{py} models/b.py", "inputs": ["models/a.out", "data/y.csv"], "outputs": ["models/b.out"]},
        {"id": "c", "run": f"{py} models/c.py", "inputs": ["models/b.out"], "outputs": ["figures/*.png"]},
    ]

    def fixture(d):
        w(d, "data/x.csv", "1\n")
        w(d, "data/y.csv", "10\n")
        w(d, "models/helper.py", "K = 1\n")
        w(d, "models/a.py", "import helper\nv = int(open('data/x.csv').read()) + helper.K\n"
                            "open('models/a.out', 'w').write(str(v))\n")
        w(d, "models/b.py", "v = int(open('models/a.out').read()) + int(open('data/y.csv').read())\n"
                            "open('models/b.out', 'w').write(str(v))\n")
        w(d, "models/c.py", "import os\nos.makedirs('figures', exist_ok=True)\n"
                            "open('figures/f1.png', 'w').write(open('models/b.out').read())\n")
        spec(d, three)

    def ids(rep, status="ok"):
        return [r["id"] for r in rep["steps"] if r.get("status") == status]

    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        fixture(d)
        steps, errs = load(d)
        ok(not errs and [s["id"] for s in steps] == ["a", "b", "c"] and steps[1]["_deps"] == ["a"],
           f"PL1 a valid 3-step pipeline loads in DAG order: {errs} {[s.get('_deps') for s in steps]}")
        ok([p["id"] for p in plan(d)] == ["a", "b", "c"], f"PL2 a pipeline that never ran is all stale: {plan(d)}")
        rep = run(d)
        ok(rep["ok"] and ids(rep) == ["a", "b", "c"] and open(os.path.join(d, "figures/f1.png")).read() == "12"
           and os.path.exists(os.path.join(d, REPORT)) and os.path.exists(os.path.join(d, STATE)),
           f"PL3 the first run builds every step and records it: {summary_line(rep)} {rep['steps']}")
        ok(plan(d) == [] and run(d)["steps"] == [], f"PL4 nothing changed: nothing reruns: {plan(d)}")
        w(d, "data/y.csv", "20\n")
        ok([p["id"] for p in plan(d)] == ["b", "c"], f"PL5 a changed input: only its step and downstream: {plan(d)}")
        rep = run(d)
        ok(ids(rep) == ["b", "c"] and open(os.path.join(d, "figures/f1.png")).read() == "22",
           f"PL6 ... and they rerun with the new value: {rep['steps']}")
        w(d, "models/helper.py", "K = 2\n")
        ok([p["id"] for p in plan(d)] == ["a", "b", "c"],
           f"PL7 a change to a module the step's script imports makes it stale: {plan(d)}")
        run(d)
        w(d, "models/b.out", "999")
        ok([p["id"] for p in plan(d)] == ["b", "c"], f"PL8 a hand-edited output is stale: {plan(d)}")
        run(d)
        _b = open(os.path.join(d, "models/b.out")).read()
        ok(_b == "23", f"PL8b ... and is rebuilt: {_b}")
        # A leftover file matching a glob output that the step no longer writes is removed.
        w(d, "figures/old.png", "stale")
        w(d, "models/b.out", "0")
        rep = run(d)
        ok(not os.path.exists(os.path.join(d, "figures/old.png"))
           and any("figures/old.png" in r.get("removed", []) for r in rep["steps"]),
           f"PL9 a stale leftover under a glob output is removed: {rep['steps']}")
        # A failing step stops its downstream; a fixed step then reruns with its downstream.
        w(d, "models/b.py", "raise SystemExit('boom')\n")
        rep = run(d)
        st = {r["id"]: r["status"] for r in rep["steps"]}
        ok(st == {"b": "failed", "c": "skipped"} and not rep["ok"] and "boom" in rep["steps"][0].get("error", ""),
           f"PL10 a failing step stops its downstream: {st}")
        ok([p["id"] for p in plan(d)] == ["b", "c"], f"PL11 after a failure both stay stale: {plan(d)}")
        w(d, "models/b.py", "open('models/b.out', 'w').write('7')\n")
        rep = run(d)
        ok(ids(rep) == ["b", "c"] and rep["ok"], f"PL12 a fix reruns the step and its downstream: {rep['steps']}")
        # A step that exits 0 without writing its declared output fails.
        w(d, "models/b.py", "pass\n")
        rep = run(d)
        ok(rep["steps"][0]["status"] == "failed" and "not written" in rep["steps"][0]["error"],
           f"PL13 a step that leaves its declared output unwritten fails: {rep['steps']}")
        w(d, "models/b.py", "open('models/b.out', 'w').write('7')\n")
        run(d)
        # Budget, timeout and environment.
        w(d, "data/x.csv", "5\n")
        rep = run(d, budget_s=0)
        ok([r["status"] for r in rep["steps"]] == ["not_started", "skipped", "skipped"],
           f"PL14 nothing starts once the budget is used: {rep['steps']}")
        w(d, "models/a.py", "import time\ntime.sleep(30)\n")
        t1 = time.time()
        spec(d, [dict(three[0], timeout_s=1)] + three[1:])
        rep = run(d)
        ok(rep["steps"][0]["returncode"] is None and time.time() - t1 < 15, f"PL15 a timed-out step is killed: {rep['steps']}")
        ok("AZURE_CLIENT_SECRET" not in script_env({"AZURE_CLIENT_SECRET": "s", "HOME": "/h"}),
           "PL16 steps run without cloud credentials")
        # Scratch mode: runs in a copy; the run dir is untouched.
        fixture(d)
        run(d)
        w(d, "models/b.py", "open('models/b.out', 'w').write('scratch')\n")
        before = _tree_hash(d)
        rep = scratch(d, "b", keep=False)
        ok(rep["ok"] and _tree_hash(d) == before and rep["scratch_dir"] != d,
           f"PL17 scratch mode leaves the run dir untouched: {summary_line(rep)}")
        rep = scratch(d, "b")
        ok(open(os.path.join(rep["scratch_dir"], "models/b.out")).read() == "scratch"
           and open(os.path.join(d, "models/b.out")).read() != "scratch",
           "PL18 scratch outputs land in the copy only")
        shutil.rmtree(os.path.dirname(rep["scratch_dir"]), ignore_errors=True)
        os.symlink(os.path.join(d, "models"), os.path.join(d, "models_link"))
        w(d, "models/b.py", "open('models_link/b.out', 'w').write('via-link')\n")
        before = _tree_hash(d)
        rep = scratch(d, "b", keep=False)
        ok(_tree_hash(d) == before, "PL19 a symlink back into the run dir cannot carry scratch writes into it")
        os.remove(os.path.join(d, "models_link"))
        # CLI dry run.
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", d, "--dry-run"],
                           capture_output=True, text=True)
        ok(r.returncode == 0 and "b" in r.stdout and "changed" in r.stdout, f"PL20 --dry-run prints the plan: {r.stdout}{r.stderr}")
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", d, "b"], capture_output=True, text=True)
        ok(r.returncode == 2, "PL20b a single step outside scratch mode is refused")

    # Validation fails closed.
    def errs_of(steps, extra=None):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            fixture(d)
            if extra:
                extra(d)
            spec(d, steps)
            return load(d)[1]

    cyc = [dict(three[0], inputs=["models/b.out"]), three[1], three[2]]
    e = errs_of(cyc)
    ok(any("cycle" in x for x in e), f"PV1 a cycle fails validation: {e}")
    e = errs_of([dict(three[0], inputs=["models/a.out"])])
    ok(any("cycle" in x for x in e), f"PV2 a step reading its own output is a cycle: {e}")
    e = errs_of([three[0], dict(three[1], outputs=["models/a.out"])])
    ok(any("same file" in x for x in e), f"PV3 an output claimed by two steps fails: {e}")
    e = errs_of([three[0], dict(three[1], outputs=["models/*.out"])])
    ok(any("same file" in x for x in e), f"PV4 an overlapping glob output fails: {e}")
    e = errs_of([dict(three[0], outputs=["../escape.csv"])])
    ok(any("leaves the run directory" in x for x in e), f"PV5 a path escape fails: {e}")
    e = errs_of([dict(three[0], inputs=["/etc/passwd"])])
    ok(any("absolute" in x for x in e), f"PV6 an absolute path fails: {e}")
    e = errs_of([dict(three[0], outputs=["out/x.csv"])],
                extra=lambda d: os.symlink(tempfile.gettempdir(), os.path.join(d, "out")))
    ok(any("symlink" in x for x in e), f"PV7 a symlink escape fails: {e}")
    e = errs_of([three[0], dict(three[1], id="a")])
    ok(any("duplicate id" in x for x in e), f"PV8 duplicate ids fail: {e}")
    e = errs_of([dict(three[0], extra=1)])
    ok(any("unknown key" in x for x in e), f"PV9 an unknown key fails: {e}")
    e = errs_of([dict(three[0], outputs=["report.md"])])
    e2 = errs_of([dict(three[0], outputs=["*.json"])])
    e3 = errs_of([dict(three[0], outputs=["models/pipeline_state.json"])])
    ok(all(any("owned elsewhere" in x for x in ee) for ee in (e, e2, e3)),
       f"PV10 outputs owned by the harness, gate or runner are refused: {e} {e2} {e3}")
    e = errs_of([{"id": "lib", "run": "compute: models/lib.py::f", "inputs": [], "outputs": ["models/r.jsonl"]}])
    ok(any("inputs_jsonl" in x for x in e) and any("purpose" in x for x in e),
       f"PV11 a compute step needs inputs_jsonl, out and purpose: {e}")
    with tempfile.TemporaryDirectory() as d:
        w(d, SPEC, "steps:\n  - id: lib\n    run: compute: models/lib.py::f\n")
        ok(load(d)[1] and "does not parse" in load(d)[1][0], "PV12 unparseable YAML fails closed")

    # A compute step goes through the compute service with its purpose.
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        w(d, "models/lib.py", "def f(p):\n    return {'y': p['x'] * 2}\n")
        w(d, "models/lib_in.jsonl", '{"x": 1}\n{"x": 2}\n')
        spec(d, [{"id": "lib", "run": "compute: models/lib.py::f", "inputs_jsonl": "models/lib_in.jsonl",
                  "out": "models/lib_out.jsonl", "purpose": "plan: self-test", "inputs": [],
                  "outputs": ["models/lib_out.jsonl"]}])
        env0 = dict(os.environ)
        os.environ["MA_COMPUTE_MAX_CORE_HOURS"] = "1"
        try:
            rep = run(d)
        finally:
            os.environ.clear()
            os.environ.update(env0)
        rows = [json.loads(x) for x in open(os.path.join(d, "models/lib_out.jsonl"))] if rep["ok"] else []
        summ = json.load(open(os.path.join(d, "models/lib_out.jsonl.summary.json"))) if rep["ok"] else {}
        ok(rep["ok"] and [r["result"]["y"] for r in rows] == [2, 4] and summ.get("purpose") == "plan: self-test",
           f"PC1 a compute step runs through compute.run_map with its purpose: {rep['steps']}")
        ok(plan(d) == [], f"PC2 ... and is fresh afterwards: {plan(d)}")

    # Ownership: declared outputs are protected; only the harness run explains writes to them.
    try:
        sys.path.insert(0, REPO)
        from agents.integrity import ArtifactIntegrity
    except Exception as e:  # noqa: BLE001
        fails.append(f"PO0 agents.integrity imports: {type(e).__name__}: {e}")
        ArtifactIntegrity = None
    if ArtifactIntegrity is not None:
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            fixture(d)
            w(d, "models/a.out", "hand-written before the declaration")
            ig = ArtifactIntegrity(d, process_check=lambda: set())
            ok("models/a.out" in ig.hashes and not ig.open_events,
               f"PO1 declared outputs are protected; files already there are baselined: {ig.open_events}")
            rep = harness_build(d, ig)
            ig.on_pre("t1", "Bash", {"command": "ls"}, "modeler", cwd=d)
            new = ig.on_post("t1")
            ok(rep["ok"] and not new and not ig.open_events,
               f"PO2 outputs written by the harness run are not flagged: {new} {ig.open_events}")
            ig.on_pre("t2", "Bash", {"command": "echo 5 > models/b.out"}, "modeler", cwd=d)
            w(d, "models/b.out", "5")
            new = ig.on_post("t2")
            ok([x["file"] for x in new] == ["models/b.out"],
               f"PO3 a declared output written outside the runner is flagged: {new}")
            ig.on_pre("t3", "Write", {"file_path": os.path.join(d, "figures/f1.png")}, "modeler", cwd=d)
            w(d, "figures/f1.png", "x")
            new = ig.on_post("t3")
            ok([x["file"] for x in new] == ["figures/f1.png"],
               f"PO4 an agent's own Write to a declared (glob) output is flagged too: {new}")
            ig.on_pre("t4", "Write", {"file_path": os.path.join(d, STATE)}, "modeler", cwd=d)
            w(d, STATE, "{}")
            new = ig.on_post("t4")
            ok([x["file"] for x in new] == ["models/pipeline_state.json"],
               f"PO5 the runner's state file is protected: {new}")
            rep = harness_build(d, ig)
            ok(rep["ok"] and not ig.open_events,
               f"PO6 the harness run rewrites the hand-edited outputs and resolves the violations: {ig.open_events}")
            # An undeclared protected file a step writes is still flagged.
            w(d, "models/c.py", open(os.path.join(d, "models/c.py")).read() + "open('report.md', 'w').write('x')\n")
            harness_build(d, ig)
            ok(any(x["file"] == "report.md" and x.get("open", True) for x in ig.events),
               f"PO7 a step writing a protected file it does not declare is flagged: {ig.open_events}")
            # Removing a declaration does not unprotect its files (sticky).
            spec(d, three[:1])
            ig.on_pre("t5", "Bash", {"command": "true"}, "modeler", cwd=d)
            ig.on_post("t5")
            w(d, "models/b.out", "6")
            ig.on_pre("t6", "Bash", {"command": "true"}, "modeler", cwd=d)
            new = ig.on_post("t6")
            ok([x["file"] for x in new] == ["models/b.out"],
               f"PO8 dropping a step from pipeline.yaml does not unprotect its outputs: {new}")
            w(d, "models/extra.csv", "made before it was declared")
            ig.on_pre("t7", "Bash", {"command": "true"}, "modeler", cwd=d)
            ig.on_post("t7")
            spec(d, three[:1] + [{"id": "x", "run": "true", "inputs": [], "outputs": ["models/extra.csv"]}])
            ig.on_pre("t8", "Write", {"file_path": os.path.join(d, SPEC)}, "modeler", cwd=d)
            new = ig.on_post("t8")
            w(d, "models/extra.csv", "edited after")
            ig.on_pre("t9", "Bash", {"command": "true"}, "modeler", cwd=d)
            new2 = ig.on_post("t9")
            ok(new == [] and [x["file"] for x in new2] == ["models/extra.csv"]
               and [p["id"] for p in plan(d)][-1:] == ["x"],
               f"PO9 a file declared after it exists is baselined (and its step is stale); later edits are flagged: "
               f"{new} {new2}")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["validate", "run"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("step", nargs="?")
    ap.add_argument("--dry-run", action="store_true", help="print the steps that would rerun, and why")
    ap.add_argument("--scratch", action="store_true", help="run <step> in a temporary copy of the run dir")
    ap.add_argument("--budget-s", type=float)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.cmd or not a.run_dir:
        ap.error("usage: pipeline.py {validate,run} <run_dir> [step] [--dry-run | --scratch]")
    steps, errs = load(a.run_dir)
    if a.cmd == "validate" or errs:
        print("\n".join(errs) if errs else f"OK: {len(steps)} steps: " + " -> ".join(s["id"] for s in steps))
        return 1 if errs else 0
    if a.scratch:
        if not a.step:
            ap.error("--scratch needs a step id")
        rep = scratch(a.run_dir, a.step)
        print(json.dumps(rep, indent=1) if a.json else summary_line(rep) + f"\nscratch copy: {rep['scratch_dir']}")
        return 0 if rep["ok"] else 1
    if a.step:
        ap.error("running a single step writes only to a scratch copy: add --scratch")
    if a.dry_run:
        p = plan(a.run_dir, steps)
        print(json.dumps(p, indent=1) if a.json else
              ("\n".join(f"{x['id']}: {x['why']}" for x in p) if p else "up to date: nothing to rerun"))
        return 0
    rep = run(a.run_dir, budget_s=a.budget_s)
    print(json.dumps(rep, indent=1) if a.json else summary_line(rep))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
