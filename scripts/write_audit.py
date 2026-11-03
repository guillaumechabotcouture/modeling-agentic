#!/usr/bin/env python3
"""Every file write, rename and removal in the production tree is through lib.safe_io or reviewed (AST-based).

    python3 scripts/write_audit.py              # list the write sites and their status
    python3 scripts/write_audit.py --self-test

Trust model (run console review rounds 2-5): agent-written artifacts in a run dir may be hostile (links, planted
`.tmp` names, paths inside records). The harness writes in a run dir through scripts/lib/safe_io.py
(`write_nofollow`, `open_append_nofollow`, `remove_nofollow`: no link followed at any component, random temporary
names, atomic publish). This audit parses every tracked Python file under agents/, scripts/, toolkit/ and tools/
(the production tree) and finds, through the module's import aliases, every call that writes, renames or removes a
file by path:

  open / io.open / codecs.open / os.fdopen / <path>.open    with a constant write mode (w, a, x, +), or any
                                                           non-constant mode (it may write)
  <path>.write_text / .write_bytes, <array>.tofile, <frame>.to_csv / to_parquet / to_json / to_excel / to_pickle,
  <figure>.savefig
  os.open                                                  with O_WRONLY, O_RDWR, O_CREAT, O_APPEND or O_TRUNC
  os.remove / unlink / rmdir / removedirs / rename / renames / replace / truncate / symlink / link
  shutil.copy / copy2 / copyfile / copytree / move / rmtree
  numpy.save / savetxt / savez / savez_compressed          and pickle/marshal/joblib dumps
  json/yaml/pickle dump to a handle that is not a plain name (a call such as os.fdopen(...))
  tempfile.mkstemp / mkdtemp / NamedTemporaryFile / TemporaryFile / SpooledTemporaryFile with dir= (a chosen
                                                           destination; without dir= it is the system's temp dir)
  getattr(<x>, "open" / a writer name)                    a writer fetched by name (getattr(builtins, "open"))
  functools.partial(open, <write or unknown mode>) / partial(os.remove, ...) and the other writers
  <name> = open / os.remove / ...                          an assigned alias: later calls of <name> are that writer
  subprocess.* with shell=True (or a non-constant shell=), [sh|bash ..., "-c", ...], os.system / os.popen /
  os.spawn*, asyncio.create_subprocess_shell               a shell can redirect into any path: each is reviewed
  lib.safe_io publish / append_text / remove_path / anchored without a run root (omitted or None): the root is
                                                           mandatory for a run-dir path (safe_io also refuses it at
                                                           run time when the path lies under a run marker)

Known limit (not solved statically): a helper in another module that returns `open` (or a writer) and is used
there, e.g. `yaml.safe_dump(doc, stream=helpers.opener(p))`, is a call this per-module scan cannot resolve; the
dump-to-handle rule flags the dump only when the stream is not a plain name. Such helpers are found by review, and
lib.safe_io's runtime refusal (a run-dir path needs its run root) covers the safe_io entry points.

Self-test and fixture code is skipped (functions named `_self_test*`, `_toy*`, `toy_*`, `derived_toy_*`,
`*fixture*`, `example_*`, and the `if __name__ == "__main__"` guard's own self-test branch is not special-cased: a
CLI that writes is production). The safe_io module itself is skipped. Every remaining site must be listed in
REVIEWED with the reason it cannot write inside a run dir a run's agents can reach; exact counts per file and kind,
so a new plain write fails CI.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCAN_DIRS = ("agents/", "scripts/", "toolkit/", "tools/")
SKIP_FILES = ("scripts/lib/safe_io.py", "scripts/write_audit.py")
_FIXTURE_PREFIXES = ("_self_test", "_run_self_test", "_toy", "toy_", "derived_toy_", "example_")
_WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_APPEND", "O_TRUNC"}
_OS_WRITE = {"remove", "unlink", "rmdir", "removedirs", "rename", "renames", "replace", "truncate", "symlink", "link"}
_SHUTIL = {"copy", "copy2", "copyfile", "copytree", "move", "rmtree"}
_NP_SAVE = {"save", "savetxt", "savez", "savez_compressed"}
_DUMP_MODS = {"json", "yaml", "pickle", "marshal", "joblib"}
_DUMPS = {"dump", "safe_dump", "dump_all", "safe_dump_all"}
_TEMP = {"mkstemp", "mkdtemp", "NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}
_METHOD_WRITES = {"write_text", "write_bytes", "tofile", "to_csv", "to_parquet", "to_json", "to_excel", "to_pickle",
                  "savefig"}
_OPENS = ("open", "io.open", "codecs.open", "builtins.open", "os.fdopen")
_SIO_MODS = ("lib.safe_io", "scripts.lib.safe_io", "safe_io")
_SIO_GETTERS = ("_sio", "_safe_io")
_SIO_ROOT_ARG = {"publish": 2, "append_text": 2, "remove_path": 1, "anchored": 1}   # index of the run-root argument
_GETATTR_WRITERS = {"open", "fdopen"} | _OS_WRITE | _SHUTIL | _METHOD_WRITES | _NP_SAVE
_SUBPROCESS = {"subprocess.run", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
               "subprocess.Popen", "asyncio.create_subprocess_exec"}
_SHELLS = {"sh", "bash", "zsh", "dash", "/bin/sh", "/bin/bash", "/bin/zsh", "/usr/bin/env"}


def _is_fixture(name: str) -> bool:
    return name.startswith(_FIXTURE_PREFIXES) or "fixture" in name


def _mode_writes(node) -> bool | None:
    """True/False for a constant mode; None when the mode is not a constant (it may write)."""
    if node is None:
        return False
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return any(c in node.value for c in "wax+")
    return None


class _Scan(ast.NodeVisitor):
    def __init__(self):
        self.alias: dict = {}        # local name -> dotted origin ("os", "os.remove", "numpy", "pathlib.Path", ...)
        self.sio: set = set()        # local names bound to lib.safe_io (an `_sio()` / `_safe_io()` result)
        self.sites: dict = {}

    def add(self, kind, node):
        self.sites.setdefault(kind, []).append(node.lineno)

    # ---- imports
    def visit_Import(self, node):
        for a in node.names:
            self.alias[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]

    def visit_ImportFrom(self, node):
        for a in node.names:
            if node.module:
                self.alias[a.asname or a.name] = f"{node.module}.{a.name}"

    # ---- assigned aliases: `w = open` makes `w(p, "w")` an open; `sio = _safe_io()` makes `sio.publish` safe_io's
    def _writer(self, dotted: str) -> bool:
        mod, _, tail = dotted.rpartition(".")
        return dotted in _OPENS or (mod == "os" and tail in _OS_WRITE) or (mod == "shutil" and tail in _SHUTIL) or (
            mod == "numpy" and tail in _NP_SAVE)

    def visit_Assign(self, node):
        v = node.value
        for t in node.targets:
            if not isinstance(t, ast.Name):
                continue
            d = self._dotted(v) if isinstance(v, (ast.Name, ast.Attribute)) else None
            if d and self._writer(d):
                self.alias[t.id] = d
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id in _SIO_GETTERS:
                self.sio.add(t.id)
        self.generic_visit(node)

    def _sio_fn(self, f) -> str | None:
        """The lib.safe_io function a call targets (publish, append_text, remove_path, anchored), or None."""
        if isinstance(f, ast.Attribute) and f.attr in _SIO_ROOT_ARG:
            b = f.value
            if (isinstance(b, ast.Call) and isinstance(b.func, ast.Name) and b.func.id in _SIO_GETTERS) or (
                    isinstance(b, ast.Name) and (b.id in self.sio or self.alias.get(b.id) in _SIO_MODS)):
                return f.attr
            d = self._dotted(b)
            return f.attr if d in _SIO_MODS else None
        d = self._dotted(f) or ""
        mod, _, tail = d.rpartition(".")
        return tail if mod in _SIO_MODS and tail in _SIO_ROOT_ARG else None

    def _shell(self, node, name, kw) -> bool:
        if name in ("os.system", "os.popen", "asyncio.create_subprocess_shell") or name.startswith("os.spawn"):
            return True
        if name not in _SUBPROCESS:
            return False
        sh = kw.get("shell")
        if sh is not None and not (isinstance(sh, ast.Constant) and not sh.value):
            return True
        cmd = node.args[0] if node.args else kw.get("args")
        if name == "asyncio.create_subprocess_exec":
            cmd = ast.List(elts=list(node.args))
        if isinstance(cmd, (ast.List, ast.Tuple)) and cmd.elts:
            c = [e.value for e in cmd.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if c and c[0] in _SHELLS and "-c" in c:
                return True
        return False

    # ---- self-test and fixture code is skipped
    def visit_FunctionDef(self, node):
        if _is_fixture(node.name):
            return
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _dotted(self, f) -> str | None:
        if isinstance(f, ast.Name):
            return self.alias.get(f.id, f.id)
        if isinstance(f, ast.Attribute):
            base = self._dotted(f.value)
            return f"{base}.{f.attr}" if base else None
        return None

    def visit_Call(self, node):
        f = node.func
        name = self._dotted(f) or ""
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        tail = name.rsplit(".", 1)[-1]
        mod = name.rsplit(".", 1)[0] if "." in name else ""
        sio = self._sio_fn(f)
        if sio:
            i = _SIO_ROOT_ARG[sio]
            root = node.args[i] if len(node.args) > i else kw.get("root")
            starred = any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords)
            if not starred and (root is None or (isinstance(root, ast.Constant) and root.value is None)):
                self.add("safeio-no-root", node)
        if name == "getattr" and len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and \
                node.args[1].value in _GETATTR_WRITERS:
            self.add("getattr-writer", node)
        elif name == "functools.partial" and node.args and self._writer(self._dotted(node.args[0]) or ""):
            t = self._dotted(node.args[0])
            if t in _OPENS:
                mode = node.args[2] if len(node.args) > 2 else kw.get("mode")
                w = _mode_writes(mode)
                if w or (w is None and mode is not None):
                    self.add("partial-writer", node)
            else:
                self.add("partial-writer", node)
        elif self._shell(node, name, kw):
            self.add("shell-command", node)
        elif name in _OPENS:
            mode = node.args[1] if len(node.args) > 1 else kw.get("mode")
            w = _mode_writes(mode)
            if w or (w is None and mode is not None):
                self.add("open-write" if w else "open-dynamic-mode", node)
        elif isinstance(f, ast.Attribute) and f.attr == "open" and not (
                isinstance(f.value, ast.Name) and f.value.id in self.alias):     # Image.open, tarfile.open: modules
            mode = node.args[0] if node.args else kw.get("mode")
            w = _mode_writes(mode)
            if w or (w is None and mode is not None):
                self.add("path-open-write" if w else "open-dynamic-mode", node)
        elif isinstance(f, ast.Attribute) and f.attr in _METHOD_WRITES and mod not in ("os", "shutil"):
            self.add("method-write", node)
        elif name == "os.open":
            flags = node.args[1] if len(node.args) > 1 else kw.get("flags")
            ids = {n.attr for n in ast.walk(flags) if isinstance(n, ast.Attribute)} | {
                n.id for n in ast.walk(flags) if isinstance(n, ast.Name)} if flags is not None else set()
            ids = {self.alias.get(i, i).rsplit(".", 1)[-1] for i in ids}
            if ids & _WRITE_FLAGS:
                self.add("os.open-write", node)
        elif mod == "os" and tail in _OS_WRITE:
            self.add("os-" + ("remove" if tail in ("remove", "unlink", "rmdir", "removedirs") else "rename"
                              if tail in ("rename", "renames", "replace") else tail), node)
        elif mod == "shutil" and tail in _SHUTIL:
            self.add("shutil", node)
        elif mod == "numpy" and tail in _NP_SAVE:
            self.add("numpy-save", node)
        elif mod in _DUMP_MODS and tail in _DUMPS:
            stream = node.args[1] if len(node.args) > 1 else kw.get("stream") or kw.get("fp") or kw.get("file")
            if stream is not None and not isinstance(stream, ast.Name) and not (
                    isinstance(stream, ast.Call) and (self._dotted(stream.func) or "") in ("open", "io.open")):
                self.add("dump-to-handle", node)
        elif mod == "tempfile" and tail in _TEMP and "dir" in kw:
            self.add("tempfile-dir", node)
        self.generic_visit(node)


def scan_text(text: str) -> dict:
    """{kind: [line numbers]} of the write sites in one module (self-test and fixture code skipped)."""
    s = _Scan()
    s.visit(ast.parse(text))
    return {k: sorted(v) for k, v in s.sites.items()}


def tracked() -> list[str]:
    out = subprocess.run(["git", "-C", REPO, "ls-files", "*.py"], capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if p.startswith(SCAN_DIRS) and p not in SKIP_FILES]


def sites(paths=None, read=None) -> dict:
    read = read or (lambda p: open(os.path.join(REPO, p), encoding="utf-8").read())
    out = {}
    for p in tracked() if paths is None else paths:
        try:
            found = scan_text(read(p))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        if found:
            out[p] = found
    return out


_EVAL = ("evaluation, replay or fixture tooling: writes the trees it creates itself (copies of runs, fixtures, eval "
         "outputs, temporary dirs), not a live run dir a run's agents write to")
_REPO = "repo tooling run by the operator: writes repo files (engine cards, benchmark results, notes, changes/)"
_TMP_REASON = "a temporary directory this process created (tempfile / the worker's own dir), never a run dir"
_FIX = "self-test fixture code in a helper the skip rule does not name"
# REVIEWED: write sites that cannot write inside a run dir a run's agents can reach.
REVIEWED: dict = {
    "agents/analysis_backend.py": {"open-write": (2, "cross_check's units subset in its own temporary work dir; "
                                                    "write_toy_units (a fixture)")},
    "agents/run_preflight.py": {"shutil": (1, "removes the preflight's own temporary work dir")},
    "scripts/assemble_changes.py": {"open-write": (3, _REPO), "os-remove": (1, _REPO)},
    "scripts/cloud_batch.py": {"open-write": (2, "the operator's local batch files")},
    "scripts/compute.py": {
        "open-write": (10, "worker init markers, chunk inputs, stderr and the worker's sync record (" + _TMP_REASON + ")"),
        "os-remove": (5, "stale bundle members and pid files on a worker (" + _TMP_REASON + "); the worker's TLS files"),
        "shutil": (1, "the worker's own __pycache__"), "os-rename": (1, "a pid file in the worker's call dir"),
        "os-link": (1, "the claim file in the worker's call dir (atomic claim)"),
        "os.open-write": (1, "the worker's TLS files (O_EXCL, private dir)")},
    "scripts/eval_private.py": {"shutil": (4, _EVAL), "open-write": (5, _EVAL)},
    "scripts/eval_suite.py": {"open-write": (7, _EVAL), "os-symlink": (1, _EVAL), "os-remove": (1, _EVAL),
                              "shell-command": (1, _EVAL + " (--execute runs the cloud launch commands it printed)")},
    "scripts/fixtures.py": {"shutil": (5, _EVAL), "open-write": (7, _EVAL), "os-remove": (1, _EVAL)},
    "scripts/gate_fuzz.py": {"shutil": (6, _EVAL), "open-write": (7, _EVAL), "os-remove": (2, _EVAL), "os-symlink": (1, _EVAL)},
    "scripts/gate_replay.py": {"shutil": (3, _EVAL), "open-write": (1, _EVAL)},
    "scripts/golden_traces.py": {"shutil": (6, _EVAL), "os-remove": (2, _EVAL), "tempfile-dir": (2, _EVAL),
                                 "open-write": (1, _EVAL)},
    "scripts/pipeline.py": {
        "shell-command": (1, "_run_cmd runs a step's declared `run` command (shell=True for a non-compute step): the "
                             "agent's own command, run in the run dir with the agent's reach; the harness writes "
                             "nothing through the shell, and the step's outputs are hashed after it exits"),
        "shutil": (10, "the step's pyc cache and derivation work dirs (" + _TMP_REASON + "); the aside dir the harness "
                       "made (mkdtemp) and a planted .pipeline_aside_* dir, removed with rmtree, which removes links, "
                       "never their targets; the scratch copy (copytree with symlinks=True, a sibling temp dir the "
                       "harness made, then rmtree)"),
        "tempfile-dir": (2, "the aside dir (mkdtemp inside the run dir, a random name the harness owns) and the "
                            "scratch copy (mkdtemp beside the run dir)"),
        "os-remove": (3, "inside the scratch copy the harness made: links re-pointed into the copy, and its own "
                         "manifest replaced"),
        "os-symlink": (1, "inside the scratch copy: a link re-pointed into the copy, never at the real run dir"),
        "open-write": (3, "the scratch copy's manifest (its own file after the link is removed); the derivation's "
                          "column-stripped input and mount points in its temporary work dirs")},
    "scripts/plant_defects.py": {"open-write": (3, _EVAL), "shutil": (4, _EVAL)},
    "scripts/regression_eval.py": {"open-write": (2, _EVAL), "shutil": (1, _EVAL)},
    "scripts/replay.py": {"shutil": (2, _EVAL), "os-link": (1, _EVAL), "open-write": (6, _EVAL), "os-remove": (1, _EVAL)},
    "scripts/replay_coiled.py": {"open-write": (18, _EVAL), "shutil": (3, _EVAL), "os-remove": (4, _EVAL)},
    "scripts/replay_compare.py": {"open-write": (1, _EVAL)},
    "scripts/results_store.py": {"open-write": (1, "download() writes where the operator names")},
    "scripts/score_critiques.py": {"open-write": (3, _EVAL)},
    "scripts/sdk_scenarios.py": {"open-write": (15, _EVAL)},
    "scripts/telemetry.py": {"open-write": (1, "the operator's --out CSV")},
    "scripts/lib/positive_control.py": {"open-write": (2, "builds the positive-control fixture run in a dir it creates")},
    "scripts/validate_critique_yaml.py": {"os-remove": (1, "its own NamedTemporaryFile in the system temp dir")},
    "toolkit/analysis/figures.py": {"method-write": (1, "savefig into an in-memory buffer, then lib.safe_io")},
    "toolkit/analysis/run.py": {"open-write": (5, _FIX + " (_resign, _direct_toy)"), "os-remove": (1, _FIX),
                                "shutil": (1, _FIX)},
    "toolkit/engines/benchmark.py": {"open-write": (4, _REPO + " (the benchmark CLI's outputs)")},
    "toolkit/engines/compartmental/benchmarks/runtime_spike.py": {"open-write": (1, _REPO)},
    "toolkit/engines/compartmental/engine.py": {"open-write": (1, _REPO + " (write_card)")},
    "toolkit/engines/malaria/benchmarks/b15_mechanisms.py": {"open-write": (1, _REPO)},
    "toolkit/engines/malaria/benchmarks/equilibration.py": {"open-write": (1, _REPO)},
    "toolkit/engines/malaria/benchmarks/guess_sensitivity.py": {"open-write": (1, _REPO)},
    "toolkit/engines/malaria/benchmarks/render_notes.py": {"open-write": (1, _REPO)},
    "toolkit/engines/malaria/benchmarks/restamp_proof.py": {"open-write": (3, _REPO)},
    "toolkit/engines/malaria/benchmarks/runtime.py": {"open-write": (1, _REPO)},
    "toolkit/engines/malaria/engine.py": {"open-write": (1, _REPO + " (write_card)")},
    "tools/run_console/run_console.py": {"open-write": (2, "the console's temporary answers file (mkstemp)"),
                                         "os-remove": (2, "the console's temporary answers file (mkstemp)")},
}
_V1 = ("v1 pipeline (main.py without --v2, its hooks and gates): writes in its run dir with plain opens, attacker-"
       "reachable under the hostile-artifact model; removed at v1 retirement (the owner deletes v1 once v2 is shown "
       "superior), not migrated")
# OPEN_ITEMS: attacker-reachable write sites not yet on lib.safe_io. Counted exactly, so none is added; each migration
# lowers a count. These are known gaps, not exemptions.
OPEN_ITEMS: dict = {
    "agents/hooks.py": {"shutil": (1, _V1)},
    "agents/integrity.py": {"open-write": (1, _V1)},
    "agents/observability.py": {"open-write": (5, _V1), "os-rename": (1, _V1), "os-remove": (3, _V1)},
    "agents/output_caps.py": {"open-write": (1, _V1)},
    "agents/run_finish.py": {"open-write": (13, _V1), "shutil": (2, _V1), "os-remove": (1, _V1)},
    "scripts/ask.py": {"open-write": (1, _V1)},
    "scripts/benchmark_match.py": {"open-write": (1, _V1)},
    "scripts/coherence_audit.py": {"open-write": (1, _V1)},
    "scripts/compare_models.py": {"open-write": (1, _V1)},
    "scripts/cross_model_critic.py": {"open-write": (4, _V1), "os-rename": (1, _V1)},
    "scripts/effort_floors.py": {"open-write": (1, _V1)},
    "scripts/eligibility_probe.py": {"open-write": (1, _V1)},
    "scripts/identifiability.py": {"open-write": (1, _V1)},
    "scripts/lessons.py": {"open-write": (3, _V1)},
    "scripts/model_card.py": {"open-write": (3, _V1), "os-rename": (1, _V1)},
    "scripts/open_problems.py": {"open-write": (2, _V1), "os-rename": (2, _V1)},
    "scripts/presentation_pass.py": {"open-write": (2, _V1), "os-rename": (1, _V1)},
    "scripts/propagate_uncertainty.py": {"open-write": (1, _V1)},
    "scripts/run_autopsy.py": {"open-write": (2, _V1)},
    "scripts/writer_qa.py": {"open-write": (1, _V1)},
}


def problems(found: dict, reviewed: dict, open_items: dict | None = None) -> list[str]:
    """A site must be reviewed (unreachable) or a counted open item; counts are exact both ways."""
    bad, open_items = [], open_items or {}
    for p, pats in sorted(found.items()):
        for name, lines in sorted(pats.items()):
            want = sum(t[0] for t in ((reviewed.get(p) or {}).get(name), (open_items.get(p) or {}).get(name)) if t)
            if not want:
                bad.append(f"{p}: {name} at lines {lines}: not through lib.safe_io and not reviewed")
            elif want != len(lines):
                bad.append(f"{p}: {name}: {len(lines)} site(s) at lines {lines}, {want} reviewed or open")
    for table in (reviewed, open_items):
        for p, pats in sorted(table.items()):
            for name, (n, _) in pats.items():
                if n and not (found.get(p) or {}).get(name):
                    bad.append(f"{p}: {name}: {n} site(s) listed but none found (stale entry)")
    return bad


_BYPASS = '''
import os as o, shutil as sh, io, tempfile, json, yaml
import numpy as np
from pathlib import Path
from os import replace as rp
from shutil import copyfile
def f(p, q, x, mode):
    with open(
        p,
        "w",
    ) as fh:
        fh.write("x")
    Path(p).open("a")
    Path(p).write_text("x")
    Path(p).write_bytes(b"x")
    io.open(p, mode="wb")
    open(p, mode)
    o.remove(p)
    rp(p, q)
    copyfile(p, q)
    sh.rmtree(q)
    np.save(p, x)
    np.savetxt(p, x)
    x.tofile(p)
    json.dump(x, o.fdopen(3, "w"))
    yaml.safe_dump(x, o.fdopen(3, "w"))
    tempfile.mkstemp(dir=q)
    fd = o.open(p, o.O_WRONLY | o.O_CREAT)
    open(p).read()
    open(p, "rb").read()
    tempfile.mkstemp()
def _self_test():
    open("x", "w")
    o.remove("x")
'''


def _self_test_writers(ok) -> None:
    """WA4-WA9: the writers Codex named (round 5), each against a planted link, run for real."""
    import json
    import tempfile
    for p in (REPO, os.path.join(REPO, "scripts"), os.path.join(REPO, "agents")):
        if p not in sys.path:
            sys.path.insert(0, p)

    def planted(d, rel, out):
        os.makedirs(os.path.dirname(os.path.join(d, rel)) or d, exist_ok=True)
        v = os.path.join(out, "victim_" + rel.replace("/", "_"))
        with open(v, "w") as f:
            f.write("untouched")
        os.symlink(v, os.path.join(d, rel))
        return v

    def untouched(*vs):
        return all(open(v, "rb").read() == b"untouched" for v in vs)

    def attempt(fn):
        try:
            fn()
        except (ValueError, OSError):
            pass
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        import compute as C
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "m.py"), "w").write("def f(p):\n    return {'y': p['x']}\n")
        os.makedirs(os.path.join(out, "batchdir"))
        os.makedirs(os.path.join(d, "analysis"))
        os.symlink(os.path.join(out, "batchdir"), os.path.join(d, "analysis", "batch"))
        attempt(lambda: C.run_map(d, "models/m.py::f", [{"x": 1}], os.path.join(d, "analysis", "batch", "r.jsonl"),
                                  backend="local", max_workers=1))
        ok(os.listdir(os.path.join(out, "batchdir")) == [],
           f"WA4 run_map anchors at the run dir: a linked analysis/batch receives no lock, rows, summary or journal "
           f"({os.listdir(os.path.join(out, 'batchdir'))})")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        import compute as C
        v1 = planted(d, C.LEDGER_REL.replace(os.sep, "/") + ".lock", out)
        v2 = planted(d, C.LEDGER_REL.replace(os.sep, "/") + ".tmp", out)

        def led():
            with C._LedgerLock(d) as L:
                L["core_seconds"] = 1.0
        attempt(led)
        ok(untouched(v1, v2), "WA5 the compute ledger's lock and its publication do not follow planted links")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        from agents import fix_loop as FL
        v = planted(d, FL.IMPACT, out)
        attempt(lambda: FL.append_impact(d, {"task": "t"}))
        ok(untouched(v), "WA6 the fix loop's impact ledger append does not follow a planted link")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        from agents import run_budget as RB
        v = planted(d, RB.BUDGET_LEDGER, out)
        attempt(lambda: RB.write_budget_ledger(d, {"sessions": []}, 1.0))
        ok(untouched(v), "WA7 the budget ledger is not written through a planted link")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        from toolkit.analysis import uncertainty as U
        v = planted(d, "uncertainty_report.yaml", out)
        attempt(lambda: U.write_report(d, {"x": 1}))
        ok(untouched(v), "WA8 the uncertainty report is not written through a planted link")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from toolkit.analysis import figures as FG
        os.makedirs(os.path.join(d, "figures"))
        v = planted(d, "figures/f.png", out)
        fig = plt.figure()
        attempt(lambda: FG._save(fig, d, "f.png", []))
        ok(untouched(v), "WA9 a figure is not saved through a planted link")


def _self_test_v2_open_items(ok) -> None:
    """WA10-WA12: the v2 open items (pipeline aside, derived_rebuild sidecars, v2_tasks stubs), each against a planted
    link, run for real."""
    import tempfile
    for p in (REPO, os.path.join(REPO, "scripts"), os.path.join(REPO, "agents")):
        if p not in sys.path:
            sys.path.insert(0, p)
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        import pipeline as P
        secret = os.path.join(out, "secret.csv")
        open(secret, "w").write("secret\n")
        os.makedirs(os.path.join(d, ".pipeline_aside_evil", "data"))
        os.symlink(secret, os.path.join(d, ".pipeline_aside_evil", "data", "x.csv"))
        try:
            P._recover_aside(d)
        except (ValueError, OSError):
            pass
        dst = os.path.join(d, "data", "x.csv")
        ok(not os.path.islink(dst) and os.path.exists(secret),
           "WA10 a planted aside entry that is a link is not moved into the run dir (pipeline._recover_aside)")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        import derived_rebuild as DR
        os.symlink(out, os.path.join(d, "figures"))
        real = DR.inside_dir
        DR.inside_dir = lambda *a, **k: True          # the check saw no link (a race)
        try:
            DR._write_sidecar(d, os.path.join(d, "figures", "f.png.provenance.json"), {"x": 1})
        except (ValueError, OSError):
            pass
        finally:
            DR.inside_dir = real
        ok(os.listdir(out) == [], f"WA11 a derivation sidecar is not written through a linked figures/ "
                                  f"({os.listdir(out)})")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        from agents import v2_tasks as V
        vs = []
        for rel in ("decision_spec.yaml.tmp", "decision_spec.signoff.json", "manuscript.md"):
            v = os.path.join(out, rel)
            open(v, "w").write("untouched")
            os.symlink(v, os.path.join(d, rel))
            vs.append(v)
        st = V.StubTasks({})
        for fn in (lambda: V._yaml_dump({"a": 1}, os.path.join(d, "decision_spec.yaml")),
                   lambda: st.sign_spec(d), lambda: st.write(d, "draft")):
            try:
                fn()
            except (ValueError, OSError, TypeError, KeyError):
                pass
        ok(all(open(v).read() == "untouched" for v in vs),
           "WA12 the v2 dry-run stub tasks write nothing through planted links")


_BYPASS2 = '''
import builtins, functools, subprocess, os
from functools import partial
from lib.safe_io import publish, append_text
from lib import safe_io
def g(p, d):
    getattr(builtins, "open")(p, "w")
    functools.partial(open, mode="w")
    partial(open, p, "a")
    w = open
    w(p, "w")
    subprocess.run("echo x > " + p, shell=True)
    subprocess.run(["bash", "-c", "echo x > f"])
    os.system("echo x > f")
    publish(p, "x")
    safe_io.append_text(p, "x")
    _sio().remove_path(p)
    publish(p, "x", None)
    publish(p, "x", d)
    _sio().publish(p, "x", root=d)
    subprocess.run(["git", "status"])
    functools.partial(open, mode="r")
'''


def _self_test_round6(ok) -> None:
    """WA13-WA20: Codex's review of #134, each reproduced: the detached launch, root-less publications, the detached
    log's reopen, the --no-ledger outputs, the rename source race, the audit bypasses and the mandatory run root."""
    import json
    import subprocess as sp
    import tempfile
    import types
    for p in (REPO, os.path.join(REPO, "scripts"), os.path.join(REPO, "agents")):
        if p not in sys.path:
            sys.path.insert(0, p)
    import compute as C
    from lib import safe_io as SIO

    def run_dir(d):
        open(os.path.join(d, "metadata.json"), "w").write("{}")      # what marks a run dir (main.py, stage_machine)
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "models", "m.py"), "w").write("def f(p):\n    return {'y': p['x']}\n")

    class _P:
        def __init__(self, argv, stdout=None, **k):
            self.pid = os.getpid()
            if stdout is not None:
                os.write(stdout.fileno(), b"launched\n")

    def call(fn, *a, **kw):
        try:
            try:
                return fn(*a, **kw)
            except TypeError:
                return fn(*a)                       # before the fix: no run-root parameter
        except (ValueError, OSError):
            return None
    real_popen = sp.Popen
    sp.Popen = _P
    try:
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            run_dir(d)
            os.makedirs(os.path.join(out, "batchdir"))
            os.makedirs(os.path.join(d, "analysis"))
            os.symlink(os.path.join(out, "batchdir"), os.path.join(d, "analysis", "batch"))
            call(C._detach, ["map"], os.path.join(d, "analysis", "batch", "r.jsonl"), 1, run_dir=d)
            ok(os.listdir(os.path.join(out, "batchdir")) == [],
               f"WA13 the detached launch anchors at the run dir: a linked analysis/batch receives no launch lock, "
               f"lock, log or job record ({os.listdir(os.path.join(out, 'batchdir'))})")
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
            run_dir(d)
            os.makedirs(os.path.join(d, "analysis", "batch"))
            victim = os.path.join(out, "victim")
            open(victim, "w").write("untouched")
            log = os.path.join(d, "analysis", "batch", "r.jsonl.log")
            real_w = SIO.write_nofollow

            def swap(base, rel, data, *a, **k):
                r = real_w(base, rel, data, *a, **k)
                if rel.endswith(".log") and os.path.exists(log):
                    os.unlink(log)
                    os.link(victim, log)            # the log swapped for a hard link to a file outside the run
                return r
            SIO.write_nofollow = swap
            try:
                call(C._detach, ["map"], os.path.join(d, "analysis", "batch", "r.jsonl"), 1, run_dir=d)
            finally:
                SIO.write_nofollow = real_w
            ok(open(victim).read() == "untouched",
               "WA14 the detached log is not reopened by name: a hard-link swap after it is created is refused")
    finally:
        sp.Popen = real_popen
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        run_dir(d)
        from toolkit.analysis import batch as Bt
        os.symlink(out, os.path.join(d, "analysis"))
        spec = types.SimpleNamespace(sha256="s", settings_sha256=None)
        call(Bt.ship, os.path.join(d, "analysis", "shipped"), spec, {"points": 1}, [{"id": "a"}],
             [{"id": "a", "ok": True, "result": {}}], platform={"system": "x"}, run_dir=d)
        ok(os.listdir(out) == [], f"WA15 a shipped bundle is not published through a planted parent link "
                                  f"({os.listdir(out)})")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        run_dir(d)
        import figure_validator as FV
        os.symlink(out, os.path.join(d, "figures"))
        open(os.path.join(out, "f.png"), "wb").write(b"png")
        call(FV.validate_figure, os.path.join(d, "figures", "f.png"))
        ok(os.listdir(out) == ["f.png"], f"WA16 figure provenance is not written through a planted figures/ link "
                                         f"({os.listdir(out)})")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        run_dir(d)
        inp = os.path.join(d, "in.jsonl")
        open(inp, "w").write(json.dumps({"x": 1}) + "\n")
        os.makedirs(os.path.join(out, "batchdir"))
        os.makedirs(os.path.join(d, "analysis"))
        os.symlink(os.path.join(out, "batchdir"), os.path.join(d, "analysis", "batch"))
        import signal
        argv, h = sys.argv, signal.getsignal(signal.SIGTERM)
        sys.argv = ["compute.py", "map", d, "--fn", "models/m.py::f", "--inputs", inp, "--out",
                    os.path.join(d, "analysis", "batch", "r.jsonl"), "--no-ledger", "--backend", "local",
                    "--max-workers", "1"]
        try:
            call(C.main)
        finally:
            sys.argv = argv
            signal.signal(signal.SIGTERM, h)
        ok(os.listdir(os.path.join(out, "batchdir")) == [],
           f"WA17 the --no-ledger outputs are not written through a planted analysis/batch link "
           f"({os.listdir(os.path.join(out, 'batchdir'))})")
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as out:
        secret = os.path.join(out, "secret")
        open(secret, "w").write("secret")
        open(os.path.join(d, "src"), "w").write("plain")
        real_dfd = SIO.dir_fd_nofollow

        def race(base, parts, create=False):
            if create and not os.path.islink(os.path.join(d, "src")):
                os.unlink(os.path.join(d, "src"))
                os.symlink(secret, os.path.join(d, "src"))   # swapped for a link after the type check
            return real_dfd(base, parts, create)
        SIO.dir_fd_nofollow = race
        try:
            call(SIO.rename_nofollow, d, "src", "sub/dst")
        finally:
            SIO.dir_fd_nofollow = real_dfd
        dst = os.path.join(d, "sub", "dst")
        ok(not os.path.islink(dst) and open(secret).read() == "secret",
           "WA18 rename_nofollow moves the bytes of the file it opened, never a link swapped in after the check")
    s = scan_text(_BYPASS2)
    lines = _BYPASS2.splitlines()
    at = (lambda frag: next(i + 1 for i, ln in enumerate(lines) if frag in ln))
    want = {"getattr-writer": [at("getattr(builtins")], "partial-writer": [at('partial(open, mode="w")'),
                                                                           at('partial(open, p, "a")')],
            "open-write": [at('w(p, "w")')],
            "shell-command": [at("shell=True"), at('"bash", "-c"'), at("os.system")],
            "safeio-no-root": [at('publish(p, "x")'), at("safe_io.append_text"), at("remove_path(p)"),
                               at('publish(p, "x", None)')]}
    clean = [at('publish(p, "x", d)'), at("root=d"), at('"git", "status"'), at('mode="r"')]
    flat = sum(s.values(), [])
    ok(all(sorted(s.get(k, [])) == v for k, v in want.items()) and not set(clean) & set(flat),
       f"WA19 the audit finds getattr(builtins, 'open'), functools.partial(open, write mode), an assigned open alias, "
       f"shell=True / sh -c / os.system, and safe_io calls without a run root; a rooted call, a plain subprocess "
       f"and a read-mode partial are not sites ({s})")
    with tempfile.TemporaryDirectory() as d:
        run_dir(d)
        os.makedirs(os.path.join(d, "analysis"))
        res = []
        for fn in (lambda: SIO.publish(os.path.join(d, "x.json"), "x"),
                   lambda: SIO.publish(os.path.join(d, "x.json"), "x", None),
                   lambda: SIO.append_text(os.path.join(d, "x.log"), "x", None),
                   lambda: SIO.remove_path(os.path.join(d, "x.json"), None),
                   lambda: SIO.publish(os.path.join(d, "analysis", "x.json"), "x", os.path.join(d, "analysis"))):
            try:
                fn()
                res.append(False)
            except (TypeError, ValueError):
                res.append(True)
        SIO.publish(os.path.join(d, "ok.json"), "x", d)
        ok(all(res) and open(os.path.join(d, "ok.json")).read() == "x",
           f"WA20 safe_io requires the run root for a path under a run dir: omitted, None, or a dir below the run "
           f"root is refused; the run root is accepted ({res})")


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    s = scan_text(_BYPASS)
    lines = _BYPASS.splitlines()
    at = (lambda frag: next(i + 1 for i, ln in enumerate(lines) if frag in ln))
    want = {"open-write": [at("with open(")], "path-open-write": [at('Path(p).open("a")')],
            "method-write": [at("write_text"), at("write_bytes"), at("tofile")], "open-dynamic-mode": [at("open(p, mode)")],
            "os-remove": [at("o.remove")], "os-rename": [at("rp(p, q)")], "shutil": [at("copyfile(p"), at("sh.rmtree")],
            "numpy-save": [at("np.save("), at("np.savetxt")], "dump-to-handle": [at("json.dump"), at("yaml.safe_dump")],
            "tempfile-dir": [at("mkstemp(dir")], "os.open-write": [at("o.O_WRONLY")]}
    reads = [at("open(p).read()"), at('open(p, "rb")'), at("tempfile.mkstemp()")]
    test_lines = [i + 1 for i, ln in enumerate(lines) if i + 1 > at("def _self_test")]
    flat = sum(s.values(), [])
    ok(all(set(v) <= set(s.get(k, [])) for k, v in want.items()) and at('io.open(p, mode="wb")') in s.get(
        "open-write", []) and not set(reads) & set(flat) and not set(test_lines) & set(flat),
       f"WA1 the AST audit finds multi-line open, Path.open/write_text/write_bytes, io.open, a dynamic mode, aliased "
       f"os/shutil calls, numpy saves and tofile, dumps to a non-name handle, tempfile dir=, os.open write flags; "
       f"reads, plain mkstemp and self-test code are not sites ({s})")
    ok(problems({"a.py": {"os-remove": [3]}}, {}) == ["a.py: os-remove at lines [3]: not through lib.safe_io and "
                                                       "not reviewed"], "WA2 an unreviewed site is a problem")
    _self_test_writers(ok)
    _self_test_v2_open_items(ok)
    _self_test_round6(ok)
    live = problems(sites(), REVIEWED, OPEN_ITEMS)
    n_open = sum(n for pats in OPEN_ITEMS.values() for n, _ in pats.values())
    print(f"NOTE {n_open} attacker-reachable write site(s) remain as counted open items (OPEN_ITEMS)")
    ok(not live, f"WA3 every write in the production tree is through lib.safe_io or reviewed ({len(live)} problems):\\n  "
       + "\\n  ".join(live[:80]))
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    found = sites()
    for p, pats in sorted(found.items()):
        for name, lines in sorted(pats.items()):
            r = (REVIEWED.get(p) or {}).get(name) or (OPEN_ITEMS.get(p) or {}).get(name)
            print(f"{p}: {name} {lines}: {r[1] if r else 'NOT REVIEWED'}")
    bad = problems(found, REVIEWED, OPEN_ITEMS)
    print(f"{len(bad)} problem(s); {sum(n for pats in OPEN_ITEMS.values() for n, _ in pats.values())} open item site(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
