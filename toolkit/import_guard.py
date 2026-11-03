#!/usr/bin/env python3
"""One guard for every import whose name comes from data: a spec, a benchmark pack, or a run artifact.

    python3 toolkit/import_guard.py --self-test

Trust model (run console review, rounds 2-3): agent-written artifacts in a run dir may be hostile, and an agent
drafts the decision spec. A name like "subprocess:Popen" in a spec must never reach importlib. Every such import
goes through `resolve(kind, "module:attr")` or `module(kind, "module")`, which check the name against the kind's
policy before anything is imported; scripts/import_audit.py fails CI on a dynamic import that does not.

Policies (a module must also be a file of this repo, checked without importing it):
  engine      modules under toolkit.engines (an Engine class), plus toolkit.spec.priors:ToyPriorEngine
  oracle      modules under toolkit.engines whose last part is `oracles` (benchmark data generators), and
              toolkit.engines.benchmark (its self-test generator, used across worker processes)
  calibrator  toolkit.analysis.calibration, or a module under toolkit.engines (benchmark calibrators)
Attribute names starting with "_" are refused. Self-tests may allow a module of their own in-process
(`allow_for_test`); nothing reads that from a file.
"""
from __future__ import annotations

import importlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICIES = {
    "engine": {"prefixes": ("toolkit.engines.",), "modules": (), "refs": ("toolkit.spec.priors:ToyPriorEngine",),
               "suffix": None},
    # toolkit.engines.benchmark holds the self-test's generator used across worker processes
    "oracle": {"prefixes": ("toolkit.engines.",), "modules": ("toolkit.engines.benchmark",), "refs": (),
               "suffix": "oracles"},
    "calibrator": {"prefixes": ("toolkit.engines.",), "modules": ("toolkit.analysis.calibration",), "refs": (),
                   "suffix": None},
}
_TEST_MODULES: dict = {k: set() for k in POLICIES}
_MOD = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_ATTR = re.compile(r"[A-Za-z]\w*")


class ImportRefused(ValueError):
    """A data-supplied module or attribute name the kind's policy does not allow (nothing was imported)."""


def allow_for_test(kind: str, module: str) -> None:
    """Self-tests only: allow a module the test made (in sys.modules or on a temporary path), in this process."""
    _TEST_MODULES[kind].add(module)


def _repo_module(mod: str) -> bool:
    rel = os.path.join(ROOT, *mod.split("."))
    return os.path.isfile(rel + ".py") or os.path.isfile(os.path.join(rel, "__init__.py"))


def check_module(kind: str, mod) -> str:
    """`mod` if the kind's policy allows it, else ImportRefused. Imports nothing."""
    if kind not in POLICIES:
        raise ImportRefused(f"unknown import kind {kind!r}")
    if not isinstance(mod, str) or not _MOD.fullmatch(mod):
        raise ImportRefused(f"{kind} module {str(mod)[:80]!r}: not a dotted module name")
    if mod in _TEST_MODULES[kind]:
        return mod
    p = POLICIES[kind]
    allowed = mod in p["modules"] or (mod.startswith(p["prefixes"]) if p["prefixes"] else False)
    if allowed and p["suffix"] and mod.rsplit(".", 1)[-1] != p["suffix"] and mod not in p["modules"]:
        allowed = False
    if not allowed or not _repo_module(mod):
        raise ImportRefused(f"{kind} {mod}: not allowed (policy: {_describe(kind)}); refused before any import")
    return mod


def check_ref(kind: str, ref) -> tuple[str, str]:
    """(module, attribute) of an allowed "module:attr", else ImportRefused. Imports nothing."""
    if not isinstance(ref, str) or ref.count(":") != 1:
        raise ImportRefused(f"{kind} reference {str(ref)[:80]!r}: not 'module:attr'")
    mod, attr = ref.split(":")
    if not _ATTR.fullmatch(attr):
        raise ImportRefused(f"{kind} {ref}: attribute {attr!r} is not a public name")
    if ref in POLICIES.get(kind, {}).get("refs", ()):
        return mod, attr
    return check_module(kind, mod), attr


def module(kind: str, mod: str):
    """Import an allowed module."""
    return importlib.import_module(check_module(kind, mod))


def resolve(kind: str, ref: str):
    """The attribute an allowed "module:attr" names (imported, not called)."""
    mod, attr = check_ref(kind, ref)
    m = importlib.import_module(mod)
    if not hasattr(m, attr):
        raise ImportRefused(f"{kind} {ref}: {mod} has no {attr}")
    return getattr(m, attr)


def attr(kind: str, mod: str, name: str):
    """getattr on an allowed module, for a data-supplied attribute name."""
    if not isinstance(name, str) or not _ATTR.fullmatch(name):
        raise ImportRefused(f"{kind} attribute {str(name)[:80]!r} is not a public name")
    m = module(kind, mod)
    if not hasattr(m, name):
        raise ImportRefused(f"{kind} {mod}: no {name}")
    return getattr(m, name)


def _describe(kind: str) -> str:
    p = POLICIES[kind]
    bits = [f"modules under {', '.join(p['prefixes'])}" + (f" named *.{p['suffix']}" if p["suffix"] else "")]
    bits += list(p["modules"]) + list(p["refs"])
    return "; ".join(bits)


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    seen, real = [], importlib.import_module
    importlib.import_module = lambda name, *a, **k: seen.append(name) or real(name, *a, **k)
    try:
        cases = [("engine", "subprocess:Popen"), ("engine", "os:system"), ("engine", "toolkit.alloc.costs:x"),
                 ("oracle", "os:getcwd"), ("oracle", "toolkit.engines.contract:ToyEngine"),
                 ("calibrator", "subprocess:run"), ("calibrator", "toolkit.analysis.calibration:_private"),
                 ("engine", "toolkit.engines.nope:X"), ("engine", "toolkit.engines.contract:ToyEngine:x")]
        res = []
        for kind, ref in cases:
            try:
                resolve(kind, ref)
                res.append(False)
            except ImportRefused:
                res.append(True)
    finally:
        importlib.import_module = real
    bad = [m for m in seen if not m.startswith("toolkit.")]
    ok(all(res) and not bad, f"IG1 names outside each policy are refused before any import ({res}; imported {seen})")
    ok(resolve("engine", "toolkit.engines.contract:ToyEngine").__name__ == "ToyEngine"
       and module("oracle", "toolkit.engines.compartmental.benchmarks.oracles") is not None
       and callable(resolve("calibrator", "toolkit.analysis.calibration:posterior"))
       and resolve("engine", "toolkit.spec.priors:ToyPriorEngine").__name__ == "ToyPriorEngine",
       "IG2 the shipped engines, oracles and calibrator resolve")
    try:
        resolve("engine", "toolkit.engines.malaria.benchmarks.render_notes:write")
        r4 = "resolved"
    except ImportRefused:
        r4 = "refused"
    ok(r4 == "refused", f"IG4 the engine policy resolves only classes with the Engine contract, not a function ({r4})")
    import types
    sys.modules["_ig_test_mod"] = types.ModuleType("_ig_test_mod")
    sys.modules["_ig_test_mod"].f = lambda: 1
    try:
        resolve("calibrator", "_ig_test_mod:f")
        r = False
    except ImportRefused:
        r = True
    allow_for_test("calibrator", "_ig_test_mod")
    ok(r and resolve("calibrator", "_ig_test_mod:f")() == 1, "IG3 a self-test's own module needs allow_for_test")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        sys.exit(_self_test())
    print(__doc__)
