#!/usr/bin/env python3
"""Checks of the malaria engine's net outcomes through its production path (vector.itn_effect), kept outside the
engine directory so that adding them does not change the engine's code fingerprint (toolkit/engines/contract.py
code_fingerprint: the .py files of toolkit/engines/malaria/ only). Arithmetic on the net functions; no simulation.

    python3 toolkit/engines/malaria/benchmarks/net_checks.py --self-test

NC1 (Codex review of #270, P2 on V38): net_deter_sat theta != 1 reaches the per-contact outcomes through
itn_effect, for aged PBO nets with a non-zero kill shift and PBO advantage decay, holes on and off, and only for
PBO nets: pyrethroid and dual-AI nets are the same at every theta.
"""
from __future__ import annotations

import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def _self_test() -> int:
    from toolkit.engines.malaria import vector as V
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    P = dict(V.NET_DEFAULTS, cfp_kill=0.4, cfp_half=900.0, net_holes=1.0, net_kill_shift=0.2, pbo_adv_decay=0.5)
    nt = np.repeat(np.array([0, 1, 2]), 4)
    ages = np.tile(np.array([0.0, 200.0, 548.0, 900.0]), 3)
    got, moved = {}, True
    for cx in ("gambiae", "funestus"):
        for x in (0.088, 0.3, 0.55, 0.9):
            for holes in (True, False):
                base = V.itn_effect(nt, x, ages, P, cx, holes)
                for th in (0.0, 0.5):
                    out = V.itn_effect(nt, x, ages, dict(P, net_deter_sat=th), cx, holes)
                    same_other = all(np.array_equal(o[nt != 1], b[nt != 1]) for o, b in zip(out, base))
                    pbo_diff = max(float(np.max(np.abs(o[nt == 1] - b[nt == 1]))) for o, b in zip(out, base))
                    got[(cx, x, holes, th)] = (same_other, pbo_diff)
                    # PBO entry differs from a pyrethroid net's wherever PBO raises hut mortality (all but x near 1)
                    if x < 0.9 and pbo_diff <= 0:
                        moved = False
    k55 = V.itn_effect(np.array([1, 0]), 0.55, np.array([0.0, 0.0]), dict(P, net_deter_sat=0.0), "gambiae", False)[1]
    ok(all(v[0] for v in got.values()) and moved and float(k55[0]) > float(k55[1]),
       f"NC1 net_deter_sat != 1 changes PBO nets' outcomes through itn_effect (aged nets, kill shift 0.2, advantage "
       f"decay 0.5, holes on/off, both complexes) and leaves pyrethroid and dual-AI nets identical; at 0, x 0.55, a "
       f"new PBO net kills {float(k55[0]):.2f} per attempt against a pyrethroid net's {float(k55[1]):.2f}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
