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

Step keys: id, run, inputs, outputs, and optionally purpose, timeout_s (a soft
deadline: warned about and recorded in pipeline_progress.json, the step
killed only at its backstop; see DEFAULT_TIMEOUT_S); a
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
cloud credentials, each in its own process group (killed at its backstop), within
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
import threading
import time
from contextlib import nullcontext

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SPEC = os.path.join("models", "pipeline.yaml")
STATE = os.path.join("models", "pipeline_state.json")
REPORT = os.path.join("models", "pipeline_report.json")
# At the run root, not under models/ (Codex on #267, P1): compute.py's code fingerprint hashes models/ and data/,
# so a progress write there would change a running batch's fingerprint and discard its rows on resume.
PROGRESS = "pipeline_progress.json"
_RUN_ID = None           # the current run()'s id (MA_PIPELINE_RUN in its steps)
_PROGRESS_DOC: dict = {}  # (run dir, run id) -> PROGRESS as this process last wrote it
_PROGRESS_LOCK = threading.Lock()   # one check-and-write of PROGRESS at a time (Codex round 4 on #267)
# The runner's own records: written by the harness run only (protected in
# agents/enforcement.py SCRIPT_OUTPUTS).
OWN_FILES = ("models/pipeline_state.json", "models/pipeline_state.json.tmp",
             "models/pipeline_report.json", "models/pipeline_report.json.tmp",
             "pipeline_progress.json")
STEP_KEYS = {"id", "run", "inputs", "outputs", "purpose", "timeout_s", "inputs_jsonl", "out"}
COMPUTE_KEYS = {"inputs_jsonl", "out"}
TOP_KEYS = {"steps", "version"}
# Warn, don't kill (owner, 2026-11-17: two real runs died at a fixed 1 h step limit; on the VM path the analyze step
# needs 5-6 h). A step's `timeout_s` (default DEFAULT_TIMEOUT_S) is a SOFT deadline: past it the runner warns on
# stderr and writes PROGRESS every PROGRESS_EVERY_S, and the step runs on. Only the backstop kills: the larger of
# BACKSTOP_MULTIPLE x the soft deadline and BACKSTOP_FLOOR_S, for a runaway or a true hang. A caller's total
# pipeline budget (`budget_s`, its own declared limit) still bounds a step.
DEFAULT_TIMEOUT_S = 3600
BACKSTOP_MULTIPLE = 3
BACKSTOP_FLOOR_S = 24 * 3600.0
PROGRESS_EVERY_S = 900.0
KILL_GRACE_S = 10           # after SIGKILL: the bounded wait for the step to be reaped
# At a step's backstop (v2 autonomy 1D-i): SIGTERM to its process group first, and this long for every process in it
# to end (compute.py flushes its rows and stops its batch on SIGTERM), then SIGKILL for whatever is left.
TERM_GRACE_S = 25.0
# A step's stdout and stderr: <LOG_DIR>/<step id>.log, appended to on every execution (lib.safe_io, no link followed).
# A diagnostic record, not a harness-certified one. A `.log` under models/ is outside compute.py's code fingerprint
# (_FP_SKIP_EXT; self-test PD5b), so a log that grows while a batch runs never discards the batch's rows.
LOG_DIR = "models/logs"
LOG_TAIL = 800              # characters of a step's output kept in its report row's `error`
# The model API's credentials: no step calls the model (the harness's LLM tasks run outside the pipeline).
SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")
_CURRENT = object()         # _progress_record's default run id: the run() in progress
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
    out = {k: v for k, v in env.items() if not k.startswith(pre) and k not in SECRET_ENV}
    # A bare `python3` or console script in a step's command is the harness interpreter's (its venv), not whatever
    # is first on the caller's PATH.
    if sys.executable:
        here = os.path.dirname(os.path.abspath(sys.executable))
        path = (out["PATH"] if out.get("PATH") else os.defpath).split(os.pathsep)
        out["PATH"] = os.pathsep.join(path if path[:1] == [here] else [here] + path)
    return out


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


def _is_step_log(path: str, run_dir: str) -> bool:
    """A step log, under any name: a `.log` file directly in the run's LOG_DIR (a real directory; behind a link the
    runner writes no log). The runner appends to these while steps run, so they are reserved: never a step's input,
    output or code, whatever a declaration matches. Other files in that directory are ordinary files."""
    logs = os.path.join(os.path.realpath(run_dir), *LOG_DIR.split("/"))
    rp = os.path.realpath(path)
    return rp.endswith(".log") and os.path.dirname(rp) == logs and os.path.realpath(logs) == logs


def _matches(run_dir: str, pat: str) -> list[str]:
    """Run-relative files matching a declared path or glob (inside the run dir only; never a step log)."""
    out = []
    for p in glob.glob(os.path.join(run_dir, pat), recursive=True):
        if os.path.isfile(p) and _inside(p, run_dir) and not _is_step_log(p, run_dir):
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
                                  or not 0 < st["timeout_s"] < float("inf")):    # NaN fails too (Codex on #267)
            errs.append(f"{where}: `timeout_s` must be a positive finite number")
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
    return _toposort(steps, run_dir)


def _toposort(steps: list[dict], run_dir: str) -> tuple[list[dict], list[str]]:
    """(steps in run order with `_deps`, errors): each output has one producing step, and a step's input that
    another step outputs is an edge; a cycle is an error. Shared by the build pipeline and the derivations."""
    errs: list[str] = []
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
        if not os.path.isfile(p) or _is_step_log(p, run_dir):
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
    prog = os.path.realpath(os.path.join(run_dir, PROGRESS))
    for pat in step_inputs(step):
        # The runner rewrites PROGRESS while a step runs (Codex on #267): never an input, also through a link, and
        # excluded before the missing check, so it alone never satisfies a glob.
        m = [r for r in _matches(run_dir, pat) if os.path.realpath(os.path.join(run_dir, r)) != prog]
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
    """Publish a harness record in the run dir atomically, with no link followed (lib.safe_io; a random temporary
    name, never a predictable `.tmp` an artifact could plant)."""
    from lib.safe_io import write_nofollow
    rel = rel.replace(os.sep, "/")
    write_nofollow(run_dir, rel, json.dumps(obj, indent=1, sort_keys=True).encode(), atomic=True)
    return rel


def _retire_outputs(run_dir: str, old: dict, entries: dict) -> list:
    """Remove the outputs of derivations no longer declared. The names come from the previous derivation record, an
    artifact: each must be a plain relative path under data/derived/ (no "..", checked by its parts, not by prefix)
    and is unlinked relative to a no-follow directory fd, never outside the run dir (review round 4, P1-1)."""
    from lib.safe_io import remove_nofollow, LinkRefused
    removed = []
    for sid, e in (old or {}).items():
        if sid in entries or not isinstance(e, dict):
            continue
        for o in e.get("outputs") or {}:
            o = str(o)
            parts = o.replace("\\", "/").split("/")
            if not o.startswith(DERIVED_PREFIX) or any(x in ("", ".", "..") for x in parts) or os.path.isabs(o):
                continue
            try:
                if remove_nofollow(run_dir, o):
                    removed.append(o)
            except (LinkRefused, OSError):
                continue
    return removed


def _publish_outputs(work: str, run_dir: str, outputs) -> dict:
    """Copy a derivation's outputs from its work dir into the run dir: each read with no link followed in the work
    dir (an output that is a link is refused) and published atomically with no link followed in the run dir.
    Returns {output: sha256 of the bytes published}."""
    import hashlib
    from lib.safe_io import read_nofollow, write_nofollow, LinkRefused
    out = {}
    for o in outputs:
        try:
            data = read_nofollow(work, o)[0]
        except LinkRefused as e:
            raise ValueError(f"derivation output {o}: {e}") from None
        write_nofollow(run_dir, o, data, atomic=True)
        out[o] = hashlib.sha256(data).hexdigest()
    return out


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

def backstop_s(soft_s: float) -> float:
    """The wall time at which a step is killed: BACKSTOP_MULTIPLE x its soft deadline, or BACKSTOP_FLOOR_S."""
    return max(BACKSTOP_MULTIPLE * float(soft_s), BACKSTOP_FLOOR_S)


def _communicate(p, timeout_s, soft_s=None, on_overrun=None):
    """p.communicate() to `timeout_s` (raises TimeoutExpired there). With `soft_s` and `on_overrun`, it calls
    on_overrun(elapsed) once the soft deadline passes and every PROGRESS_EVERY_S after, the process running on."""
    t0 = time.monotonic()
    if soft_s is None or on_overrun is None or soft_s >= timeout_s:
        return p.communicate(timeout=timeout_s)
    nxt, worker = soft_s, None
    while True:
        try:
            # Each wait is at most PROGRESS_EVERY_S (Codex round 4 on #267: a 30-day wait overflows the OS poller).
            el = time.monotonic() - t0
            return p.communicate(timeout=max(0.01, min(nxt, timeout_s, el + PROGRESS_EVERY_S) - el))
        except subprocess.TimeoutExpired:
            el = time.monotonic() - t0
            if el >= timeout_s:
                raise
            if el >= nxt:
                # Off this loop (Codex round 3 on #267): a stderr or disk that blocks never holds the backstop. One
                # report at a time; a report still running when the next is due is skipped.
                if worker is None or not worker.is_alive():
                    worker = threading.Thread(target=_report, args=(on_overrun, el), daemon=True)
                    try:
                        worker.start()
                    except RuntimeError as e:     # the OS refused a thread (Codex round 4): the step runs on
                        worker = None
                        _warn(f"[pipeline] progress report not started: {e}")
                nxt = el + PROGRESS_EVERY_S


def _report(on_overrun, el) -> None:
    try:
        on_overrun(el)
    except Exception as e:  # noqa: BLE001 -- a warning that fails never stops the step
        _warn(f"[pipeline] progress record failed: {type(e).__name__}: {e}")


def _warn(msg: str) -> None:
    """A line on stderr; a stderr that cannot be written (a closed pipe) is ignored (Codex on #267)."""
    try:
        print(msg, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


class _Sink:
    """Where a command's output goes. With `rel`: the step's log, run_dir/rel, opened for append with no link
    followed (lib.safe_io); stdout and stderr both land there. Without: an unnamed temporary file, for stderr only.
    A file, never a pipe: a process that left the step's group cannot hold the runner by keeping a pipe open, and a
    step that is flushing after SIGTERM never blocks on a pipe nobody reads."""

    def __init__(self, run_dir: str | None = None, rel: str | None = None, header: str = ""):
        self.run_dir, self.rel = run_dir, rel
        if rel is None:
            self.fh = tempfile.TemporaryFile()
        else:
            from lib.safe_io import open_append_nofollow
            self.fh = open_append_nofollow(run_dir, rel)
            self.note(header)
        self.start = os.fstat(self.fh.fileno()).st_size

    def note(self, text: str) -> None:
        """A line of the runner's own in a step log (never fatal)."""
        if self.rel is not None:
            try:
                self.fh.write(text)
                self.fh.flush()
            except (OSError, ValueError):
                pass

    def tail(self, n: int = LOG_TAIL) -> str:
        """The last `n` characters this execution wrote ("" when the file cannot be read without following a link)."""
        fd = None
        try:
            if self.rel is None:
                fd = os.dup(self.fh.fileno())
            else:
                from lib.safe_io import open_nofollow
                fd = open_nofollow(self.run_dir, self.rel)
            size = os.fstat(fd).st_size
            start = self.start if self.start <= size else 0      # truncated by the step: what is there now
            off = max(start, size - 4 * n)
            return os.pread(fd, size - off, off).decode("utf-8", "replace")[-n:]
        except (OSError, ValueError):
            return ""
        finally:
            if fd is not None:
                os.close(fd)

    def close(self) -> None:
        try:
            self.fh.close()
        except (OSError, ValueError):
            pass


def _stop_group(p, grace_s: float) -> str:
    """Stop a step's process group at its limit; returns how, for the error text. With a grace: SIGTERM, then up to
    `grace_s` for every process in the group to end, not only its leader (a shell that leads the group ends at once
    while the process it started is still flushing). Then SIGKILL for whatever is left, and a bounded wait."""
    how, gone = "process group killed", False
    try:
        if grace_s > 0:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
            how, end = f"SIGTERM, then SIGKILL after the {grace_s:.0f}s grace", time.monotonic() + grace_s
            while time.monotonic() < end:
                p.poll()                              # reap the leader, so an ended group reads as gone
                try:
                    os.killpg(p.pid, 0)
                except (ProcessLookupError, PermissionError):     # no process left (or only ones already dead)
                    how, gone = f"SIGTERM, ended within the {grace_s:.0f}s grace", True
                    break
                except OSError:
                    pass
                time.sleep(0.05)
    finally:
        # Also when the grace is interrupted (Codex round 1): the group never outlives the runner's wait. A group
        # already seen gone gets no signal (its id may have been given to another group).
        if not gone:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
    try:
        p.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        how += f"; not reaped within {KILL_GRACE_S}s"
    return how


def _run_cmd(cmd, cwd, timeout_s, env, shell=False, soft_s=None, on_overrun=None, log=None, grace_s=0.0):
    """Own process group; at `timeout_s` the whole group is stopped (_stop_group: SIGTERM and `grace_s` first when a
    grace is given, then SIGKILL). `soft_s`/`on_overrun`: see _communicate (the step's soft deadline: warned about,
    never killed). `log`: the step's _Sink (stdout and stderr); without one stdout is dropped and stderr is kept
    for the returned tail only. Returns (returncode or None at the limit, the tail of the output or the reason)."""
    sink = log if log is not None else _Sink()
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdout=sink.fh if log is not None else subprocess.DEVNULL, stderr=sink.fh,
                             stdin=subprocess.DEVNULL, env=env, start_new_session=True, shell=shell)
        try:
            _communicate(p, timeout_s, soft_s, on_overrun)
        except subprocess.TimeoutExpired:
            how = _stop_group(p, grace_s)
            past = f" at its backstop (soft deadline {soft_s:.0f}s)" if soft_s is not None else ""
            tail = sink.tail().strip()
            return None, f"timed out after {timeout_s:.0f}s{past} ({how})" + (f"; last output: {tail}" if tail else "")
        except BaseException:
            try:                    # anything else escaping the wait: the step's group never outlives it
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            raise
        # Review #4: a process the step left in the background (`cmd &`) would keep
        # writing declared outputs after the step is recorded and its grant ends.
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        return p.returncode, sink.tail()
    finally:
        if log is None:
            sink.close()


