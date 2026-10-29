#!/usr/bin/env python3
"""S1: the run's declared build pipeline (`models/pipeline.yaml`), run by the harness.

    python3 scripts/pipeline.py validate <run_dir>
    python3 scripts/pipeline.py run <run_dir> --dry-run          # print the plan
    python3 scripts/pipeline.py run <run_dir> [--budget-s N]     # the harness run
    python3 scripts/pipeline.py run <run_dir> <step> --scratch [--keep]   # try one step in a copy
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
run directory, an output the harness or a critic already owns, or a `run`
command the agents' own Bash hook would refuse (agents/enforcement.py
`bash_reason`: the harness runs it with a shell, outside that hook).

Incremental: a step is stale when it never ran, last failed, its definition
changed, the hash of its inputs or code changed (code = every file named on
its command line plus the run-local modules a Python script imports,
recursively, except declared outputs of any step and the step's own declared
inputs), or its outputs are missing or differ from what it last wrote
(a hand edit). Everything downstream of a stale step is stale. Hashes are in
`models/pipeline_state.json`; each run writes `models/pipeline_report.json`.

A step runs with its old outputs moved aside. If it fails or is killed, what it
wrote is removed and its previous outputs are put back (not a file that differs
from the harness's record of the step, nor one with an open integrity
violation); it stays stale. With a budget, a
step whose last recorded runtime exceeds what is left is not started
(`skipped_budget`), and stays stale with its outputs. A recorded output no step
declares any more is deleted, unless a current step reads it as an input: then
it is only dropped from the records (and released by the integrity record).
Processes a step leaves in the background are killed when it ends.

Owned outputs: declared outputs are protected artifacts
(agents/integrity.py). Only the harness's run (`harness_build`, with the
run's ArtifactIntegrity) may write them; it marks the outputs of the steps it
ran as harness-written and nothing else. Agents try changes with `--scratch`:
the step runs in a temporary copy of the run directory, and nothing in the
run directory changes; the copy is removed afterwards unless `--keep`.

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
        enf = _enforcement()
        # Also the signed decision brief and the single-owner files (planner / lead):
        # a harness pipeline run must not write them and mark them harness-written.
        outs = tuple(enf.SCRIPT_OUTPUTS) + tuple(enf.SIGNED_FILES) + tuple(enf.OWNED_BY)
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


def path_match(rel: str, pat: str) -> bool:
    """One matcher for scanning, ownership, grants and retirement: at least everything
    `glob(recursive=True)` would match (fnmatch's `*` also crosses `/`; `**/` may also
    match nothing, as in glob), so ownership is never narrower than the scan (Codex)."""
    return fnmatch.fnmatchcase(rel, pat) or ("**/" in pat and fnmatch.fnmatchcase(rel, pat.replace("**/", "")))


def _literal_ends(pat: str) -> tuple[str, str]:
    """The literal text before the first wildcard and after the last one. A pattern with
    a character class has no trusted literal suffix (classes can hold `*`, `?`, `]`)."""
    m = list(_GLOB.finditer(pat))
    if not m:
        return pat, pat
    tail = "" if ("[" in pat or "]" in pat) else pat[m[-1].start() + 1:]
    return pat[:m[0].start()], tail


def _overlap(a: str, b: str, run_dir: str | None = None) -> bool:
    """Two declared paths/globs CAN name the same file. Conservative (sound): they are
    disjoint only when their literal prefixes or literal suffixes cannot agree, so
    `critique_a*.y*` overlaps `critique_*.yaml` (Codex review)."""
    if a == b or path_match(a, b) or path_match(b, a):
        return True
    if run_dir and set(_matches(run_dir, a)) & set(_matches(run_dir, b)):
        return True
    if not (_GLOB.search(a) or _GLOB.search(b)):
        return False               # two different literal paths
    (pa, sa), (pb, sb) = _literal_ends(a), _literal_ends(b)
    if not (pa.startswith(pb) or pb.startswith(pa)):
        return False
    if not (sa.endswith(sb) or sb.endswith(sa)):
        return False
    return True


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
    if os.path.normpath(p) != p or "\\" in p:
        # Canonical paths only: ownership checks compare declared text (Codex: `././report.md`).
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
            if isinstance(st.get("run"), str) and st["run"].strip():
                e = _bash_refusal(st["run"], run_dir)
                if e:
                    errs.append(f"{where}: `run` {e}")
        for x in st["inputs"] + st["outputs"] + [st[k] for k in ("inputs_jsonl", "out") if isinstance(st.get(k), str)]:
            e = _check_path(x, run_dir, where)
            if e:
                errs.append(e)
        for o in step_outputs(st):
            if not isinstance(o, str):
                continue
            bad = [r for r in reserved if _overlap(o, r)] + \
                  [m for m in _matches(run_dir, o) if any(path_match(m, r) for r in reserved)]
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
    all_outs = [o for s in steps for o in step_outputs(s)]
    out = []
    for sid in order:
        s = dict(by_id[sid])
        s["_deps"] = sorted(deps[sid])
        s["_all_outputs"] = all_outs      # never hashed as code (review #1)
        out.append(s)
    return out, []


def _bash_refusal(cmd: str, run_dir: str) -> str | None:
    """Why the agents' own Bash hook would refuse this step command, or None.

    The harness runs `run:` with a shell, outside the PreToolUse hook, so the same
    rules (agents/enforcement.py `bash_reason`, as for the modeler, who writes the
    pipeline) are applied here at validation time. Fails closed."""
    try:
        reason = _enforcement().bash_reason(cmd, run_dir, "modeler", cwd=run_dir)
    except Exception as e:  # noqa: BLE001 — fail closed
        return f"could not be checked against the agents' Bash rules ({type(e).__name__}: {e})"
    return f"would be refused as an agent's Bash command: {reason}" if reason else None


class PipelineUnreadable(ValueError):
    """pipeline.yaml exists but cannot be read or parsed (callers must fail closed)."""


def _lenient_steps(run_dir: str) -> list[dict]:
    """The normalized step mappings of pipeline.yaml, without validation. No file: [].
    A file that cannot be read or parsed raises PipelineUnreadable: an unreadable
    pipeline is not a pipeline that declares nothing (review #5)."""
    import yaml
    p = os.path.join(run_dir, SPEC)
    if not os.path.lexists(p):
        return []
    try:
        with open(p) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise PipelineUnreadable(f"{SPEC}: {type(e).__name__}: {str(e)[:200]}") from e
    if doc is not None and not isinstance(doc, dict):
        raise PipelineUnreadable(f"{SPEC} is not a mapping")
    raw = doc.get("steps") if isinstance(doc, dict) else None
    out = []
    for st in raw if isinstance(raw, list) else []:
        if isinstance(st, dict):
            st = _normalize(st)
            for k in ("inputs", "outputs"):
                if not isinstance(st.get(k), list):
                    st[k] = []
            out.append(st)
    return out


def declared_inputs(run_dir: str) -> list[str]:
    """Input paths/globs pipeline.yaml declares (lenient, as declared_outputs)."""
    pats = []
    for st in _lenient_steps(run_dir):
        for i in step_inputs(st):
            if isinstance(i, str) and i.strip() and i not in pats:
                pats.append(i)
    return pats


def declared_outputs(run_dir: str) -> list[str]:
    """Output paths/globs pipeline.yaml declares, for the integrity record. Lenient on
    purpose (fail closed): a pipeline that does not validate still protects every
    output it names inside the run dir, except files another owner already holds.
    Raises PipelineUnreadable when the file exists but does not parse."""
    reserved = _reserved_patterns()
    pats = []
    for st in _lenient_steps(run_dir):
        for o in step_outputs(st):
            if isinstance(o, str) and o.strip() and not os.path.isabs(o):
                o = os.path.normpath(o).replace(os.sep, "/")   # protect what the path names
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
        elif isinstance(node, ast.ImportFrom) and node.level:
            # `from ..common import K`: resolved against the importing file's package (Codex).
            base = os.path.dirname(py)
            for _ in range(node.level - 1):
                base = os.path.dirname(base)
            rel_names = ([node.module] + [f"{node.module}.{a.name}" for a in node.names]) if node.module \
                else [a.name for a in node.names]
            for n in rel_names:
                for cand in _module_files(n, [base], run_dir):
                    _local_imports(cand, run_dir, seen)
            init = os.path.join(base, "__init__.py")
            if os.path.isfile(init) and _inside(init, run_dir):
                _local_imports(os.path.realpath(init), run_dir, seen)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{a.name}" for a in node.names)
    roots = [os.path.dirname(py), os.path.join(run_dir, "models"), run_dir]
    for name in names:
        for cand in _module_files(name, roots, run_dir):
            _local_imports(cand, run_dir, seen)


def _module_files(name: str, roots: list[str], run_dir: str) -> list[str]:
    """Run-local files importing `a.b.c` executes: the module (or package __init__ /
    __main__) and every package __init__.py along the way (Codex review)."""
    out = []
    parts = name.split(".")
    for root in roots:
        for k in range(1, len(parts) + 1):
            init = os.path.join(root, *parts[:k], "__init__.py")
            if os.path.isfile(init) and _inside(init, run_dir):
                out.append(os.path.realpath(init))
        rel = os.path.join(*parts)
        for cand in (os.path.join(root, rel + ".py"), os.path.join(root, rel, "__main__.py")):
            if os.path.isfile(cand) and _inside(cand, run_dir):
                out.append(os.path.realpath(cand))
    return out


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
    for j, t in enumerate(toks):
        if t == "-m" and j + 1 < len(toks):
            # `python -m models.worker`: the module (run from the run dir) and its imports.
            for cand in _module_files(toks[j + 1], [run_dir], run_dir):
                named.add(cand)
                _local_imports(cand, run_dir, py)
            continue
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
    # Review #1: a declared output (of any step) is data the run writes, and the step's
    # own declared inputs are hashed as inputs: neither is code. Otherwise `> out.csv` or
    # `--out out.csv` changed the step's code key while it ran. Imports are still followed.
    outs = list(step.get("_all_outputs") or step_outputs(step))
    data = outs + step_inputs(step)
    return sorted(p for p in named | py
                  if not (_inside(p, run_dir) and any(path_match(_rel(p, run_dir), pat) for pat in data)))


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


def plan(run_dir: str, steps: list[dict] | None = None, state: dict | None = None) -> list[dict]:
    """Steps to rerun, in run order: [{id, why}]. Raises ValueError if the pipeline is invalid."""
    run_dir = os.path.realpath(run_dir)
    if steps is None:
        steps, errs = load(run_dir)
        if errs:
            raise ValueError("; ".join(errs))
    state = (state if state is not None else _load_state(run_dir))["steps"]
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
        # Review #4: a process the step left in the background (`cmd &`) would keep
        # writing declared outputs after the step is recorded and its grant ends.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
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


def _recover_aside(run_dir: str, dry: bool = False) -> list[str]:
    """A harness process killed mid-step leaves the step's set-aside outputs in
    `.pipeline_aside_*/<rel>`. Its state record was not updated, so putting back the
    files whose place is empty returns the run to where it was before that step (only
    files that were not suspect are ever set aside). `dry`: just list them."""
    back = []
    for d in glob.glob(os.path.join(run_dir, ".pipeline_aside_*")):
        for dp, _dirs, fns in os.walk(d):
            for fn in fns:
                rel = os.path.relpath(os.path.join(dp, fn), d)
                dst = os.path.join(run_dir, rel)
                if os.path.lexists(dst) or not _inside(os.path.dirname(dst), run_dir):
                    continue
                if not dry:
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        os.replace(os.path.join(dp, fn), dst)
                    except OSError:
                        continue
                back.append(rel.replace(os.sep, "/"))
        if not dry:
            shutil.rmtree(d, ignore_errors=True)
    return back


def _trusted_outputs(rec) -> dict:
    """Output hashes the harness last recorded for a step (kept across a failed run)."""
    out = rec.get("outputs") if isinstance(rec, dict) else None
    return out if isinstance(out, dict) else {}


def _run_one(step: dict, run_dir: str, timeout_s: float, python: str | None, rec: dict | None = None,
             suspect=None) -> dict:
    """Move the step's existing outputs aside, run it, and check it wrote every declared output.

    The step runs with its outputs absent (like `make clean` for this step), so only files
    this execution creates count as written: a `touch` cannot pass an old or hand-edited
    file off as current, and a file a glob output no longer produces does not linger
    (Codex review). The one exception is a compute step's results file whose bytes are
    what the harness last recorded: the compute service reuses its rows by code
    fingerprint (it stays in place; a copy is kept aside).

    Review #2: if the step fails or is killed, what it wrote is removed and its previous
    outputs are put back, so a failed rebuild never leaves the run with less than it had.
    Not restored: when the harness has a record of the step's outputs, a file whose bytes
    do not match it (a hand edit, or a leftover); and a file `suspect(rel)` names (an open
    integrity violation), so a restore can never pass an edit off as harness-written.
    Without a record (the first harness build of files the agents made) the files are
    restored as they were. The step stays stale either way."""
    fp = fingerprint(step, run_dir)
    row = {"returncode": None, "seconds": 0.0, "ok": False, "written": [], "removed": [], "restored": [],
           "fingerprint": fp}
    if fp["missing_inputs"]:
        row["error"] = f"inputs missing: {', '.join(fp['missing_inputs'])}"
        return row
    trusted = _trusted_outputs(rec)
    keep = set()
    if _compute_fn(step):
        for rel in (step["out"], step["out"] + ".summary.json"):
            if rel in trusted and _sha(os.path.join(run_dir, rel)) == trusted[rel]:
                keep.add(rel)
    before = _snapshot(step, run_dir)
    try:
        aside = tempfile.mkdtemp(prefix=".pipeline_aside_", dir=run_dir)
    except OSError as e:
        row["error"] = f"could not set old outputs aside: {e}"
        return row
    good: dict[str, str] = {}      # rel -> its copy aside (the files to restore on failure)

    def restore() -> list[str]:
        back = []
        for rel in sorted(_snapshot(step, run_dir)):
            try:
                os.remove(os.path.join(run_dir, rel))          # what the failed run left
            except OSError:
                pass
        for rel, src in sorted(good.items()):
            dst = os.path.join(run_dir, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(src, dst)
                back.append(rel)
            except OSError:
                pass
        return back

    try:
        for rel in sorted(before):
            src = os.path.join(run_dir, rel)
            kept = os.path.join(aside, rel)
            is_good = (_sha(src) == trusted.get(rel) if trusted else True) \
                and not (suspect is not None and suspect(rel))
            try:
                if rel in keep or is_good:
                    os.makedirs(os.path.dirname(kept), exist_ok=True)
                if rel in keep:
                    shutil.copy2(src, kept)
                    good[rel] = kept
                    continue
                if is_good:
                    os.replace(src, kept)
                    good[rel] = kept
                else:
                    os.remove(src)
                row["removed"].append(rel)
            except OSError as e:
                row["error"] = f"could not clear old output {rel}: {e}"
                row["restored"] = restore()
                return row
        s0 = time.time()
        try:
            rc, err = _exec_step(step, run_dir, timeout_s, python)
        except OSError as e:
            rc, err = None, f"{type(e).__name__}: {e}"
        row.update(returncode=rc, seconds=round(time.time() - s0, 1), ok=rc == 0)
        after = _snapshot(step, run_dir)
        row["written"] = sorted(r for r, m in after.items() if r not in keep or before.get(r) != m)
        row["removed"] = sorted(set(row["removed"]) - set(after))
        if rc != 0:
            row["error"] = err
        else:
            problems = [f"{pat}: not produced" for pat in step_outputs(step)
                        if not any(path_match(r, pat) for r in row["written"])]
            if fingerprint(step, run_dir)["key"] != fp["key"]:
                # Its inputs or code changed while it ran: the outputs may hold the old values (Codex).
                problems.append("inputs or code changed while the step ran; rerun")
            if problems:
                row["ok"] = False
                row["error"] = "declared outputs: " + "; ".join(problems)
        if not row["ok"]:
            row["restored"] = restore()
            # What changed on disk this run: the files it wrote and the ones it removed,
            # except the restored files, which hold their previous bytes again.
            now = set(_snapshot(step, run_dir))
            row["removed"] = sorted((set(before) | set(row["written"])) - now)
            row["written"] = sorted((set(row["written"]) & now) - set(row["restored"]))
        return row
    finally:
        shutil.rmtree(aside, ignore_errors=True)


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
        _finish_granted(run_dir, report, t0, integrity)
        return report
    state = _load_state(run_dir)
    if integrity is not None:
        # The state file is protected; if an agent changed it, its records are not the
        # harness's and are dropped, so every step is rebuilt (Codex review: a forged
        # record would otherwise survive an unrelated rebuild that rewrites the file).
        integrity.check_now()
        if integrity.is_open(STATE.replace(os.sep, "/")):
            state = {"steps": {}}
            report["state_distrusted"] = True
    if only is not None:
        if only not in {s["id"] for s in steps}:
            report.update(errors=[f"no step {only!r}"], ok=False)
            _finish_granted(run_dir, report, t0, integrity)
            return report
        report["plan"] = [{"id": only, "why": "requested"}] if force else \
            [p for p in plan(run_dir, steps, state) if p["id"] == only]
    else:
        report["plan"] = plan(run_dir, steps, state)
    todo = {p["id"] for p in report["plan"]}
    by_id = {s["id"]: s for s in steps}
    # Files a step wrote earlier that no current declaration names any more (an output
    # renamed, a step removed): removed, so nothing reads a retired result as current.
    declared = [o for s in steps for o in step_outputs(s)]
    reserved = _reserved_patterns()
    gone = sorted({rel for rec in state["steps"].values() if isinstance(rec, dict)
                   for rel in (rec.get("outputs") or {})
                   if isinstance(rel, str) and not any(path_match(rel, o) for o in declared)
                   and _check_path(rel, run_dir, "") is None
                   and not any(path_match(rel, r) for r in reserved)})
    # Review #3: one a current step reads is kept; it only leaves the pipeline's records
    # (and, in agents/integrity.py, its ownership: no step claims to produce it now).
    reads = [i for s in steps for i in step_inputs(s)]
    released = [rel for rel in gone if any(path_match(rel, i) for i in reads)]
    retired = [rel for rel in gone if rel not in released]
    recover = _recover_aside(run_dir, dry=True)
    grant = [o for s in steps if s["id"] in todo for o in step_outputs(s)] + list(OWN_FILES) + retired + recover
    ctx = integrity.harness_pipeline(grant) if integrity is not None else nullcontext()
    with ctx:
        failed: set[str] = set()
        produced: list[str] = []
        recovered = _recover_aside(run_dir)
        if recovered:
            report["recovered"] = recovered     # not marked harness-written: previous bytes
        for rel in retired:
            try:
                if os.path.isfile(os.path.join(run_dir, rel)):
                    os.remove(os.path.join(run_dir, rel))
                    produced.append(rel)
            except OSError:
                pass
        report["retired"] = [r for r in retired if r in produced]
        if released:
            report["released"] = released
        state["steps"] = {k: v for k, v in state["steps"].items() if k in by_id}
        for rec in state["steps"].values():
            if isinstance(rec, dict) and isinstance(rec.get("outputs"), dict):
                rec["outputs"] = {k: v for k, v in rec["outputs"].items() if k not in gone}
        for s in steps:
            if s["id"] not in todo:
                continue
            row = {"id": s["id"], "why": next(p["why"] for p in report["plan"] if p["id"] == s["id"])}
            up = [d for d in s["_deps"] if d in failed]
            left = None if budget_s is None else budget_s - (time.time() - t0)
            rec = state["steps"].get(s["id"])
            last = rec.get("seconds") if isinstance(rec, dict) else None
            if up:
                row.update(status="skipped", error=f"upstream step not rebuilt: {', '.join(up)}")
                failed.add(s["id"])
            elif left is not None and left < 5:
                row.update(status="not_started", error="pipeline time budget used")
                failed.add(s["id"])
            elif left is not None and isinstance(last, (int, float)) and last > left:
                # Review #2: it would be killed partway; leave it stale with its outputs.
                row.update(status="skipped_budget",
                           error=f"last run took {last:.0f}s; {left:.0f}s of the pipeline budget left")
                failed.add(s["id"])
            else:
                tmo = float(s.get("timeout_s") or DEFAULT_TIMEOUT_S)
                res = _run_one(s, run_dir, tmo if left is None else min(tmo, left), python, rec,
                               suspect=integrity.is_open if integrity is not None else None)
                fp = res.pop("fingerprint", None) or fingerprint(s, run_dir)
                row.update(res, status="ok" if res["ok"] else "failed")
                produced += res["written"] + res["removed"]
                at = time.strftime("%Y-%m-%dT%H:%M:%S")
                if res["ok"]:
                    state["steps"][s["id"]] = {"ok": True, "key": fp["key"], "inputs": fp["inputs"],
                                               "code": fp["code"], "outputs": output_hashes(s, run_dir),
                                               "seconds": res["seconds"], "at": at}
                else:
                    failed.add(s["id"])
                    # The restored files keep their recorded hashes (a later hand edit still
                    # shows), and the runtime is kept for the budget check.
                    prev = _trusted_outputs(rec)
                    state["steps"][s["id"]] = {"ok": False, "at": at,
                                               "seconds": max(res["seconds"], last or 0.0),
                                               "outputs": {r: prev[r] for r in res["restored"] if r in prev}}
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
        reserved = [r for r in _reserved_patterns() if r not in OWN_FILES]
        for rel in sorted(set(produced)):
            if any(path_match(rel, r) for r in reserved):
                continue          # never accept a file another owner holds, whatever wrote it
            integrity.harness_wrote(rel)


def _finish_granted(run_dir, report, t0, integrity):
    """_finish for a run that stops early: its report is still written inside the
    harness grant (the runner's own records), as on the normal path."""
    ctx = integrity.harness_pipeline(list(OWN_FILES)) if integrity is not None else nullcontext()
    with ctx:
        _finish(run_dir, report, t0, integrity, [])


def harness_build(run_dir: str, integrity, budget_s: float | None = None) -> dict:
    """HOOK POINT (S1): the harness's pipeline run. Call it with the run's
    ArtifactIntegrity wherever the harness rebuilds (after a modeler/analyst
    stage, before a gate). Requires the integrity record: without it the
    outputs would be flagged as unexplained changes."""
    if integrity is None:
        raise ValueError("harness_build needs the run's ArtifactIntegrity")
    return run(run_dir, budget_s=budget_s, integrity=integrity)


def _stat_tree(d: str) -> dict:
    """(size, mtime) of every file reachable from the run dir, through symlinks too."""
    out, seen = {}, set()
    for dp, dirs, fns in os.walk(d, followlinks=True):
        real = os.path.realpath(dp)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for fn in fns:
            p = os.path.join(dp, fn)
            try:
                st = os.stat(p)
                out[os.path.relpath(p, d)] = (st.st_size, st.st_mtime_ns)
            except OSError:
                pass
    return out


def scratch(run_dir: str, step_id: str, python: str | None = None, keep: bool = False) -> dict:
    """Run one step in a temporary copy of the run dir. Nothing in the run dir changes.
    The copy is removed afterwards unless `keep` (review #6: copies piled up).
    `scratch_dir` names it either way; `scratch_kept` says whether it is still there."""
    run_dir = os.path.realpath(run_dir)
    before = _stat_tree(run_dir)
    # The copy is a hidden sibling of the run dir, so a run script that reaches the repo by
    # a path relative to its run (E7 attempt 7: `<run>/../../scripts` for figure_validator)
    # resolves it from the copy as from the run. Falls back to the system temp dir.
    try:
        dst = os.path.realpath(tempfile.mkdtemp(prefix=f".{os.path.basename(run_dir)}.scratch_",
                                                dir=os.path.dirname(run_dir)))
    except OSError:
        dst = os.path.realpath(tempfile.mkdtemp(prefix="pipeline_scratch_"))
    copy = dst
    shutil.copytree(run_dir, copy, symlinks=True, dirs_exist_ok=True)
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
    # Commands naming the real run dir by absolute path run the copy's files instead.
    # The copy's manifest is materialised as its own file first, so editing it can never
    # write through a symlink (Codex review).
    sp = os.path.join(copy, SPEC)
    try:
        txt = open(sp).read()
        if os.path.islink(os.path.join(copy, "models")):
            real_models = os.path.realpath(os.path.join(copy, "models"))
            os.remove(os.path.join(copy, "models"))
            shutil.copytree(real_models, os.path.join(copy, "models"), symlinks=True)
        if os.path.lexists(sp):
            os.remove(sp)
        with open(sp, "w") as f:
            f.write(txt.replace(run_dir, copy))
    except OSError as e:
        if not keep:
            shutil.rmtree(dst, ignore_errors=True)
        return {"ok": False, "errors": [f"scratch copy: could not prepare {SPEC}: {e}"], "steps": [],
                "plan": [], "scratch_dir": copy, "scratch_kept": keep}
    rep = run(copy, python=python, only=step_id, force=True)
    rep["scratch_dir"] = copy
    # No portable write sandbox: a step that still writes the real run dir (an absolute
    # path in its code) is reported and fails; the integrity record flags owned files.
    after = _stat_tree(run_dir)
    live = {"trace.jsonl", "artifact_integrity.json", "budget_ledger.json", "harness_manifest.json"}
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k)
                     and k not in live and not k.endswith(".log"))   # written by the live harness meanwhile
    if changed:
        rep["ok"] = False
        rep["real_run_changed"] = changed
        rep.setdefault("errors", []).append(
            f"the scratch run changed the real run directory: {', '.join(changed[:10])}")
    if not keep:
        shutil.rmtree(dst, ignore_errors=True)
    rep["scratch_kept"] = keep
    return rep


