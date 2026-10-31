#!/usr/bin/env python3
"""Package-rate engine: an empirical engine behind the Engine contract.

    python3 toolkit/engines/empirical/engine.py --self-test

For questions where the effect of each package is to be estimated from a unit panel (cases by
unit, period and the package delivered) rather than simulated. The outcome rate in a unit is

    rate(unit, package) = exp(log_rate[unit]) x exp(lrr[package])    per person per year

`log_rate` is a unit attribute (the rate under no package; the analysis engine calibrates it per
unit). `lrr:<combination>` is one log rate ratio per non-empty combination of components, kind
`calibrated`: each combination gets its own effect (saturated), so the data decide how
components combine; nothing assumes they multiply. Configure with the components and the outcome
name (`engine.config` in the decision spec). Outputs: per-capita outcome over the decision window,
the rate in each named window, and one cost driver per component (person-years covered).
Disease-agnostic: the names come from the spec.
"""
from __future__ import annotations

import itertools
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec, resolve_params

LRR_RANGE = (-6.0, 3.0)


def combo_key(components) -> str:
    return "+".join(sorted(components))


class PackageRateEngine:
    def __init__(self, components: list, outcome: str = "cases", lrr_range=LRR_RANGE):
        if not components or len(set(components)) != len(components):
            raise ValueError("PackageRateEngine: give distinct component names")
        self.components = [str(c) for c in components]
        self.outcome = str(outcome)
        self.lo, self.hi = (float(x) for x in lrr_range)
        self.combos = [combo_key(cs) for r in range(1, len(self.components) + 1)
                       for cs in itertools.combinations(sorted(self.components), r)]

    def describe(self) -> EngineCard:
        return EngineCard(
            name="package_rate", version="1",
            mechanisms=["empirical: one rate ratio per component combination, estimated from a unit panel"],
            components={c: [0, 1] for c in self.components},
            unit_attributes={"log_rate": "log outcome rate per person-year under no package"},
            outputs={"per_capita": [self.outcome], "windows": ["rate"],
                     "cost_drivers": [f"{c}_person_years" for c in self.components]},
            age_groups=["all"], valid_ranges={"log_rate": [-20.0, 5.0]},
            notes=["no transmission dynamics: effects are what the panel shows under the packages it contains",
                   "a combination absent from the panel is not identified (its calibrated rate ratio stays at the start)"])

    def parameters(self) -> list:
        return [ParamSpec(f"lrr:{k}", 0.0, "calibrated", units="log rate ratio", lo=self.lo, hi=self.hi,
                          source="calibrated per run to the unit panel") for k in self.combos]

    def run(self, params, unit, package, horizon, seed) -> Outcomes:
        p = resolve_params(self, params)
        on = [c for c, v in package.components.items() if int(v)]
        bad = [c for c in on if c not in self.components]
        if bad:
            raise ValueError(f"unknown components {bad}")
        lrr = p[f"lrr:{combo_key(on)}"] if on else 0.0
        rate = math.exp(float(unit.attributes.get("log_rate", 0.0)) + lrr)
        years = float(horizon.window[1] - horizon.window[0])
        wins = {w: {"rate": rate} for w in horizon.windows}
        return Outcomes(per_capita={self.outcome: {"all": rate * years}}, windows=wins,
                        cost_drivers={f"{c}_person_years": (years if c in on else 0.0) for c in self.components},
                        meta={"age_shares": {"all": 1.0}})


def _self_test() -> int:
    from toolkit.engines.contract import Horizon, Package, UnitSpec, check_engine
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    e = PackageRateEngine(["x", "y"])
    h = Horizon(0, 3, (0, 3), {"w": [0, 1]})
    u = UnitSpec("u", {"log_rate": math.log(0.2)})
    ok(check_engine(e, u, Package("xy", {"x": 1, "y": 1}), h) == [], "E1 the package-rate engine passes the contract")
    ok(sorted(p.name for p in e.parameters()) == ["lrr:x", "lrr:x+y", "lrr:y"], "E2 one calibrated rate ratio per combination")
    o = e.run({"lrr:x": math.log(0.5), "lrr:x+y": math.log(0.45), "lrr:y": math.log(0.8)}, u,
              Package("xy", {"x": 1, "y": 1}), h, 1)
    ok(abs(o.per_capita["cases"]["all"] - 0.2 * 0.45 * 3) < 1e-12,
       "E3 the combination's own rate ratio applies (not the product of the single ones)")
    try:
        e.run({}, u, Package("z", {"z": 1}), h, 1)
        ok(False, "E4 an unknown component fails")
    except ValueError:
        ok(True, "E4 an unknown component fails")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
