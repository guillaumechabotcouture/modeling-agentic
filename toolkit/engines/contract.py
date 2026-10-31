#!/usr/bin/env python3
"""Engine contract (architecture-v2 §3.3): one interface for every model engine.

    python3 toolkit/engines/contract.py --self-test

An engine is validated once against its benchmark pack and reused by every run. The
analysis engine (calibration, emulation, uncertainty, optimisation, cross-validation,
decision tests) talks to it only through this contract:

    describe()   -> EngineCard   mechanisms, interventions, outputs, valid ranges, fitted
                                  parameters and benchmark results
    parameters() -> [ParamSpec]  name, value, kind (fixed | fitted | calibrated | scenario),
                                  range, units, source
    run(params, unit, package, horizon, seed) -> Outcomes

`params` overrides engine parameters by name (fitted values come from the card unless
overridden; an unknown name is an error). `unit` carries the unit attributes the engine
declares in `EngineCard.unit_attributes`; `package` names components from
`EngineCard.components`; `horizon` gives the simulated period, the decision window and
named measurement windows. Outcomes are per-capita rates by age group over the decision
window, named-window metrics, and cost drivers, so the caller scales by its own
populations and prices.

`check_engine(engine)` is the contract test every engine must pass (run by each engine's
self-test). Nothing here names a disease.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Protocol, runtime_checkable

KINDS = ("fixed", "fitted", "calibrated", "scenario")


@dataclass
class ParamSpec:
    name: str
    value: float
    kind: str                       # fixed | fitted | calibrated | scenario
    units: str = ""
    lo: float | None = None         # plausible / fitted range (uncertainty draws use it)
    hi: float | None = None
    source: str = ""                # citation, or "fitted: <benchmark ids>", or "assumption"
    verified: str = "unverified"    # fetched | matched | unverified (evidence-service vocabulary)


@dataclass
class UnitSpec:
    id: str
    attributes: dict


@dataclass
class Package:
    id: str
    components: dict                # component name -> value (e.g. {"net": "pbo", "smc": 1})


@dataclass
class Horizon:
    start: float
    stop: float
    window: tuple                   # decision window [t0, t1) in model years
    windows: dict = field(default_factory=dict)   # name -> [t0, t1] measurement windows


@dataclass
class Outcomes:
    per_capita: dict                # outcome -> {age_group: per-person total over the window}
    windows: dict                   # window name -> {metric: value}
    cost_drivers: dict              # driver -> per-capita quantity over the window
    meta: dict = field(default_factory=dict)


@dataclass
class EngineCard:
    name: str
    version: str
    mechanisms: list
    components: dict                # component -> allowed values
    unit_attributes: dict           # attribute -> description (with range)
    outputs: dict                   # per_capita outcomes, window metrics, cost drivers
    age_groups: list
    valid_ranges: dict              # attribute or parameter -> [lo, hi]
    fitted: dict = field(default_factory=dict)        # fitted parameter -> {value, lo, hi, benchmarks}
    benchmarks: list = field(default_factory=list)    # [{id, metric, target, band, computed, in_band, role}]
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@runtime_checkable
class Engine(Protocol):
    def describe(self) -> EngineCard: ...
    def parameters(self) -> list: ...
    def run(self, params: dict, unit: UnitSpec, package: Package, horizon: Horizon, seed: int) -> Outcomes: ...


def resolve_params(engine, params: dict | None) -> dict:
    """Engine parameter values with `params` applied; unknown names fail closed."""
    specs = {p.name: p for p in engine.parameters()}
    out = {k: p.value for k, p in specs.items()}
    for k, v in (params or {}).items():
        if k not in specs:
            raise KeyError(f"unknown engine parameter {k!r}")
        out[k] = float(v)
    return out


def code_fingerprint(engine) -> str:
    """sha256 over the engine's own code: every .py file in its module's directory, plus this contract (v2,
    Codex P1-3/P1-13). Batch results and benchmark results carry it, so results from other code are refused."""
    import hashlib
    import inspect
    import os
    d = os.path.dirname(os.path.abspath(inspect.getfile(type(engine))))
    h = hashlib.sha256()
    files = sorted({os.path.join(d, f) for f in os.listdir(d) if f.endswith(".py")} | {os.path.abspath(__file__)})
    for f in files:
        h.update(os.path.basename(f).encode() + b"\0")
        with open(f, "rb") as fh:
            h.update(fh.read())
    return h.hexdigest()


def engine_identity(engine) -> dict:
    """Name, version, code fingerprint, and (Codex r2 P1-6) hashes of the resolved parameters (values, kinds and
    ranges, so a changed fitted.yaml changes the identity) and of the configured structure (components, unit
    attributes, outputs, age groups, valid ranges: what constructor configuration sets)."""
    import hashlib
    import json
    card = engine.describe()
    params = sorted((p.name, float(p.value), p.kind, p.lo, p.hi) for p in engine.parameters())
    struct = {k: getattr(card, k) for k in ("components", "unit_attributes", "outputs", "age_groups", "valid_ranges")}
    h = lambda x: hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()  # noqa: E731
    return {"name": card.name, "version": card.version, "code_sha256": code_fingerprint(engine),
            "params_sha256": h(params), "config_sha256": h(struct)}


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)


def check_engine(engine, unit: UnitSpec, package: Package, horizon: Horizon, seed: int = 1) -> list[str]:
    """Contract test. Returns a list of problems (empty = passes)."""
    bad = []
    if not isinstance(engine, Engine):
        return ["engine does not implement describe/parameters/run"]
    card = engine.describe()
    if not isinstance(card, EngineCard):
        return ["describe() did not return an EngineCard"]
    for k in ("name", "version", "mechanisms", "components", "unit_attributes", "outputs", "age_groups"):
        if not getattr(card, k):
            bad.append(f"card field {k} is empty")
    specs = engine.parameters()
    names = [p.name for p in specs]
    if len(set(names)) != len(names):
        bad.append("duplicate parameter names")
    for p in specs:
        if p.kind not in KINDS:
            bad.append(f"parameter {p.name}: kind {p.kind!r} not in {KINDS}")
        if not _finite(p.value):
            bad.append(f"parameter {p.name}: value not finite")
        if p.kind == "fitted" and (p.lo is None or p.hi is None or not p.lo <= p.value <= p.hi):
            bad.append(f"fitted parameter {p.name} needs a range containing its value")
        if not p.source:
            bad.append(f"parameter {p.name} has no source")
    for k in unit.attributes:
        if k not in card.unit_attributes:
            bad.append(f"unit attribute {k!r} not declared by the engine")
    for k, v in package.components.items():
        if k not in card.components:
            bad.append(f"package component {k!r} not declared by the engine")
        elif v not in card.components[k]:
            bad.append(f"package component {k}={v!r} not in {card.components[k]}")
    try:
        resolve_params(engine, {"__no_such_parameter__": 1.0})
        bad.append("an unknown parameter name was accepted")
    except KeyError:
        pass
    if bad:
        return bad
    a = engine.run({}, unit, package, horizon, seed)
    b = engine.run({}, unit, package, horizon, seed)
    if not isinstance(a, Outcomes):
        return ["run() did not return Outcomes"]
    for k in card.outputs.get("per_capita", []):
        if k not in a.per_capita:
            bad.append(f"per_capita outcome {k} missing")
            continue
        for g in card.age_groups:
            v = a.per_capita[k].get(g)
            if not _finite(v) or v < 0:
                bad.append(f"per_capita {k}[{g}] = {v!r} (must be finite and >= 0)")
    for w in horizon.windows:
        if w not in a.windows:
            bad.append(f"window {w} missing from outcomes")
    for k in card.outputs.get("cost_drivers", []):
        if not _finite(a.cost_drivers.get(k)):
            bad.append(f"cost driver {k} missing or not finite")
    if a.per_capita != b.per_capita or a.windows != b.windows:
        bad.append("two runs with the same seed differ (not reproducible)")
    return bad


# --------------------------------------------------------------------------- toy engine
class ToyEngine:
    """A closed-form engine for self-tests of the contract, the benchmark runner and the
    analysis engine. Burden b = base * exp(k * x) per person-year; component "a" multiplies
    it by (1 - eff_a), "b" by (1 - eff_b), and together by (1-eff_a)(1-eff_b) + synergy.
    Prevalence in a window = 1 - exp(-burden); seed adds small noise (reproducible).
    `results_path`: a benchmark results file (toolkit/engines/benchmark.py), for self-tests that need one."""

    def __init__(self, results_path: str | None = None):
        self.results_path = results_path

    def describe(self) -> EngineCard:
        return EngineCard(
            name="toy", version="1", mechanisms=["closed-form burden"],
            components={"a": [0, 1], "b": [0, 1]},
            unit_attributes={"x": "transmission index [-2, 2]", "group": "label"},
            outputs={"per_capita": ["cases", "dalys"], "windows": ["prev"], "cost_drivers": ["a_doses", "b_doses"]},
            age_groups=["all"], valid_ranges={"x": [-2, 2]},
            benchmarks=self._bench())

    def _bench(self):
        import os
        if not self.results_path or not os.path.exists(self.results_path):
            return []
        import yaml
        return (yaml.safe_load(open(self.results_path)) or {}).get("benchmarks", [])

    def parameters(self) -> list:
        return [ParamSpec("base", 0.5, "fixed", source="assumption"),
                ParamSpec("k", 0.8, "fixed", source="assumption"),
                ParamSpec("eff_a", 0.5, "fitted", lo=0.3, hi=0.7, source="fitted: T1"),
                ParamSpec("eff_b", 0.3, "fitted", lo=0.1, hi=0.5, source="fitted: T2"),
                ParamSpec("synergy", 0.0, "scenario", source="assumption"),
                ParamSpec("noise", 0.0, "fixed", source="assumption")]

    def run(self, params, unit, package, horizon, seed) -> Outcomes:
        p = resolve_params(self, params)
        x = float(unit.attributes.get("x", 0.0))
        a, b = int(package.components.get("a", 0)), int(package.components.get("b", 0))
        m = (1 - p["eff_a"] * a) * (1 - p["eff_b"] * b) + p["synergy"] * a * b
        rng = (seed * 7919 + 17) % 1000 / 1000.0 - 0.5
        burden = p["base"] * math.exp(p["k"] * x) * m * (1 + p["noise"] * rng)
        years = horizon.window[1] - horizon.window[0]
        wins = {w: {"prev": 1 - math.exp(-burden)} for w in horizon.windows}
        return Outcomes(per_capita={"cases": {"all": burden * years}, "dalys": {"all": 0.01 * burden * years}},
                        windows=wins, cost_drivers={"a_doses": float(a) * years, "b_doses": float(b)},
                        meta={"age_shares": {"all": 1.0}})


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    e = ToyEngine()
    u = UnitSpec("u1", {"x": 0.5})
    h = Horizon(0, 3, (0, 3), {"w": [1, 2]})
    ok(check_engine(e, u, Package("ab", {"a": 1, "b": 1}), h) == [], "C1 the toy engine passes the contract test")
    ok(any("not declared" in s for s in check_engine(e, UnitSpec("u", {"zz": 1}), Package("p", {}), h)),
       "C2 an undeclared unit attribute fails the contract")
    ok(any("not in" in s for s in check_engine(e, u, Package("p", {"a": 2}), h)),
       "C3 a component value outside the declared set fails the contract")
    try:
        resolve_params(e, {"nope": 1})
        ok(False, "C4 unknown parameter overrides fail closed")
    except KeyError:
        ok(True, "C4 unknown parameter overrides fail closed")

    class Bad(ToyEngine):
        def parameters(self):
            return super().parameters() + [ParamSpec("eff_c", 2.0, "fitted", lo=0, hi=1, source="")]
    probs = check_engine(Bad(), u, Package("p", {"a": 1}), h)
    ok(any("range containing" in s for s in probs) and any("no source" in s for s in probs),
       "C5 a fitted parameter outside its range, and one without a source, fail the contract")

    class Noisy(ToyEngine):
        n = 0

        def run(self, params, unit, package, horizon, seed):
            Noisy.n += 1
            return super().run({"noise": 0.2}, unit, package, horizon, seed + Noisy.n)
    ok(any("not reproducible" in s for s in check_engine(Noisy(), u, Package("p", {"a": 1}), h)),
       "C6 an engine whose runs differ at the same seed fails the contract")
    o0 = e.run({}, u, Package("0", {}), h, 1).per_capita["cases"]["all"]
    o1 = e.run({}, u, Package("a", {"a": 1}), h, 1).per_capita["cases"]["all"]
    ok(abs(o1 / o0 - 0.5) < 1e-12, "C7 toy engine: component a halves the burden at eff_a = 0.5")
    fp = code_fingerprint(e)
    ok(len(fp) == 64 and fp == code_fingerprint(ToyEngine()) and engine_identity(e)["name"] == "toy",
       "C8 the engine code fingerprint is stable and part of its identity")

    class Refit(ToyEngine):         # same code directory, another fitted value (as a changed fitted.yaml gives)
        def parameters(self):
            return [ParamSpec(p.name, 0.55 if p.name == "eff_a" else p.value, p.kind, p.units, p.lo, p.hi, p.source)
                    for p in super().parameters()]
    ok(engine_identity(Refit()) != engine_identity(e) and engine_identity(ToyEngine()) == engine_identity(e),
       "C9 a changed fitted parameter value changes the engine identity (Codex r2 P1-6)")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
