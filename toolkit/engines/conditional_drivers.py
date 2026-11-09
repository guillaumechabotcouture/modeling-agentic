#!/usr/bin/env python3
"""Contract test for conditional cost drivers (F6 S2-2, Codex r1 P2-2).

    python3 toolkit/engines/conditional_drivers.py --self-test

An engine card may declare `outputs.cost_drivers_when: {driver: {parameter: value}}`: cost drivers the engine
produces only when each named engine parameter has the stated value. `contract.check_engine` checks
`outputs.cost_drivers` in a default run; `check(engine, unit, package, horizon, seed)` checks the conditional list:
its structure (a mapping of driver name to a non-empty mapping of known engine parameter to a finite number; a name
not also listed as an unconditional driver), then one run per distinct predicate, with the predicate's parameter
values, in which each driver it activates must be present and finite. Engines call it beside check_engine.

It lives beside contract.py, not inside it: contract.py is part of every engine's code fingerprint, so editing it
would invalidate every engine's benchmark stamp. Nothing here names a disease.
"""
from __future__ import annotations

import math
import os
import sys

if __package__:
    from .contract import ToyEngine, UnitSpec, Package, Horizon, EngineCard, Outcomes
else:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from toolkit.engines.contract import ToyEngine, UnitSpec, Package, Horizon, EngineCard, Outcomes  # noqa: E402


def structure_problems(engine) -> list[str]:
    """Problems with the card's outputs.cost_drivers_when (empty when absent)."""
    out = engine.describe().outputs or {}
    when = out.get("cost_drivers_when")
    if when is None:
        return []
    if not isinstance(when, dict):
        return ["outputs.cost_drivers_when: a mapping {driver: {parameter: value}}"]
    names = {p.name for p in engine.parameters()}
    plain = set(out.get("cost_drivers") or [])
    bad = []
    for d, pred in when.items():
        w = f"outputs.cost_drivers_when.{d}"
        if d in plain:
            bad.append(f"{w}: also listed in outputs.cost_drivers (a driver is conditional or not)")
        if not isinstance(pred, dict) or not pred:
            bad.append(f"{w}: a non-empty mapping {{parameter: value}}")
            continue
        for p, v in pred.items():
            if p not in names:
                bad.append(f"{w}: {p!r} is not an engine parameter")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                bad.append(f"{w}.{p}: {v!r} is not a finite number")
    return bad


def check(engine, unit, package, horizon, seed: int = 1) -> list[str]:
    """The conditional-driver contract test (module doc). Returns a list of problems (empty = passes)."""
    bad = structure_problems(engine)
    if bad:
        return bad
    when = (engine.describe().outputs or {}).get("cost_drivers_when") or {}
    groups = {}
    for d, pred in when.items():
        groups.setdefault(tuple(sorted((p, float(v)) for p, v in pred.items())), []).append(d)
    for pred, drivers in groups.items():
        o = engine.run(dict(pred), unit, package, horizon, seed)
        for d in drivers:
            v = o.cost_drivers.get(d)
            if v is None:
                bad.append(f"conditional cost driver {d} missing under {dict(pred)}")
            elif isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                bad.append(f"conditional cost driver {d} = {v!r} under {dict(pred)} (must be finite)")
    return bad


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    class Cond(ToyEngine):
        when = {"extra_doses": {"synergy": 1.0}}
        value = 2.0                      # what run() returns for the driver when synergy is 1 (None: omitted)

        def describe(self) -> EngineCard:
            c = super().describe()
            c.outputs = dict(c.outputs, cost_drivers_when=self.when)
            return c

        def run(self, params, unit, package, horizon, seed) -> Outcomes:
            o = super().run(params, unit, package, horizon, seed)
            if float((params or {}).get("synergy", 0.0)) == 1.0 and self.value is not None:
                o.cost_drivers["extra_doses"] = self.value
            return o

    u, pk, h = UnitSpec("u1", {"x": 0.5}), Package("ab", {"a": 1, "b": 1}), Horizon(0, 3, (0, 3), {"w": [1, 2]})
    ok(check(Cond(), u, pk, h) == [], "CD1 a conditional driver produced under its predicate passes")
    miss, nan = Cond(), Cond()
    miss.value, nan.value = None, float("nan")
    ok(any("extra_doses" in s and "missing" in s for s in check(miss, u, pk, h))
       and any("extra_doses" in s for s in check(nan, u, pk, h)),
       "CD2 a conditional driver missing, or not finite, under its activating configuration fails")
    shapes = ({"extra_doses": {"nope": 1.0}}, {"extra_doses": {}}, {"extra_doses": {"synergy": "on"}},
              ["extra_doses"], {"a_doses": {"synergy": 1.0}})
    got = []
    for w in shapes:
        bad = Cond()
        bad.when = w
        got.append(bool(check(bad, u, pk, h)))
    ok(all(got), f"CD3 a malformed declaration fails: unknown parameter, empty or non-numeric predicate, not a "
                 f"mapping, a name also listed unconditionally ({got})")
    ok(check(ToyEngine(), u, pk, h) == [], "CD4 an engine with no conditional drivers passes")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
