"""Structure metrics: how compartmentalized the codebase is, measured the same
way on every PR so each restructuring step shows its effect.

    python3 scripts/structure_metrics.py                 # the working tree
    python3 scripts/structure_metrics.py --ref origin/main --against HEAD   # a change, as deltas
    python3 scripts/structure_metrics.py --json
    python3 scripts/structure_metrics.py --self-test

Measures (tracked Python files under main.py, agents/, scripts/, toolkit/, cloud/):
  lines            non-blank lines per area, and the largest file's share of its area
  big_files        files over 1,500 non-blank lines
  duplicate_fns    function bodies (>= 5 statements) defined identically in more than one
                   file (name and docstring ignored): logic that must be fixed in every copy
  shared_names     function names defined in 3+ files, interface names excepted (looser:
                   re-implemented helpers); nested functions such as self-test fakes are ignored
  gate_interface   scripts exposing a module-level gate_findings(run_dir): checks on the
                   common interface the gate collects (higher is better)
  own_yaml         scripts that parse YAML themselves instead of through scripts/lib
  lib_users        scripts that import scripts/lib
  self_tested      share of scripts with a --self-test
  files_per_pr     median files changed by the last 20 merged PRs (first-parent history)

The report is information, not a gate: nothing fails on a number.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import statistics
import subprocess
import sys

AREAS = ("main.py", "agents/", "scripts/lib/", "scripts/", "toolkit/", "cloud/")
BIG = 1500
MIN_STMTS = 5
INTERFACE = ("gate_findings", "evaluate", "check", "run", "compare", "measure", "validate")   # meant to recur


def _git(*args, cwd="."):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True)


def files_at(ref: str | None, cwd=".") -> dict[str, str]:
    """{path: source} of tracked Python files in the areas, at a ref or in the working tree."""
    names = _git("ls-tree", "-r", "--name-only", ref, cwd=cwd).splitlines() if ref else \
        _git("ls-files", cwd=cwd).splitlines()
    out = {}
    for n in names:
        if not n.endswith(".py") or area_of(n) is None:
            continue
        try:
            out[n] = _git("show", f"{ref}:{n}", cwd=cwd) if ref else open(os.path.join(cwd, n)).read()
        except (subprocess.CalledProcessError, OSError, UnicodeDecodeError):
            continue
    return out


def area_of(path: str) -> str | None:
    for a in AREAS:   # scripts/lib/ before scripts/
        if path == a or (a.endswith("/") and path.startswith(a)):
            return a
    return None


def _body_hash(fn: ast.FunctionDef) -> str | None:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if sum(1 for _ in ast.walk(ast.Module(body=body, type_ignores=[])) if isinstance(_, ast.stmt)) < MIN_STMTS:
        return None
    dump = ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False)
    return hashlib.sha256((ast.dump(fn.args, annotate_fields=False) + dump).encode()).hexdigest()[:16]


def _defs(tree):
    """Module-level functions and class methods; functions nested in others
    (self-test fakes, closures) are not part of a file's surface."""
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield n
        elif isinstance(n, ast.ClassDef):
            yield from (m for m in n.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)))


