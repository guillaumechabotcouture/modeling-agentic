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

Per-unit sampling (version 3, hb3 fix 5). A baseline measured from a panel of counts is uncertain unit by unit. With
`baseline.sampling: {model: poisson, dispersion, outcomes, source}` the card adds the unit attribute
`baseline_events` ({outcome: the event count behind the unit's measured rate}) and the uncertainty step's source
`unit_rates` draws each unit's rate of each listed outcome, independently across units and outcomes, as
rate x Gamma(shape n / phi, scale phi / n): mean 1, coefficient of variation sqrt(phi / n), n the unit's count and phi
the panel's over-dispersion (1 for Poisson). A count of 0 is refused (no multiplicative interval). Without it the card
says the baseline rates are point values the uncertainty step does not draw. In calibrated mode the per-unit log_rate
and the rate ratios are drawn jointly from the panel fit's covariance by the `calibration` source (method likelihood).

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
SAMPLING_MODELS = ("poisson",)      # baseline.sampling.model: a gamma multiplier from the unit's event count
EVENTS_ATTRIBUTE = "baseline_events"
OVERLAPS = ("independent", "comonotone")
MEASURES = ("rate_ratio", "hazard_ratio", "risk_ratio", "odds_ratio")
# The rare-outcome approximation (a risk ratio used as the rate ratio) is accepted only at a baseline risk of at most
# 10% over the trial period: there, for risk ratios 0.3-1, the constant-hazard rate ratio differs from the risk ratio
# by under 4% (e.g. p0 0.10, RR 0.5: rate ratio 0.487). Above it, convert by constant hazard.
RARE_MAX = 0.10
RISK_TO_RATE = ("constant_hazard", "rare_outcome")
TOL = 1e-12             # relative tolerance on the inclusive rare-outcome bound (log-scale round trips: exp(log 10))
MIN_P0 = 1e-300         # a smaller control risk underflows p0 x RR; refused (and the limit RR used at evaluation)


def _odds_to_risk(p0):
    return lambda x: x / (1.0 - p0 + p0 * x)


def _risk_to_rate(method, p0):
    """Risk ratio -> rate ratio. The treated risk p0 x RR is checked at every value (the point, the bounds, overrides
    and draws; construction and evaluation call this same function): below 1 for constant hazard, at most RARE_MAX
    (within TOL) for the rare-outcome approximation."""
    def rare(x):
        if not p0 * x <= RARE_MAX * (1 + TOL):
            raise ValueError(f"rare-outcome approximation: treated risk p0 x risk ratio = {p0 * x:.4g} is above "
                             f"{RARE_MAX}")
        return x

    def hazard(x):
        if not p0 * x < 1:
            raise ValueError(f"constant-hazard conversion: treated risk p0 x risk ratio = {p0 * x:.4g} must be below 1")
        if p0 * x < MIN_P0:             # the small-risk limit: the rate ratio equals the risk ratio
            return x
        return math.log1p(-p0 * x) / math.log1p(-p0)
    return rare if method == "rare_outcome" else hazard


def _measure(d, where, hi):
    """(converter: declared ratio -> rate ratio, description, problems). A rate ratio is used as it stands, and a
    hazard ratio as the rate ratio (proportional hazards). A risk ratio needs `conversion: {method: constant_hazard |
    rare_outcome, p0, source}` (p0 the control risk over the trial period; rare_outcome only at p0 <= RARE_MAX). An
    odds ratio needs `conversion: {method: odds_to_risk, p0, then: constant_hazard | rare_outcome | stand_in, source}`
    (stand_in, with a `reason`: the risk ratio is taken for the rate ratio, e.g. a prevalence ratio for incidence).
    The conversion applies at evaluation, so the point, the bounds and every draw are converted."""
    m, cv = d.get("measure"), d.get("conversion")
    if m not in MEASURES:
        return None, "", [f"{where}.measure: one of {MEASURES}"]
    if m in ("rate_ratio", "hazard_ratio"):
        bad = [f"{where}.conversion: a {m} needs none"] if cv is not None else []
        return (lambda x: x), ("hazard ratio used as the rate ratio (proportional hazards)" if m == "hazard_ratio"
                               else ""), bad
    if not isinstance(cv, dict):
        return None, "", [f"{where}: a {m} applied to a rate needs `conversion` (see the engine docstring)"]
    p0, src = cv.get("p0"), str(cv.get("source") or "").strip()
    bad = _unknown(cv, "conversion", f"{where}.conversion") + ([] if src else [f"{where}.conversion.source: the basis of p0"])
    if not (_num(p0) and MIN_P0 <= p0 < 1):
        return None, "", bad + [f"{where}.conversion.p0: the control risk, in [{MIN_P0}, 1)"]
    first = None
    method = cv.get("method")
    if m == "odds_ratio":
        if method != "odds_to_risk":
            return None, "", bad + [f"{where}.conversion.method: odds_to_risk for an odds ratio"]
        first, method = _odds_to_risk(p0), cv.get("then")
        if method == "stand_in":
            if not str(cv.get("reason") or "").strip():
                bad.append(f"{where}.conversion.reason: why the risk ratio stands for the rate ratio")
            return (None if bad else first), f"odds ratio to risk ratio at p0 {p0:g}, taken for the rate ratio " \
                f"({cv.get('reason')}; {src})", bad
    if method not in RISK_TO_RATE:
        return None, "", bad + [f"{where}.conversion.{'then' if first else 'method'}: one of "
                                f"{RISK_TO_RATE + (('stand_in',) if first else ())}"]
    if method == "rare_outcome" and p0 > RARE_MAX:
        bad.append(f"{where}.conversion: the rare-outcome approximation needs p0 <= {RARE_MAX} (p0 {p0:g}); "
                   "convert by constant_hazard")
    second = _risk_to_rate(method, p0)
    f = (lambda x: second(first(x))) if first else second
    try:            # the top of the range, through the same function evaluation uses (the maps are increasing)
        f(hi)
    except ValueError as ex:
        bad.append(f"{where}.conversion: at the upper ratio {hi:g}: {ex}"
                   + ("; convert by constant_hazard" if method == "rare_outcome" else ""))
    what = (("odds ratio to risk ratio, then " if first else "") + ("risk ratio used as the rate ratio (rare outcome)"
            if method == "rare_outcome" else "risk ratio to rate ratio by constant hazard") + f" at p0 {p0:g} ({src})")
    return (None if bad else f), what, bad


VALUE_KEYS = {"range", "dist", "source", "verified", "note"}
KEYS = {"outcome": {"baseline_source", "note"},
        "baseline": {"measured_under", "source", "overlap", "sampling", "note"},
        "sampling": {"model", "dispersion", "outcomes", "source", "note"},
        "effect": VALUE_KEYS | {"rr", "measure", "conversion"},
        "conversion": {"method", "p0", "then", "reason", "source"},
        "combination": {"rule", "source", "interactions"},
        "interaction": VALUE_KEYS | {"components", "outcome", "ratio"}, "weight": VALUE_KEYS | {"value"}}


def _unknown(d, kind, where) -> list:
    """A misspelt optional key would silently fall back to its default: every key must be one the mode reads."""
    if not isinstance(d, dict):         # a malformed scalar is reported by the section's own check
        return []
    return [f"{where}.{k}: not a {kind} setting (one of {sorted(KEYS[kind])})" for k in d if k not in KEYS[kind]]


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
    if not _num(v) or (ratio and v <= 0) or v < 0:      # a weight is >= 0 over its whole support (review r1)
        bad.append(f"{where}.{key}: a finite number" + (" > 0" if ratio else " >= 0"))
    if not src:
        bad.append(f"{where}.source: the citation is missing")
    if rng is None and ratio:
        bad.append(f"{where}.range: a trial value needs its range (the interval it is drawn over)")
    elif rng is not None:
        pos = ratio or d.get("dist") == "loguniform"
        if not (isinstance(rng, list) and len(rng) == 2 and all(_num(x) for x in rng) and rng[0] < rng[1]
                and (rng[0] > 0 if pos else rng[0] >= 0)):
            bad.append(f"{where}.range: [lo, hi] with lo < hi" + (", both > 0" if pos else ", lo >= 0"))
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
            self.config = None          # calibrated mode reports no configuration, as before (not callable)
            return
        import copy
        self._config = copy.deepcopy({"mode": mode, "components": self.components, "outcome": self.outcome,
                                      "outcomes": outcomes, "baseline": baseline, "effects": effects,
                                      "combination": combination, "compose": compose})
        # identity (the digest of _config) and execution are built from the same private copy
        self._setup_trial(*(copy.deepcopy(self._config[k])
                            for k in ("outcomes", "baseline", "effects", "combination", "compose")))

    def config(self) -> dict | None:
        """The full behavioural configuration (trial_priors), a copy: its digest is in the card version, so the
        engine identity changes with the effect graph, rule, interactions, composition and baseline."""
        import copy
        return copy.deepcopy(self._config)

    def _digest(self) -> str:
        import hashlib
        import json
        return hashlib.sha256(json.dumps(self._config, sort_keys=True, default=str).encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ trial-prior mode
    def _setup_trial(self, outcomes, baseline, effects, combination, compose):
        bad, P = [], {}          # P: parameter -> (value, lo, hi, dist, source, verified, units)
        if not isinstance(outcomes, dict) or not outcomes:
            raise ValueError("PackageRateEngine trial_priors: outcomes: {<outcome>: {baseline_source}}")
        self.outcomes = [str(o) for o in outcomes]
        names = self.components + self.outcomes + [str(t) for t in (compose or {})]
        bad += [f"name {n!r}: ':' and '+' are separators in parameter names" for n in names if set(n) & set(":+")]
        bad += [f"name {n!r}: empty or blank" for n in names if not n.strip()]
        self.baseline_sources = {}
        for o, d in outcomes.items():
            bad += _unknown(d, "outcome", f"outcomes.{o}")
            src = str(d.get("baseline_source") or "").strip() if isinstance(d, dict) else ""
            if not src:
                bad.append(f"outcomes.{o}.baseline_source: the external burden estimate's source is missing")
            self.baseline_sources[o] = src
        b = baseline if isinstance(baseline, dict) else {}
        bad += _unknown(b, "baseline", "baseline")
        self.measured_under, self.baseline_note = b.get("measured_under"), str(b.get("source") or "").strip()
        if self.measured_under not in MEASURED_UNDER:
            bad.append(f"baseline.measured_under: one of {MEASURED_UNDER} (what the baseline already includes)")
        if not self.baseline_note:
            bad.append("baseline.source: the basis of the coverage the baseline was measured under")
        self.overlap = b.get("overlap", "independent")
        if self.overlap not in OVERLAPS:
            bad.append(f"baseline.overlap: one of {OVERLAPS} (how components' baseline coverages overlap)")
        self.sampling, n0 = None, len(bad)
        sm = b.get("sampling")
        if sm is not None:
            if not isinstance(sm, dict):
                bad.append("baseline.sampling: a mapping {model, dispersion, outcomes, source}")
                sm = {}
            bad += _unknown(sm, "sampling", "baseline.sampling")
            so, disp = sm.get("outcomes", list(self.outcomes)), sm.get("dispersion", 1.0)
            if sm.get("model") not in SAMPLING_MODELS:
                bad.append(f"baseline.sampling.model: one of {SAMPLING_MODELS}")
            if not (_num(disp) and disp >= 1):
                bad.append("baseline.sampling.dispersion: a number >= 1 (the panel's over-dispersion; 1 for Poisson)")
            if not (isinstance(so, list) and so and all(isinstance(o, str) for o in so) and len(set(so)) == len(so)
                    and set(so) <= set(self.outcomes)):
                bad.append(f"baseline.sampling.outcomes: distinct declared outcomes (of {self.outcomes})")
            if not str(sm.get("source") or "").strip():
                bad.append("baseline.sampling.source: the basis of the counts and the dispersion")
            if len(bad) == n0:
                self.sampling = {"model": sm["model"], "dispersion": float(disp), "outcomes": list(so),
                                 "source": str(sm["source"]).strip()}
        effects = effects if isinstance(effects, dict) else {}
        self.relative, self.conversions, self.conv = {}, [], {}
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
                why = _unknown(e.get(o), "effect", f"effects.{c}.{o}") + why
                bad += why
                if not v:
                    continue
                conv, what, why = _measure(e[o], f"effects.{c}.{o}", v[1][1])
                bad += why
                name = f"lrr:{c}:{o}"
                if conv is None or name in P:
                    bad += [f"{name}: declared twice"] if name in P else []
                    continue
                self.conv[name] = conv
                if what:
                    self.conversions.append(
                        f"{name}: {what}: {v[0]:.4g} [{v[1][0]:.4g}, {v[1][1]:.4g}] -> rate ratio "
                        f"{conv(v[0]):.4g} [{conv(v[1][0]):.4g}, {conv(v[1][1]):.4g}]")
                vs = f"vs {e['relative_to']}" if c in self.relative else "vs none"
                P[name] = (math.log(v[0]), math.log(v[1][0]), math.log(v[1][1]), v[2], f"{v[3]} [{e[o]['measure']}]",
                           v[4], f"log {e[o]['measure']} {vs}" + (" (converted to a rate ratio at evaluation)"
                                                                  if what and e[o]["measure"] != "hazard_ratio" else ""))
        cb = combination if isinstance(combination, dict) else {}
        bad += _unknown(cb, "combination", "combination")
        self.rule, self.rule_source = cb.get("rule"), str(cb.get("source") or "").strip()
        if self.rule not in RULES:
            bad.append(f"combination.rule: one of {RULES} (declared, never implied)")
        if not self.rule_source:
            bad.append("combination.source: the basis of the combination rule")
        self.interactions = []
        for n, it in enumerate(cb.get("interactions") or []):
            w = f"combination.interactions[{n}]"
            cs, o = (it or {}).get("components"), (it or {}).get("outcome")
            bad += _unknown(it, "interaction", w)
            if self.rule != "multiplicative":
                bad.append(f"{w}: interaction terms apply under the multiplicative rule only")
            if not (isinstance(cs, list) and len(set(cs)) == len(cs) == 2 and set(cs) <= set(self.components)):
                bad.append(f"{w}.components: two distinct components")
                continue
            if o not in self.outcomes:
                bad.append(f"{w}.outcome: one of {self.outcomes}")
                continue
            if self.relative.get(cs[0], cs[0]) == self.relative.get(cs[1], cs[1]):
                bad.append(f"{w}: {cs} are alternatives (one's effect is relative to the other); no interaction")
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
                why = _unknown(d, "weight", f"compose.{t}.{o}") + why
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
        # exposure groups: a component and those whose effect is relative to it are alternatives (one state each)
        self.groups = [[c] + [x for x in self.components if self.relative.get(x) == c]
                       for c in self.components if c not in self.relative]

    def priors_doc(self) -> dict | None:
        """The engine-card priors (toolkit/spec/priors.py), from the spec's engine.config: every ranged value is a
        prior; a value declared without a range is fixed, its source the reason. None in calibrated mode."""
        if self.mode != "trial_priors":
            return None
        return {"priors": {k: {"dist": v[3], "range": [v[1], v[2]], "source": v[4], "verified": v[5]}
                           for k, v in self._p.items() if v[1] is not None},
                "fixed": {k: f"declared without a range: {v[4]}" for k, v in self._p.items() if v[1] is None}}

    def unit_sampling(self) -> dict | None:
        """The per-unit sampling the uncertainty step draws (source `unit_rates`), or None: {attribute, events_attribute,
        model, dispersion, outcomes, source}. Only trial_priors mode with `baseline.sampling` declares one."""
        if self.mode != "trial_priors" or self.sampling is None:
            return None
        return dict(self.sampling, attribute="baseline_rate", events_attribute=EVENTS_ATTRIBUTE,
                    outcomes=list(self.sampling["outcomes"]))

    def draw_unit_attributes(self, unit, rng) -> dict:
        """One draw of the unit's sampled attributes ({baseline_rate: {outcome: rate}}): each listed outcome's measured
        rate times Gamma(n / phi, scale phi / n) from `rng` (a numpy Generator), in the declared outcome order; the
        other outcomes keep their value. Fails closed on a missing or non-positive count."""
        s = self.unit_sampling()
        if s is None:
            raise ValueError("PackageRateEngine: no per-unit sampling is declared (baseline.sampling)")
        br, ev = unit.attributes.get("baseline_rate"), unit.attributes.get(EVENTS_ATTRIBUTE)
        if not isinstance(br, dict) or not isinstance(ev, dict):
            raise ValueError(f"unit {unit.id}: baseline_rate and {EVENTS_ATTRIBUTE} must each give {s['outcomes']}")
        out = dict(br)
        for o in s["outcomes"]:
            n, r = ev.get(o), br.get(o)
            if not (_num(n) and n > 0):
                raise ValueError(f"unit {unit.id}: {EVENTS_ATTRIBUTE}.{o} must be a count > 0 (got {n!r}); a zero "
                                 "count gives no multiplicative sampling interval")
            if not (_num(r) and r >= 0):
                raise ValueError(f"unit {unit.id}: baseline_rate.{o} must be a rate >= 0 (got {r!r})")
            k = float(n) / s["dispersion"]
            out[o] = float(r) * float(rng.gamma(k, 1.0 / k))
        return {"baseline_rate": out}

    def _m(self, p, o, active) -> float:
        """The rate multiplier of one exposure state (the set of components a person has)."""
        def ratio(c):                       # the declared measure, converted to a rate ratio (each draw too)
            n = f"lrr:{c}:{o}"
            return self.conv[n](math.exp(p[n]))
        rr = {c: ratio(c) * (ratio(self.relative[c]) if c in self.relative else 1.0) for c in active}
        if self.rule == "strongest":            # over active components only: a harmful RR is never clipped
            return min(rr.values()) if rr else 1.0
        m = math.prod(rr.values())
        for (a, b), oo, name in self.interactions:
            if oo == o and a in active and b in active:
                m *= math.exp(p[name])
        return m

    def _states(self, f) -> list:
        """[(probability, active components)]: the joint exposure states of a population with coverage f. Within a
        group the shares are exclusive; across groups `independent` multiplies them, `comonotone` nests them (one
        uniform position decides every group: maximal overlap)."""
        per = [[(f.get(c, 0.0), c) for c in g if f.get(c, 0.0) > 0] for g in self.groups]
        if self.overlap == "independent":
            out = []
            for combo in itertools.product(*[s + [(1.0 - sum(q for q, _ in s), None)] for s in per]):
                pr = math.prod(q for q, _ in combo)
                if pr > 0:
                    out.append((pr, frozenset(c for _, c in combo if c is not None)))
            return out
        cuts = sorted({0.0, 1.0} | {min(1.0, sum(q for q, _ in s[:k + 1])) for s in per for k in range(len(s))})
        out = []
        for lo, hi in zip(cuts, cuts[1:]):
            mid, act = (lo + hi) / 2, set()
            for s in per:
                acc = 0.0
                for q, c in s:
                    if acc <= mid < acc + q:
                        act.add(c)
                    acc += q
            out.append((hi - lo, frozenset(act)))
        return out

    def _M(self, p, o, f) -> float:
        return sum(pr * self._m(p, o, act) for pr, act in self._states(f))

    def _trial_run(self, p, unit, on, horizon) -> Outcomes:
        # the resolved values (overrides and draws included) meet the same physical constraints as the declared ones
        badp = [k for k, v in p.items() if not _num(v) or (k.startswith("w:") and v < 0)]
        if badp:
            raise ValueError(f"parameters {badp}: not finite, or a negative composition weight")
        for n, conv in self.conv.items():   # every resolved effect, whatever the package and the coverage
            try:
                conv(math.exp(p[n]))
            except ValueError as ex:
                raise ValueError(f"parameter {n} = {p[n]:.6g}: {ex}") from None
        br = unit.attributes.get("baseline_rate")
        if not isinstance(br, dict) or any(not _num(br.get(o)) or br[o] < 0 for o in self.outcomes):
            raise ValueError(f"unit {unit.id}: baseline_rate must give a rate >= 0 for each of {self.outcomes}")
        cov = {}
        if self.measured_under == "coverage_attribute":
            cov = unit.attributes.get("baseline_coverage")
            if (not isinstance(cov, dict) or set(cov) != set(self.components)
                    or any(not _num(v) or not 0 <= v <= 1 for v in cov.values())
                    or any(sum(cov[c] for c in g) > 1 + 1e-9 for g in self.groups)):
                raise ValueError(f"unit {unit.id}: baseline_coverage must give every component {self.components} a "
                                 "share in [0, 1] (0 for none), alternatives summing to at most 1")
        both = [g for g in self.groups if len(set(g) & set(on)) > 1]
        if both:
            raise ValueError(f"package {combo_key(on)}: {both[0]} are alternatives (an effect is relative to "
                             "another); a package holds at most one of them")
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
        f = ("m(state) = product of the active components' RR x the declared interactions among them"
             if self.rule == "multiplicative" else "m(state) = the smallest active RR (1 with none): only the "
             "strongest component acts")
        mech = [f"trial priors: relative effects from trials, no calibration; combination rule {self.rule} "
                f"({self.rule_source}): rate = baseline x M(package) / M(baseline coverage), M = the mean of "
                f"m(state) over joint exposure states ({self.overlap} overlap across groups; alternatives "
                f"{[g for g in self.groups if len(g) > 1]} exclusive), {f}"]
        mech += [f"interaction {combo_key(cs)} on {o}: ratio {math.exp(self._p[n][0]):.4g} "
                 f"[{math.exp(self._p[n][1]):.4g}, {math.exp(self._p[n][2]):.4g}] ({self._p[n][4]})"
                 for cs, o, n in self.interactions]
        notes = [f"baseline {o}: external estimate, {s}" for o, s in self.baseline_sources.items()]
        notes.append(f"baseline measured under {self.measured_under}: {self.baseline_note}")
        notes += [f"{k}: {show(k, v[0]):.4g}" + (f" [{show(k, v[1]):.4g}, {show(k, v[2]):.4g}]" if v[1] is not None
                                                 else " (no range: held fixed)") + f" ({v[6]}): {v[4]}"
                  for k, v in self._p.items()]
        notes += [f"conversion: {x}" for x in self.conversions]
        notes.append("no transmission dynamics or time profile: each package delivers its trial effect at a constant "
                     "rate over the decision window")
        ua = {"baseline_rate": f"{{outcome: rate per person-year}} from external estimates, for {self.outcomes}"}
        if self.measured_under == "coverage_attribute":
            ua["baseline_coverage"] = "{component: share of its trial effect in place when the baseline was measured}"
        s = self.sampling
        if s is not None:
            ua[EVENTS_ATTRIBUTE] = f"{{outcome: event count behind the measured baseline rate}}, for {s['outcomes']}"
            notes.append(f"per-unit sampling: the uncertainty source unit_rates draws each unit's baseline rate of "
                         f"{s['outcomes']} as rate x Gamma(n / phi, scale phi / n), n its {EVENTS_ATTRIBUTE}, phi "
                         f"{s['dispersion']:g}, independently across units and outcomes ({s['source']})")
        else:
            notes.append("per-unit sampling: none declared (baseline.sampling); the baseline rates are point values "
                         "the uncertainty step does not draw")
        outs = self.outcomes + list(self.compose)
        return EngineCard(
            name="package_rate", version=f"3-trial_priors-{self.rule}-{self._digest()}", mechanisms=mech,
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
    u = UnitSpec("u", {"baseline_rate": {"cases": 0.4, "deaths": 0.002},
                       "baseline_coverage": {"a": 0.5, "b": 0.0, "c": 0.0}})
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
    _self_test_review1(ok, cfg, h)
    _self_test_review2(ok, cfg, h)
    _self_test_review3(ok, h)
    _self_test_review4(ok, h)
    _self_test_sampling(ok, cfg)
    _self_test_sampling_e2e(ok)
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


def _self_test_review1(ok, cfg, h):
    """PR #217 review round 1: each reproduction is a case."""
    import copy
    from toolkit.engines.contract import Package, UnitSpec, engine_identity

    def eff(rr, lo, hi, measure="rate_ratio"):
        return {"rr": rr, "range": [lo, hi], "dist": "triangular", "measure": measure, "source": "toy"}

    def two(rr_a, rr_b, interaction=None, rule="multiplicative", overlap=None):
        c = {"components": ["a", "b"], "outcome": "cases", "mode": "trial_priors",
             "outcomes": {"cases": {"baseline_source": "toy"}},
             "baseline": {"measured_under": "coverage_attribute", "source": "toy",
                          **({"overlap": overlap} if overlap else {})},
             "effects": {"a": {"cases": eff(rr_a, rr_a * 0.9, rr_a * 1.1)},
                         "b": {"cases": eff(rr_b, rr_b * 0.9, rr_b * 1.1)}},
             "combination": {"rule": rule, "source": "toy"}}
        if interaction:
            c["combination"]["interactions"] = [{"components": ["a", "b"], "outcome": "cases", "ratio": interaction,
                                                 "range": [1.5, 2.5], "dist": "triangular", "source": "toy"}]
        return PackageRateEngine(**c)

    def rate(e, cov, comps, rate0=0.4, params=None):
        u = UnitSpec("u", {"baseline_rate": {"cases": rate0}, "baseline_coverage": cov})
        return e.run(params or {}, u, Package("p", comps), h, 1).per_capita["cases"]["all"] / 2

    e = PackageRateEngine(**cfg)
    full = {"a": 0.0, "b": 0.0, "c": 0.0}
    try:
        e.run({}, UnitSpec("u", {"baseline_rate": {"cases": 0.4, "deaths": 0.002}, "baseline_coverage": full}),
              Package("ac", {"a": 1, "c": 1}), h, 1)
        ok(False, "R1 a package with a component and the one its effect is relative to is refused (A+C)")
    except ValueError:
        ok(True, "R1 a package with a component and the one its effect is relative to is refused (A+C)")
    try:
        r = rate(two(0.6, 1.2, rule="strongest"), {"a": 0.0, "b": 0.0}, {"b": 1})
        lrr_hi = {"lrr:b:cases": math.log(1.3)}
        r2 = rate(two(0.6, 1.2, rule="strongest"), {"a": 0.0, "b": 0.0}, {"b": 1}, params=lrr_hi)
        ok(abs(r - 0.4 * 1.2) < 1e-12 and abs(r2 - 0.4 * 1.3) < 1e-12,
           f"R2 `strongest`: the minimum over active components only, so a harmful RR 1.2 (and a draw at 1.3) is "
           f"not clipped to 1 ({r:.4g}, {r2:.4g})")
    except Exception as ex:  # noqa: BLE001
        ok(False, f"R2 `strongest` with a harmful RR: {type(ex).__name__}: {ex}")
    try:
        e2 = two(0.5, 0.5, interaction=2.0)
        r = rate(e2, {"a": 0.5, "b": 0.5}, {"a": 1, "b": 1}, rate0=0.625)
        rc = rate(two(0.5, 0.5, interaction=2.0, overlap="comonotone"), {"a": 0.5, "b": 0.5}, {"a": 1, "b": 1},
                  rate0=0.75)
        ok(abs(r - 0.5) < 1e-12 and abs(rc - 0.5) < 1e-12,
           f"R3 fractional coverage is a mixture over joint exposure states (independent: baseline multiplier 0.625; "
           f"comonotone: 0.75) ({r:.6g}, {rc:.6g})")
    except Exception as ex:  # noqa: BLE001
        ok(False, f"R3 fractional coverage as a mixture: {type(ex).__name__}: {ex}")
    for what, mut in (("an odds ratio without a declared conversion",
                       lambda c: c["effects"]["a"]["cases"].update(measure="odds_ratio")),
                      ("an unknown measure", lambda c: c["effects"]["a"]["cases"].update(measure="something")),
                      ("a missing measure", lambda c: c["effects"]["a"]["cases"].pop("measure")),
                      ("a negative DALY weight range",
                       lambda c: c["compose"]["dalys"]["deaths"].update(range=[-35.0, 35.0])),
                      ("a negative fixed DALY weight", lambda c: c["compose"]["dalys"]["cases"].update(value=-0.01)),
                      ("a loguniform weight with a zero bound",
                       lambda c: c["compose"]["dalys"]["deaths"].update(range=[0.0, 35.0], dist="loguniform"))):
        c = copy.deepcopy(cfg)
        mut(c)
        try:
            PackageRateEngine(**c)
            ok(False, f"R4 {what} is refused")
        except ValueError:
            ok(True, f"R4 {what} is refused")
    try:
        r = rate(two(0.6, 0.7), {"b": 0.0}, {"a": 1})
        ok(False, f"R5 a coverage mapping that omits a component fails closed (got {r:.4g}; 0.24 vs 0.30)")
    except ValueError:
        r = rate(two(0.6, 0.7), {"a": 1.0, "b": 0.0}, {"a": 1})
        ok(abs(r - 0.4) < 1e-12, "R5 a coverage mapping that omits a component fails closed; with it declared, "
                                 "continued delivery keeps the baseline")
    c = copy.deepcopy(cfg)
    c["effects"]["c"].pop("relative_to")
    c2 = dict(copy.deepcopy(cfg), outcome="deaths")
    ids = [engine_identity(PackageRateEngine(**x)) for x in (cfg, c, c2)]
    ok(ids[0] != ids[1] and ids[0] != ids[2] and ids[0] == engine_identity(PackageRateEngine(**copy.deepcopy(cfg)))
       and callable(getattr(e, "config", None)),
       "R6 the engine identity changes with the effect graph (relative_to) and the primary outcome, and the engine "
       "gives its full configuration (config())")


def _self_test_review2(ok, cfg, h):
    """PR #217 review round 2: each reproduction is a case."""
    import copy
    from toolkit.engines.contract import Package, UnitSpec, engine_identity

    def one(eff, comps=("a",)):
        return {"components": list(comps), "outcome": "cases", "mode": "trial_priors",
                "outcomes": {"cases": {"baseline_source": "toy"}},
                "baseline": {"measured_under": "no_components", "source": "toy"},
                "effects": {c: {"cases": copy.deepcopy(eff)} for c in comps},
                "combination": {"rule": "multiplicative", "source": "toy"}}

    def rate(c, params=None, rate0=0.4):
        e = PackageRateEngine(**c)
        return e.run(params or {}, UnitSpec("u", {"baseline_rate": {"cases": rate0}}), Package("a", {"a": 1}), h,
                     1).per_capita["cases"]["all"] / 2

    def refused(c):
        try:
            PackageRateEngine(**c)
            return False
        except ValueError:
            return True

    orr = {"rr": 0.6, "range": [0.4, 0.8], "dist": "triangular", "measure": "odds_ratio", "source": "toy",
           "conversion": {"method": "odds_to_risk", "p0": 0.5, "then": "stand_in",
                          "reason": "a prevalence ratio taken for the incidence rate ratio (toy)", "source": "toy"}}
    try:
        r, rd = rate(one(orr)), rate(one(orr), {"lrr:a:cases": math.log(0.8)})
        lo = PackageRateEngine(**one(orr)).describe().notes
        ok(abs(r - 0.4 * 0.75) < 1e-12 and abs(rd - 0.4 * 0.8 / 0.9) < 1e-12
           and any("0.5714" in n and "0.8889" in n for n in lo),
           f"V1 a declared OR->RR conversion changes the numbers: OR 0.6 at p0 0.5 -> 0.75, a draw at 0.8 -> 0.8889, "
           f"bounds converted on the card ({r / 0.4:.4g}, {rd / 0.4:.4g})")
    except Exception as ex:  # noqa: BLE001
        ok(False, f"V1 a declared OR->RR conversion changes the numbers: {type(ex).__name__}: {ex}")
    risk = {"rr": 0.5, "range": [0.4, 0.6], "dist": "triangular", "measure": "risk_ratio", "source": "toy"}
    ok(refused(one(risk)), "V2 a risk ratio applied to a rate without a conversion or approximation is refused")
    ch = dict(risk, conversion={"method": "constant_hazard", "p0": 0.8, "source": "toy annual risk"})
    try:
        r = rate(one(ch))
        ok(abs(r / 0.4 - math.log(1 - 0.4) / math.log(1 - 0.8)) < 1e-12,
           f"V2 a non-rare outcome (p0 0.8, RR 0.5) converts by constant hazard to rate ratio 0.3174 ({r / 0.4:.4g})")
    except Exception as ex:  # noqa: BLE001
        ok(False, f"V2 constant-hazard conversion: {type(ex).__name__}: {ex}")
    ok(refused(one(dict(risk, conversion={"method": "rare_outcome", "p0": 0.3, "source": "toy"}))),
       "V2 the rare-outcome approximation is refused for a baseline risk of 0.3 (above 0.10)")
    try:
        r = rate(one(dict(risk, conversion={"method": "rare_outcome", "p0": 0.05, "source": "toy"})))
        ok(abs(r - 0.4 * 0.5) < 1e-12, "V2 the rare-outcome approximation (p0 0.05) uses the risk ratio as recorded")
    except Exception as ex:  # noqa: BLE001
        ok(False, f"V2 rare-outcome approximation: {type(ex).__name__}: {ex}")
    e = PackageRateEngine(**cfg)
    u = UnitSpec("u", {"baseline_rate": {"cases": 0.4, "deaths": 0.002},
                       "baseline_coverage": {"a": 0.0, "b": 0.0, "c": 0.0}})
    try:
        e.run({"w:dalys:cases": -0.1}, u, Package("a", {"a": 1}), h, 1)
        ok(False, "V3 a negative weight given as an override is refused at evaluation")
    except ValueError:
        ok(True, "V3 a negative weight given as an override is refused at evaluation")
    id0 = engine_identity(e)
    c = e.config()
    c["effects"]["c"].pop("relative_to")
    c["baseline"]["overlap"] = "comonotone"
    rc = e.run({}, u, Package("c", {"c": 1}), h, 1).per_capita["cases"]["all"] / 2
    ok(engine_identity(e) == id0 and engine_identity(PackageRateEngine(**c)) != id0 and abs(rc - 0.4 * 0.48) < 1e-12,
       "V4 config() is a copy: mutating it changes neither the engine's identity nor its behaviour, and an engine "
       "built from the mutated copy has another identity")
    eff = {"rr": 0.8, "range": [0.7, 0.9], "dist": "triangular", "measure": "rate_ratio", "source": "toy"}
    c1 = one(eff, comps=("a", "a:b"))                       # otherwise complete: only the name is wrong
    c2 = one(eff)
    c2["outcomes"]["b:c"] = {"baseline_source": "toy"}
    c2["effects"]["a"]["b:c"] = dict(eff)
    c3 = one(eff, comps=("a", "x+y"))
    ok(refused(c1) and refused(c2) and refused(c3) and not refused(one(eff)),
       "V5 a component or outcome name with ':' or '+' is refused (parameter names would be ambiguous)")
    from toolkit.engines.benchmark import config_digest
    cal = PackageRateEngine(["x", "y"])
    ok(not callable(getattr(cal, "config", None)) and config_digest(cal) is None,
       "V6 calibrated mode reports no configuration (config() not callable), as before")


def _self_test_review3(ok, h):
    """PR #217 review round 3: each reproduction is a case."""
    import copy
    from toolkit.engines.contract import Package, UnitSpec

    def one(eff, comps=("a",), combination=None):
        return {"components": list(comps), "outcome": "cases", "mode": "trial_priors",
                "outcomes": {"cases": {"baseline_source": "toy"}},
                "baseline": {"measured_under": "no_components", "source": "toy"},
                "effects": {c: {"cases": copy.deepcopy(eff)} for c in comps},
                "combination": combination or {"rule": "multiplicative", "source": "toy"}}

    def refused(c):
        try:
            PackageRateEngine(**c)
            return False
        except ValueError:
            return True

    def rate(c, comps, params=None):
        return PackageRateEngine(**c).run(params or {}, UnitSpec("u", {"baseline_rate": {"cases": 0.4}}),
                                          Package("p", comps), h, 1).per_capita["cases"]["all"] / 2

    def risk(rr, lo, hi, method, p0, measure="risk_ratio", **kw):
        return {"rr": rr, "range": [lo, hi], "dist": "triangular", "measure": measure, "source": "toy",
                "conversion": dict({"method": method, "p0": p0, "source": "toy"}, **kw)}
    ok(refused(one(risk(8.0, 6.0, 9.0, "rare_outcome", 0.1))) and refused(one(risk(8.0, 7.0, 12.0, "rare_outcome", 0.1))),
       "W1 rare outcome: a treated risk above the bound (p0 0.1 x RR 6-9, or up to 1.2) is refused across the range")
    ok(refused(one(risk(8.0, 6.0, 12.0, "constant_hazard", 0.1))),
       "W1 constant hazard: a treated risk p0 x RR at or above 1 anywhere in the range is refused")
    odd = risk(3.0, 2.0, 4.0, "odds_to_risk", 0.08, measure="odds_ratio", then="rare_outcome")
    ok(refused(one(odd)), "W1 an odds-derived risk ratio whose treated risk exceeds the rare-outcome bound is refused "
                          "(p0 0.08, OR 2-4: treated risk 0.15-0.26)")
    ok_c = one(risk(1.2, 1.0, 1.5, "rare_outcome", 0.05))
    try:
        rate(ok_c, {"a": 1}, {"lrr:a:cases": math.log(3.0)})
        ok(False, "W1 a draw or override whose treated risk exceeds the rare-outcome bound fails at evaluation")
    except ValueError:
        ok(True, "W1 a draw or override whose treated risk exceeds the rare-outcome bound fails at evaluation")
    try:
        r = rate(one(risk(0.5, 0.4, 0.6, "constant_hazard", 1e-16)), {"a": 1})
        r2 = rate(one(risk(0.5, 0.4, 0.6, "constant_hazard", 1e-20)), {"a": 1})
        ok(abs(r / 0.4 - 0.5) < 1e-9 and abs(r2 / 0.4 - 0.5) < 1e-9,
           f"W2 constant hazard keeps precision at tiny risks (p0 1e-16 and 1e-20: rate ratio 0.5) "
           f"({r / 0.4:.6g}, {r2 / 0.4:.6g})")
    except Exception as ex:  # noqa: BLE001
        ok(False, f"W2 constant hazard keeps precision at tiny risks: {type(ex).__name__}: {ex}")
    eff = {"rr": 0.5, "range": [0.4, 0.6], "dist": "triangular", "measure": "rate_ratio", "source": "toy"}
    typo = one(eff, comps=("a", "b"), combination={"rule": "multiplicative", "source": "toy", "interaction": [
        {"components": ["a", "b"], "outcome": "cases", "ratio": 4.0, "range": [3.0, 5.0], "dist": "triangular",
         "source": "toy"}]})
    c2 = one(eff)
    c2["baseline"]["overlapp"] = "comonotone"
    c3 = one(eff)
    c3["effects"]["a"]["cases"]["rnage"] = [0.3, 0.7]
    c4 = one(eff)
    c4["outcomes"]["cases"]["baseline_sorce"] = "x"
    ok(refused(typo) and refused(c2) and refused(c3) and refused(c4),
       "W3 unknown keys are refused (combination.interaction, baseline.overlapp, an effect's rnage, an outcome's "
       "baseline_sorce)")
    ok(refused(one(eff, comps=("",))) and refused(one(eff, comps=("a", " "))),
       "W4 an empty or blank component name is refused")


def _self_test_review4(ok, h):
    """PR #221 review round 1: each reproduction is a case."""
    import copy
    from toolkit.engines.contract import Package, UnitSpec

    def one(eff, comps=("a",), measured="no_components"):
        return {"components": list(comps), "outcome": "cases", "mode": "trial_priors",
                "outcomes": {"cases": {"baseline_source": "toy"}},
                "baseline": {"measured_under": measured, "source": "toy"},
                "effects": {c: {"cases": copy.deepcopy(eff)} for c in comps},
                "combination": {"rule": "multiplicative", "source": "toy"}}

    def risk(rr, lo, hi, method, p0):
        return {"rr": rr, "range": [lo, hi], "dist": "triangular", "measure": "risk_ratio", "source": "toy",
                "conversion": {"method": method, "p0": p0, "source": "toy"}}

    def run(c, comps, params=None, attrs=None):
        return PackageRateEngine(**c).run(params or {}, UnitSpec("u", dict({"baseline_rate": {"cases": 0.4}},
                                                                            **(attrs or {}))),
                                          Package("p", comps), h, 1).per_capita["cases"]["all"] / 2
    try:
        r = run(one(risk(10.0, 9.0, 10.0, "rare_outcome", 0.01)), {"a": 1})
        ok(abs(r - 4.0) < 1e-9, f"X1 the inclusive endpoint (p0 0.01, RR 10: treated risk 0.10) constructs and "
                                f"evaluates ({r:.6g})")
    except Exception as ex:  # noqa: BLE001
        ok(False, f"X1 the inclusive endpoint (p0 0.01, RR 10) constructs and evaluates: {type(ex).__name__}: {ex}")
    c = one(risk(1.2, 1.0, 1.5, "rare_outcome", 0.05), measured="coverage_attribute")
    try:
        run(c, {}, {"lrr:a:cases": math.log(3.0)}, {"baseline_coverage": {"a": 0.0}})
        ok(False, "X2 an invalid override is refused even with the component absent and zero baseline coverage")
    except ValueError:
        ok(True, "X2 an invalid override is refused even with the component absent and zero baseline coverage")
    try:
        PackageRateEngine(**one(risk(0.5, 0.4, 0.6, "constant_hazard", 5e-324)))
        ok(False, "X3 a control risk so small that p0 x RR underflows (p0 5e-324) is refused")
    except ValueError:
        ok(True, "X3 a control risk so small that p0 x RR underflows (p0 5e-324) is refused")
    bad = []
    for what, mut in (("effects.a.cases: 0.5", lambda x: x["effects"]["a"].update(cases=0.5)),
                      ("outcomes.cases: 'x'", lambda x: x["outcomes"].update(cases="x")),
                      ("baseline: 3", lambda x: x.update(baseline=3)),
                      ("combination: 'multiplicative'", lambda x: x.update(combination="multiplicative")),
                      ("compose.dalys.cases: 1.0", lambda x: x.update(compose={"dalys": {"cases": 1.0}})),
                      ("conversion: 'rare'", lambda x: x["effects"]["a"]["cases"].update(conversion="rare"))):
        x = one(risk(0.5, 0.4, 0.6, "rare_outcome", 0.05))
        mut(x)
        try:
            PackageRateEngine(**x)
            bad.append(f"{what}: accepted")
        except ValueError:
            pass
        except Exception as ex:  # noqa: BLE001
            bad.append(f"{what}: {type(ex).__name__}")
    ok(not bad, f"X4 malformed scalar sections give the contextual ValueError, never a crash ({bad})")


def _self_test_sampling(ok, cfg):
    """hb3 fix 5: per-unit sampling of measured baseline rates, declared on the card and drawn by the engine."""
    import copy
    import numpy as np
    from toolkit.engines.contract import UnitSpec
    smp = {"model": "poisson", "dispersion": 1.0, "outcomes": ["cases"], "source": "toy panel counts"}

    def with_s(s):
        c = copy.deepcopy(cfg)
        c["baseline"]["sampling"] = s
        return c
    try:
        e = PackageRateEngine(**with_s(smp))
    except Exception as ex:  # noqa: BLE001 -- a failing case, not a crash
        ok(False, f"S1 a trial-prior configuration with baseline.sampling is accepted: {type(ex).__name__}: {ex}")
        return
    c, c0 = e.describe(), PackageRateEngine(**cfg).describe()
    us = e.unit_sampling() or {}
    ok(EVENTS_ATTRIBUTE in c.unit_attributes and any("unit_rates" in n and "toy panel counts" in n for n in c.notes)
       and c.version.startswith("3-trial_priors") and us.get("outcomes") == ["cases"] and us.get("dispersion") == 1.0
       and us.get("attribute") == "baseline_rate",
       f"S1 the card declares the per-unit sampling (baseline_events, the unit_rates note) at version 3: {c.version}")
    ok(PackageRateEngine(**cfg).unit_sampling() is None and EVENTS_ATTRIBUTE not in c0.unit_attributes
       and any("point values the uncertainty step does not draw" in n for n in c0.notes)
       and PackageRateEngine(["x", "y"]).unit_sampling() is None,
       "S2 without it the card says the baseline rates are point values; calibrated mode declares no unit sampling")
    bad = []
    for what, s in (("dispersion below 1", dict(smp, dispersion=0.5)), ("an unknown model", dict(smp, model="normal")),
                    ("an undeclared outcome", dict(smp, outcomes=["nope"])), ("no source", dict(smp, source=" ")),
                    ("a misspelt key", dict(smp, dispresion=2.0)), ("a scalar", 3)):
        try:
            PackageRateEngine(**with_s(s))
            bad.append(f"{what}: accepted")
        except ValueError:
            pass
        except Exception as ex:  # noqa: BLE001
            bad.append(f"{what}: {type(ex).__name__}")
    ok(not bad, f"S3 a malformed baseline.sampling is refused with the contextual ValueError ({bad})")
    u = UnitSpec("u", {"baseline_rate": {"cases": 0.4, "deaths": 0.002}, EVENTS_ATTRIBUTE: {"cases": 25}})
    rng = np.random.default_rng(1)
    xs = [e.draw_unit_attributes(u, rng)["baseline_rate"] for _ in range(4000)]
    m = np.array([x["cases"] for x in xs]) / 0.4
    e4 = PackageRateEngine(**with_s(dict(smp, dispersion=4.0)))
    m4 = np.array([e4.draw_unit_attributes(u, rng)["baseline_rate"]["cases"] for _ in range(4000)]) / 0.4
    ok(abs(m.mean() - 1) < 0.02 and abs(m.std() / m.mean() - 0.2) < 0.02 and abs(m4.std() / m4.mean() - 0.4) < 0.04
       and all(x["deaths"] == 0.002 for x in xs),
       f"S4 a drawn rate is the rate x Gamma(n/phi, phi/n): mean 1, CV sqrt(phi/n) (n 25: {m.std() / m.mean():.3f}; "
       f"phi 4: {m4.std() / m4.mean():.3f}); an outcome not listed keeps its value")
    refused = []
    for attrs in ({"baseline_rate": {"cases": 0.4}, EVENTS_ATTRIBUTE: {"cases": 0}},
                  {"baseline_rate": {"cases": 0.4}}, {"baseline_rate": {"cases": 0.4}, EVENTS_ATTRIBUTE: {"deaths": 3}}):
        try:
            e.draw_unit_attributes(UnitSpec("u", attrs), rng)
            refused.append(False)
        except ValueError:
            refused.append(True)
    ok(all(refused), f"S5 a zero, missing or other outcome's count fails closed ({refused})")


def _self_test_sampling_e2e(ok):
    """hb3 fix 5, end to end: on a toy panel whose per-unit counts are small, drawing their sampling error (unit_rates)
    widens the headline interval; the point plan and the other sources' draws are unchanged; the report says what it
    drew; the schema refuses the source where the engine declares no sampling, and an undeclared sampling."""
    import tempfile
    import yaml
    from toolkit.spec import schema as SC
    smp = {"model": "poisson", "dispersion": 1.0, "outcomes": ["cases", "deaths"], "source": "toy panel counts"}
    try:
        with tempfile.TemporaryDirectory() as d0, tempfile.TemporaryDirectory() as d1:
            R0 = _toy_trial_run(d0, draws=100, sampling=smp, unit_rates="excluded")
            R1 = _toy_trial_run(d1, draws=100, sampling=smp, unit_rates="source")
            rep0 = yaml.safe_load(open(os.path.join(d0, "uncertainty_report.yaml")))
            rep1 = yaml.safe_load(open(os.path.join(d1, "uncertainty_report.yaml")))
            from toolkit.analysis import figure_data as FD
            rp = FD.replay(R1["_A"], R1["point"]["plan"], R1["uncertainty"])
            ok(rp["reproduced"] is True, f"SE5 (Codex r1 P1-2) the figure step's own replay redraws the unit rates and "
                                         f"reproduces the interval: {rp.get('replayed')} vs {rp.get('stored')}")
    except Exception as ex:  # noqa: BLE001
        ok(False, f"SE1 the toy panel runs with and without unit_rates: {type(ex).__name__}: {ex}")
        return
    w = [R["uncertainty"]["fixed_plan"]["dalys"]["ci_high"] - R["uncertainty"]["fixed_plan"]["dalys"]["ci_low"]
         for R in (R0, R1)]
    ok(w[1] > 1.3 * w[0], f"SE1 per-unit sampling error widens the headline interval (width {w[0]:.4g} -> {w[1]:.4g})")
    ok(R0["point"]["plan"] == R1["point"]["plan"]
       and rep0["parameter_samples"]["lrr:a:cases"] == rep1["parameter_samples"]["lrr:a:cases"],
       "SE2 the point plan and the priors' draws are unchanged by the unit_rates source (its own stream)")
    uv = rep1.get("unit_values_drawn") or {}
    ok("unit_rates" in rep1["sources_drawn"] and (uv.get("unit_rates") or {}).get("units") == 6
       and uv["unit_rates"].get("values") == 12 and rep1.get("sampler_varied_unit_values") == 12
       and "unit rates" in rep1["interval_label"] and (rep0.get("unit_values_drawn") or {}).get("n_varied") == 0
       and {"kind": "unit_rates", "status": "excluded"} in [{k: x[k] for k in ("kind", "status")}
                                                             for x in rep0["sources_not_drawn"]],
       f"SE3 the report says which unit values it drew (6 units x 2 outcomes) and labels the interval: {uv}")
    with tempfile.TemporaryDirectory() as d:
        probs = []
        for kw in ({"sampling": smp}, {"unit_rates": "source"}, {"sampling": smp, "unit_rates": "source"}):
            probs.append([x for x in SC.validate(SC.load(_toy_trial_spec(d, **kw))) if "unit_rates" in x])
        ok(probs[0] and probs[1] and probs[2] == [],
           f"SE4 a declared sampling neither drawn nor excluded, and unit_rates without one, are refused: {probs}")
        p = _toy_trial_spec(d, sampling=smp, unit_rates="source")
        raw = yaml.safe_load(open(p))
        raw["decision_units"]["engine_attributes"][EVENTS_ATTRIBUTE]["columns"].pop("deaths")
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        miss = [x for x in SC.validate(SC.load(p)) if EVENTS_ATTRIBUTE in x and "deaths" in x]
        ok(miss, f"SE4b (Codex r1 P2-5) a sampled outcome with no bound count is refused before sign-off: {miss}")


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


def _toy_trial_spec(d, sampling=None, unit_rates=None) -> str:
    """A 6-unit toy spec on the trial-prior engine (the path of decision_spec.yaml). `sampling`: engine
    baseline.sampling, with each unit's event counts bound as baseline_events; `unit_rates`: "source" or "excluded"."""
    import yaml
    with open(os.path.join(d, "units.csv"), "w") as f:
        f.write("id,zone,pop,inc,mort,cov_a,cov_b,cov_c,ev_inc,ev_mort\n")
        for i in range(6):
            f.write(f"u{i},z{i % 3},{10000 + 1000 * i},{0.1 + 0.1 * i:.2f},{0.001 * (1 + i % 3):.4f},{0.1 * i:.1f},0.2,0.0,"
                    f"{2 + i % 2},{1 + i % 2}\n")
    cfg = toy_trial_config()
    attrs = {"baseline_rate": {"columns": {"cases": "inc", "deaths": "mort"}},
             "baseline_coverage": {"columns": {"a": "cov_a", "b": "cov_b", "c": "cov_c"}}}
    if sampling is not None:
        cfg["baseline"]["sampling"] = sampling
        attrs[EVENTS_ATTRIBUTE] = {"columns": {"cases": "ev_inc", "deaths": "ev_mort"}}
    unc = {"draws": 8, "seed": 3, "sources": [{"kind": "priors"}],
           "excluded": [{"kind": "calibration", "reason": "no calibration: external baselines"},
                        {"kind": "engine_parameters", "reason": "no fitted parameters"},
                        {"kind": "costs", "reason": "toy"}, {"kind": "emulator", "reason": "direct"}]}
    if unit_rates == "source":
        unc["sources"].append({"kind": "unit_rates"})
    elif unit_rates == "excluded":
        unc["excluded"].append({"kind": "unit_rates", "reason": "toy: the rates are held at their point values"})
    spec = {
        "schema_version": 1, "question": "Which package per unit (step 0)?", "decision_maker": "programme manager",
        "decision_date": 2026,
        "engine": {"ref": "toolkit.engines.empirical.engine:PackageRateEngine", "config": cfg},
        "horizon": {"start": 2024, "stop": 2027},
        "decision_units": {"csv": "units.csv", "id": "id",
                           "populations": {"all": {"column": "pop", "age_groups": ["all"]}},
                           "engine_attributes": attrs},
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
        "uncertainty": unc,
        "analysis": {"evaluation": "direct", "stability": {"holdout_group": "zone"},
                     "sensitivity": {"parameters": "fitted"}, "decision_tests": []},
        "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation_optimized.csv",
                        "columns": {"unit": "unit", "package": "package", "value": "dalys_averted"},
                        "headline": {"name": "total_dalys_averted", "outcome": "dalys"}},
    }
    p = os.path.join(d, "decision_spec.yaml")
    with open(p, "w") as f:
        yaml.safe_dump(spec, f, sort_keys=False)
    return p


def _toy_trial_run(d, draws=8, **kw) -> dict:
    """The toy spec (_toy_trial_spec) run through toolkit/analysis/run.py (direct evaluation, unsigned)."""
    from toolkit.analysis import run as RUN
    _toy_trial_spec(d, **kw)
    return RUN.run(d, unsigned=True, log=lambda *a, **k: None, compute_kw={"max_workers": 1}, draws=draws)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
