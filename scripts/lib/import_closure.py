#!/usr/bin/env python3
"""The repo files a batch function's rows can depend on: the code its function can reach, found statically.

    python3 scripts/lib/import_closure.py --self-test
    python3 scripts/lib/import_closure.py <path.py>::<function> [--run-dir DIR]      # the closure as JSON

W0-A: the reuse key of a stored row hashes this closure, not all of scripts/ and toolkit/. Nothing is imported
(ast). From the function the walk reaches every top-level definition its code names, directly or through an import;
a class named is taken whole, a module used as a value (passed, getattr, a dunder) too. A module reached contributes
its import-time code (non-function statements, decorators, defaults, class headers), its packages' __init__.py, the
other files in its directory and its data-only subdirectories. Reached files are hashed whole.

Fails closed (`unresolved`; the caller then must not narrow the key): a repo import whose file is not found; a
dynamic import or exec site in reached code (by name or alias) of a file without a module-level literal
`FP_DYNAMIC_IMPORTS`: a tuple of repo paths (every file joins the closure, each module reached for its import-time
code and classes; `()` = loads only the closure's root), or {kind: paths} taken at each call with a literal kind
(toolkit/import_guard.py); a file not parsed; a root not defined. Non-repo names (stdlib, installed packages) are
outside: the dependency manifests stay in the key.
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
        self.defs, self.always, self.binds, self.star, self.top = {}, [], {}, [], []
        for n in self.tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not n.decorator_list:
                self.defs.setdefault(n.name, []).append(n)
                if isinstance(n, ast.ClassDef):      # its header, class-level statements, and its methods'
                    fns = [s for s in n.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))]
                    self.always.append(ast.Module(body=[*n.bases, *n.keywords, *[    # decorators and defaults
                        s for s in n.body if s not in fns], *[x for f in fns for x in (
                            *f.decorator_list, *f.args.defaults, *[d for d in f.args.kw_defaults if d])]],
                        type_ignores=[]))
                else:                                # so do a function's default values (Codex r1 P1-4)
                    self.always.append(ast.Module(body=[*n.args.defaults, *[d for d in n.args.kw_defaults if d]],
                                                  type_ignores=[]))
                continue
            if _is_main_guard(n):
                continue                                   # the CLI block never runs on import
            self.always.append(n)
            self.top += [x for x in ast.walk(n) if isinstance(x, (ast.Import, ast.ImportFrom))]
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


def _data_files(d: str) -> list[str]:
    """A module directory's data: its other files, and every file of a subdirectory holding no Python (data only)."""
    out = []
    for x in sorted(os.listdir(d)):
        p = os.path.join(d, x)
        if os.path.isfile(p) and not x.endswith((".py",) + SKIP_EXT):
            out.append(p)
        elif os.path.isdir(p) and x not in SKIP_DIRS:
            sub = [os.path.join(dp, f) for dp, ds, fs in os.walk(p) for f in fs
                   if not any(s in dp.split(os.sep) for s in SKIP_DIRS)]
            if not any(f.endswith(".py") for f in sub):
                out += [f for f in sub if not f.endswith(SKIP_EXT)]
    return out


def _is_main_guard(n) -> bool:
    """Exactly `if __name__ == "__main__":` with no else (any other test may run on import: Codex r1 P1-4)."""
    t = n.test if isinstance(n, ast.If) else None
    return (t is not None and not n.orelse and isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
            and t.left.id == "__name__" and len(t.ops) == 1 and isinstance(t.ops[0], ast.Eq)
            and isinstance(t.comparators[0], ast.Constant) and t.comparators[0].value == "__main__")


def _bound(n) -> list[str]:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [n.name]
    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
        return [n.id]
    return []


def _declared(tree, name="FP_DYNAMIC_IMPORTS"):
    """The file's FP_DYNAMIC_IMPORTS (or `name`) literal, or None when it declares none (or not as a literal of
    strings). FP_LOADERS ({loader: kinds whose load the loader checks is a class}) has the same {str: paths} form."""
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
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


