#!/usr/bin/env python3
"""Package-rate engine: an empirical engine behind the Engine contract.

    python3 toolkit/engines/empirical/engine.py --self-test

Two modes (`engine.config.mode`):

calibrated (default; unchanged). For questions where the effect of each package is estimated from a
unit panel (cases by unit, period and the package delivered) rather than simulated:

    rate(unit, package) = exp(log_rate[unit]) x exp(lrr[package])    per person per year

`log_rate` is a unit attribute (the rate under no package; the analysis engine calibrates it per
unit). `lrr:<combination>` is one log rate ratio per non-empty combination of components, kind
`calibrated`: each combination gets its own effect (saturated), so the data decide how components
combine; nothing assumes they multiply.

trial_priors (step 0: the decision by arithmetic, before any simulation). Nothing is calibrated.
For each outcome o (cases, deaths, ...):

    rate_o(unit, package) = baseline_rate[unit][o] x M_o(package) / M_o(baseline_coverage[unit])
    M_o(f) = prod_c (1 - f_c (1 - RR_c,o)) x prod_(a,b) I_ab,o ^ (f_a f_b)        rule: multiplicative
    M_o(f) = min_c (1 - f_c (1 - RR_c,o))                                          rule: strongest

`baseline_rate` is a unit attribute ({outcome: rate per person-year}) bound to columns of external
burden estimates, each outcome's source declared in `outcomes.<o>.baseline_source`. The baseline was
measured under some coverage of the components (`baseline.measured_under`): `coverage_attribute` (a
unit attribute {component: share of its trial effect in place when the baseline was measured}) or
`no_components`, so a reduction is never applied twice to a baseline that already includes it. A
package delivers each of its components' trial effect (f_c = 1). Each RR_c,o is a trial prior,
`effects.<c>.<o>: {rr, range, dist, measure, source}`, the parameter `lrr:<c>:<o>` (kind fixed, drawn
by the uncertainty step from its prior on the log scale; dist triangular or uniform); `relative_to:
<c'>` gives c's effect against c' (RR_c = RR_c' x the declared ratio). Interactions are declared per
pair and outcome (`combination.interactions`, parameter `lint:<a>+<b>:<o>`). `compose` builds further
outcomes as declared weighted sums (e.g. DALYs = YLL per death x deaths + YLD per case x cases;
parameters `w:<target>:<part>`). Every value needs a source; anything missing is refused at
construction. The rule, the interactions and every source are on the card.

Outputs: per-capita outcomes over the decision window, the rate in each named window, and one cost
driver per component (person-years covered). Disease-agnostic: names and values come from the spec.
"""
from __future__ import annotations

import itertools
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec, resolve_params

LRR_RANGE = (-6.0, 3.0)
MODES = ("calibrated", "trial_priors")
RULES = ("multiplicative", "strongest")
RATIO_DISTS = ("triangular", "uniform")         # drawn on the log scale
WEIGHT_DISTS = ("triangular", "uniform", "loguniform")
MEASURED_UNDER = ("coverage_attribute", "no_components")


def combo_key(components) -> str:
    return "+".join(sorted(components))


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _value(d, where, key, dists, ratio):
    """(value, range or None, dist, source, verified) of one declared value, and its problems. A ratio needs a
    range (a trial value is drawn over its interval); a weight may be declared without one (held fixed)."""
    if not isinstance(d, dict):
        return None, [f"{where}: a mapping {{{key}, range, dist, source}}"]
    bad = []
    v, rng, src = d.get(key), d.get("range"), str(d.get("source") or "").strip()
    if not _num(v) or (ratio and v <= 0):
        bad.append(f"{where}.{key}: a finite number" + (" > 0" if ratio else ""))
    if not src:
        bad.append(f"{where}.source: the citation is missing")
    if rng is None and ratio:
        bad.append(f"{where}.range: a trial value needs its range (the interval it is drawn over)")
    elif rng is not None:
        if not (isinstance(rng, list) and len(rng) == 2 and all(_num(x) for x in rng) and rng[0] < rng[1]
                and (not ratio or rng[0] > 0)):
            bad.append(f"{where}.range: [lo, hi] with lo < hi" + (", both > 0" if ratio else ""))
        elif _num(v) and not rng[0] <= v <= rng[1]:
            bad.append(f"{where}: {key} {v} is outside its range {rng}")
        if d.get("dist") not in dists:
            bad.append(f"{where}.dist: one of {dists}")
    return (None if bad else (float(v), rng, d.get("dist"), src, str(d.get("verified") or "unverified"))), bad


