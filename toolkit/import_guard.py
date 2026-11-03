#!/usr/bin/env python3
"""One guard for every import whose name comes from data: a spec, a benchmark pack, or a run artifact.

    python3 toolkit/import_guard.py --self-test

Trust model (run console review, rounds 2-3): agent-written artifacts in a run dir may be hostile, and an agent
drafts the decision spec. A name like "subprocess:Popen" in a spec must never reach importlib. Every such import
goes through `resolve(kind, "module:attr")` or `module(kind, "module")`, which check the name against the kind's
policy before anything is imported; scripts/import_audit.py fails CI on a dynamic import that does not.

Policies (a module must also be a file of this repo, checked without importing it):
  engine      modules under toolkit.engines (an Engine class), plus toolkit.spec.priors:ToyPriorEngine (a toy that
              reads its priors file and writes nothing)
  oracle      modules under toolkit.engines whose last part is `oracles` (benchmark data generators), and
              toolkit.engines.benchmark (its self-test generator, used across worker processes)
  calibrator  toolkit.analysis.calibration, or a module under toolkit.engines (benchmark calibrators)
Attribute names starting with "_" are refused. Self-tests may allow a module of their own in-process
(`allow_for_test`); nothing reads that from a file. The self-test toy engines (TEST_ONLY_REFS) are enabled only by
code in a self-test process (`allow_test_refs`) and reach its worker processes as a pool initializer's argument
(`pool_kwargs`), never through the environment.
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
# Self-test toy engines that are never allowed in production (ToySpecEngine appends to a call log). Only these
# references can be enabled, and only by code in a self-test process (allow_test_refs); no file and no environment
# variable is read (Codex P1 on #133: an agent-set environment must enable nothing). The worker processes a self-test
# starts receive the registration explicitly, as their pool initializer's argument (pool_kwargs, init_worker).
TEST_ONLY_REFS = {"engine": ("toolkit.spec.toy_engines:ToySpecEngine", "toolkit.spec.toy_engines:ToySpecRateEngine")}
_TEST_REFS: set = set()          # "kind|ref", this process only
_TEST_STATE: dict = {}           # plain values the self-test hands its workers with the registration (toy log root)
ENGINE_METHODS = ("describe", "parameters", "run")      # the Engine contract (toolkit/engines/contract.py)
_MOD = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_ATTR = re.compile(r"[A-Za-z]\w*")


class ImportRefused(ValueError):
    """A data-supplied module or attribute name the kind's policy does not allow (nothing was imported)."""


def _checked(kind: str, refs) -> list:
    refs = list(refs)
    bad = [r for r in refs if r not in TEST_ONLY_REFS.get(kind, ())]
    if bad:
        raise ImportRefused(f"{kind} {bad}: only the self-test toy engines {TEST_ONLY_REFS.get(kind, ())} can be "
                            "enabled for a test")
    return refs


def allow_test_refs(kind: str, refs, state: dict | None = None) -> None:
    """Self-tests only: enable named self-test toy engines (TEST_ONLY_REFS) in this process; any other reference is
    refused. `state` (plain values, e.g. the toy call log's root) travels with the registration to the worker
    processes started with pool_kwargs()."""
    _TEST_REFS.update(f"{kind}|{r}" for r in _checked(kind, refs))
    if state:
        _TEST_STATE.update(state)


def test_state(key: str):
    """A value a self-test registered with allow_test_refs (in this process, or in its parent through init_worker)."""
    return _TEST_STATE.get(key)


def worker_registration() -> dict | None:
    """This process's test registration, for a worker process's initializer; None in production."""
    if not _TEST_REFS:
        return None
    return {"refs": sorted(_TEST_REFS), "state": dict(_TEST_STATE)}


def init_worker(registration: dict | None) -> None:
    """Worker initializer: apply the registration the parent passed explicitly (each reference checked again)."""
    if not registration:
        return
    for item in registration.get("refs") or []:
        kind, _, ref = str(item).partition("|")
        _checked(kind, [ref])
        _TEST_REFS.add(f"{kind}|{ref}")
    _TEST_STATE.update(registration.get("state") or {})


def pool_kwargs() -> dict:
    """ProcessPoolExecutor keyword arguments that carry this process's test registration to its workers: {} in
    production (nothing registered), else the initializer and its argument."""
    reg = worker_registration()
    return {"initializer": init_worker, "initargs": (reg,)} if reg else {}


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
    if ref in TEST_ONLY_REFS.get(kind, ()):        # never by module prefix: only a self-test's registration
        if f"{kind}|{ref}" in _TEST_REFS and _repo_module(mod):
            return mod, attr
        raise ImportRefused(f"{kind} {ref}: a self-test toy engine, refused outside a self-test")
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
    obj = getattr(m, attr)
    if kind == "engine" and not (isinstance(obj, type) and all(callable(getattr(obj, x, None)) for x in ENGINE_METHODS)):
        raise ImportRefused(f"engine {ref}: not a class implementing the Engine contract ({', '.join(ENGINE_METHODS)})")
    return obj


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


def _ref_state(ref: str) -> str:
    """IG6 worker: whether this process allows `ref` (a spawned worker)."""
    try:
        check_ref("engine", ref)
        return "allowed"
    except ImportRefused:
        return "refused"


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
    # IG5 (Codex P1 on #133): no environment variable enables a test-only reference; a fresh process with the former
    # MA_IMPORT_GUARD_TEST_REFS set refuses them, and a pool started there carries no registration to its workers.
    import json
    import subprocess
    toys = list(TEST_ONLY_REFS["engine"])
    code = ("import sys, json; sys.path.insert(0, %r)\n"
            "from toolkit import import_guard as G\n"
            "out = {}\n"
            "for r in %r:\n"
            "    try:\n"
            "        G.check_ref('engine', r); out[r] = 'allowed'\n"
            "    except G.ImportRefused:\n"
            "        out[r] = 'refused'\n"
            "out['pool'] = repr(getattr(G, 'pool_kwargs', lambda: 'missing')())\n"
            "print(json.dumps(out))") % (ROOT, toys)
    env = dict(os.environ, MA_IMPORT_GUARD_TEST_REFS=json.dumps([f"engine|{r}" for r in toys] + toys))
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=ROOT)
    got = json.loads(res.stdout or "{}") if res.returncode == 0 else {}
    ok(got and all(got.get(r) == "refused" for r in toys) and got.get("pool") == "{}",
       f"IG5 the environment enables no test-only reference, and a production pool gets no initializer: "
       f"{got or res.stderr[-300:]}")
    # IG6: a registration made in this process reaches a worker only through the explicit pool initializer
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing as mp
    try:
        allow_test_refs("engine", toys[:1])
        kw = pool_kwargs()
        with ProcessPoolExecutor(1, mp_context=mp.get_context("spawn"), **kw) as ex:
            with_init = ex.submit(_ref_state, toys[0]).result(60)
        with ProcessPoolExecutor(1, mp_context=mp.get_context("spawn")) as ex:
            without = ex.submit(_ref_state, toys[0]).result(60)
        ig6 = (with_init, without)
    except Exception as e:  # noqa: BLE001 -- reported in the case
        ig6 = (f"{type(e).__name__}: {e}"[:200], None)
    ok(ig6 == ("allowed", "refused"), f"IG6 a worker allows a test reference only with the pool initializer ({ig6})")
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