def step_log(step: dict) -> str:
    """The run-relative path of a step's log (a step id holds no path separator)."""
    return f"{LOG_DIR}/{step['id']}.log"


def _exec_step(step: dict, run_dir: str, timeout_s: float, python: str | None, soft_s: float | None = None,
               on_overrun=None, info: dict | None = None) -> tuple[int | None, str]:
    """Run one step to `timeout_s` (its backstop); `soft_s`/`on_overrun` as _communicate. A compute step's batch
    gets the backstop as its own wall limit, so the batch is not stopped at the soft deadline either. The step's
    output goes to its log (step_log); `info` receives `log`, or `log_error` when the log cannot be opened without
    following a link (the step then runs as before the log existed: stderr's tail only)."""
    rel, sink = step_log(step), None
    try:
        sink = _Sink(run_dir, rel, f"=== step {step['id']} | pipeline run {_RUN_ID} | started {_iso(time.time())} ===\n")
    except (OSError, ValueError) as e:
        _warn(f"[pipeline] step {step['id']}: no log ({rel}: {e}); the step runs without one")
    if info is not None:
        info.update({"log": rel} if sink is not None else {"log_error": f"{rel} could not be opened; no log kept"})
    env = script_env()
    env["MA_PIPELINE_RUN_DIR"] = run_dir     # the trusted root a harness step writes under (the dir it runs in)
    if _RUN_ID:
        env["MA_PIPELINE_RUN"] = _RUN_ID     # compute.py records it in each batch summary (stage_machine._infra_batch)
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
            res = _run_cmd(cmd, run_dir, timeout_s + 60, env, soft_s=soft_s, on_overrun=on_overrun, log=sink,
                           grace_s=TERM_GRACE_S)
        else:
            res = _run_cmd(step["run"], run_dir, timeout_s, env, shell=True, soft_s=soft_s, on_overrun=on_overrun,
                           log=sink, grace_s=TERM_GRACE_S)
        if sink is not None:
            sink.note(f"=== ended {_iso(time.time())} | " + (f"returncode {res[0]}" if res[0] is not None
                                                             else res[1].split("; last output:")[0]) + " ===\n")
        return res
    finally:
        if sink is not None:
            sink.close()
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
    from lib.safe_io import rename_nofollow, LinkRefused
    back = []
    for d in glob.glob(os.path.join(run_dir, ".pipeline_aside_*")):
        if os.path.islink(d) or not os.path.isdir(d):
            continue                         # a planted link, not an aside dir the harness made
        aside_rel = os.path.basename(d)
        for dp, _dirs, fns in os.walk(d):
            for fn in fns:
                rel = os.path.relpath(os.path.join(dp, fn), d).replace(os.sep, "/")
                if os.path.lexists(os.path.join(run_dir, rel)):
                    continue
                if not dry:
                    try:              # no link followed on either side; a link entry is refused, never moved
                        rename_nofollow(run_dir, f"{aside_rel}/{rel}", rel)
                    except (LinkRefused, OSError):
                        continue
                back.append(rel)
        if not dry:
            shutil.rmtree(d, ignore_errors=True)   # rmtree removes links, never their targets
    return back


def _trusted_outputs(rec) -> dict:
    """Output hashes the harness last recorded for a step (kept across a failed run)."""
    out = rec.get("outputs") if isinstance(rec, dict) else None
    return out if isinstance(out, dict) else {}


def _progress_record(run_dir: str, step: dict, soft_s: float, hard_s: float, t_start: float, elapsed: float | None,
                     last_s, state: str, rid=_CURRENT) -> None:
    """PROGRESS, this step's entry: where it stands against its soft deadline and its backstop, and a projection of
    when it ends (a compute step: from the rows its batch has written; otherwise its last recorded runtime)."""
    now = time.time()
    rid = _RUN_ID if rid is _CURRENT else rid     # the step's own run: a late report outlives run()'s reset of the id
    el = (now - t_start) if elapsed is None else elapsed
    proj, basis = None, None
    fn = _compute_fn(step)
    if fn is not None:
        try:
            total = sum(1 for ln in open(os.path.join(run_dir, step["inputs_jsonl"])) if ln.strip())
            done = sum(1 for ln in open(os.path.join(run_dir, step["out"])) if ln.strip())
            if total and done:
                proj, basis = el * total / done, f"{done} of {total} rows written"
        except OSError:
            pass
    if proj is None and isinstance(last_s, (int, float)) and last_s > el:
        proj, basis = float(last_s), "the step's last recorded runtime"
    entry = {"state": state, "pipeline_run": rid, "started_at": _iso(t_start), "at": _iso(now),
             "elapsed_s": round(el, 1), "soft_deadline_s": soft_s, "backstop_s": hard_s,
             "backstop_at": _iso(t_start + hard_s), "backstop_rule": f"max({BACKSTOP_MULTIPLE} x soft deadline, "
             f"{BACKSTOP_FLOOR_S / 3600:g} h), within any pipeline budget",
             "projected_total_s": round(proj, 1) if proj is not None else None, "projection_basis": basis,
             "projected_past_backstop": (proj > hard_s) if proj is not None else None}
    # Kept in memory for this pipeline run and only ever replaced on disk, never read back (Codex on #267: a FIFO at
    # the path would block the deadline loop, and an edited entry would be republished as the harness's).
    # Under one lock, the check and the write (Codex round 4 on #267): a 'running' report that passed the check
    # cannot land on disk after the final state.
    with _PROGRESS_LOCK:
        doc = _PROGRESS_DOC.setdefault((run_dir, rid), {"pipeline_run": rid, "steps": {}})
        prev = doc["steps"].get(step["id"]) or {}
        if state == "running" and prev.get("started_at") == entry["started_at"] and prev.get("state") != "running":
            return                # a late report (its thread) never overwrites the step's final state
        doc["steps"][step["id"]] = entry
        _write_json(run_dir, PROGRESS, doc)
    if state == "running":
        _warn(f"[pipeline] WARNING step {step['id']}: running {_dur(el)}, past its soft deadline of {_dur(soft_s)}; "
              f"it runs on, killed only at its backstop of {_dur(hard_s)} ({entry['backstop_at']})"
              + (f"; projected total {_dur(proj)} ({basis})" if proj is not None else "; no projection yet"))


def _dur(s: float) -> str:
    s = float(s)
    return f"{s:.0f} s" if s < 120 else f"{s / 60:.0f} min" if s < 7200 else f"{s / 3600:.1f} h"


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def _run_one(step: dict, run_dir: str, timeout_s: float, python: str | None, rec: dict | None = None,
             suspect=None, soft_s: float | None = None) -> dict:
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
    good: dict[str, str] = {}      # rel -> its copy aside, relative to the run dir (the files to restore on failure)
    from lib.safe_io import copy_nofollow, remove_nofollow, rename_nofollow, LinkRefused
    aside_rel = os.path.relpath(aside, os.path.realpath(run_dir)).replace(os.sep, "/")

    def restore() -> list[str]:
        back = []
        for rel in sorted(_snapshot(step, run_dir)):
            try:
                remove_nofollow(run_dir, rel)                   # what the failed run left (the entry, never a target)
            except (LinkRefused, OSError):
                pass
        for rel, src in sorted(good.items()):
            try:
                rename_nofollow(run_dir, src, rel)
                back.append(rel)
            except (LinkRefused, OSError):
                pass
        return back

    try:
        for rel in sorted(before):
            src = os.path.join(run_dir, rel)
            kept = f"{aside_rel}/{rel}"
            is_good = (_sha(src) == trusted.get(rel) if trusted else True) \
                and not (suspect is not None and suspect(rel))
            try:              # every move, copy and removal relative to no-follow dir fds (lib.safe_io)
                if rel in keep:
                    copy_nofollow(run_dir, rel, kept)
                    good[rel] = kept
                    continue
                if is_good:
                    rename_nofollow(run_dir, rel, kept)
                    good[rel] = kept
                else:
                    remove_nofollow(run_dir, rel)
                row["removed"].append(rel)
            except (LinkRefused, OSError) as e:
                row["error"] = f"could not clear old output {rel}: {e}"
                row["restored"] = restore()
                return row
        s0 = time.time()
        last = rec.get("seconds") if isinstance(rec, dict) else None
        over, rid, info = [], _RUN_ID, {}

        def on_overrun(el):
            over.append(el)
            _progress_record(run_dir, step, soft_s, timeout_s, s0, el, last, "running", rid)
        try:
            rc, err = _exec_step(step, run_dir, timeout_s, python, soft_s, on_overrun if soft_s is not None else None,
                                 info=info)
        except OSError as e:
            rc, err = None, f"{type(e).__name__}: {e}"
        row.update(info, returncode=rc, seconds=round(time.time() - s0, 1), ok=rc == 0)
        if over:
            row["over_soft_deadline"] = True
            row["soft_deadline_s"], row["backstop_s"] = soft_s, timeout_s
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
        if over:              # the final state once its outputs are checked (Codex on #267: never `ok` for a rollback)
            try:
                _progress_record(run_dir, step, soft_s, timeout_s, s0, row["seconds"], last,
                                 "ok" if row["ok"] else "failed", rid)
            except Exception:  # noqa: BLE001 -- the record is a report, never the step's outcome
                pass
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
    global _RUN_ID
    # F6: an id for this run of the pipeline, in its report and in its steps' environment, so a reader knows which
    # batch summaries this run wrote without trusting mtimes (a run dir copied or restored from a checkpoint)
    rid = _RUN_ID = f"{time.strftime('%Y%m%dT%H%M%S')}-{os.urandom(4).hex()}"
    try:
        return _run(run_dir, budget_s, integrity, python, only, force)
    finally:
        # A step run outside this run() never exports this run's id (MA_PIPELINE_RUN). Only this run's own id is
        # reset: one process runs one pipeline at a time, and an id another run() set is left alone.
        if _RUN_ID == rid:
            _RUN_ID = None


def _run(run_dir, budget_s, integrity, python, only, force) -> dict:
    run_dir = os.path.realpath(run_dir)
    t0 = time.time()
    steps, errs = load(run_dir)
    report = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "errors": errs, "plan": [], "steps": [], "ok": not errs,
              "pipeline_run": _RUN_ID}
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
        from lib.safe_io import remove_nofollow, LinkRefused
        for rel in retired:
            try:
                if os.path.isfile(os.path.join(run_dir, rel)) and remove_nofollow(run_dir, rel):
                    produced.append(rel)
            except (LinkRefused, OSError):
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
                soft = float(s.get("timeout_s") or DEFAULT_TIMEOUT_S)
                hard = backstop_s(soft) if left is None else min(backstop_s(soft), left)
                res = _run_one(s, run_dir, hard, python, rec, suspect=integrity.is_open if integrity is not None else None,
                               soft_s=min(soft, hard))

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
        doc = _PROGRESS_DOC.get((run_dir, _RUN_ID))
        if doc is not None:
            # The runner's own record, republished (Codex round 3 on #267): a later step that overwrote the file
            # cannot have its bytes certified as harness-written.
            try:
                with _PROGRESS_LOCK:
                    produced.append(_write_json(run_dir, PROGRESS, doc))
            except OSError:
                pass
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