def gate_findings(run_dir: str) -> list[dict]:
    """STAGE 7: a run that declares a pipeline must have it built. Invalid, or any step
    stale (never built, failed, skipped, out of budget, or changed since), is a HIGH,
    not scope-declarable: the gate would otherwise grade outputs older than their
    inputs (Codex review)."""
    run_dir = os.path.realpath(run_dir)
    if not os.path.exists(os.path.join(run_dir, SPEC)):
        return []
    steps, errs = load(run_dir)
    base = {"severity": "HIGH", "stage": "MODEL", "target_stage": "MODEL", "scope_declarable": False}
    if errs:
        return [{**base, "kind": "pipeline_invalid", "claim": f"{SPEC} does not validate: " + "; ".join(errs[:5])}]
    try:
        stale = plan(run_dir, steps)
    except Exception as e:  # noqa: BLE001 — fail closed
        return [{**base, "kind": "pipeline_unreadable", "claim": f"{SPEC}: could not plan: {type(e).__name__}: {e}"}]
    if not stale:
        return []
    return [{**base, "kind": "pipeline_outputs_stale",
             "claim": f"{len(stale)} step(s) of {SPEC} are not built from current inputs: "
                      + "; ".join(f"{p['id']} ({p['why']})" for p in stale[:8])
                      + ". The harness rebuilds them; fix the failing step (try it with "
                        "`scripts/pipeline.py run <run_dir> <step> --scratch`)."}]


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

    def rd(d, rel):
        try:
            return open(os.path.join(d, rel)).read()
        except OSError:
            return None

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
        ok(rep["steps"][0]["status"] == "failed" and "not produced" in rep["steps"][0]["error"],
           f"PL13 a step that leaves its declared output unwritten fails: {rep['steps']}")
        w(d, "models/b.py", "open('models/b.out', 'w').write('7')\n")
        run(d)
        # (Codex) `touch` cannot pass a hand-edited output off as freshly built.
        w(d, "models/b.out", "hand")
        w(d, "models/b.py", "import pathlib\npathlib.Path('models/b.out').touch()\n")
        rep = run(d)
        ok(open(os.path.join(d, "models/b.out")).read() == "",
           f"PL13b (Codex) old outputs are cleared before a step runs, so touch does not launder them: {rep['steps']}")
        w(d, "models/b.py", "open('models/b.out', 'w').write('7')\n")
        run(d)
        # (Codex) A renamed output: the retired file is removed.
        spec(d, [three[0], dict(three[1], outputs=["models/b2.out"]), dict(three[2], inputs=["models/b2.out"])])
        w(d, "models/b.py", "open('models/b2.out', 'w').write('7')\n")
        rep = run(d)
        ok(not os.path.exists(os.path.join(d, "models/b.out")) and rep.get("retired") == ["models/b.out"]
           and [r["status"] for r in rep["steps"]] == ["ok", "failed"],
           f"PL13c (Codex) an output no declaration names any more is removed (its old reader fails loudly): "
           f"{rep.get('retired')} {rep['steps']}")
        spec(d, three)
        w(d, "models/b.py", "open('models/b.out', 'w').write('7')\n")
        run(d)
        # (Codex) `python -m` entry points and package __init__ files are code.
        w(d, "models/pkg/__init__.py", "K = 1\n")
        w(d, "models/pkg/mod.py", "V = 1\n")
        w(d, "models/mrun.py", "import models.pkg.mod\nopen('models/m.out', 'w').write('m')\n")
        spec(d, three + [{"id": "m", "run": f"{py} -m models.mrun", "inputs": [], "outputs": ["models/m.out"]}])
        run(d)
        w(d, "models/mrun.py", "import models.pkg.mod\nopen('models/m.out', 'w').write('m2')\n")
        p1 = [p["id"] for p in plan(d)]
        run(d)
        w(d, "models/pkg/__init__.py", "K = 2\n")
        p2 = [p["id"] for p in plan(d)]
        ok(p1 == ["m"] and p2 == ["m"], f"PL13d (Codex) -m modules and package __init__ files are hashed: {p1} {p2}")
        spec(d, three)
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
        rep = scratch(d, "b", keep=True)
        ok(open(os.path.join(rep["scratch_dir"], "models/b.out")).read() == "scratch"
           and open(os.path.join(d, "models/b.out")).read() != "scratch",
           "PL18 scratch outputs land in the copy only")
        shutil.rmtree(rep["scratch_dir"], ignore_errors=True)
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
        # (Codex) A command naming the real run dir by absolute path runs the copy's file.
        w(d, "models/b.py", "import os\nopen(os.path.join(os.path.dirname(__file__), 'b.out'), 'w').write('abs')\n")
        spec(d, [three[0], dict(three[1], run=f"{py} {d}/models/b.py"), three[2]])
        before = _tree_hash(d)
        rep = scratch(d, "b", keep=False)
        ok(rep["ok"] and _tree_hash(d) == before, f"PL21 (Codex) scratch rebases absolute run-dir paths: {rep}")
        w(d, "models/b.py", f"open({os.path.join(d, 'models', 'b.out')!r}, 'w').write('abs')\n")
        rep = scratch(d, "b", keep=False)
        ok(not rep["ok"] and "models/b.out" in rep.get("real_run_changed", []),
           f"PL22 a scratch step that still writes the real run dir is reported and fails: {rep.get('errors')}")
        # (Codex r2) A manifest that is a symlink back into the run dir is not edited through.
        w(d, "models/b.py", "open('models/b.out', 'w').write('7')\n")
        spec(d, [three[0], dict(three[1], run=f"{py} {d}/models/b.py"), three[2]])
        os.replace(os.path.join(d, SPEC), os.path.join(d, "pipeline_source.yaml"))
        os.symlink(os.path.join(d, "pipeline_source.yaml"), os.path.join(d, SPEC))
        before = _tree_hash(d)
        rep = scratch(d, "b", keep=False)
        ok(_tree_hash(d) == before and rep["ok"], f"PL25 (Codex r2) scratch never writes through a symlinked manifest: {rep}")
        os.remove(os.path.join(d, SPEC))
        os.replace(os.path.join(d, "pipeline_source.yaml"), os.path.join(d, SPEC))

    # Real-check (E7 attempt 7): run code reaches the repo relative to the run dir
    # (`<run>/../../scripts/figure_validator.py`); a scratch copy must resolve it too.
    with tempfile.TemporaryDirectory() as top:
        top = os.path.realpath(top)
        d = os.path.join(top, "runs", "r1")
        w(top, "scripts/tool.py", "K = 5\n")
        w(d, "models/a.py", "import os, sys\nsys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname("
                            "os.path.dirname(os.path.abspath(__file__)))), '..', 'scripts'))\n"
                            "import tool\nopen('models/a.out', 'w').write(str(tool.K))\n")
        spec(d, [{"id": "a", "run": f"{py} models/a.py", "inputs": [], "outputs": ["models/a.out"]}])
        ok(run(d)["ok"], "PL26a fixture: the step runs in the run dir")
        before = _tree_hash(d)
        rep = scratch(d, "a", keep=False)
        ok(rep["ok"] and _tree_hash(d) == before and not os.path.exists(rep["scratch_dir"])
           and os.listdir(os.path.join(top, "runs")) == ["r1"],
           f"PL26 a scratch copy resolves paths relative to the run's place (and is removed): {rep.get('steps')}")

    # Review fixes (PR "Pipeline runner: review fixes").
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        # (review #1) A step's own outputs named on its command line are not code.
        w(d, "data/x.csv", "1\n")
        w(d, "models/o.py", "print(open('data/x.csv').read().strip())\n")
        w(d, "models/wr.py", "import sys\nopen(sys.argv[sys.argv.index('--out') + 1], 'w')"
                             ".write(open('models/o.out').read())\n")
        spec(d, [{"id": "o", "run": f"{py} models/o.py > models/o.out", "inputs": ["data/x.csv"],
                  "outputs": ["models/o.out"]},
                 {"id": "wr", "run": f"{py} models/wr.py --out models/alloc.csv", "inputs": ["models/o.out"],
                  "outputs": ["models/alloc.csv"]}])
        r1 = run(d)
        w(d, "data/x.csv", "2\n")
        r2 = run(d)
        p2 = plan(d)
        alloc = rd(d, "models/alloc.csv")
        w(d, "models/o.py", "print(3)\n")
        p3 = [p["id"] for p in plan(d)]
        ok(r1["ok"] and r2["ok"] and ids(r2) == ["o", "wr"] and p2 == [] and alloc == "2\n" and p3 == ["o", "wr"],
           f"PR1 (review #1) outputs named on a command line are not hashed as code; scripts still are: "
           f"{r1['steps']} {r2['steps']} {p3}")
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        fixture(d)
        run(d)
        good = rd(d, "models/b.out")
        # (review #2) A step whose last runtime exceeds the remaining budget is not started.
        stt = json.load(open(os.path.join(d, STATE)))
        stt["steps"]["b"]["seconds"] = 999.0
        w(d, STATE, json.dumps(stt))
        w(d, "data/y.csv", "20\n")
        rep = run(d, budget_s=60)
        st = {r["id"]: r.get("status") for r in rep["steps"]}
        ok(st == {"b": "skipped_budget", "c": "skipped"} and not rep["ok"]
           and rd(d, "models/b.out") == good and [p["id"] for p in plan(d)] == ["b", "c"],
           f"PR2 (review #2) a step that does not fit the budget is not started; its outputs stay: {rep['steps']}")
        # (review #2) A failed or killed step's last good outputs are restored; it stays stale.
        w(d, "models/b.py", "open('models/b.out', 'w').write('partial')\nraise SystemExit('boom')\n")
        rep = run(d)
        ok(rep["steps"][0]["status"] == "failed" and rd(d, "models/b.out") == good
           and [p["id"] for p in plan(d)] == ["b", "c"],
           f"PR3 (review #2) a failed step's previous outputs are restored and it stays stale: {rep['steps']}")
        w(d, "models/b.py", "import time\nopen('models/b.out', 'w').write('partial')\ntime.sleep(30)\n")
        spec(d, [three[0], dict(three[1], timeout_s=1), three[2]])
        rep = run(d)
        ok(rep["steps"][0]["returncode"] is None and rd(d, "models/b.out") == good,
           f"PR4 (review #2) a killed step's previous outputs are restored: {rep['steps']}")
        # Anti-laundering: a hand edit is not restored as a good output.
        w(d, "models/b.out", "hand")
        w(d, "models/b.py", "raise SystemExit(1)\n")
        rep = run(d)
        ok(not os.path.exists(os.path.join(d, "models/b.out")) and "b" in [p["id"] for p in plan(d)],
           f"PR5 (review #2) a hand-edited output is not restored after a failed run: {rep['steps']}")
        # Without a record (a first harness build of files the agents made), a failed
        # step's existing outputs are restored as they were.
        os.remove(os.path.join(d, STATE))
        w(d, "models/b.out", "agent-made")
        rep = run(d)
        ok(rep["steps"][1]["status"] == "failed" and rd(d, "models/b.out") == "agent-made"
           and rep["steps"][1].get("restored") == ["models/b.out"] and "b" in [p["id"] for p in plan(d)],
           f"PR5c (review #2, real-check) a failed first build restores the outputs it found: {rep['steps']}")
        # A harness killed mid-step left the set-aside outputs: the next run puts them back.
        spec(d, three)
        w(d, "models/b.py", "open('models/b.out', 'w').write('7')\n")
        ok(run(d)["ok"], "PR5d fixture: rebuild")
        good = rd(d, "models/b.out")
        os.makedirs(os.path.join(d, ".pipeline_aside_crash", "models"))
        os.replace(os.path.join(d, "models/b.out"), os.path.join(d, ".pipeline_aside_crash", "models", "b.out"))
        w(d, "models/b.py", "raise SystemExit(1)\n")
        rep = run(d)
        ok(rep.get("recovered") == ["models/b.out"] and rd(d, "models/b.out") == good
           and not os.path.exists(os.path.join(d, ".pipeline_aside_crash")),
           f"PR5e (review #2) outputs a crashed harness run left aside are put back: {rep}")
        w(d, "models/b.py", "open('models/b.out', 'w').write('7')\n")
        ok(run(d)["ok"], "PR5b fixture: rebuild")
        # (review #3) A retired output a current step reads is kept, not deleted.
        spec(d, three[1:])
        rep = run(d)
        ok(os.path.exists(os.path.join(d, "models/a.out")) and rep.get("released") == ["models/a.out"]
           and not rep.get("retired") and rep["ok"],
           f"PR6 (review #3) a retired output a current step declares as input is kept: {rep}")
        spec(d, three)
        run(d)
        # (review #4) A step command the agent's own Bash would be refused is invalid; background
        # processes a step leaves behind are killed when it ends.
        late = os.path.join(d, "models", "late.txt")
        spec(d, [{"id": "z", "inputs": [], "outputs": ["models/z.out"],
                  "run": f"({py} -c \"import time; time.sleep(2); open('models/late.txt', 'w').write('x')\") "
                         f"> /dev/null 2>&1 & echo 1 > models/z.out"}])
        rep = run(d)
        time.sleep(3)
        ok(rep["ok"] and not os.path.exists(late),
           f"PR7 (review #4) a process a step leaves in the background is killed when the step ends: {rep['steps']}")
        # (review #6) The CLI removes the scratch copy unless --keep.
        fixture(d)
        run(d)
        parent = os.path.dirname(d)
        sib = lambda: sorted(x for x in os.listdir(parent) if x.startswith(f".{os.path.basename(d)}.scratch_"))
        s0 = sib()
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", d, "b", "--scratch"],
                           capture_output=True, text=True)
        s1 = sib()
        r2 = subprocess.run([sys.executable, os.path.abspath(__file__), "run", d, "b", "--scratch", "--keep"],
                            capture_output=True, text=True)
        kept = [x for x in sib() if x not in s1]
        ok(r.returncode == 0 and s1 == s0 and r2.returncode == 0 and len(kept) == 1 and kept[0] in r2.stdout,
           f"PR8 (review #6) a scratch copy is removed unless --keep, and printed when kept: "
           f"{r.stdout}{r.stderr} {r2.stdout}{r2.stderr} {s0} {s1} {kept}")
        for x in kept:
            shutil.rmtree(os.path.join(parent, x), ignore_errors=True)
        # (review #5) An unparsable pipeline.yaml is not "no outputs".
        w(d, SPEC, "steps: [")
        try:
            declared_outputs(d)
            raised = False
        except ValueError:
            raised = True
        os.remove(os.path.join(d, SPEC))
        ok(raised and declared_outputs(d) == [],
           "PR9 (review #5) declared_outputs raises on an unparsable pipeline.yaml (and is [] without one)")

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
    e = [errs_of([dict(three[0], outputs=[o])]) for o in
         ("decision_brief.yaml", "success_criteria.yaml", "round_plan.yaml", "brief_amendments.yaml")]
    ok(all(any("owned elsewhere" in x for x in ee) for ee in e),
       f"PV10b (real-check) the signed brief and single-owner files are refused as outputs: {e}")
    e = errs_of([{"id": "lib", "run": "compute: models/lib.py::f", "inputs": [], "outputs": ["models/r.jsonl"]}])
    ok(any("inputs_jsonl" in x for x in e) and any("purpose" in x for x in e),
       f"PV11 a compute step needs inputs_jsonl, out and purpose: {e}")
    with tempfile.TemporaryDirectory() as d:
        w(d, SPEC, "steps:\n  - id: lib\n    run: compute: models/lib.py::f\n")
        ok(load(d)[1] and "does not parse" in load(d)[1][0], "PV12 unparseable YAML fails closed")
    e = errs_of([dict(three[0], outputs=["critique_a*.y*"])])
    ok(any("owned elsewhere" in x for x in e), f"PV13 (Codex) a glob that can match a critique file is refused: {e}")
    e = errs_of([dict(three[0], outputs=["././report.md"])])
    e2 = errs_of([dict(three[0], outputs=["./models/a.out"])])
    ok(any("normalized" in x for x in e) and any("normalized" in x for x in e2),
       f"PV14 (Codex) non-canonical paths are refused: {e} {e2}")
    e = errs_of([three[0], dict(three[1], outputs=["models/a*.o*"])])
    ok(any("same file" in x for x in e), f"PV15 glob outputs that can overlap are refused: {e}")
    ok(not _overlap("figures/*.png", "figures/*.csv") and not _overlap("figures/*.png", "report.md"),
       "PV16 provably disjoint globs do not overlap")
    e = errs_of([dict(three[0], outputs=["critique_[ab].y[a?l]ml"])])
    ok(any("owned elsewhere" in x for x in e), f"PV17 (Codex r2) a character-class glob over a critique file is refused: {e}")
    ok(path_match("models/a.csv", "models/**/*.csv") and path_match("models/x/a.csv", "models/**/*.csv"),
       "PV18 (Codex r2) `**/` matches zero or more directories, as glob does")
    bad_cmds = {"protected write": "echo x > critique_domain.yaml; echo 1 > models/a.out",
                "credentials": "cat .env > models/a.out",
                "harness chmod": "chmod +x scripts/pipeline.py; echo 1 > models/a.out"}
    e = {k: errs_of([dict(three[0], run=v)]) for k, v in bad_cmds.items()}
    e_ok = errs_of([dict(three[0], run=f"nohup {py} models/a.py > models/a.log 2>&1")])
    ok(all(any("Bash" in x and "refused" in x for x in v) for v in e.values()) and e_ok == [],
       f"PV19 (review #4) a step command the agents' Bash deny rules refuse is invalid; an allowed one loads: "
       f"{e} {e_ok}")

    # Gate findings, input changed mid-run, relative imports.
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        fixture(d)
        g0 = gate_findings(d)
        run(d)
        g1 = gate_findings(d)
        w(d, "data/x.csv", "3\n")
        g2 = gate_findings(d)
        ok([g["kind"] for g in g0] == ["pipeline_outputs_stale"] and g1 == []
           and [g["kind"] for g in g2] == ["pipeline_outputs_stale"] and g2[0]["severity"] == "HIGH"
           and not g2[0]["scope_declarable"],
           f"PG1 (Codex r2) the gate blocks on an unbuilt or stale pipeline: {g0} {g1} {g2}")
        w(d, SPEC, "steps: [")
        ok([g["kind"] for g in gate_findings(d)] == ["pipeline_invalid"], "PG2 an invalid pipeline is a gate finding")
        fixture(d)
        run(d)
        w(d, "models/a.py", "import helper\nv = open('data/x.csv').read()\nopen('data/x.csv', 'w').write('9\\n')\n"
                            "open('models/a.out', 'w').write(str(int(v) + helper.K))\n")
        rep = run(d)
        ok(rep["steps"][0]["status"] == "failed" and "changed while" in rep["steps"][0]["error"]
           and "a" in [p["id"] for p in plan(d)],
           f"PL23 (Codex r2) a step whose inputs change while it runs is not recorded as current: {rep['steps'][:1]}")
        w(d, "models/pkg/__init__.py", "")
        w(d, "models/pkg/common.py", "K = 1\n")
        w(d, "models/pkg/sub/__init__.py", "")
        w(d, "models/pkg/sub/run.py", "from ..common import K\nopen('models/r.out', 'w').write(str(K))\n")
        spec(d, [{"id": "r", "run": f"{py} -m models.pkg.sub.run", "inputs": [], "outputs": ["models/r.out"]}])
        run(d)
        w(d, "models/pkg/common.py", "K = 2\n")
        ok([p["id"] for p in plan(d)] == ["r"], f"PL24 (Codex r2) parent-relative imports are hashed: {plan(d)}")

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
            ok(rep["ok"] and not ig.open_events and rep.get("state_distrusted")
               and [r["id"] for r in rep["steps"]] == ["a", "b", "c"],
               f"PO6 the harness run distrusts an edited state file, rebuilds everything and resolves the "
               f"violations: {ig.open_events} {rep.get('state_distrusted')}")
            # (Codex) A forged state record (inputs changed, output not rebuilt) is not laundered
            # by an unrelated rebuild: the edit is seen and every step reruns.
            w(d, "data/y.csv", "30\n")
            stt = json.load(open(os.path.join(d, STATE)))
            stt["steps"]["b"]["key"] = fingerprint(next(s for s in load(d)[0] if s["id"] == "b"), d)["key"]
            stt["steps"]["b"]["inputs"]["data/y.csv"] = _sha(os.path.join(d, "data/y.csv"))
            w(d, STATE, json.dumps(stt))
            w(d, "data/x.csv", "2\n")
            rep = harness_build(d, ig)
            ok(rep.get("state_distrusted") and "b" in [r["id"] for r in rep["steps"]]
               and open(os.path.join(d, "models/b.out")).read() == "33",
               f"PO6b (Codex) a forged state record is dropped, not laundered: {rep['steps']}")
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
            spec(d, three[:1] + [{"id": "x", "run": "true", "inputs": [], "outputs": ["models/**/*.csv"]}])
            ig.on_pre("t10", "Write", {"file_path": os.path.join(d, SPEC)}, "modeler", cwd=d)
            ig.on_post("t10")
            ig.on_pre("t11", "Write", {"file_path": os.path.join(d, "models/rec.csv")}, "modeler", cwd=d)
            w(d, "models/rec.csv", "agent")
            new = ig.on_post("t11")
            ok([x["file"] for x in new] == ["models/rec.csv"],
               f"PO10 (Codex r2) a Write to a file a recursive glob output names is flagged: {new}")
        # (review #3) An output retired into another step's input leaves the pipeline's ownership.
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            fixture(d)
            ig = ArtifactIntegrity(d, process_check=lambda: set())
            ok(harness_build(d, ig)["ok"], "PO11a fixture: build")
            spec(d, three[1:])
            ig.on_pre("u1", "Write", {"file_path": os.path.join(d, SPEC)}, "modeler", cwd=d)
            ig.on_post("u1")
            rep = harness_build(d, ig)
            ig.on_pre("u2", "Write", {"file_path": os.path.join(d, "models/a.out")}, "modeler", cwd=d)
            w(d, "models/a.out", "4")
            new = ig.on_post("u2")
            ok(rep["ok"] and new == [] and not ig.open_events and [p["id"] for p in plan(d)] == ["b", "c"],
               f"PO11 (review #3) a retired output now read as an input is the agents' file again: "
               f"{rep.get('released')} {new} {ig.open_events}")
            spec(d, three)
            ig.on_pre("u3", "Write", {"file_path": os.path.join(d, SPEC)}, "modeler", cwd=d)
            new = ig.on_post("u3")
            w(d, "models/a.out", "5")
            ig.on_pre("u4", "Bash", {"command": "true"}, "modeler", cwd=d)
            new2 = ig.on_post("u4")
            ok(new == [] and [x["file"] for x in new2] == ["models/a.out"] and plan(d)[0]["id"] == "a",
               f"PO12 (review #3) declared an output again, it is baselined and protected: {new} {new2}")
        # (review #2) A failed rebuild never restores a file with an open violation (it would
        # pass the edit off as harness-written), even without a pipeline record.
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            fixture(d)
            ig = ArtifactIntegrity(d, process_check=lambda: set())
            ok(harness_build(d, ig)["ok"], "PO14a fixture: build")
            os.remove(os.path.join(d, STATE))
            ig.on_pre("x1", "Bash", {"command": "true"}, "modeler", cwd=d)
            w(d, "models/b.out", "hand")
            ig.on_post("x1")
            w(d, "models/b.py", "raise SystemExit(1)\n")
            ig.on_pre("x2", "Write", {"file_path": os.path.join(d, "models/b.py")}, "modeler", cwd=d)
            ig.on_post("x2")
            rep = harness_build(d, ig)
            ok(rd(d, "models/b.out") is None and any(e["file"] == "models/b.out" for e in ig.open_events),
               f"PO14 (review #2) a failed step does not restore an output with an open violation: "
               f"{[r.get('restored') for r in rep['steps']]} {ig.open_events}")
        # (review) An invalid pipeline's report is written inside the harness grant.

        class _Probe(ArtifactIntegrity):
            def harness_wrote(self, rel):
                self._check(where="probe")      # a hook's check landing between write and accept
                super().harness_wrote(rel)

        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(d)
            fixture(d)
            ig = _Probe(d, process_check=lambda: set())
            harness_build(d, ig)
            w(d, SPEC, "steps: [")
            ig.on_pre("v1", "Write", {"file_path": os.path.join(d, SPEC)}, "modeler", cwd=d)
            ig.on_post("v1")
            rep = harness_build(d, ig)
            ok(rep["errors"] and not ig.open_events,
               f"PO13 (review) an invalid pipeline's report is written inside the harness grant: {ig.open_events}")
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
    ap.add_argument("--keep", action="store_true", help="with --scratch: keep the copy (its path is printed)")
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
        rep = scratch(a.run_dir, a.step, keep=a.keep)
        where = (f"scratch copy kept: {rep['scratch_dir']}" if a.keep
                 else "scratch copy removed (pass --keep to inspect it)")
        print(json.dumps(rep, indent=1) if a.json else summary_line(rep) + "\n" + where)
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
