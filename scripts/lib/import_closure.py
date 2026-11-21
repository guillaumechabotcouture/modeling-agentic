#!/usr/bin/env python3
"""The repo files a batch function's rows can depend on: the code its function can reach, found statically.

    python3 scripts/lib/import_closure.py --self-test
    python3 scripts/lib/import_closure.py <path.py>::<function> [--run-dir DIR]      # the closure as JSON

W0-A (v2 autonomy brief addendum): the reuse key of a stored row hashes this closure instead of all of scripts/ and
toolkit/. Nothing is imported: each file is parsed (ast) and the walk follows names. From the function it reaches
every top-level definition its code names, in its own module or through an import (`from X import f`, `X.f` on an
imported module), and so on; a class named is taken whole. A module reached at all contributes its import-time code:
every top-level statement that is not a plain function or class definition (assignments, imports, decorated
definitions: a decorator can register a function no code names), each class's bases and class-level statements, its packages' `__init__.py`, and the other files in its directory (data such as a card).
A module used as a value (passed, or read with getattr) is taken whole. Files are the unit hashed: a reached file is
hashed whole.

It fails closed: `unresolved` lists why the closure is not known, and a caller then must not narrow the key.
  - an import whose top-level name is a repo package or module (on the paths load_fn puts on sys.path) but whose
    file is not found;
  - a dynamic import or exec site (importlib.import_module, __import__, spec_from_file_location, exec_module,
    runpy, exec, eval, globals) in reached code of a file that does not declare what it can load. A file declares it
    with a module-level literal `FP_DYNAMIC_IMPORTS = ("<repo-relative dir or file>", ...)`: every file under each
    named path joins the closure, and each module there is reached with its import-time code and every top-level
    class (what a `module:Class` reference loads; repo dynamic imports load classes through toolkit/import_guard). An empty tuple declares that the file's dynamic imports load
    only the function the closure starts from;
  - a file that cannot be read or parsed, or a root function that is not defined in its file.
Names that are not repo modules (the standard library, installed packages) are outside the closure: the dependency
manifests stay in the key for them.
"""
from __future__ import annotations

import ast
import os
import sys

DYNAMIC_ATTRS = {"import_module", "spec_from_file_location", "exec_module", "run_path", "run_module"}
DYNAMIC_NAMES = {"__import__", "exec", "eval", "globals", "import_module"}
SKIP_DIRS = ("__pycache__", ".git", "_truth")
SKIP_EXT = (".pyc", ".log", ".tmp", ".lock")
MODULE = "<module>"
CLASSES = "<classes>"
SELF_TEST = "_self_test"     # a module taken whole leaves out its self-tests (repo convention: no row code calls one)


