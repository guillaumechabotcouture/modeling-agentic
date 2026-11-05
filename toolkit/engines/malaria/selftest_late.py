#!/usr/bin/env python3
"""The malaria engine's v0.8-v0.11 self-test sections, in their own process.

    python3 toolkit/engines/malaria/selftest_late.py --self-test

engine.py --self-test runs M1-M21 and the v0.5-v0.7 sections. This module runs the v0.8 figure hooks (S0-S15),
the v0.9 net survival (N0-N7), the v0.10 past SMC and per-campaign net types (H0-H7) and the v0.11 SMC
mechanism (Q0-Q6). The sections are defined
in engine.py, beside the code they test; they are split across two processes only so that each one stays well
inside CI's per-test time limit (scripts/self_tests.py discovers and runs both modules).
"""
from __future__ import annotations

import os
import sys

if __package__:
    from . import engine as E
else:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    from toolkit.engines.malaria import engine as E  # noqa: E402

SECTIONS = ("_self_test_v08", "_self_test_v09", "_self_test_v010", "_self_test_v011")


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    import inspect
    src = inspect.getsource(E._self_test)
    orphan = sorted(n for n in dir(E) if n.startswith("_self_test_v") and n not in SECTIONS and f"{n}(" not in src)
    ok(not orphan, f"L0 every engine self-test section runs in engine.py or here (not run: {orphan})")
    e, unit = E.MalariaEngine(), E._self_test_unit()
    for name in SECTIONS:
        fn = getattr(E, name, None)
        if fn is None:          # fail closed: a section that is not defined is a failing case, not a skip
            ok(False, f"{name} is not defined in engine.py")
            continue
        fn(ok, e, unit)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