class PackageRateEngine:
    def __init__(self, components: list, outcome: str = "cases", lrr_range=LRR_RANGE, mode: str = "calibrated",
                 outcomes: dict | None = None, baseline: dict | None = None, effects: dict | None = None,
                 combination: dict | None = None, compose: dict | None = None):
        if not components or len(set(components)) != len(components):
            raise ValueError("PackageRateEngine: give distinct component names")
        self.components = [str(c) for c in components]
        self.outcome = str(outcome)
        self.lo, self.hi = (float(x) for x in lrr_range)
        self.combos = [combo_key(cs) for r in range(1, len(self.components) + 1)
                       for cs in itertools.combinations(sorted(self.components), r)]
        if mode not in MODES:
            raise ValueError(f"PackageRateEngine: mode {mode!r} not in {MODES}")
        self.mode = mode
        extra = [k for k, v in (("outcomes", outcomes), ("baseline", baseline), ("effects", effects),
                                ("combination", combination), ("compose", compose)) if v is not None]
        if mode == "calibrated":
            if extra:
                raise ValueError(f"PackageRateEngine: {extra} are trial_priors settings; set mode: trial_priors")
            return
        self._setup_trial(outcomes, baseline, effects, combination, compose)

    # ------------------------------------------------------------------ trial-prior mode
    def _setup_trial(self, outcomes, baseline, effects, combination, compose):
        bad, P = [], {}          # P: parameter -> (value, lo, hi, dist, source, verified, units)
        if not isinstance(outcomes, dict) or not outcomes:
            raise ValueError("PackageRateEngine trial_priors: outcomes: {<outcome>: {baseline_source}}")
        self.outcomes = [str(o) for o in outcomes]
        self.baseline_sources = {}
        for o, d in outcomes.items():
            src = str(d.get("baseline_source") or "").strip() if isinstance(d, dict) else ""
            if not src:
                bad.append(f"outcomes.{o}.baseline_source: the external burden estimate's source is missing")
            self.baseline_sources[o] = src
        b = baseline if isinstance(baseline, dict) else {}
        self.measured_under, self.baseline_note = b.get("measured_under"), str(b.get("source") or "").strip()
        if self.measured_under not in MEASURED_UNDER:
            bad.append(f"baseline.measured_under: one of {MEASURED_UNDER} (what the baseline already includes)")
        if not self.baseline_note:
            bad.append("baseline.source: the basis of the coverage the baseline was measured under")
        effects = effects if isinstance(effects, dict) else {}
        self.relative = {}
        for c in effects:
            if c not in self.components:
                bad.append(f"effects.{c}: not a component")
        for c in self.components:
            e = effects.get(c)
            if not isinstance(e, dict):
                bad.append(f"effects.{c}: missing (every component declares its effect on every outcome)")
                continue
            if e.get("relative_to") is not None:
                r = e["relative_to"]
                if r not in self.components or r == c or (effects.get(r) or {}).get("relative_to") is not None:
                    bad.append(f"effects.{c}.relative_to: {r!r} must be another component whose effect is "
                               "against no component")
                self.relative[c] = r
            bad += [f"effects.{c}.{k}: not a declared outcome" for k in e if k not in self.outcomes + ["relative_to"]]
            for o in self.outcomes:
                v, why = _value(e.get(o), f"effects.{c}.{o}", "rr", RATIO_DISTS, True)
                bad += why
                if v:
                    vs = f"vs {e['relative_to']}" if c in self.relative else "vs none"
                    P[f"lrr:{c}:{o}"] = (math.log(v[0]), math.log(v[1][0]), math.log(v[1][1]), v[2],
                                         f"{v[3]} [{e[o].get('measure') or 'measure not stated'}]", v[4],
                                         f"log rate ratio {vs}")
        cb = combination if isinstance(combination, dict) else {}
        self.rule, self.rule_source = cb.get("rule"), str(cb.get("source") or "").strip()
        if self.rule not in RULES:
            bad.append(f"combination.rule: one of {RULES} (declared, never implied)")
        if not self.rule_source:
            bad.append("combination.source: the basis of the combination rule")
        self.interactions = []
        for n, it in enumerate(cb.get("interactions") or []):
            w = f"combination.interactions[{n}]"
            cs, o = (it or {}).get("components"), (it or {}).get("outcome")
            if self.rule != "multiplicative":
                bad.append(f"{w}: interaction terms apply under the multiplicative rule only")
            if not (isinstance(cs, list) and len(set(cs)) == len(cs) == 2 and set(cs) <= set(self.components)):
                bad.append(f"{w}.components: two distinct components")
                continue
            if o not in self.outcomes:
                bad.append(f"{w}.outcome: one of {self.outcomes}")
                continue
            v, why = _value(it, w, "ratio", RATIO_DISTS, True)
            bad += why
            name = f"lint:{combo_key(cs)}:{o}"
            if name in P:
                bad.append(f"{w}: {name} declared twice")
            elif v:
                P[name] = (math.log(v[0]), math.log(v[1][0]), math.log(v[1][1]), v[2], v[3], v[4], "log ratio")
                self.interactions.append((sorted(cs), o, name))
        self.compose = {}
        for t, parts in (compose or {}).items():
            if t in self.outcomes or not isinstance(parts, dict) or not parts:
                bad.append(f"compose.{t}: a new outcome name with {{<outcome>: {{value, source}}}}")
                continue
            for o, d in parts.items():
                if o not in self.outcomes:
                    bad.append(f"compose.{t}.{o}: not a declared outcome")
                    continue
                v, why = _value(d, f"compose.{t}.{o}", "value", WEIGHT_DISTS, False)
                bad += why
                if v:
                    rng = v[1] or [None, None]
                    P[f"w:{t}:{o}"] = (v[0], rng[0], rng[1], v[2], v[3], v[4], f"{t} per {o}")
            self.compose[t] = list(parts)
        if self.outcome not in self.outcomes and self.outcome not in self.compose:
            bad.append(f"outcome {self.outcome!r}: one of the declared or composed outcomes")
        if bad:
            raise ValueError("PackageRateEngine trial_priors: " + "; ".join(bad))
        self._p = P

    def priors_doc(self) -> dict | None:
        """The engine-card priors (toolkit/spec/priors.py), from the spec's engine.config: every ranged value is a
        prior; a value declared without a range is fixed, its source the reason. None in calibrated mode."""
        if self.mode != "trial_priors":
            return None
        return {"priors": {k: {"dist": v[3], "range": [v[1], v[2]], "source": v[4], "verified": v[5]}
                           for k, v in self._p.items() if v[1] is not None},
                "fixed": {k: f"declared without a range: {v[4]}" for k, v in self._p.items() if v[1] is None}}

    def _M(self, p, o, f) -> float:
        def lrr(c):
            return p[f"lrr:{c}:{o}"] + (p[f"lrr:{self.relative[c]}:{o}"] if c in self.relative else 0.0)
        terms = [1.0 - float(f.get(c, 0.0)) * (1.0 - math.exp(lrr(c))) for c in self.components]
        if self.rule == "strongest":
            return min(terms)
        m = math.prod(terms)
        for (a, b), oo, name in self.interactions:
            if oo == o:
                m *= math.exp(p[name] * float(f.get(a, 0.0)) * float(f.get(b, 0.0)))
        return m

    def _trial_run(self, p, unit, on, horizon) -> Outcomes:
        br = unit.attributes.get("baseline_rate")
        if not isinstance(br, dict) or any(not _num(br.get(o)) or br[o] < 0 for o in self.outcomes):
            raise ValueError(f"unit {unit.id}: baseline_rate must give a rate >= 0 for each of {self.outcomes}")
        cov = {}
        if self.measured_under == "coverage_attribute":
            cov = unit.attributes.get("baseline_coverage")
            if not isinstance(cov, dict) or any(c not in self.components or not _num(v) or not 0 <= v <= 1
                                                for c, v in cov.items()):
                raise ValueError(f"unit {unit.id}: baseline_coverage must be {{component: share in [0, 1]}}")
        years = float(horizon.window[1] - horizon.window[0])
        pkg = {c: 1.0 for c in on}
        rate = {o: float(br[o]) * self._M(p, o, pkg) / self._M(p, o, cov) for o in self.outcomes}
        for t, parts in self.compose.items():
            rate[t] = sum(p[f"w:{t}:{o}"] * rate[o] for o in parts)
        wins = {w: dict({"rate": rate[self.outcome]}, **{f"rate_{o}": r for o, r in rate.items()})
                for w in horizon.windows}
        return Outcomes(per_capita={o: {"all": r * years} for o, r in rate.items()}, windows=wins,
                        cost_drivers={f"{c}_person_years": (years if c in on else 0.0) for c in self.components},
                        meta={"age_shares": {"all": 1.0}})

    def _trial_card(self) -> EngineCard:
        def show(k, x):
            return math.exp(x) if k.startswith("l") else x
        f = ("M = prod_c (1 - f_c (1 - RR_c)) x the declared interactions" if self.rule == "multiplicative"
             else "M = min_c (1 - f_c (1 - RR_c)): only the strongest component acts")
        mech = [f"trial priors: relative effects from trials, no calibration; combination rule {self.rule} "
                f"({self.rule_source}): rate = baseline x M(package) / M(baseline coverage), {f}"]
        mech += [f"interaction {combo_key(cs)} on {o}: ratio {math.exp(self._p[n][0]):.4g} "
                 f"[{math.exp(self._p[n][1]):.4g}, {math.exp(self._p[n][2]):.4g}] ({self._p[n][4]})"
                 for cs, o, n in self.interactions]
        notes = [f"baseline {o}: external estimate, {s}" for o, s in self.baseline_sources.items()]
        notes.append(f"baseline measured under {self.measured_under}: {self.baseline_note}")
        notes += [f"{k}: {show(k, v[0]):.4g}" + (f" [{show(k, v[1]):.4g}, {show(k, v[2]):.4g}]" if v[1] is not None
                                                 else " (no range: held fixed)") + f" ({v[6]}): {v[4]}"
                  for k, v in self._p.items()]
        notes.append("no transmission dynamics or time profile: each package delivers its trial effect at a constant "
                     "rate over the decision window")
        ua = {"baseline_rate": f"{{outcome: rate per person-year}} from external estimates, for {self.outcomes}"}
        if self.measured_under == "coverage_attribute":
            ua["baseline_coverage"] = "{component: share of its trial effect in place when the baseline was measured}"
        outs = self.outcomes + list(self.compose)
        return EngineCard(
            name="package_rate", version=f"2-trial_priors-{self.rule}", mechanisms=mech,
            components={c: [0, 1] for c in self.components}, unit_attributes=ua,
            outputs={"per_capita": outs, "windows": ["rate"] + [f"rate_{o}" for o in outs],
                     "cost_drivers": [f"{c}_person_years" for c in self.components]},
            age_groups=["all"], valid_ranges={"baseline_coverage": [0.0, 1.0]}, notes=notes)

    # ------------------------------------------------------------------ the contract
    def describe(self) -> EngineCard:
        if self.mode == "trial_priors":
            return self._trial_card()
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
        if self.mode == "trial_priors":
            return [ParamSpec(k, v[0], "fixed", units=v[6], lo=v[1], hi=v[2], source=v[4], verified=v[5])
                    for k, v in self._p.items()]
        return [ParamSpec(f"lrr:{k}", 0.0, "calibrated", units="log rate ratio", lo=self.lo, hi=self.hi,
                          source="calibrated per run to the unit panel") for k in self.combos]

    def run(self, params, unit, package, horizon, seed) -> Outcomes:
        p = resolve_params(self, params)
        on = [c for c, v in package.components.items() if int(v)]
        bad = [c for c in on if c not in self.components]
        if bad:
            raise ValueError(f"unknown components {bad}")
        if self.mode == "trial_priors":
            return self._trial_run(p, unit, on, horizon)
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
       and abs(ps["lrr:a:cases"].value - math.log(0.6)) < 1e-12 and ps["lrr:a:cases"].lo == math.log(0.5)
       and "trial A" in ps["lrr:a:cases"].source,
       "T2 effects are trial priors (kind fixed, with the trial's range and source), never calibrated or fitted")
    card = PRI.load_card(e)
    ok(card["problems"] == [] and set(card["priors"]) >= {"lrr:a:cases", "lrr:b:cases", "lint:a+b:cases"}
       and card["priors"]["lrr:a:cases"].dist == "triangular",
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
        ok(R["point"]["plan"] and "lrr:a:cases" in drawn and R["calibration"]["method"] == "none",
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