def measure(files: dict[str, str], git_cwd: str | None = ".") -> dict:
    lines, biggest = {}, {}
    big, by_hash, by_name = [], {}, {}
    own_yaml, lib_users, scripts, tested, interface = [], [], [], [], []
    for path, src in sorted(files.items()):
        a = area_of(path)
        n = sum(1 for ln in src.splitlines() if ln.strip())
        lines[a] = lines.get(a, 0) + n
        if n > biggest.get(a, ("", 0))[1]:
            biggest[a] = (path, n)
        if n > BIG:
            big.append((path, n))
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        if any(isinstance(n, ast.FunctionDef) and n.name == "gate_findings" for n in tree.body):
            interface.append(path)
        for node in _defs(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                h = _body_hash(node)
                if h:
                    by_hash.setdefault(h, set()).add((path, node.name))
                if not node.name.startswith("__") and node.name not in ("main", "_self_test", "_run_self_test") \
                        and node.name not in INTERFACE:
                    by_name.setdefault(node.name, set()).add(path)
        if a in ("scripts/", "scripts/lib/") and not path.endswith("__init__.py"):
            scripts.append(path)
            if "--self-test" in src:
                tested.append(path)
            if a == "scripts/":
                if "yaml.safe_load" in src or "yaml.load(" in src:
                    own_yaml.append(path)
                if "from lib" in src or "import lib." in src or "scripts.lib" in src:
                    lib_users.append(path)
    dups = sorted(([sorted(v) for v in by_hash.values() if len({p for p, _ in v}) > 1]), key=lambda g: -len(g))
    shared = sorted(((k, sorted(v)) for k, v in by_name.items() if len(v) >= 3), key=lambda kv: -len(kv[1]))
    out = {
        "lines": lines,
        "largest_share": {a: {"file": p, "lines": n, "share": round(n / lines[a], 3)} for a, (p, n) in biggest.items()},
        "big_files": sorted(big, key=lambda x: -x[1]),
        "duplicate_fns": {"groups": len(dups), "copies": sum(len(g) for g in dups), "top": dups[:8]},
        "shared_names": {"count": len(shared), "top": [(k, len(v)) for k, v in shared[:10]]},
        "gate_interface": len(interface),
        "own_yaml": len(own_yaml),
        "lib_users": len(lib_users),
        "scripts": len([s for s in scripts if s.startswith("scripts/") and not s.startswith("scripts/lib/")]),
        "self_tested": round(len(tested) / len(scripts), 3) if scripts else None,
    }
    if git_cwd is not None:
        out["files_per_pr"] = files_per_pr(git_cwd)
    return out


def files_per_pr(cwd=".", n=20):
    """Median files changed by the last n merge commits on the first-parent line."""
    try:
        shas = _git("log", "--first-parent", "--merges", f"-{n}", "--format=%H", cwd=cwd).split()
        counts = [len(_git("diff", "--name-only", f"{s}^1", s, cwd=cwd).split()) for s in shas]
    except subprocess.CalledProcessError:
        return None
    return {"median": statistics.median(counts), "max": max(counts), "prs": len(counts)} if counts else None


KEYS = (("lines total", lambda m: sum(m["lines"].values())),
        ("files over 1,500 lines", lambda m: len(m["big_files"])),
        ("duplicate function groups", lambda m: m["duplicate_fns"]["groups"]),
        ("duplicate function copies", lambda m: m["duplicate_fns"]["copies"]),
        ("names defined in 3+ files", lambda m: m["shared_names"]["count"]),
        ("checks exposing gate_findings(run_dir)", lambda m: m["gate_interface"]),
        ("scripts parsing YAML themselves", lambda m: m["own_yaml"]),
        ("scripts using scripts/lib", lambda m: m["lib_users"]),
        ("share of scripts self-tested", lambda m: m["self_tested"]))


def render(m: dict, base: dict | None = None) -> str:
    out = ["## Structure metrics", "", "| measure | value |" + (" change |" if base else ""),
           "|---|---|" + ("---|" if base else "")]
    for label, f in KEYS:
        v = f(m)
        row = f"| {label} | {v} |"
        if base:
            d = v - f(base) if isinstance(v, (int, float)) and f(base) is not None else None
            row += f" {'+' if d and d > 0 else ''}{round(d, 3) if d else 0} |"
        out.append(row)
    out += ["", "| area | lines | largest file (share) |", "|---|---|---|"]
    for a in AREAS:
        if a in m["lines"]:
            ls = m["largest_share"][a]
            out.append(f"| {a} | {m['lines'][a]} | {ls['file']} ({ls['share']:.0%}) |")
    if m.get("files_per_pr"):
        f = m["files_per_pr"]
        out += ["", f"Files changed per merged PR (last {f['prs']}): median {f['median']}, max {f['max']}."]
    if m["duplicate_fns"]["top"]:
        out += ["", "Largest duplicate groups:"]
        out += ["- " + ", ".join(f"{p}::{n}" for p, n in g) for g in m["duplicate_fns"]["top"][:5]]
    return "\n".join(out)


def _self_test() -> int:
    fails = []

    def ok(c, msg):
        print(("PASS " if c else "FAIL ") + msg)
        if not c:
            fails.append(msg)

    body = "    x = 1\n    y = 2\n    z = x + y\n    w = z * 2\n    return w\n"
    files = {
        "scripts/a.py": f'import yaml\nC = yaml.safe_load("a: 1")\ndef f(p):\n    """doc a"""\n{body}def main():\n    pass\n# --self-test\n',
        "scripts/b.py": f"from lib import safe_io\ndef g(p):\n{body}def gate_findings(d):\n    return []\n"
                        "def _self_test():\n    def boom():\n        pass\n",
        "scripts/c.py": "def tiny(p):\n    return p\n",
        "scripts/lib/x.py": "def h(q):\n" + body + "# --self-test\n",
        "agents/z.py": "\n".join(f"v{i} = {i}" for i in range(BIG + 1)),
        "docs/skip.py": f"def f(p):\n{body}",
    }
    files = {k: v for k, v in files.items() if area_of(k)}
    m = measure(files, git_cwd=None)
    ok("docs/skip.py" not in files and m["lines"]["agents/"] == BIG + 1, "SM1 areas: docs/ is out, agents/ counted")
    ok(m["duplicate_fns"]["groups"] == 1 and m["duplicate_fns"]["copies"] == 2,
       f"SM2 same body under another name and docstring is one duplicate group of 2 (h has other args): {m['duplicate_fns']}")
    ok(m["own_yaml"] == 1 and m["lib_users"] == 1 and m["scripts"] == 3, "SM3 own YAML parsing and lib use counted")
    ok(m["self_tested"] == round(2 / 4, 3), "SM4 self-test share over scripts/ and scripts/lib/")
    ok(m["big_files"] == [("agents/z.py", BIG + 1)], "SM5 a file over the size line is listed")
    ok(m["gate_interface"] == 1, "SM8 a module-level gate_findings counts as the check interface")
    m2 = measure({**files, "scripts/d.py": "def _self_test():\n    def boom():\n        pass\n",
                  "scripts/e.py": "def _self_test():\n    def boom():\n        pass\n"}, git_cwd=None)
    ok(m2["shared_names"]["count"] == 0, f"SM9 nested self-test fakes are not shared helpers: {m2['shared_names']}")
    ok(area_of("scripts/lib/x.py") == "scripts/lib/" and area_of("main.py") == "main.py" and area_of("README.md") is None,
       "SM6 area mapping")
    r = render(m, m)
    ok("| duplicate function groups | 1 | 0 |" in r, "SM7 render with a base shows changes")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv) -> int:
    if "--self-test" in argv:
        return _self_test()
    ref = argv[argv.index("--ref") + 1] if "--ref" in argv else None
    against = argv[argv.index("--against") + 1] if "--against" in argv else None
    if against:
        base, m = measure(files_at(ref), git_cwd=None), measure(files_at(against))
    else:
        base, m = None, measure(files_at(ref))
    print(json.dumps(m, indent=1, default=list) if "--json" in argv else render(m, base))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
