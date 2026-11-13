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
    _self_test_trial(ok)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_trial(ok):
    """Trial-prior mode (step 0): effects are priors from trials, combined by a declared rule, on external baselines."""
    import copy
    import tempfile
    from toolkit.engines.contract import Horizon, Package, UnitSpec, check_engine
    from toolkit.spec import priors as PRI
    cfg = toy_trial_config()
    try:
        e = PackageRateEngine(**cfg)
    except Exception as ex:  # noqa: BLE001 -- a failing case, not a crash
        ok(False, f"T0 the trial-prior configuration is accepted: {type(ex).__name__}: {ex}")
        return
    h = Horizon(0, 2, (0, 2), {"w": [0, 1]})
    u = UnitSpec("u", {"baseline_rate": {"cases": 0.4, "deaths": 0.002}, "baseline_coverage": {"a": 0.5, "b": 0.0}})
    ok(check_engine(e, u, Package("ab", {"a": 1, "b": 1}), h) == [], "T1 the trial-prior engine passes the contract")
    ps = {p.name: p for p in e.parameters()}
    ok(not [p for p in ps.values() if p.kind in ("calibrated", "fitted")]
       and abs(ps["lrr:a@cases"].value - math.log(0.6)) < 1e-12 and ps["lrr:a@cases"].lo == math.log(0.5)
       and "trial A" in ps["lrr:a@cases"].source,
       "T2 effects are trial priors (kind fixed, with the trial's range and source), never calibrated or fitted")
    card = PRI.load_card(e)
    ok(card["problems"] == [] and set(card["priors"]) >= {"lrr:a@cases", "lrr:b@cases", "lint:a+b@cases"}
       and card["priors"]["lrr:a@cases"].dist == "triangular",
       f"T3 the uncertainty step draws every effect from its declared prior ({card['problems']})")
    ref = 1 - 0.5 * (1 - 0.6)                   # the baseline was measured with half of a's effect in place
    o = e.run({}, u, Package("ab", {"a": 1, "b": 1}), h, 1)
    ok(abs(o.per_capita["cases"]["all"] - 0.4 / ref * 0.6 * 0.7 * 1.1 * 2) < 1e-12,
       "T4 multiplicative rule with the declared a x b interaction, relative to the baseline's own coverage")
    o0 = e.run({}, u, Package("0", {}), h, 1)
    ok(abs(o0.per_capita["cases"]["all"] - 0.4 / ref * 2) < 1e-12
       and abs(o0.per_capita["dalys"]["all"] - (30 * 0.002 / (1 - 0.5 * 0.2) + 0.01 * 0.4 / ref) * 2) < 1e-12,
       "T5 the external baseline sets the rate (no calibration); DALYs composed from deaths and cases as declared")
    oc = e.run({}, u, Package("c", {"c": 1}), h, 1)
    ok(abs(oc.per_capita["cases"]["all"] - 0.4 / ref * 0.6 * 0.8 * 2) < 1e-12,
       "T6 an effect relative to another component composes with that component's effect")
    d = e.describe()
    rule = " ".join(d.mechanisms + d.notes)
    ok("multiplicative" in rule and "a+b" in rule and "trial A" in rule and "MAP-like" in rule,
       "T7 the combination rule, its interaction terms and every source are declared on the card")
    es = PackageRateEngine(**dict(copy.deepcopy(cfg), combination={"rule": "strongest", "source": "pessimistic"}))
    ok(abs(es.run({}, u, Package("ab", {"a": 1, "b": 1}), h, 1).per_capita["cases"]["all"] - 0.4 / ref * 0.6 * 2)
       < 1e-12, "T8 the `strongest` rule keeps only the larger single effect")
    for what, mut in (("an effect without a source", lambda c: c["effects"]["a"]["cases"].pop("source")),
                      ("an effect without a range", lambda c: c["effects"]["b"]["cases"].pop("range")),
                      ("a component without an effect on an outcome", lambda c: c["effects"]["b"].pop("deaths")),
                      ("a baseline without a source", lambda c: c["outcomes"]["cases"].pop("baseline_source")),
                      ("an undeclared combination rule",
                       lambda c: c.update(combination={"rule": "additive", "source": "x"})),
                      ("a point value outside its range", lambda c: c["effects"]["a"]["cases"].update(rr=0.9)),
                      ("an interaction without a source",
                       lambda c: c["combination"]["interactions"][0].pop("source"))):
        c = copy.deepcopy(cfg)
        mut(c)
        try:
            PackageRateEngine(**c)
            ok(False, f"T9 {what} is refused")
        except ValueError:
            ok(True, f"T9 {what} is refused")
    try:
        e.run({}, UnitSpec("u", {"baseline_coverage": {}}), Package("0", {}), h, 1)
        ok(False, "T10 a unit without its external baseline fails closed")
    except ValueError:
        ok(True, "T10 a unit without its external baseline fails closed")
    with tempfile.TemporaryDirectory() as d:
        try:
            R = _toy_trial_run(d)
        except Exception as ex:  # noqa: BLE001
            ok(False, f"T11 an end-to-end toy run through toolkit/analysis/run.py: {type(ex).__name__}: {ex}")
            return
        drawn = [p["name"] for p in (R["uncertainty"].get("draw_plan") or {}).get("drawn", [])]
        ok(R["point"]["plan"] and "lrr:a@cases" in drawn and R["calibration"]["method"] == "none",
           f"T11 an end-to-end toy run through toolkit/analysis/run.py: a plan, no calibration, and draws over the "
           f"trial priors ({drawn})")