# ---- derivations (v2-derived-inputs) ------------------------------------- #
# docs/design/v2-derived-inputs.md. Every data file the decision spec reads that is not a staged input is made by a
# derivation: a pure script the planner writes and the harness runs here, in a sandbox, twice, with the hashes of
# its script, inputs and outputs recorded. The planner writes code, never tables.
#
#   data/derivations.yaml   (the planner's declaration)
#     derivations:
#       - id: units
#         script: data/derive/units.py          # run as: python <script> --inputs <paths> --outputs <paths>
#         inputs: [data/inputs/<staged>.csv, data/derived/<upstream>.csv]
#         outputs: [data/derived/units.csv]
#         columns:                               # every column of every staged table is used or ignored
#           uses: {data/inputs/<staged>.csv: [colA, "month_*"]}
#           ignores: {data/inputs/<staged>.csv: {colC: "<reason>"}}
#     ignored_inputs: {data/inputs/<staged>.csv: "<reason the whole table is not used>"}
#   data/derivations_record.json   (the harness's record; agents cannot write it)
DERIVATIONS = "data/derivations.yaml"
DERIVATIONS_RECORD = "data/derivations_record.json"
DERIVE_PREFIX, DERIVED_PREFIX, INPUTS_PREFIX = "data/derive/", "data/derived/", "data/inputs/"
INPUTS_PROVENANCE = "data/inputs_provenance.json"
DERIVATION_KEYS = {"id", "script", "inputs", "outputs", "columns", "timeout_s"}
DERIVATION_TOP = {"derivations", "ignored_inputs", "version"}
# Imports a derivation may use (owner, 2026-11-01): the standard library, numpy, pandas and pyyaml. Geometry is
# staged as tables. Dynamic imports and code from strings are refused: the scan could not see what they load.
IMPORT_ALLOW = ("numpy", "pandas", "yaml")
IMPORT_REFUSED = ("importlib", "ctypes", "runpy", "imp", "pkgutil", "zipimport")
CALLS_REFUSED = ("__import__", "exec", "eval", "compile")
TABULAR = (".csv", ".tsv")
# Resource caps (RLIMIT_AS, RLIMIT_CPU, RLIMIT_FSIZE, RLIMIT_NPROC) and the wall-clock timeout.
LIMITS = {"memory_bytes": 4 << 30, "cpu_s": 600, "file_bytes": 1 << 30, "nproc": 1024, "wall_s": 900}
DERIVATION_CODES = ("units_not_derived", "data_ref_not_derived", "derivation_stale", "derivation_failed",
                    "derivation_nondeterministic", "derivation_unsandboxed", "input_column_unaccounted",
                    "ignored_column_used")
_SANDBOX: list = []


def _prov_inputs(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, INPUTS_PROVENANCE)) as f:
            return (json.load(f) or {}).get("inputs") or {}
    except (OSError, ValueError):
        return {}


def load_derivations(run_dir: str) -> tuple[list[dict], dict, list[str]]:
    """(derivations in run order, ignored_inputs, errors). Any error: no derivations (fail closed)."""
    import yaml
    run_dir = os.path.realpath(run_dir)
    p = os.path.join(run_dir, DERIVATIONS)
    if not os.path.isfile(p):
        return [], {}, [f"{DERIVATIONS} not found"]
    try:
        with open(p) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        return [], {}, [f"{DERIVATIONS} does not parse: {type(e).__name__}: {str(e)[:200]}"]
    if not isinstance(doc, dict) or not isinstance(doc.get("derivations"), list) or not doc["derivations"]:
        return [], {}, [f"{DERIVATIONS}: a mapping with a non-empty `derivations` list"]
    errs = [f"{DERIVATIONS}: unknown top-level key {k!r} (allowed: {sorted(DERIVATION_TOP)})" for k in doc
            if k not in DERIVATION_TOP]
    ign = doc.get("ignored_inputs") or {}
    if not isinstance(ign, dict) or not all(isinstance(v, str) and v.strip() for v in ign.values()):
        errs.append(f"{DERIVATIONS}: ignored_inputs is {{staged input: reason}}, each with a reason")
        ign = {}
    out, seen = [], set()
    for n, d in enumerate(doc["derivations"]):
        where = f"derivation {n + 1}"
        if not isinstance(d, dict):
            errs.append(f"{where}: must be a mapping")
            continue
        d = _normalize(d)
        sid = d.get("id")
        if not isinstance(sid, str) or not _ID.fullmatch(sid):
            errs.append(f"{where}: `id` must match {_ID.pattern}")
        else:
            where = f"derivation {sid!r}"
            if sid in seen:
                errs.append(f"{where}: duplicate id")
            seen.add(sid)
        errs += [f"{where}: unknown key {k!r} (allowed: {sorted(DERIVATION_KEYS)})" for k in d if k not in DERIVATION_KEYS]
        sc = d.get("script")
        if not (isinstance(sc, str) and sc.startswith(DERIVE_PREFIX) and sc.endswith(".py")):
            errs.append(f"{where}: `script` must be a .py file under {DERIVE_PREFIX}")
        for k in ("inputs", "outputs"):
            if not isinstance(d.get(k), list) or not all(isinstance(x, str) for x in d[k]):
                errs.append(f"{where}: `{k}` must be a list of paths")
                d[k] = []
        if not d["inputs"]:
            errs.append(f"{where}: reads no input (a derivation transforms staged inputs; a table typed into a "
                        "script is not derived)")
        if not d["outputs"]:
            errs.append(f"{where}: declares no outputs")
        for x in d["inputs"]:
            if not x.startswith((INPUTS_PREFIX, DERIVED_PREFIX)):
                errs.append(f"{where}: input {x!r} is neither a staged input ({INPUTS_PREFIX}) nor a derived file "
                            f"({DERIVED_PREFIX})")
        for x in d["outputs"]:
            if not x.startswith(DERIVED_PREFIX):
                errs.append(f"{where}: output {x!r} must be under {DERIVED_PREFIX}")
        for x in d["inputs"] + d["outputs"] + ([sc] if isinstance(sc, str) else []):
            if _GLOB.search(x):
                errs.append(f"{where}: {x!r}: name files, not globs")
            e = _check_path(x, run_dir, where)
            if e:
                errs.append(e)
        if "timeout_s" in d and (not isinstance(d["timeout_s"], (int, float)) or isinstance(d["timeout_s"], bool)
                                 or d["timeout_s"] <= 0):
            errs.append(f"{where}: `timeout_s` must be a positive number")
        cols = d.get("columns") or {}
        if not isinstance(cols, dict) or set(cols) - {"uses", "ignores"}:
            errs.append(f"{where}: `columns` is {{uses: {{input: [columns]}}, ignores: {{input: {{column: reason}}}}}}")
            cols = {}
        for k, v in cols.items():
            if not isinstance(v, dict):
                errs.append(f"{where}: columns.{k} is a mapping keyed by input path")
                continue
            for inp, cs in v.items():
                if inp not in d["inputs"]:
                    errs.append(f"{where}: columns.{k} names {inp!r}, which is not one of its inputs")
                if k == "uses" and not (isinstance(cs, list) and all(isinstance(c, str) for c in cs)):
                    errs.append(f"{where}: columns.uses.{inp} is a list of column names or globs")
                if k == "ignores" and not (isinstance(cs, dict) and all(isinstance(r, str) and r.strip()
                                                                        for r in cs.values())):
                    errs.append(f"{where}: columns.ignores.{inp} is {{column or glob: reason}}, each with a reason")
        d["columns"] = cols
        out.append(d)
    if errs:
        return [], {}, errs
    order, e = _toposort(out, run_dir)
    if e:
        return [], {}, e
    made = {o for d in order for o in d["outputs"]}
    errs += [f"derivation {d['id']!r}: input {x!r} is under {DERIVED_PREFIX} but no derivation outputs it"
             for d in order for x in d["inputs"] if x.startswith(DERIVED_PREFIX) and x not in made]
    return (order, ign, []) if not errs else ([], {}, errs)


def import_problems(path: str) -> list[str]:
    """Imports outside the allowlist, found by an AST scan before the run (fail closed)."""
    import ast as _ast
    try:
        with open(path, encoding="utf-8") as f:
            tree = _ast.parse(f.read(), path)
    except (OSError, SyntaxError, ValueError) as e:
        return [f"the script cannot be parsed: {type(e).__name__}: {e}"]
    std = set(getattr(sys, "stdlib_module_names", ()))
    if not std:
        return ["this Python does not list its standard library (3.10 or later is needed), so imports cannot be checked"]
    bad = []
    for node in _ast.walk(tree):
        names = []
        if isinstance(node, _ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, _ast.ImportFrom):
            if node.level:
                bad.append(f"line {node.lineno}: a relative import (a derivation is one self-contained script)")
                continue
            names = [node.module or ""]
        elif isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name) and node.func.id in CALLS_REFUSED:
            bad.append(f"line {node.lineno}: {node.func.id}() is refused (code the import scan cannot see)")
        for nm in names:
            top = nm.split(".")[0]
            if top in IMPORT_REFUSED or not (top in std or top in IMPORT_ALLOW):
                bad.append(f"line {node.lineno}: import {nm} is not allowed (the standard library, numpy, pandas and "
                           "pyyaml only)")
    return bad


def _bwrap_cmd(kind: str, binds: list, envargs: list, chdir: str, child: list) -> list:
    """The bubblewrap command. `bwrap`: unprivileged, every namespace unshared, run as this user. `bwrap-sudo`: started
    by root (where the host forbids unprivileged user namespaces), with no user namespace so root can bind the
    run's private files, and the child drops to this user with no capabilities and no new privileges (setpriv)."""
    uid, gid = str(os.getuid()), str(os.getgid())
    if kind == "bwrap":
        ns = ["--unshare-all", "--uid", uid, "--gid", gid]
        drop = []
    else:
        ns = ["--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-cgroup-try"]
        drop = ["setpriv", "--reuid", uid, "--regid", gid, "--clear-groups", "--no-new-privs", "--inh-caps=-all",
                "--bounding-set=-all", "--"]
    return (["sudo", "-n"] if kind == "bwrap-sudo" else []) + [
        "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", *binds, *ns, "--die-with-parent",
        "--new-session", "--clearenv", *envargs, "--chdir", chdir, *drop, *child]