def _dynamic(node, binds=None) -> int | None:
    """The line of a dynamic import or exec site: by its name, by an alias bound to one (`from importlib import
    import_module as load`), or a module's `__dict__` / `vars()` (Codex r1 P1-3)."""
    binds = binds or {}
    literal = {id(n.func) for n in ast.walk(node) if isinstance(n, ast.Call) and _literal_import(n)}
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and n.attr == "__dict__" and isinstance(n.value, ast.Name) \
                and n.value.id in binds:
            return n.lineno                                     # an imported name's namespace, read by key
        if id(n) in literal:
            continue
        # any reference, called or not (`load = importlib.import_module`, Codex r2), or an alias bound to one
        if (isinstance(n, ast.Attribute) and n.attr in DYNAMIC_ATTRS) or (isinstance(n, ast.Name) and (
                n.id in DYNAMIC_NAMES or any(a in DYNAMIC_ATTRS | DYNAMIC_NAMES for _, a in binds.get(n.id, ())))):
            return n.lineno
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "vars" and n.args \
                and isinstance(n.args[0], ast.Name) and n.args[0].id in binds:
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
    def __init__(self, repo: str, roots: list[str], opaque: dict | None = None, run_dir: str | None = None):
        self.repo, self.roots, self.opaque = os.path.realpath(repo), roots, opaque or {}
        self.run_root = os.path.join(os.path.realpath(run_dir), "") if run_dir else None
        self.mods, self.seen, self.files, self.unresolved = {}, set(), set(), set()

    def rel(self, p: str) -> str:
        return os.path.relpath(p, self.repo).replace(os.sep, "/")

    def find(self, name: str) -> str | None:
        """The module file (a package before a same-named module, as Python does). A name two roots resolve to
        different files fails closed: which one runs depends on sys.path at run time (Codex r2)."""
        parts, hits = name.split("."), []
        for r in self.roots:
            p = os.path.join(r, *parts)
            hits += [os.path.realpath(c) for c in (os.path.join(p, "__init__.py"), p + ".py") if os.path.isfile(c)][:1]
        if len(set(hits)) > 1:
            self.unresolved.add(f"import {name}: found in more than one search root ({len(set(hits))} files)")
        return hits[0] if hits else None

    def decl(self, m: _Mod, name: str = "FP_DYNAMIC_IMPORTS"):
        """A file's declaration, honoured only in repo code (scripts/, toolkit/) outside the run dir: run code that
        declares its own dynamic imports is not believed (Codex r2)."""
        code = any(m.path.startswith(os.path.join(self.repo, d, "")) for d in ("scripts", "toolkit"))
        return _declared(m.tree, name) if code and not (self.run_root and m.path.startswith(self.run_root)) else None

    def reexports(self, m: _Mod) -> bool:
        """`from m import *` would bind a name m imports from repo code (a re-export the walk does not follow)."""
        alls = _declared(m.tree, "__all__")
        for n in m.top:
            mod = m._absolute(n) if isinstance(n, ast.ImportFrom) else None
            for a in n.names:
                src = mod if isinstance(n, ast.ImportFrom) else a.name
                name = a.asname or a.name.split(".")[0]
                if src is None or (src and self.is_repo_top(src.split(".")[0])):
                    if a.name == "*" or (not name.startswith("_") and (alls is None or name in alls)):
                        return True
        return False

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
                self.files.update(_data_files(os.path.dirname(path)))
                pkg = m.pkg.split(".") if m.pkg else []
                for k in range(1, len(pkg) + 1):
                    init = self.find(".".join(pkg[:k]))
                    if init:
                        todo.append((init, MODULE))
                for s in m.star:
                    self.import_target(m, s, None, todo, whole=True, star=True)
                nodes = m.always
            else:
                todo.append((path, MODULE))
                nodes = None if name in self.opaque.get(path, ()) else m.defs.get(name)
                if nodes is None and name not in self.opaque.get(path, ()):
                    for mod, attr in m.binds.get(name, ()):         # a re-export (Codex r1 P1-2)
                        self.import_target(m, mod, attr, todo)
                    for s in m.star:
                        self.import_target(m, s, name, todo)
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
        line = _dynamic(node, m.binds)
        if line is not None:
            decl = self.decl(m)
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
            decl = self.decl(tm) if tm else None
            if isinstance(decl, dict):
                a0 = call.args[0] if call.args else None
                kind = a0.value if isinstance(a0, ast.Constant) and isinstance(a0.value, str) else None
                f = call.func
                fname = f.attr if isinstance(f, ast.Attribute) else next(
                    (a for _, a in m.binds.get(getattr(f, "id", None), ()) if a), getattr(f, "id", None))
                loaders = self.decl(tm, "FP_LOADERS") or {}
                if fname in loaders and kind not in loaders[fname]:    # Codex r2: a function, not walked
                    self.unresolved.add(f"{self.rel(m.path)}:{call.lineno}: {fname}() loads by a data-supplied name "
                                        f"(a loader with no class check for kind {kind!r}): not followed")
                self.declared(tm, decl[kind] if kind in decl else tuple(p for ps in decl.values() for p in ps), todo)
        for n in ast.walk(node):                       # an import runs its module's import-time code
            if isinstance(n, ast.Import):
                for a in n.names:
                    self.import_target(m, a.name, None, todo, module_only=True)
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    self.import_target(m, m._absolute(n), None if a.name == "*" else a.name, todo,
                                       whole=a.name == "*", module_only=a.name != "*", star=a.name == "*")
        for chain in _chains(node):
            head = chain[0]
            if head in m.defs:
                todo.append((m.path, head))
            for mod, attr in m.binds.get(head, ()):
                self.import_target(m, mod, attr, todo, rest=chain[1:])

    def import_target(self, m: _Mod, mod, attr, todo, rest=(), whole=False, module_only=False, star=False) -> None:
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
        tm = self.mod(best) if star else None
        if tm is not None and self.reexports(tm):        # Codex r2: not followed, so not guessed
            self.unresolved.add(f"{self.rel(m.path)}: from {mod} import *: {mod} re-exports names it imports")
        if module_only:
            todo.append((best, MODULE))
        elif whole or not nxt or nxt[0].startswith("__"):
            todo.append((best, "*"))                    # a module used as a value (or its dunders): all of it
        else:
            todo.append((best, nxt[0]))