def toy_trial_config() -> dict:
    """A small trial-prior configuration (self-tests): a and b with trial effects, c relative to a, an a x b term."""
    def eff(rr, lo, hi, src):
        return {"rr": rr, "range": [lo, hi], "dist": "triangular", "measure": "rate_ratio", "source": src}
    return {"components": ["a", "b", "c"], "outcome": "cases", "mode": "trial_priors",
            "outcomes": {"cases": {"baseline_source": "MAP-like incidence surface (toy)"},
                         "deaths": {"baseline_source": "MAP-like mortality surface (toy)"}},
            "baseline": {"measured_under": "coverage_attribute", "source": "toy survey coverage"},
            "effects": {"a": {"cases": eff(0.6, 0.5, 0.7, "trial A"), "deaths": eff(0.8, 0.7, 0.9, "trial A deaths")},
                        "b": {"cases": eff(0.7, 0.6, 0.8, "trial B"), "deaths": eff(1.0, 0.9, 1.1, "trial B deaths")},
                        "c": {"relative_to": "a", "cases": eff(0.8, 0.6, 1.0, "trial C vs A"),
                              "deaths": eff(1.0, 0.8, 1.2, "assumption: as cases")}},
            "combination": {"rule": "multiplicative", "source": "independent action (toy)",
                            "interactions": [{"components": ["a", "b"], "outcome": "cases", "ratio": 1.1,
                                              "range": [1.0, 1.3], "dist": "triangular", "source": "factorial trial"}]},
            "compose": {"dalys": {"deaths": {"value": 30.0, "range": [25.0, 35.0], "dist": "triangular",
                                             "source": "toy YLL per death"},
                                  "cases": {"value": 0.01, "source": "toy YLD per case"}}}}


def _toy_trial_run(d) -> dict:
    """A 6-unit toy spec on the trial-prior engine, run through toolkit/analysis/run.py (direct evaluation, unsigned)."""
    import yaml
    from toolkit.analysis import run as RUN
    with open(os.path.join(d, "units.csv"), "w") as f:
        f.write("id,zone,pop,inc,mort,cov_a\n")
        for i in range(6):
            f.write(f"u{i},z{i % 3},{10000 + 1000 * i},{0.1 + 0.1 * i:.2f},{0.001 * (1 + i % 3):.4f},{0.1 * i:.1f}\n")
    spec = {
        "schema_version": 1, "question": "Which package per unit (step 0)?", "decision_maker": "programme manager",
        "decision_date": 2026,
        "engine": {"ref": "toolkit.engines.empirical.engine:PackageRateEngine", "config": toy_trial_config()},
        "horizon": {"start": 2024, "stop": 2027},
        "decision_units": {"csv": "units.csv", "id": "id",
                           "populations": {"all": {"column": "pop", "age_groups": ["all"]}},
                           "engine_attributes": {"baseline_rate": {"columns": {"cases": "inc", "deaths": "mort"}},
                                                 "baseline_coverage": {"columns": {"a": "cov_a"}}}},
        "options": {"populations": {"total": "pop"}, "components": {"a": {"group": "g"}, "b": {}, "c": {"group": "g"}},
                    "packages": {"generate": True},
                    "engine_map": {"a": {"a": 1}, "b": {"b": 1}, "c": {"c": 1}},
                    "prices": {"base_year": "2024", "index": {"2024": 1.0}},
                    "cost_items": [{"id": k, "component": k, "value": v, "price_year": "2024",
                                    "price_rule": "as_published", "basis": "per_person", "includes": [k]}
                                   for k, v in (("a", 1.0), ("b", 0.8), ("c", 1.3))]},
        "objective": {"outcome": "dalys", "horizon": [2024, 2027], "discounting": "none"},
        "constraints": {"envelope_usd": 60000},
        "comparator": {"package": "none", "text": "no package"},
        "outcomes_reported": ["dalys", "cases", "deaths"],
        "calibration": {"method": "none"},
        "uncertainty": {"draws": 8, "seed": 3, "sources": [{"kind": "priors"}],
                        "excluded": [{"kind": "calibration", "reason": "no calibration: external baselines"},
                                     {"kind": "engine_parameters", "reason": "no fitted parameters"},
                                     {"kind": "costs", "reason": "toy"}, {"kind": "emulator", "reason": "direct"}]},
        "analysis": {"evaluation": "direct", "stability": {"holdout_group": "zone"},
                     "sensitivity": {"parameters": "fitted"}, "decision_tests": []},
        "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation_optimized.csv",
                        "columns": {"unit": "unit", "package": "package", "value": "dalys_averted"},
                        "headline": {"name": "total_dalys_averted", "outcome": "dalys"}},
    }
    with open(os.path.join(d, "decision_spec.yaml"), "w") as f:
        yaml.safe_dump(spec, f, sort_keys=False)
    return RUN.run(d, unsigned=True, log=lambda *a, **k: None, compute_kw={"max_workers": 1}, draws=8)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