class _Mod:
    """One parsed file: its top-level definitions, every import binding in it, and its import-time statements."""

    def __init__(self, path: str, pkg: str | None):
        with open(path, encoding="utf-8", errors="surrogateescape") as fh:
            self.tree = ast.parse(fh.read())
        self.path, self.pkg = path, pkg
        self.defs, self.always, self.binds, self.star = {}, [], {}, []
        for n in self.tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.decorator_list:
                self.defs.setdefault(n.name, []).append(n)
                if isinstance(n, ast.ClassDef):      # its bases and class-level statements run on import
                    self.always.append(ast.Module(body=[*n.bases, *n.keywords, *[
                        s for s in n.body if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]],
                        type_ignores=[]))
                continue
            if _is_main_guard(n):
                continue                                   # the CLI block never runs on import
            self.always.append(n)
            for t in ast.walk(n) if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [n]:
                for name in _bound(t):
                    self.defs.setdefault(name, []).append(n)
        for n in ast.walk(self.tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.asname:
                        self.binds.setdefault(a.asname, []).append((a.name, None))
                    else:                                  # `import a.b.c` binds a; chains are resolved by prefix
                        self.binds.setdefault(a.name.split(".")[0], []).append((a.name.split(".")[0], None))
            elif isinstance(n, ast.ImportFrom):
                mod = self._absolute(n)
                for a in n.names:
                    if a.name == "*":
                        self.star.append(mod)
                    else:
                        self.binds.setdefault(a.asname or a.name, []).append((mod, a.name))

    def _absolute(self, n) -> str | None:
        if not n.level:
            return n.module or ""
        if self.pkg is None:
            return None
        base = self.pkg.split(".")
        base = base[:len(base) - (n.level - 1)] if n.level > 1 else base
        return ".".join(base + ([n.module] if n.module else []))


def _is_main_guard(n) -> bool:
    return (isinstance(n, ast.If) and isinstance(n.test, ast.Compare) and isinstance(n.test.left, ast.Name)
            and n.test.left.id == "__name__")


def _bound(n) -> list[str]:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [n.name]
    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
        return [n.id]
    return []


def _declared(tree):
    """The file's FP_DYNAMIC_IMPORTS literal, or None when it declares none (or not as a literal of strings)."""
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "FP_DYNAMIC_IMPORTS"
                                             for t in n.targets):
            try:
                v = ast.literal_eval(n.value)
            except ValueError:
                return None
            paths = lambda x: isinstance(x, (tuple, list)) and all(isinstance(y, str) for y in x)  # noqa: E731
            if isinstance(v, dict) and v and all(isinstance(k, str) and paths(x) for k, x in v.items()):
                return {k: tuple(x) for k, x in v.items()}
            return tuple(v) if paths(v) else None
    return None


def _literal_import(call) -> str | None:
    """`importlib.import_module("a.b")` / `__import__("a.b")` with a literal name: the module it imports."""
    f, a = call.func, call.args
    name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
    if name in ("import_module", "__import__") and len(a) == 1 and not call.keywords and \
            isinstance(a[0], ast.Constant) and isinstance(a[0].value, str):
        return a[0].value
    return None


def _dynamic(node) -> int | None:
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and _literal_import(n) is None:
            f = n.func
            if (isinstance(f, ast.Attribute) and f.attr in DYNAMIC_ATTRS) or (
                    isinstance(f, ast.Name) and f.id in DYNAMIC_NAMES):
                return n.lineno
    return None


def _chains(node):
    """(dotted name chain, bare): each Name, with the attribute chain read from it (`a.b.c` -> ["a","b","c"]); bare
    when the outermost expression is the name or module itself, not an attribute of it (a module used as a value)."""
    parents = {}
    for p in ast.walk(node):
        for c in ast.iter_child_nodes(p):
            parents[c] = p
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            chain, cur = [n.id], n
            while isinstance(parents.get(cur), ast.Attribute) and parents[cur].value is cur:
                cur = parents[cur]
                chain.append(cur.attr)
            yield chain