def closure(fn_spec: str, repo: str, run_dir: str | None = None, roots_in: tuple = (), opaque=None) -> dict:
    """{"files": sorted repo-relative paths, "outside": files outside the repo and the run dir, "unresolved": [...]}
    for `<path.py>::<function>` (path relative to run_dir unless absolute). `roots_in`: (file, names) pairs walked
    besides the function (compute.py's own code that runs it); `opaque`: {file: names} never followed (their file
    is still in the closure). Files inside the run dir go to "run" (run-relative): the caller hashes those outside
    the run's models/ and data/. Search roots in load_fn's sys.path order (Codex r1 P1-5): the repo's scripts/, the
    function file's directory, the run dir, then the repo root."""
    repo = os.path.realpath(repo)
    path, _, name = fn_spec.partition("::")
    fn_file = os.path.realpath(path if os.path.isabs(path) or not run_dir else os.path.join(run_dir, path))
    roots = [os.path.join(repo, "scripts"), os.path.dirname(fn_file)] + (
        [os.path.realpath(run_dir)] if run_dir else []) + [repo]
    w = Walk(repo, roots, opaque, run_dir)
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
            "run": sorted(os.path.relpath(f, run) for f in inside), "unresolved": sorted(w.unresolved)}


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
        for rel, fn in (("a/helper", "h"), ("a/mod", "g"), ("a/lazy", "z"), ("a/figs", "draw")):
            put(f"toolkit/{rel}.py", f"def {fn}():\n    return 2\n")
        for rel, text in (("a/card.yaml", "version: 1\n"), ("b/__init__.py", ""), ("b/far.py", "x = 1\n"),
                          ("c/__init__.py", "")):
            put(f"toolkit/{rel}", text)
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
        # Codex r1: a package re-export, a default value run on import, an aliased dynamic import, a data subdir
        put("toolkit/r/__init__.py", "from toolkit.r.impl import score\n")
        put("toolkit/r/impl.py", "def score(p):\n    from toolkit.b import far\n    return far.x\n")
        put("toolkit/r/data/t.csv", "a\n")
        put("toolkit/c/re.py", "from toolkit.r import score\n\ndef run(p):\n    return score(p)\n")
        r = closure(os.path.join(td, "toolkit/c/re.py") + "::run", td)
        ok({"toolkit/r/impl.py", "toolkit/b/far.py", "toolkit/r/data/t.csv"} <= set(r["files"]),
           f"IC8 a re-exported function's body and a data-only subdirectory are in the closure: {r}")
        put("toolkit/c/dflt.py", "from toolkit.b import far\nX = {}\n\ndef unused(x=far.init()):\n    pass\n\n"
            "def run(p):\n    return X\n")
        dd = closure(os.path.join(td, "toolkit/c/dflt.py") + "::run", td)
        ok("toolkit/b/far.py" in dd["files"], f"IC9 a default value runs on import: its code is in the closure: {dd}")
        put("toolkit/c/alias.py", "from importlib import import_module as load\n\ndef run(p):\n    return load(p)\n")
        al = closure(os.path.join(td, "toolkit/c/alias.py") + "::run", td)
        ok(any("dynamic import" in u for u in al["unresolved"]), f"IC10 an aliased dynamic import fails closed: {al}")
        # Codex r2 (η): what the walk cannot follow statically fails closed instead of being guessed
        put("toolkit/s/__init__.py", "")
        put("toolkit/s/impl.py", "def score(p):\n    from toolkit.b import far\n    return far.x\n")
        put("toolkit/s/api.py", "from toolkit.s.impl import score\n")
        put("toolkit/s/plain.py", "import os\n\ndef score(p):\n    return 1\n")
        put("toolkit/c/star.py", "from toolkit.s.api import *\n\ndef run(p):\n    return score(p)\n")
        put("toolkit/c/star2.py", "from toolkit.s.plain import *\n\ndef run(p):\n    return score(p)\n")
        st, st2 = (closure(os.path.join(td, f"toolkit/c/{n}.py") + "::run", td) for n in ("star", "star2"))
        ok(any("re-exports" in u for u in st["unresolved"]) and not st2["unresolved"],
           f"IC11 a star import of a module that re-exports fails closed; of one that does not, it does not: {st} {st2}")
        put("toolkit/g/__init__.py", "")
        put("toolkit/g/guard.py", "import importlib\nFP_DYNAMIC_IMPORTS = {'engine': ('toolkit/b',), 'oracle': ('toolkit/b',)}\n"
            "FP_LOADERS = {'resolve': ('engine',), 'attr': (), 'module': ()}\n\n"
            "def resolve(kind, ref):\n    return importlib.import_module(ref)\n\n"
            "def attr(kind, mod, name):\n    return getattr(importlib.import_module(mod), name)\n")
        put("toolkit/c/gattr.py", "from toolkit.g import guard as G\n\ndef run(p):\n"
            "    return G.attr('oracle', p['m'], 'gen_' + p['n'])()\n")
        put("toolkit/c/gres.py", "from toolkit.g import guard as G\n\ndef run(p):\n    return G.resolve('engine', p['r'])\n")
        ga, gr = (closure(os.path.join(td, f"toolkit/c/{n}.py") + "::run", td) for n in ("gattr", "gres"))
        ok(any("attr" in u and "loader" in u for u in ga["unresolved"]) and not gr["unresolved"]
           and "toolkit/b/far.py" in gr["files"],
           f"IC12 a function loaded through a guard loader fails closed; a checked class load does not: {ga} {gr}")
        rd = os.path.join(td, "run")
        put("run/models/job.py", "import importlib\nFP_DYNAMIC_IMPORTS = ()\n\ndef run(p):\n"
            "    return importlib.import_module(p['m']).X\n")
        rj = closure(os.path.join(rd, "models/job.py") + "::run", td, rd)
        ok(any("dynamic import" in u for u in rj["unresolved"]),
           f"IC13 run code cannot declare its own dynamic imports: a non-literal import in it fails closed: {rj}")
    print(f"{'OK: all self-test cases passed.' if not fails else f'FAILED: {len(fails)}'}")
    return 1 if fails else 0


if __name__ == "__main__":
    import json
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    a, here = sys.argv[1:], os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    print(json.dumps(closure(a[0], here, a[a.index("--run-dir") + 1] if "--run-dir" in a else None), indent=1))
