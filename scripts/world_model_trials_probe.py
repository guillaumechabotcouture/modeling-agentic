#!/usr/bin/env python3
"""Scale probe for a world-model trial template (run in a VM job, never on the laptop): the engine's per-capita
outcomes for one unit at several population sizes, all else equal. A scale-free engine gives identical per-capita
outcomes, so a sweep association with population is then an association in the units table, not in the engine.

    python3 scripts/world_model_trials_probe.py <template dir> --out <dir> [--pops 50000,900000] [--s0 0.8]
        [--seed-share 0.001]
    python3 scripts/world_model_trials_probe.py --self-test
"""
from __future__ import annotations

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def probe(template, pops, attrs) -> dict:
    """{population: {package: per-capita primary outcome}} from the template's engine and horizon."""
    from toolkit.engines.contract import Package, UnitSpec
    from toolkit.engines.horizon import make_horizon
    from toolkit.spec import schema
    from toolkit.analysis import batch as B
    from toolkit.spec import engine_parameters as PV
    s = schema.load(os.path.join(template, "decision_spec.yaml"))
    e, o = s.engine(), s.outcomes()[0]
    h = s["horizon"]
    hz = make_horizon(h["start"], h["stop"], list(s["objective"]["horizon"]))
    box = B.design_box(s, e)[0]                     # the design's parameters at their point values (priors' modes)
    params = PV.params(s, {k: v[2] for k, v in box.items()})
    out = {"params": params}
    for n in pops:
        out[str(n)] = {name: e.run(params, UnitSpec("u", dict(attrs, N=float(n))),
                                   Package(name, s.engine_package(cs)), hz, 1).per_capita[o]["all"]
                       for name, cs in s.packages().items()}
    return out


def main(argv=None) -> int:
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser()
    ap.add_argument("template")
    ap.add_argument("--out", required=True)
    ap.add_argument("--pops", default="50000,900000")
    ap.add_argument("--s0", type=float, default=0.8)
    ap.add_argument("--seed-share", type=float, default=1e-3)
    a = ap.parse_args(argv)
    out = os.path.expandvars(a.out)
    res = probe(a.template, [float(x) for x in a.pops.split(",")], {"s0": a.s0, "seed": a.seed_share})
    from lib import safe_io
    os.makedirs(out, exist_ok=True)
    safe_io.write_nofollow(out, "probe.json", json.dumps(res, indent=1).encode(), atomic=True)
    print(json.dumps(res))
    return 0


def _self_test() -> int:
    """The probe on the renewal engine's own scale invariance (a tiny run: three packages, two sizes)."""
    fails = []
    tpl = os.path.join(REPO, "data", "world_model_trials", "ebola_tracing_threshold")
    try:
        r = probe(tpl, [5e4, 9e5], {"s0": 0.8, "seed": 1e-3})
        a, b = r["50000.0"], r["900000.0"]
        same = all(abs(a[k] - b[k]) <= 1e-12 * max(1.0, abs(a[k])) for k in a)
        print(("PASS " if same else "FAIL ") + f"P1 the probe reports per-capita outcomes by population: {r}")
        if not same:
            fails.append("P1")
    except Exception as e:  # noqa: BLE001
        print(f"FAIL P1 probe: {type(e).__name__}: {e}")
        fails.append("P1")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