def _bwrap_probe(kind: str) -> bool:
    try:
        r = subprocess.run(_bwrap_cmd(kind, [], ["--setenv", "PATH", os.environ.get("PATH", "/usr/bin:/bin")], "/",
                                      ["true"]), capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def sandbox_kind() -> str | None:
    """`bwrap` (Linux namespaces, unprivileged), `bwrap-sudo` (the same, started by root and run as this user: in a
    container whose host forbids unprivileged user namespaces, as on the Coiled VMs), or None (no sandbox: macOS,
    or Linux without bubblewrap). Probed once per process."""
    if _SANDBOX:
        return _SANDBOX[0]
    kind = None
    if sys.platform.startswith("linux") and shutil.which("bwrap"):
        if _bwrap_probe("bwrap"):
            kind = "bwrap"
        elif shutil.which("sudo") and shutil.which("setpriv") and _bwrap_probe("bwrap-sudo"):
            kind = "bwrap-sudo"
    _SANDBOX.append(kind)
    return kind


def _child(argv: list) -> int:
    """Inside the sandbox: set the caps, then run the derivation script as __main__ (this file is the launcher)."""
    import resource
    import runpy
    lim, strict, script, rest = json.loads(argv[0]), argv[1] == "strict", argv[2], argv[3:]
    for name, key, extra in (("RLIMIT_AS", "memory_bytes", 0), ("RLIMIT_CPU", "cpu_s", 2),
                             ("RLIMIT_FSIZE", "file_bytes", 0), ("RLIMIT_NPROC", "nproc", 0)):
        r = getattr(resource, name, None)
        if r is None:
            continue
        try:
            resource.setrlimit(r, (int(lim[key]), int(lim[key]) + extra))
        except (ValueError, OSError):
            if strict:
                raise                    # a cap that cannot be set is a sandbox that does not hold
    sys.argv = [script] + rest
    sys.path[:] = [p for p in sys.path if p and os.path.realpath(p) != os.path.realpath(HERE)]
    runpy.run_path(script, run_name="__main__")
    return 0


def _env(work: str) -> dict:
    """A minimal environment: no credentials and nothing the run's machine set (determinism)."""
    keep = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TZ") if k in os.environ}
    return dict(keep, HOME=work, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1",
                OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")


def _strip_columns(src: str, dest: str, cols: list[str]) -> None:
    import csv
    delim = "\t" if src.endswith(".tsv") else ","
    with open(src, newline="", encoding="utf-8") as f, open(dest, "w", newline="", encoding="utf-8") as g:
        r = csv.reader(f, delimiter=delim)
        head = next(r, [])
        keep = [i for i, c in enumerate(head) if c not in cols]
        w = csv.writer(g, delimiter=delim, lineterminator="\n")
        w.writerow([head[i] for i in keep])
        for row in r:
            w.writerow([row[i] for i in keep if i < len(row)])


def _run_derivation(d: dict, run_dir: str, kind: str, lim: dict, strip: dict | None = None) -> dict:
    """One run of one derivation in a private work directory (removed by the caller: `work`). The script sees its
    declared inputs (read-only), its own outputs' directories (writable) and nothing else of the run directory."""
    work = os.path.realpath(tempfile.mkdtemp(prefix="derive_"))
    side = os.path.realpath(tempfile.mkdtemp(prefix="derive_in_"))
    res = {"work": work, "side": side, "rc": None, "err": "", "outputs": {}, "undeclared": [], "cap": None,
           "input_written": []}
    mounts, copies = [], {}
    for rel in d["inputs"] + [d["script"]]:
        src = os.path.join(run_dir, rel)
        if strip and rel in strip:
            alt = os.path.join(side, rel.replace("/", "__"))
            _strip_columns(src, alt, strip[rel])
            src = alt
        dest = os.path.join(work, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if kind == "none":
            shutil.copyfile(src, dest)         # a copy: an unsandboxed run cannot touch the original
            copies[rel] = _sha(dest)
        else:
            open(dest, "w").close()            # the mount point
            mounts += ["--ro-bind", src, dest]
    for rel in d["outputs"]:
        os.makedirs(os.path.dirname(os.path.join(work, rel)), exist_ok=True)
    child = [sys.executable, "-I", os.path.abspath(__file__), "_derive_child", json.dumps(lim),
             "strict" if kind != "none" else "lenient", d["script"], "--inputs", *d["inputs"], "--outputs", *d["outputs"]]
    env = _env(work)
    if kind == "none":
        cmd = child
    else:
        envargs = [a for k, v in env.items() for a in ("--setenv", k, v)]
        cmd = _bwrap_cmd(kind, ["--tmpfs", run_dir, "--remount-ro", run_dir,     # the run directory is not visible
                                "--bind", work, work, *mounts], envargs, work, child)
    t0 = time.time()
    rc, err = _run_cmd(cmd, work, float(d.get("timeout_s") or lim["wall_s"]), env)
    res.update(rc=rc, err=err, runtime_s=round(time.time() - t0, 3))
    if rc is None:
        res["cap"] = "wall_s"
    elif rc in (-signal.SIGXCPU, 128 + signal.SIGXCPU, -signal.SIGKILL, 128 + signal.SIGKILL):
        res["cap"] = "cpu_s"
    elif "MemoryError" in err or "Cannot allocate memory" in err:
        res["cap"] = "memory_bytes"
    elif "File too large" in err or "[Errno 27]" in err or rc in (-signal.SIGXFSZ, 128 + signal.SIGXFSZ):
        res["cap"] = "file_bytes"
    inputs = set(d["inputs"]) | {d["script"]}
    for dp, _dirs, fns in os.walk(work):
        for fn in fns:
            rel = os.path.relpath(os.path.join(dp, fn), work).replace(os.sep, "/")
            if rel in inputs:
                if kind == "none" and _sha(os.path.join(dp, fn)) != copies.get(rel):
                    res["input_written"].append(rel)
            elif rel in d["outputs"]:
                res["outputs"][rel] = _sha(os.path.join(dp, fn))
            else:
                res["undeclared"].append(rel)
    return res


def _decl_sha(d: dict) -> str:
    return hashlib.sha256(json.dumps({k: d[k] for k in sorted(DERIVATION_KEYS) if k in d}, sort_keys=True).encode()).hexdigest()


def _libraries() -> dict:
    from importlib import metadata
    out = {}
    for name in ("numpy", "pandas", "pyyaml"):
        try:
            out[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            out[name] = None
    return out


def load_record(run_dir: str) -> dict:
    try:
        with open(os.path.join(run_dir, DERIVATIONS_RECORD)) as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) and isinstance(rec.get("derivations"), dict) else {"derivations": {}}
    except (OSError, ValueError):
        return {"derivations": {}}


def _current(d: dict, e: dict | None, run_dir: str) -> str | None:
    """Why the record of a derivation is not current (None: current)."""
    if not isinstance(e, dict):
        return "never run"
    if e.get("declaration_sha256") != _decl_sha(d):
        return "its declaration changed"
    if (e.get("script") or {}).get("sha256") != _sha(os.path.join(run_dir, d["script"])):
        return f"its script {d['script']} changed"
    ch = [i for i in d["inputs"] if (e.get("inputs") or {}).get(i) != _sha(os.path.join(run_dir, i))]
    if ch:
        return f"input(s) changed: {', '.join(ch)}"
    ch = [o for o in d["outputs"] if (e.get("outputs") or {}).get(o) != _sha(os.path.join(run_dir, o))]
    if e.get("ok") and ch:
        return f"output(s) differ from what the harness wrote: {', '.join(ch)}"
    return None


def derive(run_dir: str, unsandboxed_ok: bool = False, limits: dict | None = None) -> dict:
    """Run the declared derivations that are not current, in order, and write the record. Each runs twice (different
    output hashes: derivation_nondeterministic) and, when it ignores columns of a staged table, once more with them
    removed (a different output or a failure: ignored_column_used). Outputs reach data/derived/ only from a run that
    passed. {ok, problems: [{code, id, claim}], ran, up_to_date}. No declaration: nothing to run."""
    run_dir = os.path.realpath(run_dir)
    rep = {"ok": True, "problems": [], "ran": [], "up_to_date": []}
    if not os.path.exists(os.path.join(run_dir, DERIVATIONS)):
        return rep
    lim = dict(LIMITS, **(limits or {}))
    ders, _ign, errs = load_derivations(run_dir)
    if errs:
        rep.update(ok=False, problems=[{"code": "derivation_failed", "id": None, "claim": e} for e in errs])
        return rep
    kind = found = sandbox_kind()
    old = load_record(run_dir)["derivations"]

    def current(d):
        """Up to date: recorded ok, nothing changed, and made in a sandbox (an unsandboxed record is re-made
        wherever a sandbox exists). A current derivation needs no sandbox: a resumed run on a machine without one
        only checks the hashes."""
        e = old.get(d["id"])
        rk = ((e or {}).get("sandbox") or {}).get("kind")
        return (isinstance(e, dict) and e.get("ok") and _current(d, e, run_dir) is None
                and (rk != "none" or found is None))
    if kind is None and not unsandboxed_ok and not all(current(d) for d in ders):
        rep.update(ok=False, problems=[{"code": "derivation_unsandboxed", "id": None, "claim":
                   f"no sandbox on this machine ({sys.platform}): derivations run only in Linux namespaces (bubblewrap). "
                   "The development flag runs them unsandboxed, and a run that uses it cannot be accepted"}])
        return rep
    kind = kind or "none"
    prov = _prov_inputs(run_dir)
    entries, bad_outputs = {}, set()
    for d in ders:
        sid = d["id"]
        e = old.get(sid)
        if current(d):
            entries[sid] = e
            rep["up_to_date"].append(sid)
            continue
        entry = {"id": sid, "declaration_sha256": _decl_sha(d),
                 "script": {"path": d["script"], "sha256": _sha(os.path.join(run_dir, d["script"]))},
                 "inputs": {i: _sha(os.path.join(run_dir, i)) for i in d["inputs"]}, "outputs": {},
                 "ok": False, "code": "derivation_failed", "reason": "", "cap": None,
                 "sandbox": {"kind": kind, "limits": lim}, "python": sys.version.split()[0], "libraries": _libraries(),
                 "runtime_s": None, "stderr_tail": "", "runs": 0}
        entries[sid] = entry
        from lib.safe_io import remove_nofollow, LinkRefused
        for o in d["outputs"]:                 # nothing stale stays readable while this derivation is not current
            try:
                remove_nofollow(run_dir, str(o))       # the entry itself, relative to a no-follow dir fd
            except (LinkRefused, OSError):
                pass
        why = ([f"input {i} is missing" for i in d["inputs"] if entry["inputs"][i] is None]
               + [f"input {i} comes from derivation output that failed" for i in d["inputs"] if i in bad_outputs]
               + [f"staged input {i} is not the file the harness staged (its hash differs from {INPUTS_PROVENANCE})"
                  for i in d["inputs"] if i.startswith(INPUTS_PREFIX) and (prov.get(i) or {}).get("sha256")
                  != entry["inputs"][i]])
        if entry["script"]["sha256"] is None:
            why.append(f"script {d['script']} is missing")
        else:
            why += [f"refused import: {x}" for x in import_problems(os.path.join(run_dir, d["script"]))]
        runs = []
        try:
            if not why:
                runs.append(_run_derivation(d, run_dir, kind, lim))
                r1 = runs[0]
                entry.update(runs=1, runtime_s=r1["runtime_s"], stderr_tail=r1["err"][-800:], cap=r1["cap"])
                if r1["rc"] != 0:
                    why.append(f"the script failed (exit {r1['rc']})" + (f": the {r1['cap']} cap ({lim[r1['cap']]}) "
                                                                         "was reached" if r1["cap"] else ""))
                if r1["undeclared"]:
                    why.append(f"undeclared output(s): {', '.join(sorted(r1['undeclared'])[:5])}")
                if r1["input_written"]:
                    why.append(f"it wrote to its input(s): {', '.join(r1['input_written'])}")
                miss = [o for o in d["outputs"] if o not in r1["outputs"]]
                if not why and miss:
                    why.append(f"declared output(s) not written: {', '.join(miss)}")
            if not why:
                runs.append(_run_derivation(d, run_dir, kind, lim))
                entry["runs"] = 2
                if runs[1]["rc"] != 0 or runs[1]["outputs"] != runs[0]["outputs"]:
                    entry["code"] = "derivation_nondeterministic"
                    why.append("two runs on the same inputs gave different outputs: "
                               + ", ".join(o for o in d["outputs"] if runs[1]["outputs"].get(o) != runs[0]["outputs"].get(o)))
            strip = _ignored_columns(d, run_dir) if not why else {}
            if strip:
                runs.append(_run_derivation(d, run_dir, kind, lim, strip=strip))
                entry["runs"] = 3
                entry["ignored_columns_checked"] = strip
                if runs[-1]["rc"] != 0 or runs[-1]["outputs"] != runs[0]["outputs"]:
                    entry["code"] = "ignored_column_used"
                    why.append("with the ignored columns removed (" + "; ".join(f"{k}: {', '.join(v)}" for k, v in
                                                                               strip.items()) + ") the output "
                               + ("could not be made" if runs[-1]["rc"] != 0 else "changed")
                               + ", so the derivation uses them: move them to columns.uses")
            if not why:
                try:
                    published = _publish_outputs(runs[0]["work"], run_dir, d["outputs"])
                except (ValueError, OSError) as e:
                    why.append(f"outputs not published: {e}")
                    entry["code"] = "derivation_output_unsafe"
                else:
                    entry.update(ok=True, code=None, outputs=published)
                    rep["ran"].append(sid)
        finally:
            for r in runs:
                shutil.rmtree(r["work"], ignore_errors=True)
                shutil.rmtree(r["side"], ignore_errors=True)
        if why:
            entry["reason"] = "; ".join(why)[:1500]
            bad_outputs |= set(d["outputs"])
            rep["ok"] = False
            rep["problems"].append({"code": entry["code"], "id": sid, "claim": f"derivation {sid}: {entry['reason']}"
                                    + (f" | stderr: {entry['stderr_tail'][-400:]}" if entry["stderr_tail"] else "")})
    _retire_outputs(run_dir, old, entries)
    _write_json(run_dir, DERIVATIONS_RECORD, {"version": 1, "producer": "scripts/pipeline.py derive",
                                              "declaration_sha256": _sha(os.path.join(run_dir, DERIVATIONS)),
                                              "derivations": entries})
    return rep


def _header(path: str) -> list[str]:
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f, delimiter="\t" if path.endswith(".tsv") else ","), [])


def _ignored_columns(d: dict, run_dir: str) -> dict:
    """{staged table: [columns its `ignores` patterns name]} for a derivation's tabular inputs."""
    out = {}
    for inp, pats in ((d.get("columns") or {}).get("ignores") or {}).items():
        if inp.endswith(TABULAR) and os.path.isfile(os.path.join(run_dir, inp)):
            cols = [c for c in _header(os.path.join(run_dir, inp)) if any(path_match(c, p) for p in pats)]
            if cols:
                out[inp] = cols
    return out


def cited_paths(spec: dict, run_dir: str) -> list[str]:
    """Every path a decision spec names: by extension, by key (csv, file, path, table) and any value naming an existing
    file or directory in the run (engine config included). The deliverable's paths are outputs, not inputs. The
    evidence section names the owner-signed evidence dossier (toolkit/spec/evidence_dossier.py), not data: it is
    bound by hash in the signed input closure, not derived."""
    from_ext = (".csv", ".tsv", ".json", ".yaml", ".yml", ".txt", ".xlsx", ".parquet")
    found = []
    skip = ("deliverable", "deliverable_file", "evidence")

    def walk(x, key=""):
        if isinstance(x, dict):
            for k, v in x.items():
                if k in skip:
                    continue
                if k in ("csv", "file", "path", "table") and isinstance(v, str) and v.strip():
                    found.append(v.strip())
                walk(v, str(k))
        elif isinstance(x, list):
            for v in x:
                walk(v, key)
        elif isinstance(x, str) and x.strip():
            v = x.strip()
            if v.lower().endswith(from_ext):
                found.append(v)
            elif "/" in v or os.sep in v or key.endswith(("path", "file", "dir")):
                if ":" in v and not v.startswith("/") and "/" not in v.split(":", 1)[0]:
                    return             # a module reference such as pkg.mod:Class
                if os.path.exists(os.path.join(run_dir, os.path.expanduser(v))):
                    found.append(v)
    # a joint calibration group target's `key: {csv: <column>, units: <column>}` names columns, not a file
    cal = spec.get("calibration") if isinstance(spec, dict) else None
    joint = cal.get("joint") if isinstance(cal, dict) else None
    jt = joint.get("targets") if isinstance(joint, dict) else None
    if isinstance(jt, list):
        jt = [dict(t, key={k: v for k, v in t["key"].items() if k != "csv"})
              if isinstance(t, dict) and t.get("kind") == "group" and isinstance(t.get("key"), dict)
              and isinstance(t["key"].get("csv"), str) else t for t in jt]
        spec = dict(spec, calibration=dict(cal, joint=dict(joint, targets=jt)))
    walk(spec)
    return sorted(set(found))


def _norm_rel(p: str, run_dir: str) -> str:
    full = os.path.realpath(os.path.join(run_dir, p))
    return os.path.relpath(full, os.path.realpath(run_dir)).replace(os.sep, "/")


def derivation_problems(run_dir: str, spec: dict | None = None, required: bool = False) -> list[dict]:
    """What blocks the spec's data before SIGN (and at the gate): [{code, claim}]. With `required` (a spec the
    stage machine drafted) the units table must be a derivation output and every other data path derived or staged;
    without it the checks apply only when the run declares derivations (an opt-in for hand-written specs)."""
    run_dir = os.path.realpath(run_dir)
    out: list[dict] = []
    declared = os.path.exists(os.path.join(run_dir, DERIVATIONS))
    if not declared and not required:
        return out
    ders, ign, errs = load_derivations(run_dir) if declared else ([], {}, [])
    out += [{"code": "derivation_failed", "claim": e} for e in errs]
    entries = load_record(run_dir)["derivations"]
    made: dict = {}
    for d in ders:
        e = entries.get(d["id"])
        if isinstance(e, dict) and not e.get("ok") and _current(d, dict(e, ok=False), run_dir) is None:
            out.append({"code": e.get("code") or "derivation_failed", "claim": f"derivation {d['id']}: {e.get('reason')}"})
        else:
            why = _current(d, e, run_dir)
            if why:
                out.append({"code": "derivation_stale", "claim": f"derivation {d['id']}: {why}; the harness re-runs it"})
        for o in d["outputs"]:
            made[o] = d["id"]
    prov = _prov_inputs(run_dir)
    cited = [_norm_rel(p, run_dir) for p in cited_paths(spec, run_dir)] if isinstance(spec, dict) else []
    # every column of every staged table is used or ignored with a reason
    readers: dict = {}
    for d in ders:
        for i in d["inputs"]:
            readers.setdefault(i, []).append(d)
    for inp in sorted(prov):
        if not inp.endswith(TABULAR) or inp in ign or inp in cited or not os.path.isfile(os.path.join(run_dir, inp)):
            continue
        if inp not in readers:
            out.append({"code": "input_column_unaccounted", "claim": f"staged table {inp} is read by no derivation: "
                        f"use it, or list it in {DERIVATIONS} ignored_inputs with a reason"})
            continue
        pats = [p for d in readers[inp] for k in ("uses", "ignores")
                for p in (((d.get("columns") or {}).get(k) or {}).get(inp) or [])]
        miss = [c for c in _header(os.path.join(run_dir, inp)) if not any(path_match(c, p) for p in pats)]
        if miss:
            out.append({"code": "input_column_unaccounted", "claim": f"{inp}: column(s) neither used nor ignored with a "
                        f"reason: {', '.join(miss[:12])}" + (f" and {len(miss) - 12} more" if len(miss) > 12 else "")})
    if isinstance(spec, dict) and (required or declared):
        units = ((spec.get("decision_units") or {}).get("csv"))
        u = _norm_rel(units, run_dir) if isinstance(units, str) and units.strip() else None
        if u is None or u not in made:
            out.append({"code": "units_not_derived", "claim": f"decision_units.csv ({units}) is not the output of a "
                        f"declared derivation: a table written by hand or by an agent cannot be checked or re-made. "
                        f"Write a script under {DERIVE_PREFIX} and declare it in {DERIVATIONS}; the harness runs it"})
        for rel in cited:
            if rel == u or rel in made:
                continue
            if rel.startswith(INPUTS_PREFIX) and rel in prov:
                continue
            out.append({"code": "data_ref_not_derived", "claim": f"the spec reads {rel}, which is neither a declared "
                        f"derivation output nor a staged input"})
    return out


def derivation_findings(run_dir: str, spec: dict | None = None, required: bool = False) -> list[dict]:
    """The gate's view: derivation_problems plus derivation_unsandboxed for a record made with the development flag
    (HIGH, not declarable: such a run cannot be accepted)."""
    base = {"severity": "HIGH", "stage": "PLAN", "target_stage": "PLAN", "scope_declarable": False}
    out = [dict(base, kind=p["code"], key=p["claim"][:60], claim=p["claim"])
           for p in derivation_problems(run_dir, spec, required)]
    none = sorted(sid for sid, e in load_record(run_dir)["derivations"].items()
                  if ((e or {}).get("sandbox") or {}).get("kind") == "none")
    if none:
        out.append(dict(base, kind="derivation_unsandboxed", key="sandbox",
                        claim=f"derivation(s) {', '.join(none)} ran without a sandbox (development flag): the run cannot "
                              "be accepted; re-run them on Linux with bubblewrap"))
    return out


# ---- end of derivations -------------------------------------------------- #


# CLAUDE.md: harness code is disease-agnostic (DI13 scans the section above with this).
_DOMAIN = re.compile(r"\b(malaria\w*|nigeria\w*|lgas?|774|smc|pfpr\w*|itns?|irs|measles|dengue|tuberculosis|polio|"
                     r"influenza|hiv|vaccin\w*|mosquito\w*|plasmod\w*|dhs)\b", re.I)


def domain_terms(text: str) -> list[str]:
    """Disease, place or intervention words in harness code (CLAUDE.md: harness code is disease-agnostic)."""
    return sorted({m.group(0).lower() for m in _DOMAIN.finditer(text)})


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
        with tempfile.TemporaryDirectory() as d2:   # F6: each pipeline run has an id its steps see (MA_PIPELINE_RUN)
            d2 = os.path.realpath(d2)
            w(d2, "models/e.py", "import os\nopen('models/e.out', 'w').write(os.environ.get('MA_PIPELINE_RUN', ''))\n")
            spec(d2, [{"id": "e", "run": f"{py} models/e.py", "inputs": [], "outputs": ["models/e.out"]}])
            r2 = run(d2)
            on_disk = json.load(open(os.path.join(d2, REPORT))).get("pipeline_run")
            ok(r2.get("pipeline_run") and rd(d2, "models/e.out") == r2["pipeline_run"] == on_disk,
               f"PL3b a pipeline run's id is in its report and in its steps' environment (MA_PIPELINE_RUN): "
               f"{r2.get('pipeline_run')} {rd(d2, 'models/e.out')} {on_disk}")
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
        # Warn, don't kill (owner, 2026-11-17: two real runs died at the fixed 1 h step limit, whose analyze step
        # needs 5-6 h on the VM path). A step's timeout_s is a soft deadline; only the backstop kills.
        try:
            bk = [backstop_s(1), backstop_s(3600), backstop_s(10 * 3600), backstop_s(24 * 3600)]
        except NameError as e:
            bk = [repr(e)]
        g_ = globals()
        ok(bk == [24 * 3600, 24 * 3600, 30 * 3600, 72 * 3600] and g_.get("BACKSTOP_MULTIPLE") == 3
           and g_.get("BACKSTOP_FLOOR_S") == 24 * 3600,
           f"PL15a the backstop is 3x the soft deadline or 24 h, whichever is larger: {bk}")
        saved = {k: g_.get(k) for k in ("BACKSTOP_FLOOR_S", "PROGRESS_EVERY_S")}
        g_["BACKSTOP_FLOOR_S"], g_["PROGRESS_EVERY_S"] = 0.0, 0.5     # backstop 3 s for timeout_s=1
        try:
            # PL15c: a step past its soft deadline keeps running, writes a progress record while it runs (the step
            # copies the record it can see at 1.8 s into its output) and finishes ok.
            w(d, "models/a.py", "import time, shutil\ntime.sleep(1.8)\n"
                                "shutil.copy('pipeline_progress.json', 'models/a.seen')\n"
                                "open('models/a.out', 'w').write('5')\n")
            spec(d, [dict(three[0], timeout_s=1, outputs=["models/a.out", "models/a.seen"])] + three[1:])
            rep = run(d)
            try:
                seen = json.load(open(os.path.join(d, "models", "a.seen")))["steps"]["a"]
            except (OSError, ValueError, KeyError, TypeError):
                seen = {}
            try:
                final = json.load(open(os.path.join(d, PROGRESS)))["steps"]["a"]
            except (NameError, OSError, ValueError, KeyError, TypeError):
                final = {}
            r0 = rep["steps"][0] if rep["steps"] else {}
            ok(r0.get("status") == "ok" and r0.get("over_soft_deadline") is True
               and seen.get("state") == "running" and seen.get("soft_deadline_s") == 1
               and seen.get("backstop_s") == 3 and seen.get("elapsed_s", 0) >= 1
               and "backstop_at" in seen and final.get("state") == "ok",
               f"PL15c a step past its soft deadline is warned about, recorded while running, and finishes: "
               f"{r0} seen={seen} final={final}")
            # PL15e (Codex on #267, P1): a progress write leaves a batch's code fingerprint (compute.py: models/ and
            # data/) unchanged, so a resumed batch keeps its rows.
            import compute as _compute
            fp0 = _compute.code_fingerprint(d, "models/a.py::f")
            _progress_record(d, {"id": "z", "run": "x"}, 1.0, 3.0, time.time(), 2.0, None, "running")
            ok(_compute.code_fingerprint(d, "models/a.py::f") == fp0,
               "PL15e writing the progress record does not change a batch's code fingerprint")
            # PL15f-i (Codex re-review of #267, round 2, 4 P1):
            # f: a step whose inputs glob matches the progress record ("*.json") still finishes ok past its deadline.
            w(d, "models/g.py", "import time\ntime.sleep(1.8)\nopen('models/g.out', 'w').write('g')\n")
            w(d, "cfg.json", "{}")
            spec(d, three + [{"id": "g", "run": f"{py} models/g.py", "inputs": ["*.json"], "outputs": ["models/g.out"],
                              "timeout_s": 1}])
            rep = run(d)
            rg = next((r for r in rep["steps"] if r["id"] == "g"), {})
            ok(rg.get("status") == "ok" and rg.get("over_soft_deadline"),
               f"PL15f a step whose inputs match the progress record finishes past its soft deadline: {rg}")
            spec(d, three)
            # g: a broken stderr (the warning cannot be printed) never escapes the runner: the step finishes.
            import io
            class _Broken(io.StringIO):
                def write(self, *a):
                    raise BrokenPipeError(32, "Broken pipe")
            w(d, "models/a.py", "import time\ntime.sleep(1.8)\nopen('models/a.out', 'w').write('5')\n")
            spec(d, [dict(three[0], timeout_s=1)] + three[1:])
            real_err = sys.stderr
            sys.stderr = _Broken()
            try:
                rep = run(d)
                r0, esc = (rep["steps"][0] if rep["steps"] else {}), None
            except BaseException as e:  # noqa: BLE001
                r0, esc = {}, e
            finally:
                sys.stderr = real_err
            ok(esc is None and r0.get("status") == "ok",
               f"PL15g a warning that cannot be written (broken stderr) never escapes; the step finishes: {esc!r} {r0}")
            # h: a FIFO planted at the progress path never blocks the runner (it is never read, only replaced).
            if hasattr(os, "mkfifo"):
                try:
                    os.remove(os.path.join(d, PROGRESS))
                except OSError:
                    pass
                os.mkfifo(os.path.join(d, PROGRESS))
                w(d, "models/a.py", "import time\ntime.sleep(1.8)\nopen('models/a.out', 'w').write('6')\n")
                t1 = time.time()
                import threading
                box = {}
                th = threading.Thread(target=lambda: box.update(rep=run(d)), daemon=True)
                th.start()
                th.join(20)
                ok(not th.is_alive() and (box.get("rep") or {}).get("steps", [{}])[0].get("status") == "ok",
                   f"PL15h a FIFO at the progress path does not block the runner ({time.time() - t1:.0f}s, {box})")
            # i: a timeout_s that is not finite is a validation error (a NaN backstop would never be reached).
            spec(d, [dict(three[0], timeout_s=float("nan"))] + three[1:])
            errs_nan = load(d)[1]
            spec(d, [dict(three[0], timeout_s=float("inf"))] + three[1:])
            errs_inf = load(d)[1]
            ok(any("timeout_s" in e for e in errs_nan) and any("timeout_s" in e for e in errs_inf),
               f"PL15i a NaN or infinite timeout_s is refused: {errs_nan} {errs_inf}")
            spec(d, [dict(three[0], timeout_s=1)] + three[1:])
            # PL15j-l (Codex round 3 on #267, 3 P1):
            # j: the progress record alone (also through a link) never satisfies a step's input glob: it is missing.
            for f_ in ("cfg.json",):
                try:
                    os.remove(os.path.join(d, f_))
                except OSError:
                    pass
            _write_json(d, PROGRESS, {"steps": {}})
            os.makedirs(os.path.join(d, "lnk"), exist_ok=True)
            try:
                os.symlink(os.path.join(d, PROGRESS), os.path.join(d, "lnk", "p.json"))
            except OSError:
                pass
            fpj = fingerprint({"id": "j", "run": "true", "inputs": ["*.json", "lnk/*.json"], "outputs": ["x"]}, d)
            ok(sorted(fpj["missing_inputs"]) == ["*.json", "lnk/*.json"] and not fpj["inputs"],
               f"PL15j the progress record (or a link to it) is never an input, and alone it leaves the glob missing: "
               f"{fpj['missing_inputs']} {fpj['inputs']}")
            shutil.rmtree(os.path.join(d, "lnk"), ignore_errors=True)
            # k: a stderr that blocks (a full pipe whose reader stays open) never holds the backstop: the step is
            # killed at its 3 s backstop although each warning write blocks for 8 s.
            class _Slow(io.StringIO):
                def write(self, *a):
                    time.sleep(8)
                    return 0
            w(d, "models/a.py", "import time\ntime.sleep(30)\n")
            spec(d, [dict(three[0], timeout_s=1)] + three[1:])
            real_err, t1 = sys.stderr, time.time()
            sys.stderr = _Slow()
            try:
                rep = run(d)
            finally:
                sys.stderr = real_err
            took = time.time() - t1
            ok(rep["steps"][0]["returncode"] is None and took < 3 + KILL_GRACE_S + 4,
               f"PL15k a blocking stderr does not delay the backstop kill ({took:.1f}s): {rep['steps'][0].get('error')}")
            # l: a later step that overwrites the progress record cannot have its bytes certified as the runner's:
            # the runner republishes its own record before the run ends.
            w(d, "models/a.py", "import time\ntime.sleep(1.8)\nopen('models/a.out', 'w').write('7')\n")
            w(d, "models/b.py", "open('pipeline_progress.json', 'w').write('forged')\n"
                                "v = int(open('models/a.out').read()) + int(open('data/y.csv').read())\n"
                                "open('models/b.out', 'w').write(str(v))\n")
            spec(d, [dict(three[0], timeout_s=1)] + three[1:])
            rep = run(d)
            try:
                after = json.load(open(os.path.join(d, PROGRESS)))
            except (OSError, ValueError):
                after = {}
            ok(after.get("pipeline_run") == rep.get("pipeline_run") and "a" in (after.get("steps") or {}),
               f"PL15l the progress record on disk is the runner's own at the end of the run: {str(after)[:160]}")
            w(d, "models/b.py", "v = int(open('models/a.out').read()) + int(open('data/y.csv').read())\n"
                                "open('models/b.out', 'w').write(str(v))\n")
            # PL15m-p (Codex round 4 on #267, in-scope P1s):
            # m: a warning thread the OS refuses to start never kills the step.
            import contextlib
            import io
            from unittest import mock as _mock
            slow = [sys.executable, "-c", "import time; time.sleep(1.5)"]
            with _mock.patch.object(threading.Thread, "start",
                                    lambda self: (_ for _ in ()).throw(RuntimeError("can't start new thread"))):
                try:
                    res_m = _run_cmd(slow, d, 20, None, soft_s=0.3, on_overrun=lambda el: None)
                except BaseException as e:  # noqa: BLE001
                    res_m = e
            ok(isinstance(res_m, tuple) and res_m[0] == 0,
               f"PL15m a warning thread that cannot start never kills the step: {res_m!r}")
            # n: a 'running' report that serialized before the final state never lands after it.
            real_wj, gate_n = _write_json, threading.Event()

            def slow_wj(run_dir, rel, obj):
                snap = json.loads(json.dumps(obj))
                if (snap.get("steps", {}).get("race") or {}).get("state") == "running":
                    gate_n.set()
                    time.sleep(1.0)
                return real_wj(run_dir, rel, snap)
            t_n = time.time()
            with _mock.patch.dict(globals(), {"_write_json": slow_wj}), \
                    contextlib.redirect_stderr(io.StringIO()):
                th_n = threading.Thread(target=_progress_record, daemon=True,
                                        args=(d, {"id": "race", "run": "x"}, 1.0, 3.0, t_n, 2.0, None, "running"))
                th_n.start()
                gate_n.wait(5)
                _progress_record(d, {"id": "race", "run": "x"}, 1.0, 3.0, t_n, 2.5, None, "ok")
                th_n.join(5)
            try:
                st_n = (json.load(open(os.path.join(d, PROGRESS))).get("steps") or {}).get("race", {}).get("state")
            except (OSError, ValueError):
                st_n = None
            ok(st_n == "ok", f"PL15n a late 'running' report never replaces the final state on disk: {st_n}")
            # o: output that is not UTF-8 never hides the resumable kill message.
            junk = [sys.executable, "-c", "import sys, time; sys.stderr.buffer.write(b'\\xff\\xfe'); "
                                          "sys.stderr.flush(); time.sleep(30)"]
            try:
                res_o = _run_cmd(junk, d, 2, None)
            except BaseException as e:  # noqa: BLE001
                res_o = e
            ok(isinstance(res_o, tuple) and res_o[0] is None and res_o[1].startswith("timed out after "),
               f"PL15o non-UTF-8 output never hides the resumable kill message: {res_o!r}")
            # p: an accepted finite timeout far beyond the OS poller's limit (30 days) never kills the step at once.
            try:
                res_p = _run_cmd([sys.executable, "-c", "pass"], d, backstop_s(2592000.0), None,
                                 soft_s=2592000.0, on_overrun=lambda el: None)
            except BaseException as e:  # noqa: BLE001
                res_p = e
            ok(isinstance(res_p, tuple) and res_p[0] == 0,
               f"PL15p a 30-day soft deadline never kills a step at once: {res_p!r}")
            # PL15: past the backstop it is killed, and reads as a timeout (stage_machine.resumable_failure).
            w(d, "models/a.py", "import time\ntime.sleep(30)\n")
            t1 = time.time()
            spec(d, [dict(three[0], timeout_s=1)] + three[1:])
            rep = run(d)
            ok(rep["steps"][0]["returncode"] is None and time.time() - t1 < 15
               and rep["steps"][0]["error"].startswith("timed out after ") and "backstop" in rep["steps"][0]["error"],
               f"PL15 a step past its backstop is killed: {rep['steps']}")
            # PL15b (v2 analyze timeout, 2026-11-02): a process that left the step's group (as compute.py's pool
            # processes do) and keeps its pipes open must not hang the runner after the kill.
            w(d, "models/a.py", "import subprocess, sys, time\n"
                                "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], start_new_session=True)\n"
                                "open('models/a.pid', 'w').write(str(c.pid))\ntime.sleep(30)\n")
            t1 = time.time()
            rep = run(d)
            took = time.time() - t1
            try:
                os.kill(int(open(os.path.join(d, "models", "a.pid")).read()), signal.SIGKILL)
            except (OSError, ValueError):
                pass
            ok(rep["steps"][0]["returncode"] is None and took < 3 + KILL_GRACE_S + 10,
               f"PL15b a timed-out step whose escaped child holds its pipes ends within the grace ({took:.0f}s)")
            # PL15d: a compute step's batch runs to the backstop, not the soft deadline.
            got, real_run_cmd = [], g_["_run_cmd"]
            g_["_run_cmd"] = lambda cmd, cwd, t, env, shell=False, **k: got.append((cmd, t, k.get("soft_s"))) or (0, "")
            try:
                _exec_step({"id": "k", "run": "compute: models/k.py::f", "inputs_jsonl": "models/k.jsonl",
                            "out": "models/k.out", "purpose": "p"}, d, backstop_s(1.0), None, soft_s=1.0)
            except TypeError as e:
                got.append(repr(e))
            finally:
                g_["_run_cmd"] = real_run_cmd
            cmd = got[0][0] if got and isinstance(got[0], tuple) else []
            ok(cmd and cmd[cmd.index("--timeout-s") + 1] == "3.0" and got[0][1:] == (63.0, 1.0),
               f"PL15d a compute step's batch gets the backstop as its wall limit: {got}")
        finally:
            g_.update(saved)
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
        saved = globals().get("BACKSTOP_FLOOR_S")
        globals()["BACKSTOP_FLOOR_S"] = 0.0          # killed at its 3 s backstop (PL15a)
        try:
            rep = run(d)
        finally:
            globals()["BACKSTOP_FLOOR_S"] = saved
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
    try:
        _self_test_durability(ok)
    except Exception as e:  # noqa: BLE001 -- before the fix exists (step α) this is the failure
        print(f"FAIL PD durability tests: {type(e).__name__}: {e}")
        ok(False, f"PD durability tests: {type(e).__name__}: {e}")
    try:
        _self_test_derivations(ok)
    except Exception as e:  # noqa: BLE001 -- before the runner exists (step α) this is the failure
        print(f"FAIL DI derivation tests: {type(e).__name__}: {e}")
        ok(False, f"DI derivation tests: {type(e).__name__}: {e}")
    # PL-RW3 (round 4, P1-1): retiring outputs of a derivation no longer declared never removes outside the run dir
    with tempfile.TemporaryDirectory() as root:
        rd = os.path.join(root, "a", "b", "run")
        os.makedirs(os.path.join(rd, "data", "derived"))
        victim = os.path.join(root, "victim.md")
        open(victim, "w").write("keep me")
        keep = os.path.join(rd, "data", "derived", "old.csv")
        open(keep, "w").write("old\n")
        rel = "data/derived/" + os.path.relpath(victim, os.path.join(rd, "data", "derived"))
        try:
            _retire_outputs(rd, {"gone": {"outputs": {rel: "x", "data/derived/old.csv": "y"}}}, {})
            r = None
        except NameError as e:
            r = e
        ok(r is None and os.path.exists(victim) and not os.path.exists(keep),
           f"PL-RW3 an old record's ../ output name is not removed outside the run dir; a real retired output is ({r})")
    # PL-RW1/2 (run console review round 3, P1-2): harness publication in a run dir never follows a link
    with tempfile.TemporaryDirectory() as rd, tempfile.TemporaryDirectory() as out:
        victim = os.path.join(out, "victim")
        open(victim, "w").write("untouched")
        os.makedirs(os.path.join(rd, "data"))
        os.symlink(victim, os.path.join(rd, "data", "derivations_record.json.tmp"))
        try:
            _write_json(rd, "data/derivations_record.json", {"x": 1})
        except (ValueError, OSError):
            pass
        ok(open(victim).read() == "untouched", "PL-RW1 a planted .tmp link is not written through by _write_json")
        work = os.path.join(rd, "_work")
        os.makedirs(os.path.join(work, "data", "derived"))
        os.makedirs(os.path.join(rd, "data", "derived"))
        secret = os.path.join(out, "secret.csv")
        open(secret, "w").write("SECRET\n")
        os.symlink(secret, os.path.join(work, "data", "derived", "a.csv"))
        open(os.path.join(work, "data", "derived", "b.csv"), "w").write("b\n")
        os.symlink(victim, os.path.join(rd, "data", "derived", "b.csv.tmp"))
        res = None
        try:
            res = _publish_outputs(work, rd, ["data/derived/a.csv", "data/derived/b.csv"])
        except (ValueError, OSError, NameError) as e:
            res = e
        a = os.path.join(rd, "data", "derived", "a.csv")
        ok(not isinstance(res, NameError) and open(victim).read() == "untouched"
           and not (os.path.exists(a) and "SECRET" in open(a).read()),
           f"PL-RW2 derivation outputs are published without following a link in the work dir or a planted .tmp ({res})")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: all self-test cases passed.")
    return 0


_DI_GOOD = '''import argparse
import csv
ap = argparse.ArgumentParser()
ap.add_argument("--inputs", nargs="+")
ap.add_argument("--outputs", nargs="+")
a = ap.parse_args()
rows = list(csv.DictReader(open(a.inputs[0], newline="")))
with open(a.outputs[0], "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["id", "x", "pop"])
    for r in rows:
        w.writerow([r["id"], round(float(r["a"]) - float(r["b"]), 6), r["pop"]])
{extra}
'''


def di_fixture(d: str, extra: str = "", uses=("id", "a", "b", "pop"), ignores=None, n: int = 6) -> str:
    """A run directory with one staged input (recorded in the provenance, as stage_inputs does), one derivation
    script and its declaration. `extra` is appended to the script (a planted defect); `ignores`: {column: reason}."""
    import yaml
    os.makedirs(os.path.join(d, "data", "inputs"), exist_ok=True)
    os.makedirs(os.path.join(d, "data", "derive"), exist_ok=True)
    raw = os.path.join(d, "data", "inputs", "raw.csv")
    with open(raw, "w") as f:
        f.write("id,a,b,c,pop\n" + "".join(f"u{i},{i * 0.5:.2f},{i * 0.1:.2f},{i % 3},{1000 + i}\n" for i in range(n)))
    with open(os.path.join(d, "data", "inputs_provenance.json"), "w") as f:
        json.dump({"inputs": {"data/inputs/raw.csv": {"source": "fixture/raw.csv", "sha256": _sha(raw)}}, "cited": {}}, f)
    with open(os.path.join(d, "data", "derive", "units.py"), "w") as f:
        f.write(_DI_GOOD.replace("{extra}", extra))
    cols = {"uses": {"data/inputs/raw.csv": list(uses)},
            "ignores": {"data/inputs/raw.csv": dict({"c": "a label the units table does not need"} if ignores is None
                                                    else ignores)}}
    with open(os.path.join(d, DERIVATIONS), "w") as f:
        yaml.safe_dump({"derivations": [{"id": "units", "script": "data/derive/units.py",
                                         "inputs": ["data/inputs/raw.csv"], "outputs": ["data/derived/units.csv"],
                                         "columns": cols}]}, f, sort_keys=False)
    return d


def _self_test_durability(ok) -> None:
    """PD1-PD10 (v2 autonomy 1D-i): a graceful stop at the backstop, step logs, and the steps' environment."""
    from unittest import mock as _mock
    py = shlex.quote(sys.executable)

    def w(d, rel, txt):
        os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
        with open(os.path.join(d, rel), "w") as f:
            f.write(txt)

    def one(d, body, cmd=None, **kw):
        import yaml
        w(d, "data/x.csv", "1\n")
        w(d, "models/a.py", body)
        step = dict({"id": "a", "run": cmd or f"{py} models/a.py", "inputs": ["data/x.csv"],
                     "outputs": ["models/a.out"]}, **kw)
        w(d, SPEC, yaml.safe_dump({"steps": [step]}, sort_keys=False))
        rep = run(d)
        return rep["steps"][0] if rep.get("steps") else {"error": str(rep.get("errors"))}

    def rd(d, rel):
        try:
            with open(os.path.join(d, rel), errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    log = "models/logs/a.log"
    fast = {"BACKSTOP_FLOOR_S": 1.0, "PROGRESS_EVERY_S": 0.5, "TERM_GRACE_S": 6.0}
    with tempfile.TemporaryDirectory() as d, _mock.patch.dict(globals(), fast):
        d = os.path.realpath(d)
        # PD1: at the backstop the step's group gets SIGTERM and time to flush before any SIGKILL. The command is a
        # compound one, so the shell (the group leader) ends at once while the process that flushes is still running.
        flush = ("import signal, sys, time\n"
                 "def bye(*a):\n    time.sleep(1.0)\n    open('models/flushed', 'w').write('rows')\n    sys.exit(0)\n"
                 "signal.signal(signal.SIGTERM, bye)\nprint('before-hang', flush=True)\ntime.sleep(60)\n")
        t0 = time.time()
        r = one(d, flush, cmd=f"{py} models/a.py && true", timeout_s=1)
        err = r.get("error") or ""
        ok(r.get("returncode") is None and rd(d, "models/flushed") == "rows" and time.time() - t0 < 12,
           f"PD1 a step past its backstop gets SIGTERM and a grace period to flush: {r}")
        ok(err.startswith("timed out after ") and "backstop" in err and "SIGTERM" in err,
           f"PD1b the timeout still reads as a resumable timeout and says how the step was stopped: {err[:200]}")
        # PD4: the output before the stop is in the step's log, and its tail is in the error.
        ok("before-hang" in rd(d, log) and "before-hang" in err and r.get("log") == log,
           f"PD4 a timed-out step's output is kept in {log} and its tail in the error: {r} {rd(d, log)[:200]!r}")
        # PD2: a step that ignores SIGTERM is killed once the grace is over, and the runner does not wait longer.
        with _mock.patch.dict(globals(), {"TERM_GRACE_S": 1.0}):
            t0 = time.time()
            r = one(d, "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n",
                    timeout_s=1)
            took = time.time() - t0
        ok(r.get("returncode") is None and 3.5 < took < 3 + 1 + KILL_GRACE_S and "SIGKILL" in (r.get("error") or ""),
           f"PD2 a step that ignores SIGTERM is killed after the grace ({took:.1f}s): {r.get('error')}")
        # PD3: a failed step's stdout and stderr are both in its log, with the tail in the error.
        r = one(d, "import sys\nprint('out-line', flush=True)\nprint('err-line', file=sys.stderr)\nsys.exit(3)\n")
        txt = rd(d, log)
        ok(r.get("returncode") == 3 and "out-line" in txt and "err-line" in txt and "err-line" in (r.get("error") or "")
           and r.get("log") == log, f"PD3 a failed step's stdout and stderr are in {log}: {r} {txt[-200:]!r}")
        ok("before-hang" in txt, f"PD3b the log is appended to, never truncated, across runs: {txt[:200]!r}")
        # PD5: the log is never a step input, and never part of the compute code fingerprint.
        w(d, "models/logs/zz.log", "x\n")
        w(d, "models/logs/table.csv", "1\n")
        fp = fingerprint({"id": "a", "run": f"{py} models/a.py models/logs/a.log",
                          "inputs": ["data/x.csv", "models/logs/*", "models/logs/*.log"], "outputs": []}, d)
        ok(sorted(k for k in fp["inputs"] if k.startswith("models/logs/")) == ["models/logs/table.csv"]
           and fp["missing_inputs"] == ["models/logs/*.log"] and "models/logs/a.log" not in fp["code"],
           f"PD5 a step log is never a step input or code; another file beside it still is: {fp}")
        import compute as _compute
        w(d, "models/f.py", "def f(p):\n    return p\n")
        f0 = _compute.code_fingerprint(d, "models/f.py::f")
        w(d, "models/logs/zz.log", "more\n")
        ok(_compute.code_fingerprint(d, "models/f.py::f") == f0,
           "PD5b a step log does not change the compute code fingerprint (stored rows stay reusable)")
        # PD6: a link planted at the log path is never followed; the step still runs, and the row says so.
        w(d, "target.txt", "keep")
        if os.path.lexists(os.path.join(d, log)):
            os.remove(os.path.join(d, log))
        os.symlink(os.path.join(d, "target.txt"), os.path.join(d, log))
        r = one(d, "import sys\nprint('x')\nprint('err-link', file=sys.stderr)\nsys.exit(4)\n")
        ok(r.get("returncode") == 4 and rd(d, "target.txt") == "keep" and "err-link" in (r.get("error") or "")
           and r.get("log_error"), f"PD6 a link at the log path is not followed and the step still runs: {r}")
        # PD6b (Codex round 1): the fallback keeps the tail on a timeout too.
        r = one(d, "import sys, time\nprint('err-hang', file=sys.stderr, flush=True)\ntime.sleep(60)\n", timeout_s=1)
        ok(r.get("returncode") is None and "err-hang" in (r.get("error") or "") and r.get("log_error"),
           f"PD6b without a log, a timed-out step's stderr tail is still in the error: {r}")
        os.remove(os.path.join(d, log))
        # PD11 (Codex round 1): a FIFO at the log path never blocks the runner: the step runs without a log.
        os.mkfifo(os.path.join(d, log))
        got = []
        th = threading.Thread(target=lambda: got.append(one(d, "import sys\nsys.exit(5)\n")), daemon=True)
        th.start()
        th.join(20)
        hung = th.is_alive()
        if hung:                             # unblock the open so the thread ends
            fd = os.open(os.path.join(d, log), os.O_RDONLY | os.O_NONBLOCK)
            th.join(10)
            os.close(fd)
        ok(not hung and bool(got) and got[0].get("returncode") == 5 and got[0].get("log_error"),
           f"PD11 a FIFO at the log path never blocks the runner (blocked: {hung}): {got}")
        os.remove(os.path.join(d, log))
        # PD12 (Codex round 1): the runner's own log never counts as a step's declared output.
        r = one(d, "pass\n", outputs=["models/logs/*.log"])
        ok(r.get("status") == "failed" and "not produced" in (r.get("error") or "")
           and os.path.isfile(os.path.join(d, log)),
           f"PD12 a step's log never satisfies a declared output, and a failed step's log is kept: {r}")
        # PD13 (Codex round 1): an interrupt during the grace still kills the step's group.
        c = subprocess.Popen([sys.executable, "-c", "import signal, sys, time\n"
                              "signal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('up', flush=True)\ntime.sleep(60)"],
                             stdout=subprocess.PIPE, start_new_session=True)
        c.stdout.readline()
        with _mock.patch.object(time, "sleep", side_effect=KeyboardInterrupt):
            try:
                _stop_group(c, 5.0)
                esc = None
            except KeyboardInterrupt as e:
                esc = e
        try:
            rc13 = c.wait(timeout=5)
        except subprocess.TimeoutExpired:
            rc13 = "alive"
            c.kill()
        c.stdout.close()
        ok(esc is not None and rc13 == -signal.SIGKILL,
           f"PD13 an interrupt during the SIGTERM grace still kills the step's group: {esc!r} {rc13}")
        # PD7: the run id is reset when run() ends, so a later step outside run() never exports a past run's id.
        r = one(d, "open('models/a.out', 'w').write('1')\n")
        ok(r.get("status") == "ok" and globals()["_RUN_ID"] is None,
           f"PD7 run() resets its run id: {globals()['_RUN_ID']} {r}")
        with _mock.patch.dict(globals(), {"load": lambda rd_: (_ for _ in ()).throw(ValueError("boom"))}):
            try:
                run(d)
            except ValueError:
                pass
        ok(globals()["_RUN_ID"] is None, f"PD7b run() resets its run id when it raises: {globals()['_RUN_ID']}")
        # PD7c (Codex round 1): a run that ends never clears an id that is not its own.

        def other(rd_):
            globals()["_RUN_ID"] = "another-run"
            raise ValueError("boom")
        with _mock.patch.dict(globals(), {"load": other}):
            try:
                run(d)
            except ValueError:
                pass
            seen = globals()["_RUN_ID"]          # read here: patch.dict puts the globals back on exit
        ok(seen == "another-run", f"PD7c run() leaves another run's id alone: {seen}")
    # PD8-PD10: the steps' environment.
    env = script_env({"ANTHROPIC_API_KEY": "k", "ANTHROPIC_AUTH_TOKEN": "t", "CLAUDE_CODE_OAUTH_TOKEN": "o",
                      "HOME": "/h", "PATH": "/pd-bin::/usr/bin"})
    ok(not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"} & set(env) and env.get("HOME") == "/h",
       f"PD8 steps run without the model API credentials: {sorted(env)}")
    here = os.path.dirname(os.path.abspath(sys.executable))
    ok(env.get("PATH", "").split(os.pathsep) == [here, "/pd-bin", "", "/usr/bin"],
       f"PD9 the harness interpreter's directory is first on the steps' PATH: {env.get('PATH')}")
    env2 = script_env({"PATH": os.pathsep.join([here, "/pd-bin"])})
    ok(env2["PATH"].split(os.pathsep) == [here, "/pd-bin"]
       and script_env({"HOME": "/h"}).get("PATH") == here + os.pathsep + os.defpath,
       f"PD10 the PATH entry is added once, and before the default path when PATH is unset: {env2['PATH']}")



def _self_test_derivations(ok) -> None:
    """DI2-DI6, DI10, DI13 and the fail-closed path (docs/design/v2-derived-inputs.md §4). DI2-DI5 need the Linux
    sandbox; without one they are reported SKIP, or FAIL when MA_REQUIRE_SANDBOX=1 (the VM run)."""
    def rep(c, m):
        print(("PASS " if c else "FAIL ") + m)
        ok(c, m)

    def codes(r):
        return [p.get("code") for p in (r or {}).get("problems") or []]

    try:
        kind = sandbox_kind()
    except Exception as e:  # noqa: BLE001 -- the tests below report what is missing
        kind = None
        rep(False, f"DI0 sandbox_kind() is callable: {type(e).__name__}: {e}")
    need = os.environ.get("MA_REQUIRE_SANDBOX") == "1"
    # The fail-closed path (owner decision 1): no sandbox, no run, unless the development flag is given.
    if kind is None:
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(di_fixture(d))
            try:
                r = derive(d)
                rep("derivation_unsandboxed" in codes(r) and not os.path.exists(os.path.join(d, "data/derived/units.csv")),
                    f"DI-U1 no sandbox on {sys.platform}: derivations fail closed (derivation_unsandboxed) {codes(r)}")
                r = derive(d, unsandboxed_ok=True)
                recd = json.load(open(os.path.join(d, DERIVATIONS_RECORD)))
                kinds = [f["kind"] for f in derivation_findings(d)]
                sev = {f["kind"]: (f["severity"], f["scope_declarable"]) for f in derivation_findings(d)}
                rep(r["ok"] and recd["derivations"]["units"]["sandbox"]["kind"] == "none" and "derivation_unsandboxed" in kinds
                    and sev["derivation_unsandboxed"] == ("HIGH", False),
                    f"DI-U2 the development flag runs them, records sandbox none, and leaves a HIGH non-declarable "
                    f"finding (no ACCEPT): {codes(r)} {sev}")
            except Exception as e:  # noqa: BLE001
                rep(False, f"DI-U1/U2 fail-closed path: {type(e).__name__}: {e}")
    sandboxed = kind is not None
    if not sandboxed:
        msg = f"no sandbox on this machine ({sys.platform})"
        for t in ("DI2 a socket fails inside the sandbox", "DI3 writes to an input or outside the output directory fail",
                  "DI4 memory, CPU and file-size caps"):
            if need:
                rep(False, f"{t}: {msg} (MA_REQUIRE_SANDBOX=1)")
            else:
                print(f"SKIP {t}: {msg}")
    else:
        lim = {"wall_s": 120}
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(di_fixture(d, extra="import socket\nsocket.create_connection(('1.1.1.1', 53), timeout=5)"))
            r = derive(d, limits=lim)
            e = json.load(open(os.path.join(d, DERIVATIONS_RECORD)))["derivations"]["units"]
            rep(codes(r) == ["derivation_failed"] and not e["ok"] and "unreachable" in (e["reason"] + e["stderr_tail"]).lower(),
                f"DI2 a derivation that opens a socket fails in the sandbox, the reason recorded: {codes(r)} "
                f"{(e['reason'] + ' | ' + e['stderr_tail'][-160:])!r}")
        for label, extra in (("an input", "open(a.inputs[0], 'a').write('x')"),
                             ("outside its output directory", "open('/tmp/di3_escape.txt', 'w').write('x')"),
                             ("into the run directory", "open({run!r} + '/data/units.csv', 'w').write('x')"),
                             ("an undeclared output", "open('data/derived/extra.csv', 'w').write('x')")):
            with tempfile.TemporaryDirectory() as d:
                d = os.path.realpath(d)
                di_fixture(d, extra=extra.replace("{run!r}", repr(d)))
                before = _sha(os.path.join(d, "data/inputs/raw.csv"))
                r = derive(d, limits=lim)
                rep(codes(r) == ["derivation_failed"] and _sha(os.path.join(d, "data/inputs/raw.csv")) == before
                    and not os.path.exists(os.path.join(d, "data/units.csv")) and not os.path.exists("/tmp/di3_escape.txt")
                    and not os.path.exists(os.path.join(d, "data/derived/units.csv")),
                    f"DI3 a derivation that writes {label} fails and the input's hash is unchanged: {codes(r)}")
        for cap, extra, lim2 in (("memory_bytes", "b = bytearray(3 << 30)", {"memory_bytes": 768 << 20}),
                                 ("cpu_s", "while True:\n    pass", {"cpu_s": 2}),
                                 ("file_bytes", "open(a.outputs[0], 'w').write('x' * (8 << 20))", {"file_bytes": 1 << 20}),
                                 ("wall_s", "import time\ntime.sleep(90)", {"wall_s": 3})):
            with tempfile.TemporaryDirectory() as d:
                d = os.path.realpath(di_fixture(d, extra=extra))
                r = derive(d, limits=dict(lim, **lim2))
                e = json.load(open(os.path.join(d, DERIVATIONS_RECORD)))["derivations"]["units"]
                rep(codes(r) == ["derivation_failed"] and e.get("cap") == cap and cap in e["reason"],
                    f"DI4 the {cap} cap kills a derivation and the record names it: {codes(r)} cap={e.get('cap')} "
                    f"{e['reason'][:120]!r}")
    flag = not sandboxed          # DI5, DI6 and DI10 run everywhere: unsandboxed (flag) where there is no sandbox
    for label, extra in (("an unseeded random", "import random\nopen(a.outputs[0], 'a').write(str(random.random()))"),
                         ("a time-dependent", "import time\nopen(a.outputs[0], 'a').write(str(time.time_ns()))")):
        with tempfile.TemporaryDirectory() as d:
            d = os.path.realpath(di_fixture(d, extra=extra))
            try:
                r = derive(d, unsandboxed_ok=flag)
                rep(codes(r) == ["derivation_nondeterministic"] and not os.path.exists(os.path.join(d, "data/derived/units.csv")),
                    f"DI5 {label} derivation gives derivation_nondeterministic: {codes(r)}")
            except Exception as e:  # noqa: BLE001
                rep(False, f"DI5 {label} derivation: {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(di_fixture(d))
        try:
            r = derive(d, unsandboxed_ok=flag)
            out = os.path.join(d, "data/derived/units.csv")
            clean = [p["code"] for p in derivation_problems(d)]
            rep(r["ok"] and os.path.exists(out) and clean == [] and r["ran"] == ["units"],
                f"DI6a a correct derivation runs (twice) and its outputs are current: {codes(r)} {clean}")
            r2 = derive(d, unsandboxed_ok=flag)
            rep(r2["ok"] and r2["ran"] == [] and r2["up_to_date"] == ["units"],
                f"DI6b nothing changed: nothing re-runs ({r2['ran']})")
            for what, path, txt in (("its output", "data/derived/units.csv", "id,x,pop\nu0,9,9\n"),
                                    ("its script", "data/derive/units.py", None),
                                    ("an input", "data/inputs/raw.csv", None)):
                p = os.path.join(d, path)
                orig = open(p).read()
                open(p, "w").write(txt if txt is not None else orig + "\n# edited\n")
                got = [x["code"] for x in derivation_problems(d)]
                if path == "data/inputs/raw.csv":
                    open(p, "w").write(orig)            # restore the staged input (its provenance hash)
                    with open(os.path.join(d, "data/derive/units.py"), "a") as f:
                        f.write("")
                r3 = derive(d, unsandboxed_ok=flag)
                after = [x["code"] for x in derivation_problems(d)]
                rep("derivation_stale" in got and r3["ok"] and after == [],
                    f"DI6 editing {what} gives derivation_stale and re-running clears it: {got} -> {after}")
        except Exception as e:  # noqa: BLE001
            rep(False, f"DI6 staleness: {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory() as d:          # DI6e: a resume past SIGN on a machine without a sandbox
        d = os.path.realpath(di_fixture(d))
        try:
            derive(d, unsandboxed_ok=flag)
            rp = os.path.join(d, DERIVATIONS_RECORD)
            rec = json.load(open(rp))
            rec["derivations"]["units"]["sandbox"]["kind"] = "bwrap"      # as if made on the VM
            json.dump(rec, open(rp, "w"))
            saved = list(_SANDBOX)
            _SANDBOX[:] = [None]
            try:
                r = derive(d)
            finally:
                _SANDBOX[:] = saved
            rep(r["ok"] and r["up_to_date"] == ["units"] and not r["problems"],
                f"DI6e a current sandboxed record needs no sandbox to be verified (a resumed run only checks hashes): "
                f"{codes(r)} {r['up_to_date']}")
        except Exception as e:  # noqa: BLE001
            rep(False, f"DI6e: {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(di_fixture(d, ignores={}))
        try:
            got = [p["code"] for p in derivation_problems(d)]
            rep("input_column_unaccounted" in got and any("c" in p["claim"] for p in derivation_problems(d)),
                f"DI10a an input column neither used nor ignored gives input_column_unaccounted: {got}")
        except Exception as e:  # noqa: BLE001
            rep(False, f"DI10a column accounting: {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(di_fixture(d, uses=("id", "a", "pop"), ignores={"b": "said to be unused", "c": "label"}))
        try:
            r = derive(d, unsandboxed_ok=flag)
            rep(codes(r) == ["ignored_column_used"], f"DI10b an ignored column whose removal changes the output gives "
                                                     f"ignored_column_used: {codes(r)}")
        except Exception as e:  # noqa: BLE001
            rep(False, f"DI10b ignored column used: {type(e).__name__}: {e}")
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(di_fixture(d, extra="import scipy"))
        try:
            r = derive(d, unsandboxed_ok=flag)
            e = json.load(open(os.path.join(d, DERIVATIONS_RECORD)))["derivations"]["units"]
            rep(codes(r) == ["derivation_failed"] and "scipy" in e["reason"],
                f"DI2b an import outside the allowlist (standard library, numpy, pandas, pyyaml) is refused before the run: "
                f"{codes(r)} {e['reason'][:100]!r}")
        except Exception as e:  # noqa: BLE001
            rep(False, f"DI2b import allowlist: {type(e).__name__}: {e}")
    # DI13: the new harness code and its messages name no disease, place or intervention (CLAUDE.md convention).
    try:
        src = open(os.path.abspath(__file__), encoding="utf-8").read()
        a, b = src.index("# ---- derivations (v2-derived-inputs)"), src.index("# ---- end of derivations")
        rep(not domain_terms(src[a:b]), f"DI13 the derivation runner names no disease, place or intervention: "
                                         f"{domain_terms(src[a:b])}")
    except Exception as e:  # noqa: BLE001
        rep(False, f"DI13 domain terms: {type(e).__name__}: {e}")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["_derive_child"]:          # the launcher inside a derivation's sandbox
        return _child(argv[1:])
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
