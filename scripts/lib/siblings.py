"""Load scripts/<name>.py by path under a private module name.

    python3 scripts/lib/siblings.py --self-test

A run puts its own models/ on sys.path, and a run's module may share a name
with a harness script (E7 attempt 7: models/pipeline.py), so a plain import
can return the run's code. `load_private` imports the harness file by path
under `<prefix><name>` in sys.modules, so the run's module is never picked
up and never replaced. Phase 48 α: the copies in data_use, mechanism_manifest
and calibration_spec; each passes its old prefix, so the sys.modules keys and
the module objects they share are unchanged. (The validator's
`_load_sibling` has different rules, plain name first, and stays there.)
"""
from __future__ import annotations

import importlib.util
import os
import sys

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_private(name: str, prefix: str):
    """scripts/<name>.py as sys.modules[prefix + name], loaded once; a module
    whose import raises is removed again, so the next call retries."""
    key = f"{prefix}{name}"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, os.path.join(SCRIPTS, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return mod


def _self_test() -> int:
    global SCRIPTS
    import tempfile
    fails = []

    def ok(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            fails.append(label)

    # S1: loads the harness file under the private key, once.
    m = load_private("data_schema", "_sib_selftest_")
    ok(sys.modules.get("_sib_selftest_data_schema") is m
       and os.path.realpath(m.__file__) == os.path.realpath(os.path.join(SCRIPTS, "data_schema.py")),
       "S1 private key, harness file")
    ok(load_private("data_schema", "_sib_selftest_") is m, "S1b loaded once")
    # S2: a module of the same plain name elsewhere on sys.path is not used.
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "data_schema.py"), "w") as f:
        f.write("RUN_COPY = True\n")
    sys.path.insert(0, d)
    try:
        m2 = load_private("data_schema", "_sib_selftest2_")
        ok(not hasattr(m2, "RUN_COPY"), "S2 run's module of the same name ignored")
    finally:
        sys.path.remove(d)
    # S3: an import that raises leaves no entry behind (a temporary scripts dir).
    real = SCRIPTS
    SCRIPTS = d
    try:
        with open(os.path.join(d, "raises.py"), "w") as f:
            f.write("raise RuntimeError('x')\n")
        try:
            load_private("raises", "_sib_selftest_")
            ok(False, "S3 raising import propagates")
        except RuntimeError:
            ok("_sib_selftest_raises" not in sys.modules, "S3 raising import leaves no entry")
    finally:
        SCRIPTS = real
    for k in ("_sib_selftest_data_schema", "_sib_selftest2_data_schema"):
        sys.modules.pop(k, None)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