class Walk:
    def __init__(self, repo: str, roots: list[str], opaque: dict | None = None):
        self.repo, self.roots, self.opaque = os.path.realpath(repo), roots, opaque or {}
        self.mods, self.seen, self.files, self.unresolved = {}, set(), set(), set()

    def rel(self, p: str) -> str:
        return os.path.relpath(p, self.repo).replace(os.sep, "/")

    def find(self, name: str) -> str | None:
        parts = name.split(".")
        for r in self.roots:
            p = os.path.join(r, *parts)
            for c in (p + ".py", os.path.join(p, "__init__.py")):
                if os.path.isfile(c):
                    return os.path.realpath(c)
        return None

    def is_repo_top(self, top: str) -> bool:
        return any(os.path.isfile(os.path.join(r, top + ".py")) or os.path.isdir(os.path.join(r, top))
                   for r in self.roots)

    def pkg_of(self, path: str) -> str | None:
        """The package a repo module is imported in: under scripts/ (on sys.path) before the repo root."""
        for r in (os.path.join(self.repo, "scripts"), self.repo):
            rr = os.path.join(r, "")
            if path.startswith(rr):
                parts = os.path.relpath(path, rr).replace(os.sep, "/")[:-3].split("/")
                return ".".join(parts[:-1]) or None
        return None

    def mod(self, path: str):
        if path not in self.mods:
            try:
                self.mods[path] = _Mod(path, self.pkg_of(path))
            except (OSError, SyntaxError, ValueError) as e:
                self.unresolved.add(f"{self.rel(path)}: not parsed ({type(e).__name__})")
                self.mods[path] = None
        return self.mods[path]

    def run(self, todo: list) -> None:
        while todo:
            item = todo.pop()
            path, name = item[0], item[1]
            if (path, name) in self.seen:
                continue
            self.seen.add((path, name))
            m = self.mod(path)
            if m is None:
                continue
            if name == CLASSES:                 # a module a declared dynamic import can load: what it can return
                todo += [(path, MODULE)] + [(path, k) for k, ns in m.defs.items()
                                            if any(isinstance(x, ast.ClassDef) for x in ns)]
                continue
            if name == "*":
                todo += [(path, MODULE)] + [(path, k) for k in m.defs if not k.startswith(SELF_TEST)]
                continue
            if name == MODULE:
                self.files.add(path)
                d = os.path.dirname(path)
                self.files.update(os.path.join(d, x) for x in os.listdir(d)
                                  if os.path.isfile(os.path.join(d, x)) and not x.endswith(".py")
                                  and not x.endswith(SKIP_EXT))
                pkg = m.pkg.split(".") if m.pkg else []
                for k in range(1, len(pkg) + 1):
                    init = self.find(".".join(pkg[:k]))
                    if init:
                        todo.append((init, MODULE))
                for s in m.star:
                    self.import_target(m, s, None, todo, whole=True)
                nodes = m.always
            else:
                todo.append((path, MODULE))
                nodes = None if name in self.opaque.get(path, ()) else m.defs.get(name)
                if nodes is None:
                    continue                    # a name read through a module that it does not define (a local)
            for node in nodes:
                self.node(m, node, todo)

    def declared(self, m: _Mod, paths, todo: list) -> None:
        """Every file under the declared paths joins the closure; its modules are reached for their classes."""
        for d in paths:
            p = os.path.join(self.repo, d)
            if not os.path.exists(p):
                self.unresolved.add(f"{self.rel(m.path)}: FP_DYNAMIC_IMPORTS names {d}, which does not exist")
            for x in ([p] if os.path.isfile(p) else
                      [os.path.join(dp, f) for dp, ds, fs in os.walk(p) for f in fs
                       if not any(s in dp.split(os.sep) for s in SKIP_DIRS) and not f.endswith(SKIP_EXT)]):
                x = os.path.realpath(x)
                self.files.add(x)
                if x.endswith(".py"):
                    todo.append((x, CLASSES))

    def callee(self, m: _Mod, func) -> str | None:
        """The repo module file a call goes into (`G.resolve(...)`, or a name imported from it), else None."""
        chain = []
        while isinstance(func, ast.Attribute):
            chain.insert(0, func.attr)
            func = func.value
        if not isinstance(func, ast.Name):
            return None
        for mod, attr in m.binds.get(func.id, ()):
            if mod and self.is_repo_top(mod.split(".")[0]):
                names = mod.split(".") + ([attr] if attr else []) + chain[:-1]
                for k in range(len(names), 0, -1):
                    hit = self.find(".".join(names[:k]))
                    if hit:
                        return hit
        return None

    def node(self, m: _Mod, node, todo: list) -> None:
        line = _dynamic(node)
        if line is not None:
            decl = _declared(m.tree)
            if decl is None:
                self.unresolved.add(f"{self.rel(m.path)}:{line}: dynamic import without FP_DYNAMIC_IMPORTS")
            elif not isinstance(decl, dict):
                self.declared(m, decl, todo)           # a {kind: paths} declaration is taken at its call sites
        for call in (n for n in ast.walk(node) if isinstance(n, ast.Call)):
            lit = _literal_import(call)
            if lit:
                self.import_target(m, lit, None, todo, whole=True)     # the module object is used as a value
            target = self.callee(m, call.func)
            tm = self.mod(target) if target else None
            decl = _declared(tm.tree) if tm else None
            if isinstance(decl, dict):
                a0 = call.args[0] if call.args else None
                kind = a0.value if isinstance(a0, ast.Constant) and isinstance(a0.value, str) else None
                self.declared(tm, decl[kind] if kind in decl else tuple(p for ps in decl.values() for p in ps), todo)
        for n in ast.walk(node):                       # an import runs its module's import-time code
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.import_target(m, a.name, None, todo, module_only=True)
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    self.import_target(m, m._absolute(n), None if a.name == "*" else a.name, todo,
                                       whole=a.name == "*", module_only=a.name != "*")
        for chain in _chains(node):
            head = chain[0]
            if head in m.defs:
                todo.append((m.path, head))
            for mod, attr in m.binds.get(head, ()):
                self.import_target(m, mod, attr, todo, rest=chain[1:])

    def import_target(self, m: _Mod, mod, attr, todo, rest=(), whole=False, module_only=False) -> None:
        if mod is None:
            self.unresolved.add(f"{self.rel(m.path)}: relative import outside a package")
            return
        if not mod or not self.is_repo_top(mod.split(".")[0]):
            return                                      # the standard library or an installed package
        names = mod.split(".") + ([attr] if attr else []) + list(rest)
        best, used = None, 0                            # the longest dotted prefix that is a module file
        for k in range(1, len(names) + 1):
            hit = self.find(".".join(names[:k]))
            if hit:
                best, used = hit, k
        if best is None or used < len(mod.split(".")):
            self.unresolved.add(f"{self.rel(m.path)}: import {mod} not found in the repo")
            return
        nxt = names[used:]
        if module_only:
            todo.append((best, MODULE))
        elif whole or not nxt:
            todo.append((best, "*"))                    # a module used as a value: all of it
        else:
            todo.append((best, nxt[0]))


