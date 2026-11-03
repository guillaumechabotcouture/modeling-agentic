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
        if name in ("open", "io.open", "codecs.open", "builtins.open", "os.fdopen"):
            mode = node.args[1] if len(node.args) > 1 else kw.get("mode")
            w = _mode_writes(mode)
            if w or (w is None and mode is not None):
                self.add("open-write" if w else "open-dynamic-mode", node)
        elif isinstance(f, ast.Attribute) and f.attr == "open" and mod not in ("os", "io", "codecs", "gzip", "tarfile",
                                                                                 "zipfile", "webbrowser", "builtins"):
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


# {file: {kind: (count, reason)}}: write sites that cannot write inside a run dir a run's agents can reach.
REVIEWED: dict = {}


def problems(found: dict, reviewed: dict) -> list[str]:
    bad = []
    for p, pats in sorted(found.items()):
        for name, lines in sorted(pats.items()):
            want = (reviewed.get(p) or {}).get(name)
            if want is None:
                bad.append(f"{p}: {name} at lines {lines}: not through lib.safe_io and not reviewed")
            elif want[0] != len(lines):
                bad.append(f"{p}: {name}: {len(lines)} site(s) at lines {lines}, {want[0]} reviewed")
    for p, pats in sorted(reviewed.items()):
        for name, (n, _) in pats.items():
            if n and not (found.get(p) or {}).get(name):
                bad.append(f"{p}: {name}: reviewed {n} site(s) but none found (stale entry)")
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
    live = problems(sites(), REVIEWED)
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
            r = (REVIEWED.get(p) or {}).get(name)
            print(f"{p}: {name} {lines}: {r[1] if r else 'NOT REVIEWED'}")
    bad = problems(found, REVIEWED)
    print(f"{len(bad)} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