def closure(fn_spec: str, repo: str, run_dir: str | None = None, roots_in: tuple = (), opaque=None) -> dict:
    """{"files": sorted repo-relative paths, "outside": files outside the repo and the run dir, "unresolved": [...]}
    for `<path.py>::<function>` (path relative to run_dir unless absolute). `roots_in`: (file, names) pairs walked
    besides the function (compute.py's own code that runs it); `opaque`: {file: names} never followed (their file
    is still in the closure). Files inside the run dir are left out: the key hashes the run's models/ and data/
    whole. Search roots are those load_fn puts on sys.path: the run dir, the function file's directory, the repo's
    scripts/, and the repo root."""
    repo = os.path.realpath(repo)
    path, _, name = fn_spec.partition("::")
    fn_file = os.path.realpath(path if os.path.isabs(path) or not run_dir else os.path.join(run_dir, path))
    roots = ([os.path.realpath(run_dir)] if run_dir else []) + [os.path.dirname(fn_file),
                                                                os.path.join(repo, "scripts"), repo]
    w = Walk(repo, roots, opaque)
    for f, n in [(fn_file, name)] + [(os.path.realpath(f), n) for f, ns in roots_in for n in ns]:
        m = w.mod(f) if os.path.isfile(f) else None
        if m is None or n not in m.defs:
            w.unresolved.add(f"{w.rel(f)}::{n}: the function is not defined in its file")
    todo = [(fn_file, name)] + [(os.path.realpath(f), n) for f, ns in roots_in for n in ns]
    w.run(todo)
    run = os.path.join(os.path.realpath(run_dir), "") if run_dir else None
    files = {os.path.realpath(f) for f in w.files if os.path.isfile(f)}
    inside = {f for f in files if run and f.startswith(run)}
    return {"files": sorted(w.rel(f) for f in files - inside if f.startswith(repo + os.sep)),
            "outside": sorted(f for f in files - inside if not f.startswith(repo + os.sep)),
            "unresolved": sorted(w.unresolved)}


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, msg):
        print(("PASS " if c else "FAIL ") + msg)
        if not c:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as td:
        def put(rel, text):
            p = os.path.join(td, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(text)
        put("toolkit/__init__.py", "")
        put("toolkit/a/__init__.py", "")
        put("toolkit/a/job.py", "import os\nfrom toolkit.a import helper\nfrom toolkit.a.lib import used\n"
            "import toolkit.a.mod as M\n\ndef run(p):\n    from toolkit.a import lazy\n"
            "    return used(p) + helper.h() + M.g() + lazy.z() + os.getpid()\n\n"
            "def unrelated():\n    from toolkit.a import figs\n    return figs.draw()\n")
        put("toolkit/a/lib.py", "def used(p):\n    return 1\n\ndef other():\n    from toolkit.b import far\n")
        put("toolkit/a/helper.py", "def h():\n    return 2\n")
        put("toolkit/a/mod.py", "def g():\n    return 3\n")
        put("toolkit/a/lazy.py", "def z():\n    return 4\n")
        put("toolkit/a/figs.py", "def draw():\n    return 0\n")
        put("toolkit/a/card.yaml", "version: 1\n")
        put("toolkit/b/__init__.py", "")
        put("toolkit/b/far.py", "x = 1\n")
        put("toolkit/c/__init__.py", "")
        put("toolkit/c/dyn.py", "import importlib\n\ndef run(p):\n    return importlib.import_module(p)\n")
        put("toolkit/c/decl.py", "import importlib\nFP_DYNAMIC_IMPORTS = ('toolkit/b',)\n\n"
            "def run(p):\n    return importlib.import_module(p)\n")
        put("toolkit/c/missing.py", "from toolkit.nothere import q\n\ndef run(p):\n    return q(p)\n")
        c = closure(os.path.join(td, "toolkit/a/job.py") + "::run", td)
        want = {"toolkit/__init__.py", "toolkit/a/__init__.py", "toolkit/a/job.py", "toolkit/a/lib.py",
                "toolkit/a/helper.py", "toolkit/a/mod.py", "toolkit/a/lazy.py", "toolkit/a/card.yaml"}
        ok(set(c["files"]) == want and not c["unresolved"],
           f"IC1 the closure holds the reached modules, their packages and data, not figs.py or far.py: {c}")
        d = closure(os.path.join(td, "toolkit/c/dyn.py") + "::run", td)
        ok(any("dynamic import" in u for u in d["unresolved"]), f"IC2 an undeclared dynamic import fails closed: {d}")
        e = closure(os.path.join(td, "toolkit/c/decl.py") + "::run", td)
        ok(not e["unresolved"] and "toolkit/b/far.py" in e["files"],
           f"IC3 a declared dynamic import takes the declared tree: {e}")
        f = closure(os.path.join(td, "toolkit/c/missing.py") + "::run", td)
        ok(any("toolkit.nothere not found" in u for u in f["unresolved"]), f"IC4 a missing repo module fails closed: {f}")
        g = closure(os.path.join(td, "toolkit/a/job.py") + "::nosuch", td)
        ok(any("not defined" in u for u in g["unresolved"]), f"IC5 an undefined root function fails closed: {g}")
        put("toolkit/c/bad.py", "def run(p:\n")
        h = closure(os.path.join(td, "toolkit/c/bad.py") + "::run", td)
        ok(any("not parsed" in u for u in h["unresolved"]), f"IC6 a file that does not parse fails closed: {h}")
        put("toolkit/c/val.py", "import toolkit.a.mod as M\n\ndef run(p):\n    return getattr(M, p)()\n")
        put("toolkit/a/mod.py", "def g():\n    return 3\n\ndef g2():\n    from toolkit.b import far\n")
        v = closure(os.path.join(td, "toolkit/c/val.py") + "::run", td)
        ok("toolkit/b/far.py" in v["files"], f"IC7 a module used as a value is taken whole: {v}")
    print(f"{'OK: all self-test cases passed.' if not fails else f'FAILED: {len(fails)}'}")
    return 1 if fails else 0


if __name__ == "__main__":
    import json
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    a = sys.argv[1:]
    rd = a[a.index("--run-dir") + 1] if "--run-dir" in a else None
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(json.dumps(closure(a[0], here, rd), indent=1))
