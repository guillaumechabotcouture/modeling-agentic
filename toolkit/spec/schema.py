#!/usr/bin/env python3
"""Decision spec schema and validator (architecture-v2 §3.1), fail closed.

    python3 toolkit/spec/schema.py <decision_spec.yaml>      # prints problems, exit 1 if any

One owner-signed YAML file drives every later layer. Paths inside it are relative to the
spec file's directory (the run directory). Typed sections (design:
docs/design/v2-spec-and-analysis.md §1):

    schema_version: 1
    question: "..."
    decision_maker: "<a role>"
    decision_date: 2026                    # a year or an ISO date
    engine: {ref: "pkg.module:Class", config: {...}}       # constructor keyword arguments; or `auto` /
                                                           # {auto: true, candidates: [...]}: chosen after sign-off,
                                                           # read from engine_choice.json (harness-written)
    engine_parameters:                     # optional: a fixed or scenario parameter's point value for this run
      p: {value, units, source, reason, basis, fitted_to, outside_card_range, range, dist}   # engine_parameters.py
    horizon: {start: 2016.0, stop: 2027.0}                 # simulated period
    decision_units:
      csv: data/units.csv
      id: unit_id
      label: name                          # optional
      region: region_column                # optional: region-level effects and uncertainty
      engine_attributes:                   # engine unit attribute <- column(s) or a value
        a: {column: col}                   # {column, split[, empty]}, {columns: [..] or {k: col}}, {value, source}
                                           # (empty: the cell token of an empty list, e.g. "none")
      populations:                         # population group <- unit column and engine age groups
        all: {column: pop, age_groups: [all]}
      defaults_accepted: {attr: "<reason>"}   # an engine card default taken by name (the card's attribute_binding.yaml);
                                           # every other card attribute is bound or calibrated (v2-derived-inputs)
    options:                              # toolkit/alloc sections (costs.py, packages.py)
      populations: {total: pop}            # alloc population names -> unit columns
      components: {c1: {group: g}, ...}
      packages: {generate: true} | {name: [components]}
                                       # {generate: true, within_envelope: true}: drop the generated packages no
                                       # plan can afford (cost lower bound above the envelope in every unit)
      engine_map: {c1: {engine_component: value}}
      prices: {...}
      cost_items:                          # toolkit/alloc/costs.py validates them (fail closed)
        - {id, component, value, price_year, price_rule, basis, includes: [parts the price covers], ...}
                                       # basis: per_person | per_eligible | per_reached | per_commodity | per_outcome
                                       # | per_unit (F6 S1-3): a fixed amount per unit whose package has the
                                       # component (setup, supervision, capacity), whatever its population; `fixed`
                                       # is its older name. Two items of one component whose includes overlap are
                                       # refused unless one states not_double_counted: {<other id>: "<reason>"}.
                                       # Incremental per_outcome costs floored at zero are counted in
                                       # analysis/point.yaml cost_floors and stated in results.md.
    objective:
      outcome: dalys                       # an engine per-capita output
      sense: avert                         # benefit = comparator outcome - package outcome
      horizon: [2024.0, 2027.0]            # the decision window (outcomes are counted over it)
      delivery: [2024.0, 2026.0]           # optional: the years whose components the decision delivers and pays
                                           # for, inside horizon (default: horizon); only for an engine that declares
                                           # supports_delivery (toolkit/engines/horizon.py)
      discounting: none | {rate: 0.03, base_year: 2024}
    constraints:
      envelope_usd: 1000000                # plain number (in YAML 1.1 write 1000000 or 1.0e+6, not 1.0e6)
      fixed_costs_usd: {name: amount}      # spent outside the allocation
      eligibility: {c2: {column: elig}}    # toolkit/alloc packages.py rules
      caps: {c2: {max_units: 10}}
      coverage: {c2: {max_units_share: 0.5} | {max_population_share: 0.5, population: total}}
      locks: {...}
      footprints: {...}
      group_rules:                         # spike lesson 10: one decision for a group of units
        - {id: campaign, components: [c1], by: region_column, rule: same_choice, reason: "..."}
    comparator: {package: none, text: "what happens without the plan"}
    outcomes_reported: [dalys, cases]
    calibration:
      method: emulator_inversion | likelihood | posterior | none
      ...                                  # see CALIBRATION_FIELDS
      targets: {kind: observations, ...}   # posterior on observations of group states and outcomes through
                                       # declared sources (toolkit/analysis/observed_targets.py)
      target_frame: {band, source, mapping: {to, method, source}}   # the population band the target measures; a
                                           # mapping when it is not the compared output's (targets.frame for a panel)
    uncertainty:                           # spike lesson 6: sources declared up front
      level: 0.95
      draws: 64
      seed: 42
      sources: [{kind: engine_parameters}, {kind: calibration}, {kind: costs, items: {id: [0.8, 1.2]}}]
      excluded: [{kind: emulator, reason: "..."}, {parameter: <prior parameter>, reason: "..."}]
      priors: {<scenario or fixed engine parameter>: {dist: uniform | loguniform | triangular, range: [lo, hi],
               source: "<citation>", verified: unverified}}
                                           # engine-card priors (the engine's priors.yaml) are drawn without being
                                           # listed (kind `priors`, toolkit/spec/priors.py); the label names them
      affordability: {max_share_over_budget: 0.1, enforce: false}   # optional (F6 S1-1): the share of draws in
                                           # which the point plan may cost more than the allocatable budget; above
                                           # it, a gate finding. Without it the share is reported, not judged.
                                           # enforce: true (S1-1b) plans within a reserve: the best visited
                                           # budget fraction (a grid from 0.5, then bisection) whose plan meets the
                                           # rule on the uncertainty draws, not a proven optimum; the same fraction
                                           # applies to envelope tests and the frontier; no visited fraction
                                           # meeting the rule blocks the analysis
    analysis:
      evaluation: emulator | direct        # an emulator for slow engines, the engine itself for cheap ones
      emulator: {points, holdout_share, seed, features, design_seed}
                                           # features: [column | {attribute, encoding[, value][, empty]}]: emulator
                                           # inputs from unit columns or encoded engine unit attributes
                                           # (toolkit/analysis/features.py, F6 S2-4)
      cost_outputs: {treated: {cost_driver: treated_all}}    # per_outcome cost items <- engine cost drivers
      diagnostics: {name: {window: cal, metric: m, summary: [max, median]}}
      stability: {holdout_group: col, covariates: [..]}
      sensitivity: {parameters: fitted | [names], cost_items: {id: [lo, hi]}}
      share_threshold: 0.10                # decision tests: share of units changing package
      decision_tests: [{id, type, text, finding, ...}]     # canonical per-type decision tests:
                                           # type cost_items: multipliers {cost_item_id: m}, one
                                           # price case in which the listed unit costs change together;
                                           # type unit_attribute: unit_attribute {column, units: [ids] | all,
                                           # values | scale | set_from} (a units-table column, F6);
                                           # type calibration_target: calibration_target {units: [ids], scale}
                                           # (the listed units' calibration targets scaled, F6 run 1);
                                           # type outcome_ratio: outcome_ratio {component, reference, values}
                                           # (a package's burden at a ratio of its counterpart's, F6 run 2);
                                           # type burden_scale: burden_scale {references: [validation.burden row
                                           # ids], shares: [s in (0, 1]]} (each row's group's burden scaled by
                                           # (reference / model)^s, F6 run 3)
    validation:                            # optional (F6 S2-5): external estimates the analysis compares with
      burden: {csv, group, arm, daly_parts, window, window_metrics}   # toolkit/analysis/burden.py (the contract)
    evidence:                              # F6: required when the options use a component whose engine card declares
      dossier: evidence_dossier.yaml       # decision inputs: signing refuses until each has a checked, sourced entry
                                           # (toolkit/spec/evidence_dossier.py); the signature binds the file's sha256
    principle_departures:                  # optional: a decision design principle the spec departs from, by the
      - {principle: 2, check: headline_comparator, reason: "..."}   # pre-sign check that finds it (spec_critique.py)
    deliverable:
      kind: package_per_unit
      allocation_file: allocation_optimized.csv
      columns: {unit: district, package: package, value: cases_averted}
      headline: {name: total_cases_averted, outcome: cases}
      plan: expected_value             # optional: point (the default) or expected_value, the plan with the best mean
                                       # over the uncertainty draws, per-unit means in the CSV, a fixed-plan mean
                                       # claim (toolkit/analysis/plan_rule.py)
      by_group: {columns: {group, quantity, value}, quantity: {<engine group>: <cost_outputs name>}}
                                       # optional: one row per (unit, engine group) with the plan's quantity and
                                       # the outcome averted there (a joint package within a unit)

Every check names the section it failed. Nothing here names a disease, a place or an
intervention; those live in engines and specs.

Derived inputs (docs/design/v2-derived-inputs.md): a spec the stage machine drafts, or one in a run
that declares data/derivations.yaml, reads only staged inputs and harness-derived files
(scripts/pipeline.py), binds every engine attribute or accepts its default by name, and declares
its calibration target's frame. Those problems start with their code (units_not_derived,
data_ref_not_derived, derivation_stale, engine_attribute_unbound, attribute_column_empty,
target_frame_unmapped, ...); so does prior_target_not_parameter, which applies to every spec.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import importlib
import json
import math
import os
import re
import sys
from collections.abc import Mapping

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.alloc import costs as acosts, packages as apackages  # noqa: E402
from toolkit.engines.contract import Engine, Horizon  # noqa: E402
from toolkit.engines.horizon import make_horizon  # noqa: E402

SCHEMA_VERSION = 1
REQUIRED = ("schema_version", "question", "decision_maker", "decision_date", "engine", "horizon", "decision_units",
            "options", "objective", "constraints", "comparator", "outcomes_reported", "calibration", "uncertainty",
            "analysis", "deliverable")
CALIBRATION_METHODS = ("emulator_inversion", "likelihood", "posterior", "none")
EVALUATIONS = ("emulator", "direct")
# Spike lesson 6: every kind is either a declared source or excluded with a reason.
UNCERTAINTY_KINDS = {
    "engine_parameters": "fitted engine parameters, uniform within their engine-card ranges",
    "calibration": "calibration (target error for emulator inversion; parameter covariance for likelihood)",
    "costs": "unit costs (a multiplier range per cost item)",
    "emulator": "emulator error (held-out residuals)",
}
# Drawn whenever there is something to draw, so a spec need not list them (v2d note §2.4: version-1 specs stay valid);
# a spec may list one (no-op) or exclude it with a reason, and the interval label then says it is not covered.
OPTIONAL_KINDS = {"priors": "engine parameters with published ranges (engine-card priors.yaml or uncertainty.priors), "
                            "drawn from their declared priors"}
UNCERTAINTY_SHORT = {"engine_parameters": "engine parameters", "calibration": "calibration", "costs": "unit costs",
                     "emulator": "emulator error", "burden_anchor": "external burden estimates",
                     "unit_rates": "per-unit measured rates", "health_weights": "health-measure weights"}
# F6 S2-5d: declared or excluded only when validation.burden has anchor rows with an interval (burden.py checks it)
CONDITIONAL_KINDS = {"burden_anchor": "the anchors' external burden estimates (validation.burden), lognormal within "
                                      "each published interval",
                     # hb3 fix 5: declared or excluded only when the engine declares per-unit sampling (its card)
                     "unit_rates": "each unit's measured rates, drawn from their sampling error as the engine card "
                                   "declares (engine unit_sampling), independently across units",
                     # CEA design §4.3: declared or excluded only when cost_effectiveness.health_measure has weight ranges
                     "health_weights": "health-measure disability/utility weights with declared ranges "
                                       "(cost_effectiveness.health_measure)"}
UNIT_RATES = "unit_rates"
DECISION_TEST_TYPES = ("interaction_rescale", "parameter_value", "group_rule", "envelope", "cost_items",
                       "unit_attribute", "calibration_target", "outcome_ratio", "burden_scale", "boundary_engine")
DELIVERABLE_KINDS = ("package_per_unit", "estimates", "cost_effectiveness")    # estimates/cost_effectiveness: no allocation file
PLAN_RULES = ("point", "expected_value")     # deliverable.plan (toolkit/analysis/plan_rule.py); absent: point
CALIBRATION_FIELDS = {
    "emulator_inversion": ("attribute", "range", "target_column", "window", "metric"),
    "likelihood": ("attribute", "range", "parameters", "targets"),
    "posterior": ("attribute", "range", "parameters", "targets", "priors", "observation"),   # v2d §3.1
    "none": (),
}
TARGET_FIELDS = ("csv", "unit", "start", "package", "observed", "exposure")   # and `stop` (a column) or `period` (years)


class SpecError(ValueError):
    pass


class UnregisteredEngine(SpecError):
    """An engine reference the import policy allows but the engine registry does not hold (REGISTERED_ENGINES)."""


# --------------------------------------------------------------------------- loading
SETTINGS = "analysis_settings.yaml"
# Purely computational settings the run (and the fix loop) may change without a new owner sign-off. Only these
# leaves may live in analysis_settings.yaml; everything that can change costs, constraints, calibration, its
# uncertainty, outcomes, the interval's coverage or the decision tests is in the signed spec (Codex P1-1).
# A leaf set in both files is an error.
SETTINGS_PATHS = frozenset({("uncertainty", "draws"), ("uncertainty", "seed"), ("analysis", "emulator", "points"),
                            ("analysis", "emulator", "seed"), ("analysis", "workers")})


def _leaves(d, pre=()):
    for k, v in d.items():
        if isinstance(v, dict) and v:
            yield from _leaves(v, pre + (k,))
        else:
            yield pre + (k,), v


def load(path: str, data: bytes | None = None) -> "Spec":
    """The spec at `path`. `data`: its bytes, already read (the sign-off validates, hashes and copies one read)."""
    if data is None:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    else:
        raw = yaml.safe_load(data.decode("utf-8"))
    if not isinstance(raw, dict):
        raise SpecError(f"{path}: the spec must be a mapping")
    base = os.path.dirname(os.path.abspath(path))
    sp = os.path.join(base, SETTINGS)
    settings_path = None
    if os.path.exists(sp):
        with open(sp, encoding="utf-8") as f:
            st = yaml.safe_load(f) or {}
        if not isinstance(st, dict):
            raise SpecError(f"{SETTINGS}: must be a mapping")
        for leaf, v in _leaves(st):
            name = ".".join(map(str, leaf))
            if leaf not in SETTINGS_PATHS:
                raise SpecError(f"{SETTINGS}: {name} is not a computational setting "
                                f"({', '.join('.'.join(x) for x in sorted(SETTINGS_PATHS))}); it belongs in the signed "
                                "decision spec")
            node = raw
            for k in leaf[:-1]:
                node = node.setdefault(k, {})
                if not isinstance(node, dict):
                    raise SpecError(f"{SETTINGS}: {name}: the signed spec's {k} is not a mapping")
            if leaf[-1] in node:
                raise SpecError(f"{SETTINGS}: {name} is also in the signed spec; keep it in one place")
            node[leaf[-1]] = v
        settings_path = sp
    s = Spec(raw, base, path)
    s.settings_path = settings_path
    _resolve_engine_choice(s)
    return s


ENGINE_CHOICE = "engine_choice.json"


def engine_is_auto(eng) -> bool:
    return eng == "auto" or (isinstance(eng, Mapping) and eng.get("auto") is True)


def engine_has_build(eng) -> bool:
    return isinstance(eng, Mapping) and isinstance(eng.get("build"), Mapping)


def _resolve_engine_choice(s: "Spec") -> None:
    """engine: auto (or {auto: true, candidates: [...]}) and/or engine: {build: {...}} in the signed spec: the engine
    is chosen or built after sign-off and recorded by the harness in engine_choice.json beside the spec. Fail closed on
    a malformed choice, reserved config keywords, or a choice outside the signed candidates. An explicit named engine
    without `build` wins and the choice file is ignored."""
    eng = s.raw.get("engine")
    has_auto = engine_is_auto(eng)
    has_build = engine_has_build(eng)
    if not (has_auto or has_build):
        return
    s.engine_auto = has_auto
    if has_build:
        s.engine_build = eng["build"]
    cands = eng.get("candidates") if isinstance(eng, dict) else None
    if cands is not None and (not isinstance(cands, list) or not all(isinstance(c, str) and ":" in c for c in cands)):
        raise SpecError("engine.candidates: a list of 'module:Class' references")
    for c in (cands or []) + ([eng["ref"]] if isinstance(eng, dict) and eng.get("ref") is not None else []):
        check_engine_ref(c)         # the candidates, and a ref written beside auto: true or build (Codex r1 of #367)
    cp = os.path.join(s.base, ENGINE_CHOICE)
    if not os.path.exists(cp):
        s.engine_pending = True
        return
    try:
        doc = json.load(open(cp, encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SpecError(f"{ENGINE_CHOICE}: unreadable: {e}") from e
    # Pending build choice record before ENGINE_SIGN: {"build": {"id", "status": "requested"|"building"|"admitted"}}
    if has_build and isinstance(doc, dict) and isinstance(doc.get("build"), dict) and "engine" not in doc:
        b = doc["build"]
        if b.get("status") not in ("requested", "building", "admitted") or b.get("id") != eng["build"].get("id"):
            raise SpecError(f"{ENGINE_CHOICE}: malformed pending build choice record")
        s.engine_pending = True
        s.engine_choice_path = cp
        return
    e = doc.get("engine") if isinstance(doc, dict) else None
    if not isinstance(e, dict) or not isinstance(e.get("ref"), str) or ":" not in e["ref"] \
            or not isinstance(e.get("config") or {}, dict):
        raise SpecError(f"{ENGINE_CHOICE}: engine must be {{ref: 'module:Class', config: {{...}}}}")
    from toolkit.engines.built import BUILT_ADAPTER_REF
    is_built_adapter = has_build and e["ref"] == BUILT_ADAPTER_REF
    if has_build and not has_auto and not is_built_adapter:
        raise SpecError(f"{ENGINE_CHOICE}: a plain engine.build spec may resolve only to {BUILT_ADAPTER_REF}, "
                        f"not {e['ref']!r}")
    if cands and not is_built_adapter and e["ref"] not in cands:
        raise SpecError(f"{ENGINE_CHOICE}: engine {e['ref']} is not among the signed candidates {cands}")
    check_engine_ref(e["ref"], allow_built=has_build)
    if is_built_adapter:
        from toolkit.engines.built.requested import RESERVED_CONFIG_KEYS
        res_hits = sorted(RESERVED_CONFIG_KEYS & set(e.get("config") or {}))
        if res_hits:
            raise SpecError(f"{ENGINE_CHOICE}: reserved harness keywords in built engine config: {res_hits}")
    s.raw["engine"] = {"ref": e["ref"], **({"config": dict(e["config"])} if e.get("config") else {})}
    s.engine_choice_path = cp


def sha256_file(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


class Spec:
    """The raw mapping plus the derived views every step uses."""

    def __init__(self, raw: dict, base: str, path: str | None = None):
        self.raw, self.base, self.path = raw, base, path
        self.settings_path = None
        self.engine_auto, self.engine_pending, self.engine_choice_path = False, False, None
        self.engine_build = None
        self.derived = None          # derived-input obligations: None = when the run declares derivations
        if (engine_is_auto(raw.get("engine")) or engine_has_build(raw.get("engine"))) and path is None:
            self.engine_auto = engine_is_auto(raw.get("engine"))
            if engine_has_build(raw.get("engine")):
                self.engine_build = raw["engine"]["build"]
            self.engine_pending = True      # a mapping with no file beside it: no choice to read
        try:          # the engine declaration as signed (an engine choice replaces raw["engine"] later)
            self.engine_declared = copy.deepcopy(raw.get("engine"))
        except TypeError:
            self.engine_declared = raw.get("engine")
        self._engine = None
        self._engine_ref = None      # the engine declaration _engine was loaded from
        self._units = None
        self._cal_scale = None

    def __getitem__(self, k):
        return self.raw[k]

    def get(self, k, d=None):
        return self.raw.get(k, d)

    def resolve(self, p: str) -> str:
        return p if os.path.isabs(p) else os.path.join(self.base, p)

    @property
    def sha256(self) -> str | None:
        return sha256_file(self.path) if self.path else None

    @property
    def settings_sha256(self) -> str | None:
        return sha256_file(self.settings_path) if self.settings_path else None

    @property
    def engine_choice_sha256(self) -> str | None:
        return sha256_file(self.engine_choice_path) if self.engine_choice_path else None

    # ---- engine
    def _engine_key(self) -> str:
        return json.dumps(self.raw.get("engine"), sort_keys=True, default=str)

    def engine(self):
        if self.engine_pending:
            raise SpecError(f"engine: auto, and the harness has not written {ENGINE_CHOICE} yet")
        # loaded again when the spec's engine changed since (an engine set in place, with no key, is kept)
        if self._engine is None or (self._engine_ref is not None and self._engine_ref != self._engine_key()):
            self._engine = load_engine(self.raw["engine"], allow_built=(self.engine_build is not None),
                                       run_dir=self.base)
            self._engine_ref = self._engine_key()
        return self._engine

    def scale_cards(self):
        """check_scale's cards for this spec: each signed candidate while the engine choice is pending (scale_cards
        on the raw mapping), else the engine's own card (a read failure is the entry, so the check refuses)."""
        if self.raw.get("engine") is None:
            return None               # no engine named: the documented default, as for a raw mapping
        if self.engine_pending or engine_is_auto(self.raw.get("engine")):
            return scale_cards(self.raw)
        try:
            eng = self.engine()
        except Exception as e:  # noqa: BLE001 -- fail closed
            return [(self._engine_key(), e)]
        return scale_cards(self.raw, engine=eng)

    def check_scale(self) -> tuple[str | None, list[str]]:
        """check_scale for this spec: the cards are read only when the answer depends on them (an attribute, and a
        scale that is not a declared linear one)."""
        cal = self.raw.get("calibration") if isinstance(self.raw, Mapping) else None
        cal = cal if isinstance(cal, Mapping) else {}
        try:
            need = _scale_of(cal) != "linear" and isinstance(cal.get("attribute"), str)
        except CalibrationScaleError as e:
            return None, [str(e)]
        return check_scale(cal, self.scale_cards() if need else None)

    def calibration_scale(self) -> str:
        """The calibrated attribute's scale (check_scale: declared, else from the engine card, else linear), or
        CalibrationScaleError. An answer is kept per calibration attribute, declared scale and engine; a refusal (a
        card read failure included) is never kept, so every call refuses until the card is read."""
        cal = self.raw.get("calibration") if isinstance(self.raw, Mapping) else None
        cal = cal if isinstance(cal, Mapping) else {}
        key = (repr(cal.get("attribute")), "scale" in cal, repr(cal.get("scale")), self.engine_pending,
               self._engine_key())
        if self._cal_scale is not None and self._cal_scale[0] == key:
            return self._cal_scale[1]
        sc, bad = self.check_scale()
        if bad:
            raise CalibrationScaleError("; ".join(bad))
        self._cal_scale = (key, sc)
        return sc

    def horizon(self, windows: dict | None = None) -> Horizon:
        """The engine horizon; a DeliveryHorizon when the spec gives objective.delivery (else the contract's own)."""
        h, o = self.raw["horizon"], self.raw["objective"]
        return make_horizon(h["start"], h["stop"], tuple(float(x) for x in o["horizon"]), windows, o.get("delivery"))

    # ---- units
    def units(self) -> list[dict]:
        if self._units is None:
            du = self.raw.get("decision_units") if isinstance(self.raw.get("decision_units"), Mapping) else {}
            if "csv" not in du and isinstance(du.get("units_inline"), list):
                self._units = [dict(u) for u in du["units_inline"] if isinstance(u, Mapping)]
            else:
                with open(self.resolve(self.raw["decision_units"]["csv"]), newline="", encoding="utf-8") as f:
                    self._units = list(csv.DictReader(f))
        return self._units

    @property
    def uid(self) -> str:
        return self.raw["decision_units"]["id"]

    def unit_ids(self) -> list[str]:
        return [str(u[self.uid]) for u in self.units()]

    def unit_attributes(self, row: dict) -> dict:
        """Engine unit attributes for one units-table row."""
        out = {}
        for attr, m in (self.raw["decision_units"].get("engine_attributes") or {}).items():
            if "value" in m:
                out[attr] = m["value"]
            elif "column" in m:
                v = row[m["column"]]
                if "split" not in m:
                    out[attr] = _num(v)
                elif "empty" in m and str(v).strip() == str(m["empty"]).strip():
                    out[attr] = []          # the declared token of an empty list (a blank cell is refused)
                else:
                    out[attr] = [float(x) for x in str(v).split(m["split"])]
            elif isinstance(m["columns"], dict):
                out[attr] = {k: float(row[c]) for k, c in m["columns"].items()}
            else:
                out[attr] = [float(row[c]) for c in m["columns"]]
        return out

    # ---- options and constraints, in toolkit/alloc form
    def alloc_spec(self) -> dict:
        """The toolkit/alloc spec: options plus the constraints (one place to read eligibility and caps)."""
        o, c = self.raw["options"], self.raw.get("constraints") or {}
        a = {k: copy.deepcopy(v) for k, v in o.items() if k in ("components", "packages", "prices", "cost_items")}
        p = o.get("packages")
        if isinstance(p, dict) and p.get("generate") is True and p.get("within_envelope") is True:
            a["packages"] = {n: sorted(cs) for n, cs in self.packages().items()}    # the envelope-pruned list
        a["units"] = {"csv": self.resolve(self.raw["decision_units"]["csv"]), "id": self.uid,
                      "populations": dict(o.get("populations") or {})}
        for k in ("eligibility", "locks", "footprints"):
            if c.get(k):
                a[k] = copy.deepcopy(c[k])
        caps = copy.deepcopy(c.get("caps") or {})
        units = self.units()
        for comp, cov in (c.get("coverage") or {}).items():
            cap = caps.setdefault(comp, {})
            if "max_units_share" in cov:      # keep both limits: the binding one is the smaller (Codex P1-2)
                v = float(cov["max_units_share"]) * len(units)
                cap["max_units"] = min(v, float(cap["max_units"])) if "max_units" in cap else v
            if "max_population_share" in cov:
                pop = cov.get("population", "total")
                col = (o.get("populations") or {}).get(pop, pop)
                v = float(cov["max_population_share"]) * sum(float(u[col]) for u in units)
                if "max_quantity" in cap and cap.get("quantity", pop) != pop:
                    raise SpecError(f"constraints: {comp} has a max_quantity on {cap.get('quantity')} and a coverage "
                                    f"share on {pop}; give one quantity")
                cap["max_quantity"] = min(v, float(cap["max_quantity"])) if "max_quantity" in cap else v
                cap["quantity"] = pop
        if caps:
            a["caps"] = caps
        if c.get("group_rules"):
            a["group_rules"] = {g["id"]: {k: v for k, v in g.items() if k != "id"} for g in c["group_rules"]}
        return a

    def packages(self) -> dict:
        """Package name -> components. With `options.packages: {generate: true, within_envelope: true}`, a generated
        package whose cost exceeds the envelope in every unit is dropped (no feasible plan can use it): the cost is
        bounded below by its non-outcome cost items, which must be non-negative. Exact, and it keeps a joint package
        space (dose blocks over groups) to the packages a plan can afford."""
        pk = acosts.packages(self.raw["options"])
        p = self.raw["options"].get("packages")
        if not (isinstance(p, dict) and p.get("generate") is True and p.get("within_envelope") is True):
            return pk
        if getattr(self, "_pruned", None) is not None:
            return self._pruned
        o = self.raw["options"]
        a = {"components": o.get("components") or {}, "prices": o.get("prices") or {},
             "units": {"populations": dict(o.get("populations") or {})}}
        items = [it for it in o.get("cost_items") or [] if it.get("basis") != "per_outcome"]
        if any(not _isnum(it.get("value")) or it["value"] < 0 for it in items):
            raise SpecError("options.packages.within_envelope: every cost item value must be a non-negative number")
        budget = self.budget()
        # Codex r1 P1-4: the bound holds in every scenario the analysis evaluates. Costs are taken at the lowest
        # multiplier any uncertainty draw or sensitivity case applies to the item; budgets above the envelope (the
        # frontier's upper fractions, envelope decision tests) are refused by those steps (prune_budget).
        lo = {}
        for rng_items in ((self.raw.get("uncertainty") or {}).get("sources") or []):
            if isinstance(rng_items, dict) and rng_items.get("kind") == "costs":
                for it_id, r in (rng_items.get("items") or {}).items():
                    lo[it_id] = min(lo.get(it_id, 1.0), float(r[0]))
        for it_id, r in (((self.raw.get("analysis") or {}).get("sensitivity") or {}).get("cost_items") or {}).items():
            lo[it_id] = min(lo.get(it_id, 1.0), float(r[0]))
        for dt in ((self.raw.get("analysis") or {}).get("decision_tests") or []):      # a cost_items test's price case
            if isinstance(dt, dict) and dt.get("type") == "cost_items" and isinstance(dt.get("multipliers"), dict):
                for it_id, m in dt["multipliers"].items():
                    if _isnum(m):
                        lo[it_id] = min(lo.get(it_id, 1.0), float(m))
        comp_cost = {}
        from toolkit.analysis import decision_tests as DT     # #228 r1 P2-3: and over every unit_attribute arm's table
        tables = [self.units()] + DT.arm_tables(self)
        for n_, u in ((n_, u) for n_, rows in enumerate(tables) for u in rows):
            uid = f"{n_}:{u[self.uid]}"
            comp_cost[uid] = {}
            for it in items:
                comp_cost[uid][it["component"]] = (comp_cost[uid].get(it["component"], 0.0)
                                                   + acosts.item_cost(it, u, a) * min(1.0, lo.get(it["id"], 1.0)))
        comp = self.raw["comparator"].get("package")
        keep = {}
        for name, cs in pk.items():
            low = min(sum(comp_cost[uid].get(c, 0.0) for c in cs) for uid in comp_cost)
            if name == comp or low <= budget * (1 + 1e-12) + 1e-9:
                keep[name] = cs
        self._pruned = keep
        self.prune_budget = budget
        return keep

    def engine_package(self, comps) -> dict:
        out = {}
        for c in sorted(comps):
            out.update(self.raw["options"]["engine_map"][c])
        return out

    @property
    def comparator(self) -> str:
        return self.raw["comparator"]["package"]

    def budget(self) -> float:
        c = self.raw["constraints"]
        return float(c["envelope_usd"]) - sum(float(v) for v in (c.get("fixed_costs_usd") or {}).values())

    def outcomes(self) -> list[str]:
        prim = self.raw["objective"]["outcome"]
        rest = [o for o in self.raw.get("outcomes_reported") or [] if o != prim]
        return [prim] + rest

    def uncertainty_scope(self) -> dict:
        """What an interval covers and what it does not (spike lesson 6), for labels."""
        u = self.raw["uncertainty"]
        inc = [s["kind"] for s in u.get("sources") or [] if s.get("kind") not in OPTIONAL_KINDS]
        exc = [(e["kind"], e.get("reason", "")) for e in u.get("excluded") or [] if "kind" in e]
        lvl = float(u.get("level", 0.95))
        meta = {"level": lvl, "covers": inc, "excludes": [k for k, _ in exc]}
        if not self.engine_pending:
            # v2 priors: the label names what the draws vary and what they hold fixed (toolkit/spec/priors.py)
            from toolkit.spec import priors as PRI
            P = PRI.plan(self)
            if P["prior_dominated"]:
                at = inc.index("engine_parameters") + 1 if "engine_parameters" in inc else 0
                meta["covers"] = inc = inc[:at] + ["priors"] + inc[at:]
                meta["prior_dominated"] = P["prior_dominated"]
            if P["label_fixed"].get("spec_fixed"):
                meta["spec_fixed"] = P["label_fixed"]["spec_fixed"]
            if P["label_fixed"]["named"]:
                meta["fixed"] = P["label_fixed"]["named"]
            if P["label_fixed"]["other"]:
                meta["fixed_other"] = P["label_fixed"]["other"]
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import render_claims    # one rule for the label, from structured metadata only (Codex P1-5)
        return {"level": lvl, "covers": inc, "excludes": [{"kind": k, "reason": r} for k, r in exc],
                "interval": meta, "label": render_claims.interval_label(meta)}


# Run console review rounds 2-3 (P1-1): the spec names its engine as "module:Class", and an agent drafts the spec, so
# the reference is untrusted. Every engine import goes through toolkit/import_guard.py (policy "engine": modules
# under toolkit/engines/, checked as files before anything is imported); the class must implement the Engine
# contract before it is instantiated.
ENGINE_METHODS = ("describe", "parameters", "run")


# The engine registry's references: the engines agents/analysis_backend.py ENGINES lists with their card, configuration
# and benchmark checks (its self-test B_REGISTRY holds the two lists equal). Kept here, not read from agents/, because
# engine workers receive toolkit/ and scripts/ only (scripts/compute.py REPO_CODE_DIRS).
REGISTERED_ENGINES = ("toolkit.engines.contract:ToyEngine", "toolkit.engines.malaria.engine:MalariaEngine",
                      "toolkit.engines.empirical.engine:PackageRateEngine",
                      "toolkit.engines.compartmental.engine:CompartmentalEngine",
                      "toolkit.engines.renewal.engine:RenewalEngine")


def registered_engines() -> list[str]:
    """The engine references a spec may name (REGISTERED_ENGINES)."""
    return list(REGISTERED_ENGINES)


def check_engine_ref(ref, *, allow_built: bool = False) -> tuple[str, str]:
    """(module, class) of an allowed engine reference, or SpecError. Imports nothing. The import guard's policy, then
    the registry (PR #363 question 15): a ref the policy allows but the registry does not hold (a hook or toy engine
    under toolkit/engines/) is refused, whether the signed spec names it, an engine: auto candidate lists it or an
    engine choice picks it. `toolkit.engines.built:BuiltEngine` is accepted only when `allow_built=True` (resolved
    from a signed build spec); submodules under `toolkit.engines.built.*` are always refused. Self-tests extend the
    registry in-process only (import_guard.register_test_engine)."""
    from toolkit import import_guard as G
    from toolkit.engines.built import BUILT_ADAPTER_REF, BUILT_REF_PREFIX
    if isinstance(ref, str) and (ref == BUILT_ADAPTER_REF or ref.startswith(BUILT_REF_PREFIX + ".")) and not (
            allow_built and ref == BUILT_ADAPTER_REF):
        raise UnregisteredEngine(f"engine {ref}: built-engine adapter is reachable only through a signed engine.build request")
    try:
        out = G.check_ref("engine", ref)
    except G.ImportRefused as e:
        raise SpecError(f"engine {str(ref)[:80]}: {e}") from None
    if allow_built and ref == BUILT_ADAPTER_REF:
        return out
    reg = registered_engines()
    if ref not in reg and not G.test_engine_registered(ref):
        raise UnregisteredEngine(f"engine {ref}: not a registered engine; a spec may name one of: {', '.join(reg)}")
    return out


def engine_ref_problems(raw: Mapping, *, allow_built: bool = False) -> list[str]:
    """check_engine_ref on the engine a spec mapping names, or on each engine: auto candidate and on a `ref` written
    beside `auto: true` or `build` (never left unchecked): the refusals."""
    eng = raw.get("engine")
    if engine_is_auto(eng):
        refs = eng.get("candidates") if isinstance(eng, Mapping) and isinstance(eng.get("candidates"), list) else []
        refs = list(refs) + ([eng["ref"]] if isinstance(eng, Mapping) and eng.get("ref") is not None else [])
    elif engine_has_build(eng):
        refs = [eng["ref"]] if eng.get("ref") is not None else []
    else:
        refs = [eng.get("ref") if isinstance(eng, Mapping) else eng] if eng is not None else []
    out = []
    for r in refs:
        try:
            check_engine_ref(r, allow_built=allow_built)
        except SpecError as e:
            out.append(str(e))
    return out


def engine_class(ref: str, *, allow_built: bool = False):
    """The engine class an allowed reference names, checked against the Engine contract; never instantiated."""
    from toolkit import import_guard as G
    check_engine_ref(ref, allow_built=allow_built)
    try:
        k = G.resolve("engine", ref)
    except G.ImportRefused as e:
        raise SpecError(str(e)) from None
    if not isinstance(k, type) or not all(callable(getattr(k, m, None)) for m in ENGINE_METHODS):
        raise SpecError(f"engine {ref}: not a class implementing the Engine contract ({', '.join(ENGINE_METHODS)})")
    return k


def load_engine(ref, *, allow_built: bool = False, run_dir: str | None = None):
    if isinstance(ref, str):
        ref = {"ref": ref}
    if not isinstance(ref, dict):
        raise SpecError("engine: {ref: 'module:Class', config: {...}}")
    config = ref.get("config") or {}
    if not isinstance(config, dict):
        raise SpecError("engine.config must be a mapping")
    from toolkit.engines.built import BUILT_ADAPTER_REF
    from toolkit.engines.built.requested import RESERVED_CONFIG_KEYS
    res_hits = sorted(k for k in RESERVED_CONFIG_KEYS if k in config)
    if res_hits:
        raise SpecError(f"engine.config: reserved harness keywords refused: {res_hits}")
    cls = engine_class(ref.get("ref"), allow_built=allow_built)
    if ref.get("ref") == BUILT_ADAPTER_REF:
        return cls(run_dir=run_dir, **config)
    return cls(**config)


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# --------------------------------------------------------------------------- validation
def validate(spec: "Spec | dict", base: str | None = None, pending_ok: bool = False,
             derived: bool | None = None) -> list[str]:
    """Problems with the spec (empty = valid). Never raises on a malformed spec. With engine: auto and no engine choice
    yet, `pending_ok` (sign-off) checks the sections that do not depend on the engine; otherwise that is a problem.
    `derived` (docs/design/v2-derived-inputs.md): True for a spec the stage machine drafted, which must read derived or
    staged data only and bind every engine attribute; None applies the same obligations when the run declares
    derivations (data/derivations.yaml beside the spec), the opt-in for hand-written specs; False never."""
    if isinstance(spec, dict):
        spec = Spec(spec, base or os.getcwd())
    if derived is not None:
        spec.derived = derived
    try:
        has_build_ctx = spec.engine_build is not None or engine_has_build(getattr(spec, "engine_declared", None))
        unreg = engine_ref_problems(spec.raw, allow_built=has_build_ctx)
        if has_build_ctx and getattr(spec, "engine_declared", None) is not None:
            unreg += [p for p in engine_ref_problems({"engine": spec.engine_declared}, allow_built=False)
                      if p not in unreg]
        if unreg:
            return unreg
        pres = presentation_refusal(spec.raw)         # every path, sign-off with engine: auto pending included
        from toolkit.engines import benchmark_coverage as BC      # owner decision 2026-11-12: signed departures
        pres = pres + BC.departure_problems(spec.raw)
        from toolkit.analysis import conflicts as CF       # W0-F: conflicts.policy, signed with the spec
        pres = pres + CF.spec_problems(spec.raw)
        if spec.engine_pending:
            if engine_has_build(spec.raw.get("engine")):
                from toolkit.engines.built import requested as BREQ
                bprobs = BREQ.validate_build(spec.raw["engine"]["build"])
                if bprobs:
                    return pres + bprobs
                if not pending_ok:
                    return [f"engine: build {spec.raw['engine']['build'].get('id')!r} has not been built and signed "
                            f"in {ENGINE_CHOICE} yet"] + pres
                req_eng = BREQ.RequestedEngine(spec.raw["engine"]["build"])
                s_req = with_engine(spec, req_eng)
                from toolkit.spec import preconditions as PC
                return pres + _validate(s_req) + (_derived_checks(s_req, req_eng) if obligations(spec) else []) \
                    + PC.problems(spec.base, None, None, spec=spec)
            if not pending_ok:
                return [f"engine: auto, and the harness has not written {ENGINE_CHOICE} yet (engine-dependent sections "
                        "cannot be checked)"] + pres
            from toolkit.spec import preconditions as PC       # every candidate the engine choice could pick
            return pres + _validate_pending(spec) + (_derived_checks(spec, None) if obligations(spec) else []) \
                + PC.problems(spec.base, None, None, spec=spec)
        # the signed spec's preconditions at the execution boundary (the analysis validates here; #215, #218)
        from toolkit.spec import preconditions as PC
        return pres + _validate(spec) + (_derived_checks(spec, spec.engine()) if obligations(spec) else []) \
            + PC.problems(spec.base, None, None, spec=spec)
    except Exception as e:  # noqa: BLE001 -- a malformed section must be a finding, not a crash
        return [f"spec could not be checked: {type(e).__name__}: {e}"]


# --------------------------------------------------------------------------- derived inputs (v2-derived-inputs)
DERIVATIONS = "data/derivations.yaml"
BINDING_FILE = "attribute_binding.yaml"


def obligations(s: Spec) -> bool:
    """Do the derived-input obligations apply? A machine-drafted spec (derived=True), or a run that declares them."""
    if s.derived is not None:
        return bool(s.derived)
    return os.path.exists(os.path.join(s.base, DERIVATIONS))


def _derived_checks(s: Spec, eng) -> list[str]:
    """Data the spec reads is derived or staged (scripts/pipeline.py), the target's frame matches the output it is
    compared with, and (with an engine) every engine attribute is bound or its default accepted by name."""
    if os.path.join(ROOT, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import pipeline as P
    bad = [f"{p['code']}: {p['claim']}" for p in P.derivation_problems(s.base, s.raw, required=True)]
    bad += frame_problems(s)
    if eng is not None:
        try:
            bad += attribute_problems(s, eng)
        except Exception as e:  # noqa: BLE001 -- fail closed
            bad.append(f"engine_attribute_unbound: the attribute binding could not be checked: {type(e).__name__}: {e}")
    return bad


def _read_binding(path: str | None) -> tuple[dict, list[str]]:
    if not path or not os.path.exists(path):
        return {}, []
    try:
        doc = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        return {}, [f"engine_attribute_unbound: attribute binding {path} unreadable: {e}"]
    b = doc.get("attribute_binding") if isinstance(doc, dict) else None
    if not isinstance(b, dict):
        return {}, [f"engine_attribute_unbound: {path}: `attribute_binding` must be a mapping"]
    out, bad = {}, []
    for k, v in b.items():
        if v == "required" or (isinstance(v, dict) and "default" in v and str(v.get("meaning") or "").strip()):
            why = _binding_extras(v) if isinstance(v, dict) else []
            if why:
                bad += [f"engine_attribute_unbound: {path}: {k}: {w}" for w in why]
            else:
                out[str(k)] = v
        else:
            bad.append(f"engine_attribute_unbound: {path}: {k} must be `required` or {{default, meaning}}")
    return out, bad


BINDING_PRIOR_DISTS = ("uniform", "loguniform", "triangular")


def _binding_extras(v: dict) -> list[str]:
    """The optional card keys of a defaulted attribute: `prior` {dist, range: [lo, hi], source, ...}, the sourced range a
    spec that calibrates the attribute starts from; `limitation`, what the attribute simplifies, which the results
    state whenever a run calibrates it."""
    bad = []
    if "limitation" in v and not (isinstance(v["limitation"], str) and v["limitation"].strip()):
        bad.append("limitation must be a non-empty text")
    if "prior" in v:
        p = v["prior"]
        if not isinstance(p, dict):
            return bad + ["prior must be {dist, range: [lo, hi], source}"]
        if p.get("dist") not in BINDING_PRIOR_DISTS:
            bad.append(f"prior dist {p.get('dist')!r} is not one of {list(BINDING_PRIOR_DISTS)}")
        r = p.get("range")
        try:
            lo, hi = (float(x) for x in r)
            if not (math.isfinite(lo) and math.isfinite(hi) and lo < hi):
                raise ValueError
        except (TypeError, ValueError):
            bad.append(f"prior range {r!r} must be [lo, hi] with lo < hi")
        if not str(p.get("source") or "").strip():
            bad.append("prior needs a source")
    return bad


CALIBRATION_SCALES = ("log", "linear")


class CalibrationScaleError(SpecError):
    """The calibrated attribute's scale cannot be resolved: an invalid declared scale (null included), a log scale on
    a card range going below 0, candidate cards that disagree, or an engine card that could not be read. Never a
    silent fallback to another scale."""


def card_scale(card, attr) -> str | None:
    """The calibration scale an engine card implies for a unit attribute, from the attribute's valid range
    (EngineCard.valid_ranges): "linear" when the range goes below 0, -inf included (exp(z) > 0 never produces such a
    value, so a log scale cannot fit it), "log" otherwise (a range from 0 up: exp(z) reaches as close to 0 as the
    calibration range asks, the default these attributes always had); None when the card gives no range."""
    rng = (getattr(card, "valid_ranges", None) or {}).get(attr) if isinstance(attr, str) else None
    try:
        lo = float(rng[0])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    if math.isnan(lo):
        return None
    return "linear" if lo < 0 else "log"


def _scale_of(cal: Mapping) -> str | None:
    """The declared scale, None when undeclared; CalibrationScaleError for anything but log or linear (null too)."""
    if "scale" not in cal:
        return None
    if cal["scale"] not in CALIBRATION_SCALES:
        raise CalibrationScaleError(f"calibration.scale: log or linear (got {cal['scale']!r}; an explicit null is not "
                                    "the default)")
    return cal["scale"]


def check_scale(cal, cards) -> tuple[str | None, list[str]]:
    """(scale, problems): the one place the calibrated attribute's scale is decided and checked. The declared
    calibration.scale; else what the engine cards imply for the attribute (card_scale; no range: linear); else, with
    no engine to read (`cards` None), linear. `cards`: [(label, card or the exception its read raised)] for every
    engine the scale can depend on (the engine; with engine: auto before the choice, each signed candidate).
    Problems (scale None): an invalid declared scale; a log scale (declared or implied) on a card range going below
    0; candidates implying different scales for an undeclared scale; a card that could not be read when the answer
    depends on it (a declared linear scale does not). (Before: toolkit/analysis/batch.py read an undeclared scale as
    log while attribute_notes read it as linear, so a log-named attribute on [-8, 2] was given exp(z) > 0.)"""
    cal = cal if isinstance(cal, Mapping) else {}
    try:
        sc = _scale_of(cal)
    except CalibrationScaleError as e:
        return None, [str(e)]
    a = cal.get("attribute")
    if not isinstance(a, str) or not a or sc == "linear" or cards is None:
        return sc or "linear", []
    bad, implied = [], {}
    for label, card in cards:
        if isinstance(card, BaseException):
            bad.append(f"calibration.scale: the engine card of {label} could not be read to resolve the scale of {a} "
                       f"({type(card).__name__}: {str(card)[:200]}); no scale is assumed")
            continue
        cs = card_scale(card, a)
        implied[label] = cs or "linear"
        if cs == "linear" and sc == "log":
            rng = (getattr(card, "valid_ranges", None) or {}).get(a)
            bad.append(f"calibration.scale: log for {a}, whose engine card range {list(rng)} ({label}) includes "
                       f"values below 0: a log scale sets {a} = exp(z) > 0 and cannot reach them. Declare scale: "
                       f"linear (the range is then on {a}'s own scale)")
    if bad:
        return None, bad
    if sc is None:
        got = set(implied.values())
        if len(got) > 1:
            return None, [f"calibration.scale: undeclared, and the candidate engines' cards imply different scales "
                          f"for {a} ({implied}); declare log or linear"]
        sc = got.pop() if got else "linear"
    return sc, []


def calibration_scale(cal, card=None) -> str:
    """check_scale against one card already in hand (None: no card): the scale, or CalibrationScaleError."""
    sc, bad = check_scale(cal, [("the engine", card)] if card is not None else None)
    if bad:
        raise CalibrationScaleError("; ".join(bad))
    return sc


def _card_or_error(ref):
    try:
        return load_engine(ref).describe()
    except Exception as e:  # noqa: BLE001 -- fail closed: check_scale refuses when the answer needs this card
        return e


def _auto_cards(raw: Mapping) -> list:
    """[(ref, card or exception)] for engine: auto: the engines engine selection considers (the signed candidates,
    else every registered engine: agents/analysis_backend.py candidate_entries), each built with the configuration
    the selection gives it. A candidate whose card cannot be read that way, or that does not declare the calibrated
    attribute, is one the selection never picks, so it is left out; when none is left, an entry that refuses."""
    from agents import analysis_backend as AB
    eng = raw.get("engine")
    r = dict(raw, engine=dict(eng)) if isinstance(eng, Mapping) else dict(raw)
    cal = r.get("calibration") if isinstance(r.get("calibration"), Mapping) else {}
    a = cal.get("attribute")
    out, why = [], []
    try:
        entries = AB.candidate_entries(r)
    except Exception as e:  # noqa: BLE001 -- fail closed
        return [("engine: auto", e)]
    for entry in entries:
        ref, card_path, cfg_fn = (tuple(entry) + (None, None))[:3]
        try:
            card = AB._card(ref, card_path, cfg_fn(r) if cfg_fn else {})
        except Exception as e:  # noqa: BLE001 -- not a candidate the selection can pick
            why.append(f"{ref}: card unreadable ({type(e).__name__}: {str(e)[:120]})")
            continue
        if a not in (getattr(card, "unit_attributes", None) or {}):
            why.append(f"{ref}: does not declare {a}")
            continue
        out.append((ref, card))
    return out or [("engine: auto", SpecError(f"no candidate engine can run the calibrated attribute {a} "
                                              f"({'; '.join(why) or 'none listed'})"))]


def scale_cards(raw: Mapping, engine=None):
    """[(label, card or exception)] the scale of a raw spec mapping depends on: `engine` (an engine instance in hand)
    or the engine the mapping names; with engine: auto, the engines the selection may pick (_auto_cards); None when
    no engine is named."""
    if engine is not None:
        try:
            return [(type(engine).__name__, engine.describe())]
        except Exception as e:  # noqa: BLE001 -- fail closed
            return [(type(engine).__name__, e)]
    eng = raw.get("engine")
    if eng is None:
        return None
    if engine_is_auto(eng):
        cards = _auto_cards(raw)
        if engine_has_build(eng):
            from toolkit.engines.built import requested as BREQ
            try:
                cards = [c for c in cards if c[0] != "engine: auto"] + [(f"build:{eng['build'].get('id')}",
                                                                         BREQ.RequestedEngine(eng["build"]).describe())]
            except Exception as e:  # noqa: BLE001
                cards = list(cards) + [(f"build:{eng['build'].get('id')}", e)]
        return cards
    if engine_has_build(eng):
        from toolkit.engines.built import requested as BREQ
        try:
            return [(f"build:{eng['build'].get('id')}", BREQ.RequestedEngine(eng["build"]).describe())]
        except Exception as e:  # noqa: BLE001
            return [(f"build:{eng['build'].get('id')}", e)]
    label = eng if isinstance(eng, str) else eng.get("ref") if isinstance(eng, Mapping) else repr(eng)
    return [(str(label), _card_or_error(dict(eng) if isinstance(eng, Mapping) else eng))]


def resolved_scale(spec) -> str:
    """The calibrated attribute's scale for a Spec (Spec.calibration_scale) or any raw spec mapping (read-only ones
    included): check_scale against the cards of the engine(s) it names. Raises CalibrationScaleError, never falls
    back. Every consumer (the batch design, the joint calibration, the figure-data rebuild) reads the scale here."""
    if hasattr(spec, "calibration_scale"):
        return spec.calibration_scale()
    raw = spec if isinstance(spec, Mapping) else {}
    cal = raw.get("calibration")
    cal = cal if isinstance(cal, Mapping) else {}
    try:
        need = _scale_of(cal) != "linear" and isinstance(cal.get("attribute"), str)
    except CalibrationScaleError:
        need = False
    sc, bad = check_scale(cal, scale_cards(raw) if need else None)
    if bad:
        raise CalibrationScaleError("; ".join(bad))
    return sc


def scale_problems(cal, card) -> list[str]:
    """Pre-sign, one card in hand: check_scale's problems (a log scale for an attribute whose card range includes
    negative values, an invalid declared scale). A range from 0 up is accepted: exp(z) reaches as close to 0 as the
    calibration range asks (a seed share on a log range of tiny values is valid)."""
    return check_scale(cal, [("the engine", card)] if card is not None else None)[1]


def spec_scale_problems(s: "Spec") -> list[str]:
    """Every path that signs or runs a spec checks its calibration scale here (Spec.check_scale: the engine's card,
    or each signed candidate's while the choice is pending), whatever the calibration method (method none included:
    the emulator design still consumes the attribute and its range)."""
    cal = s.raw.get("calibration")
    if not isinstance(cal, Mapping) or ("scale" not in cal and not isinstance(cal.get("attribute"), str)):
        return []
    bad = s.check_scale()[1]
    dec = getattr(s, "engine_declared", None)
    if engine_is_auto(dec) and not engine_is_auto(s.raw.get("engine")):
        # an engine choice is written, but the signature covers engine: auto (the choice can be revisited): the
        # scale must also hold on every engine the signed declaration lets the selection pick
        bad += check_scale(cal, scale_cards(dict(s.raw, engine=dec)))[1]
    return list(dict.fromkeys(bad))


def attribute_notes(cal: dict, binding: dict, scale: str | None = None) -> list[dict]:
    """[{attribute, limitation, card_prior, run_prior}] for the unit attribute a calibration moves, when its card gives
    a limitation. `scale`: the spec's resolved scale (Spec.calibration_scale), which the run's prior is on; without
    it, the declared scale, else linear."""
    cal = cal if isinstance(cal, dict) else {}
    a = cal.get("attribute")
    if cal.get("method") in (None, "none") or not isinstance(a, str) or not a:
        return []
    rule = binding_rule(binding, a)
    if not isinstance(rule, dict) or not str(rule.get("limitation") or "").strip():
        return []
    run = (cal.get("priors") or {}).get(a) if isinstance(cal.get("priors"), dict) else None
    return [{"attribute": a, "limitation": rule["limitation"].strip(), "card_prior": rule.get("prior"),
             "run_prior": run if isinstance(run, dict) else None, "scale": str(scale or cal.get("scale") or "linear")}]


def _prior_text(p, scale: str = "linear", attr: str = "") -> str:
    """A declared prior in words: its distribution, its range or shape parameters, the scale it is declared on, and
    its source."""
    if not isinstance(p, dict):
        return "none declared"
    src = str(p.get("source") or "no source").strip()
    if p.get("range") is not None or "lo" in p:
        r = p.get("range") or [p.get("lo"), p.get("hi")]
        body = f"{p.get('dist', '?')} on [{r[0]}, {r[1]}]"
        if p.get("mode") is not None:
            body += f", mode {p['mode']}"
    else:
        shape = {k: v for k, v in p.items() if k not in ("dist", "source", "verified", "note", "basis", "units")}
        body = f"{p.get('dist', '?')} with " + ", ".join(f"{k}={v:g}" if isinstance(v, (int, float)) else f"{k}={v}"
                                                         for k, v in shape.items())
    if scale and scale != "linear":
        body += f" on the {scale} scale (the attribute is exp of the drawn value)" if scale == "log" else \
            f" on the {scale} scale"
    return f"{body} ({src})"


def attribute_notes_markdown(notes: list[dict], problems=None) -> list[str]:
    """Lines of a results section, one entry per note; [] when there is none. `problems`: errors reading the engine
    card's binding, stated in the section rather than dropped (fail closed)."""
    if not notes and not problems:
        return []
    L = ["## Simplifications in the calibrated inputs", ""]
    for n in notes:
        L += [f"- **{n['attribute']}**: {n['limitation']}",
              f"  - Engine card prior: {_prior_text(n['card_prior'])}.",
              f"  - This run's prior: {_prior_text(n['run_prior'], n.get('scale', 'linear'))}."]
    for p in problems or []:
        L.append(f"- The engine card's attribute binding could not be read, so its limitations are not listed: {p}")
    return L + [""]


def load_binding(engine) -> tuple[dict, list[str]]:
    """The engine card's attribute binding ({attribute or glob: required | {default, meaning}}): an engine's own
    `attribute_binding()` method (RequestedEngine / BuiltEngine), else `binding_path` on the engine, else
    attribute_binding.yaml beside the module of the engine's class or of its nearest base class that has one (the
    priors.yaml convention). No file: every attribute is required."""
    import inspect
    if not isinstance(engine, type) and callable(getattr(engine, "attribute_binding", None)):
        return dict(engine.attribute_binding()), []
    p = getattr(engine, "binding_path", None) if not isinstance(engine, type) else None
    if p:
        return _read_binding(p)
    for klass in (engine if isinstance(engine, type) else type(engine)).__mro__:
        try:
            cand = os.path.join(os.path.dirname(os.path.abspath(inspect.getfile(klass))), BINDING_FILE)
        except (TypeError, OSError):
            continue
        if os.path.exists(cand):
            return _read_binding(cand)
    return {}, []


def binding_for_ref(ref: str) -> dict:
    """The attribute binding of an engine class named 'module:Class' (imported through the guard, not
    constructed)."""
    return load_binding(engine_class(ref))[0]


def binding_rule(binding: dict, attr: str):
    import fnmatch
    if attr in binding:
        return binding[attr]
    for k, v in binding.items():
        if fnmatch.fnmatchcase(attr, k):
            return v
    return "required"


def attribute_problems(s: Spec, eng) -> list[str]:
    """No silent defaults: every unit attribute on the engine card is bound to a column or a value, moved by the
    calibration, or (when the card gives it a default) accepted by name with a reason; a bound column has a value in
    every row (engine_attribute_unbound, attribute_column_empty)."""
    card = eng.describe()
    binding, bad = load_binding(eng)
    du = s.raw["decision_units"]
    ea = du.get("engine_attributes") or {}
    acc = du.get("defaults_accepted") or {}
    if not isinstance(acc, dict):
        bad.append("engine_attribute_unbound: decision_units.defaults_accepted is {attribute: reason}")
        acc = {}
    cal = s.raw.get("calibration") or {}
    moved = {cal.get("attribute")} if cal.get("method") not in (None, "none") else set()
    if cal.get("method") == "posterior":     # a unit attribute with a declared prior is moved by the calibration
        moved |= set(cal.get("priors") or {})
    for a in card.unit_attributes:
        if a in ea or a in moved:
            continue
        rule = binding_rule(binding, a)
        if rule == "required":
            bad.append(f"engine_attribute_unbound: engine {card.name}: {a} (required"
                       + ("; it has no default to accept" if a in acc else "") + "): bind it in "
                       "decision_units.engine_attributes")
        elif not str(acc.get(a) or "").strip():
            bad.append(f"engine_attribute_unbound: engine {card.name}: {a} (default {rule['default']!r}: "
                       f"{rule['meaning']}): bind it, or accept the default by name in "
                       "decision_units.defaults_accepted with a reason")
    units = s.units()
    for a, m in ea.items():
        if not isinstance(m, dict):
            continue
        cols = [m["column"]] if "column" in m else (list(m["columns"].values()) if isinstance(m.get("columns"), dict)
                                                     else list(m.get("columns") or []))
        for c in cols:
            blank = sum(1 for u in units if str(u.get(c) if u.get(c) is not None else "").strip() == "")
            if blank:
                bad.append(f"attribute_column_empty: decision_units.engine_attributes.{a}: column {c} is "
                           + ("missing" if c not in units[0] else f"blank in {blank} of {len(units)} rows"))
    return bad


def frame_problems(s: Spec) -> list[str]:
    """A calibration target declares the population band it measures (its frame); when that band differs from the
    engine output it is compared with, the spec declares the mapping (target_frame_unmapped). v2d §3.4: no source
    is the truth."""
    cal = s.raw.get("calibration") or {}
    m = cal.get("method")
    if m in (None, "none") or not isinstance(cal, dict):
        return []
    if (cal.get("targets") or {}).get("kind") == "observations":
        # each observation row names the engine groups it measures and a declared source with its sampling frame
        # (toolkit/analysis/observed_targets.py checks both): the band is per row, never a whole-unit output
        return []
    if m == "emulator_inversion":
        compared, fr, where = cal.get("metric"), cal.get("target_frame"), "calibration.target_frame"
    else:
        t = cal.get("targets") or {}
        compared = t.get("outcome", (s.raw.get("objective") or {}).get("outcome"))
        if compared is None:            # an estimates spec: the latent quantity of the target's source
            src = [x for x in ((s.raw.get("observations") or {}).get("sources") or [])
                   if isinstance(x, dict) and x.get("id") == t.get("source")]
            compared = ((src[0].get("latent") or {}).get("quantity")) if src else None
        fr, where = t.get("frame"), "calibration.targets.frame"
    if not (isinstance(fr, dict) and str(fr.get("band") or "").strip() and str(fr.get("source") or "").strip()):
        return [f"target_frame_unmapped: {where}: declare the frame of the calibration target {{band, source}}: the "
                f"population band it measures, compared with engine output {compared}"]
    if fr["band"] == compared:
        return []
    mp = fr.get("mapping")
    if isinstance(mp, dict) and mp.get("to") == compared and str(mp.get("method") or "").strip() \
            and str(mp.get("source") or "").strip():
        return []
    return [f"target_frame_unmapped: {where}: the target measures {fr['band']} and is compared with engine output "
            f"{compared}; declare mapping {{to: {compared}, method, source}}, or compare it with the output for its band"]


def with_engine(s: Spec, ref, config: dict | None = None) -> Spec:
    """The spec with one candidate engine (a `'module:Class'` ref or an `Engine` instance such as `RequestedEngine`)
    in place of `auto` / pending `build` (the pre-sign check of each candidate)."""
    raw = copy.deepcopy(s.raw)
    if isinstance(ref, str):
        raw["engine"] = {"ref": ref, **({"config": dict(config)} if config else {})}
        out = Spec(raw, s.base, s.path)
    else:
        out = Spec(raw, s.base, s.path)
        out._engine = ref
        out._engine_ref = None
        out.engine_pending = False
    out.settings_path, out.derived = s.settings_path, s.derived
    return out


def _validate_pending(s: Spec) -> list[str]:
    """engine: auto before the choice: everything that does not need the engine card."""
    r = s.raw
    from toolkit.spec import cea as CEA, estimates as ES
    if ES.is_estimates(r):              # no allocation units: its own sections (v2d §2.3)
        return ES.validate_pending(s)
    req = tuple(k for k in REQUIRED if k != "constraints") if CEA.is_cea(r) else REQUIRED
    bad = [f"missing section {k}" for k in req if k not in r]
    if bad:
        return bad
    if r["schema_version"] != SCHEMA_VERSION:
        return [f"schema_version {r['schema_version']!r} is not {SCHEMA_VERSION}"]
    for k in ("question", "decision_maker"):
        if not str(r.get(k) or "").strip():
            bad.append(f"{k}: empty")
    try:
        units = s.units()
    except OSError as e:
        return bad + [f"decision_units: units table unreadable: {e}"]
    if not units:
        return bad + ["decision_units: the units table is empty"]
    cols = set(units[0])
    a = s.alloc_spec()
    bad += [f"options: {e}" for e in acosts.validate(a, cols)]
    bad += [f"constraints: {e}" for e in apackages.validate(a, units)]
    if not bad:
        try:
            apackages.feasibility(a, units)
        except ValueError as e:
            bad.append(f"constraints: {e}")
    if r["comparator"].get("package") not in s.packages():
        bad.append(f"comparator: package {r['comparator'].get('package')!r} is not a package")
    bad += [e for e in _validate_uncertainty(s, {}) if "the engine" not in e]
    from toolkit.spec import engine_parameters as PV
    bad += PV.shape_problems(s)           # the engine-dependent checks run per candidate and after the choice
    for dt in r["analysis"].get("decision_tests") or []:
        if not isinstance(dt, dict):
            bad.append("analysis.decision_tests: every entry must be a mapping")
            continue
        if str(dt.get("id") or "").startswith(("prior_range_", "fitted_range_", "scenario_excluded_",
                                                "anchor_range_")):       # W0-B review r1: also before the engine
            bad.append(f"decision test {dt.get('id')}: that id is one the harness generates; the spec cannot use it")
        if dt.get("type") not in DECISION_TEST_TYPES:
            bad.append(f"decision test {dt.get('id')}: type {dt.get('type')!r} not in {DECISION_TEST_TYPES}")
        if "with" in dt and dt.get("type") != "parameter_value":       # as after the engine choice (Codex r2)
            bad.append(f"decision test {dt.get('id')}: with (companion parameters) is only for a parameter_value "
                       "test")
    bad += _validate_deliverable(s)
    if CEA.has_cea(r):
        bad += CEA.problems(s, card=None, pk=s.packages())
    return bad


def _validate(s: Spec) -> list[str]:
    r = s.raw
    from toolkit.spec import cea as CEA, estimates as ES
    if ES.is_estimates(r):              # no allocation units: its own sections (v2d §2.3)
        bad = ES.validate_pending(s)
        if bad:
            return bad
        try:
            eng = s.engine()
        except Exception as e:  # noqa: BLE001
            return [f"engine {r['engine']} does not load: {type(e).__name__}: {e}"]
        if not isinstance(eng, Engine):
            return ["engine does not implement the Engine contract (describe, parameters, run)"]
        return ES.validate(s, eng)
    req = tuple(k for k in REQUIRED if k != "constraints") if CEA.is_cea(r) else REQUIRED
    bad = [f"missing section {k}" for k in req if k not in r]
    if bad:
        return bad
    if r["schema_version"] != SCHEMA_VERSION:
        return [f"schema_version {r['schema_version']!r} is not {SCHEMA_VERSION}"]
    for k in ("question", "decision_maker"):
        if not str(r.get(k) or "").strip():
            bad.append(f"{k}: empty")
    if not (_isnum(r["decision_date"]) or (isinstance(r["decision_date"], str) and r["decision_date"][:4].isdigit())):
        bad.append("decision_date: give a year or an ISO date")
    try:
        eng = s.engine()
    except Exception as e:  # noqa: BLE001
        return bad + [f"engine {r['engine']} does not load: {type(e).__name__}: {e}"]
    if not isinstance(eng, Engine):
        return bad + ["engine does not implement the Engine contract (describe, parameters, run)"]
    card = eng.describe()
    pnames = {p.name: p for p in eng.parameters()}
    from toolkit.spec import engine_parameters as PV
    bad += PV.problems(s, eng)            # before the checks that return early: a candidate engine shows them too
    try:
        units = s.units()
    except OSError as e:
        return bad + [f"decision_units: units table unreadable: {e}"]
    if not units:
        return bad + ["decision_units: the units table is empty"]
    cols = set(units[0])
    du = r["decision_units"]
    need = {du.get("id"), du.get("label"), du.get("region")}
    for k, pm in (du.get("populations") or {}).items():
        if not isinstance(pm, dict) or "column" not in pm or not pm.get("age_groups"):
            bad.append(f"decision_units.populations.{k}: give column and age_groups")
            continue
        need.add(pm["column"])
        bad += [f"decision_units.populations.{k}: the engine has no age group {g}" for g in pm["age_groups"]
                if g not in card.age_groups]
    if not du.get("populations"):
        bad.append("decision_units.populations: at least one population group")
    for attr, m in (du.get("engine_attributes") or {}).items():
        if attr not in card.unit_attributes:
            bad.append(f"decision_units.engine_attributes: {attr} is not declared by engine {card.name}")
        if not isinstance(m, dict):
            bad.append(f"decision_units.engine_attributes.{attr}: must be a mapping")
        elif "column" in m:
            need.add(m["column"])
            if "empty" in m and ("split" not in m or not isinstance(m["empty"], str) or not m["empty"].strip()
                                 or any(isinstance(_num(x), float) for x in [m["empty"]]
                                        + (m["empty"].split(m["split"]) if isinstance(m["empty"], str) else []))):
                # Codex r1 P1-4 / P2-6: a non-blank, non-numeric string, so no reader can take the token for data
                bad.append(f"decision_units.engine_attributes.{attr}: `empty` (the token of an empty list) needs "
                           "`split` and a non-blank, non-numeric string (e.g. none)")
        elif "columns" in m:
            need |= set(m["columns"].values() if isinstance(m["columns"], dict) else m["columns"])
        elif "value" not in m:
            bad.append(f"decision_units.engine_attributes.{attr}: give column, columns or value")
    ids = [str(u.get(du.get("id"), "")) for u in units]
    if len(set(ids)) != len(ids):
        bad.append("decision_units: unit ids are not unique")
    # horizon and objective
    h, o = r["horizon"], r["objective"]
    if not (isinstance(h, dict) and _isnum(h.get("start")) and _isnum(h.get("stop")) and h["start"] < h["stop"]):
        bad.append("horizon: start < stop (numbers)")
    win = o.get("horizon")
    if not (isinstance(win, list) and len(win) == 2 and all(_isnum(x) for x in win) and win[0] < win[1]):
        bad.append("objective.horizon: [t0, t1] with t0 < t1")
    elif isinstance(h, dict) and _isnum(h.get("start")) and not (h["start"] <= win[0] and win[1] <= h["stop"]):
        bad.append("objective.horizon: the decision window must lie inside the simulated horizon")
    if o.get("delivery") is not None:
        dl = o["delivery"]
        if not (isinstance(dl, list) and len(dl) == 2 and all(_isnum(x) for x in dl) and dl[0] < dl[1]):
            bad.append("objective.delivery: [t0, t1] with t0 < t1")
        elif isinstance(win, list) and len(win) == 2 and all(_isnum(x) for x in win) and not (
                win[0] <= dl[0] and dl[1] <= win[1]):
            bad.append("objective.delivery: the delivery period must lie inside the decision window objective.horizon")
        if not getattr(eng, "supports_delivery", False):
            bad.append(f"objective.delivery: engine {card.name} does not honour a delivery period (it would deliver "
                       "and count costs over the whole decision window); remove the field or use an engine that "
                       "declares supports_delivery")
    outs = card.outputs.get("per_capita", [])
    if o.get("outcome") not in outs:
        bad.append(f"objective.outcome: {o.get('outcome')!r} is not an engine per-capita output {outs}")
    if o.get("sense", "avert") != "avert":
        bad.append("objective.sense: only `avert` (benefit = comparator outcome - package outcome) is supported")
    disc = o.get("discounting", "none")
    if disc != "none" and not (isinstance(disc, dict) and _isnum(disc.get("rate")) and 0 <= disc["rate"] < 1
                               and _isnum(disc.get("base_year"))):
        bad.append("objective.discounting: `none` or {rate: 0 <= r < 1, base_year}")
    bad += [f"outcomes_reported: {x!r} is not an engine per-capita output" for x in r.get("outcomes_reported") or []
            if x not in outs]
    # options and constraints (toolkit/alloc)
    opts = r["options"]
    if not isinstance(opts, dict) or not isinstance(opts.get("components"), dict):
        return bad + ["options: components are required"]
    for k, col in (opts.get("populations") or {}).items():
        need.add(col)
    need.discard(None)
    bad += [f"decision_units: the units table has no column {c}" for c in sorted(need - cols)]
    if bad:
        return bad
    if callable(getattr(eng, "validate_unit_values", None)):
        bad += eng.validate_unit_values(units, du.get("engine_attributes") or {})
        if bad:
            return bad
    a = s.alloc_spec()
    struct_bad = [f"options: {e}" for e in acosts.validate(a, cols)]
    struct_bad += [f"constraints: {e}" for e in apackages.validate(a, units)]
    bad += struct_bad
    c = r.get("constraints") or {}
    if not CEA.is_cea(r):
        if not _isnum(c.get("envelope_usd")):
            raw_env = c.get("envelope_usd")
            hint = " (YAML 1.1 parses '1.0e6' as a string; write an integer or '1.0e+6')" if isinstance(raw_env, str) else ""
            bad.append(f"constraints.envelope_usd: a number{hint}")
        elif s.budget() <= 0:
            bad.append("constraints: envelope_usd must exceed the fixed costs")
        if "envelope_exact" in c and not isinstance(c.get("envelope_exact"), bool):
            bad.append("constraints.envelope_exact: true or false")
    for g in c.get("group_rules") or []:
        if not isinstance(g, dict) or not g.get("id"):
            bad.append("constraints.group_rules: each rule needs an id")
    if struct_bad or any(not isinstance(g, dict) for g in c.get("group_rules") or []):
        return bad
    try:
        apackages.feasibility(a, units)
    except ValueError as e:
        bad.append(f"constraints: {e}")
    pk = s.packages()
    emap = opts.get("engine_map") or {}
    for comp in opts["components"]:
        if comp not in emap:
            bad.append(f"options.engine_map: component {comp} has no engine mapping")
            continue
        for k, v in emap[comp].items():
            if k not in card.components or v not in card.components[k]:
                bad.append(f"options.engine_map.{comp}: {k}={v!r} is not an engine component value")
    for name, cs in pk.items():
        keys = [k for comp in cs for k in (emap.get(comp) or {})]
        if len(keys) != len(set(keys)):
            bad.append(f"options: package {name}: two components map to the same engine component")
    if r["comparator"].get("package") not in pk:
        bad.append(f"comparator: package {r['comparator'].get('package')!r} is not a package")
    an = r["analysis"]
    # F6 S2-2: cost_drivers_when names drivers an engine produces only under a parameter setting
    when = card.outputs.get("cost_drivers_when") or {}
    drivers = list(card.outputs.get("cost_drivers", []))
    drivers += [d for d in when if d not in drivers]
    if when:
        try:
            from toolkit.spec import priors as PRI
            dr = {k: (q.lo, q.hi) for k, q in PRI.drawn(s).items()}
        except Exception as ex:  # noqa: BLE001 -- fail closed: the draws cannot be read, so the domain is unknown
            dr, bad = {}, bad + [f"analysis.cost_outputs: the drawn priors could not be read to check conditional "
                                 f"cost drivers ({type(ex).__name__}: {ex})"]
        dom = parameter_domain(r, {n: p.value for n, p in pnames.items()}, dr)
        for k, m in (an.get("cost_outputs") or {}).items():
            bad += conditional_driver_problems(k, (m or {}).get("cost_driver"), when, dom,
                                               {n: q.kind for n, q in pnames.items()})
    cost_outs = an.get("cost_outputs") or {}
    for it in opts.get("cost_items") or []:
        if it.get("basis") == "per_outcome" and it.get("outcome") not in cost_outs:
            bad.append(f"analysis.cost_outputs: cost item {it.get('id')} uses outcome {it.get('outcome')!r}; map it to "
                       "an engine cost driver")
    for k, m in cost_outs.items():
        if (m or {}).get("cost_driver") not in drivers:
            bad.append(f"analysis.cost_outputs.{k}: {m!r} is not an engine cost driver {drivers}")
    bad += _validate_calibration(s, card, pnames, cols)
    bad += _validate_uncertainty(s, pnames)
    bad += unit_rates_problems(s, eng)
    from toolkit.spec import priors as PRI
    bad += PRI.problems(s)
    bad += _validate_analysis(s, card, pnames, cols, pk)
    bad += _validate_deliverable(s)
    if CEA.has_cea(r):
        bad += CEA.problems(s, card=card, pk=pk)
    from toolkit.analysis import burden as BV
    bad += BV.load_references(s)[1]
    return bad


def presentation_refusal(raw: dict) -> list[str]:
    """Display labels are never part of the signed spec (owner, 2026-11-02): a presentation block is refused on
    every validation path."""
    if isinstance(raw, dict) and "presentation" in raw:
        return ["presentation: display labels are not part of the signed spec; put them in presentation.yaml beside "
                "the spec (unsigned, read only by the figures) or pass --labels to the figure renderer"]
    return []


PRESENTATION_LABELS = ("components", "parameters", "outcomes", "metrics")
PRESENTATION_TITLES = ("regions_title", "units_title")


def labels_problems(raw: dict, labels: dict, pnames=None, card=None) -> list[str]:
    """Problems with a labels mapping from the unsigned presentation file (docs/design/v2-figures.md §3): strings
    only, and every id must be a component, engine parameter, reported outcome or engine metric of this spec."""
    if not isinstance(labels, dict):
        return ["labels: a mapping"]
    bad = [f"labels.{k}: unknown key (known: {', '.join(PRESENTATION_LABELS + PRESENTATION_TITLES)})"
           for k in labels if k not in PRESENTATION_LABELS + PRESENTATION_TITLES]
    outs = (getattr(card, "outputs", None) if card is not None and not isinstance(card, dict)
            else (card or {}).get("outputs")) or {}
    known = {"components": set((raw.get("options") or {}).get("components") or {}),
             "parameters": set(pnames or ()),
             "outcomes": {(raw.get("objective") or {}).get("outcome")} | set(raw.get("outcomes_reported") or []),
             "metrics": set(outs.get("windows", []) or []) | set((outs.get("series") or {}))}
    for k in PRESENTATION_LABELS:
        m = labels.get(k)
        if m is None:
            continue
        if not isinstance(m, dict) or not all(isinstance(v, str) and v.strip() for v in m.values()):
            bad.append(f"labels.{k}: a mapping of id to a non-empty display name")
            continue
        check = (k != "parameters" or pnames) and (k != "metrics" or known["metrics"])
        unknown = [i for i in m if i not in known[k]] if check else []
        if unknown:
            bad.append(f"labels.{k}: {unknown} are not {k} of this spec")
    for k in PRESENTATION_TITLES:
        if k in labels and not (isinstance(labels[k], str) and labels[k].strip()):
            bad.append(f"labels.{k}: a non-empty string")
    return bad


def parameter_domain(raw: dict, defaults: dict, drawn: dict) -> dict:
    """{parameter: [(where, [values])]}: every value the analysis can give an engine parameter: the point value (the
    spec's engine_parameters value, else the engine default), a range (engine_parameters range, uncertainty.priors
    range, or a drawn prior's (lo, hi) from `drawn`), and each parameter_value decision test's values."""
    from toolkit.spec import engine_parameters as PV
    ep = {n: e for n, e in ((raw or {}).get(PV.FIELD) or {}).items() if isinstance(e, dict)}
    dom = {}
    for n, v in defaults.items():
        dom[n] = [("point value", [ep[n].get("value") if n in ep else v])]
    for n, e in ep.items():
        if e.get("range") is not None:
            dom.setdefault(n, []).append((f"{PV.FIELD}.{n}.range", list(e["range"])))
    for n, q in (((raw or {}).get("uncertainty") or {}).get("priors") or {}).items():
        if isinstance(q, dict) and q.get("range") is not None:
            dom.setdefault(n, []).append((f"uncertainty.priors.{n}.range", list(q["range"])))
    for n, (lo, hi) in (drawn or {}).items():
        dom.setdefault(n, []).append((f"the drawn prior on {n}", [lo, hi]))
    for dt in (((raw or {}).get("analysis") or {}).get("decision_tests") or []):
        if not isinstance(dt, dict):
            continue
        if dt.get("type") == "parameter_value" and dt.get("parameter") in dom:
            dom[dt["parameter"]].append((f"decision test {dt.get('id')}", list(dt.get("values") or [])))
        for n, v in PV.companions(dt).items():        # a companion value (`with`) is a value the run evaluates
            if n in dom:
                dom[n].append((f"decision test {dt.get('id')} (with)", [v]))
    return dom


def conditional_driver_problems(name, driver, when: dict, domain: dict, kinds: dict | None = None) -> list[str]:
    """F6 S2-2 (Codex r1 P2-1): a cost driver the engine card lists under outputs.cost_drivers_when
    ({driver: {parameter: value}}) is produced only when each parameter has its stated value, so every value the
    analysis can give it (parameter_domain: the point value, any range, every decision-test value) must equal that
    value. A range is refused unless both ends equal it (a continuous range reaches fractional switches).
    Codex r2 P2-2: with `kinds` ({parameter: kind}), a predicate may name only a `fixed` engine parameter; fitted,
    calibrated and scenario parameters are moved by the analysis (draws, emulator designs, tests), so it is refused."""
    out = []
    for p, v in ((when or {}).get(driver) or {}).items():
        if kinds is not None and kinds.get(p) != "fixed":
            out.append(f"analysis.cost_outputs.{name}: engine cost driver {driver} depends on {p}, a "
                       f"{kinds.get(p, 'unknown')} parameter; a conditional driver's predicate must name a fixed "
                       "(structural) engine parameter")
            continue
        for where, vals in domain.get(p) or [("point value", [None])]:
            try:
                same = bool(vals) and all(float(x) == float(v) for x in vals)
            except (TypeError, ValueError):
                same = False
            if not same:
                out.append(f"analysis.cost_outputs.{name}: engine cost driver {driver} is produced only when {p} = "
                           f"{v:g}, but {where} gives {p} {vals!r}: set {p} to {v:g} in engine_parameters, with no "
                           "range, prior or decision test that moves it")
    return out


def _validate_calibration(s, card, pnames, cols) -> list[str]:
    cal = s.raw["calibration"]
    m = cal.get("method") if isinstance(cal, dict) else None
    if m not in CALIBRATION_METHODS:
        return [f"calibration.method: one of {CALIBRATION_METHODS}"]
    # the scale on every method and path (method none and observed targets included): spec_scale_problems, run
    # by validate() as the shared precondition calibration_scale (toolkit/spec/preconditions.py)
    return _validate_calibration_method(s, card, pnames, cols, cal, m)


def _validate_calibration_method(s, card, pnames, cols, cal, m) -> list[str]:
    from toolkit.analysis import observed_targets as OT
    if OT.applies(s):          # posterior on observations of group states and outcomes, through declared sources
        return OT.problems(s, card, pnames)
    bad = [f"calibration.{k}: required for {m}" for k in CALIBRATION_FIELDS[m] if k not in cal]
    if bad or m == "none":
        return bad
    if cal["attribute"] not in card.unit_attributes:
        bad.append(f"calibration.attribute: {cal['attribute']} is not declared by the engine")
    rg = cal["range"]
    if not (isinstance(rg, list) and len(rg) == 2 and all(_isnum(x) for x in rg) and rg[0] < rg[1]):
        bad.append("calibration.range: [lo, hi] on the attribute's scale (calibration.scale; when undeclared, what "
                   "the engine card's range implies, else linear)")
    if m == "emulator_inversion":
        if cal["target_column"] not in cols:
            bad.append(f"calibration.target_column: the units table has no column {cal['target_column']}")
        if cal["metric"] not in card.outputs.get("windows", []):
            bad.append(f"calibration.metric: {cal['metric']} is not an engine window metric")
        if s.raw["analysis"].get("evaluation") != "emulator":
            bad.append("calibration: emulator_inversion needs analysis.evaluation: emulator")
        bad += [f"calibration.covariates: no column {c}" for c in cal.get("covariates") or [] if c not in cols]
    else:
        for p in cal["parameters"]:
            if p not in pnames:
                bad.append(f"calibration.parameters: {p} is not an engine parameter")
            elif pnames[p].kind != "calibrated":
                bad.append(f"calibration.parameters: {p} is of kind {pnames[p].kind}, not calibrated (the engine card "
                           "says which parameters a run may fit)")
        t = cal["targets"]
        miss = [k for k in TARGET_FIELDS if k not in (t or {})]
        if miss:
            return bad + [f"calibration.targets: give {', '.join(miss)}"]
        try:
            with open(s.resolve(t["csv"]), newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        except OSError as e:
            return bad + [f"calibration.targets: table unreadable: {e}"]
        if not rows:
            return bad + ["calibration.targets: the table is empty"]
        tc = set(rows[0])
        bad += [f"calibration.targets.{k}: no column {t[k]}" for k in TARGET_FIELDS[1:] if t[k] not in tc]
        if "stop" in t:
            if t["stop"] not in tc:
                bad.append(f"calibration.targets.stop: no column {t['stop']}")
        elif not (_isnum(t.get("period")) and t["period"] > 0):
            bad.append("calibration.targets: give `stop` (a column) or `period` (years per row)")
        if bad:
            return bad
        ids = set(s.unit_ids())
        pk = s.packages()
        pmap = t.get("package_map") or {}
        unk_u = sorted({row[t["unit"]] for row in rows} - ids)
        unk_p = sorted({pmap.get(row[t["package"]], row[t["package"]]) for row in rows} - set(pk))
        if unk_u:
            bad.append(f"calibration.targets: units not in decision_units: {unk_u[:5]}")
        if unk_p:
            bad.append(f"calibration.targets: packages not in options (map them with package_map): {unk_p[:5]}")
        if t.get("outcome", s.raw["objective"]["outcome"]) not in card.outputs.get("per_capita", []):
            bad.append("calibration.targets.outcome: not an engine per-capita output")
    if m == "posterior":
        bad += _validate_posterior(cal, s.raw["analysis"].get("evaluation"), s.raw["decision_units"])
    if cal.get("joint") is not None:       # joint calibration (toolkit/analysis/joint_calibration.py)
        from toolkit.analysis import joint_calibration as JC
        bad += JC.spec_problems(s)
    return bad


def _validate_posterior(cal, evaluation="direct", du=None) -> list[str]:
    """v2d §3.1: every quantity the posterior moves has a declared prior with its source (no silent defaults), and the
    count panel names its observation model."""
    from toolkit.analysis.calibration import Prior
    bad = []
    if evaluation != "direct":      # Codex r2-5: the posterior evaluates the engine itself
        bad.append("calibration.method posterior needs analysis.evaluation: direct (no emulator path yet)")
    pri = cal.get("priors")
    if not isinstance(pri, dict):
        return ["calibration.priors: a mapping {name: {dist, range, source}}"]
    for n in list(cal.get("parameters") or []) + [cal.get("attribute")]:
        d = pri.get(n)
        if d is None:
            bad.append(f"calibration.priors: {n} has no prior (no silent defaults)")
            continue
        try:
            Prior.from_decl(n, d)
        except ValueError as e:
            bad.append(f"calibration.priors.{n}: {e}")
        if not str((d or {}).get("source") or "").strip():
            bad.append(f"calibration.priors.{n}: give its source")
    bound = data_bound_attributes(du or {})
    for n in sorted(set(pri) & set(bound)):
        bad.append(f"prior_target_not_parameter: calibration.priors: {n} is a unit attribute bound to data column "
                   f"{bound[n]}, not an engine parameter: declare that column's uncertainty where it arises, as the "
                   "observation of a latent quantity through a source with a frame and a bias model (v2d §3.4)")
    extra = sorted(set(pri) - set(cal.get("parameters") or []) - {cal.get("attribute")} - set(bound))
    if extra:
        bad.append(f"calibration.priors: {extra} are not calibrated by this spec")
    ob = cal.get("observation") or {}
    if ob.get("model") not in ("poisson", "negbin"):
        bad.append("calibration.observation.model: poisson | negbin")
    elif ob["model"] == "negbin" and not (_isnum(ob.get("dispersion")) and ob["dispersion"] > 0):
        bad.append("calibration.observation.dispersion: a positive number for negbin")
    return bad


def data_bound_attributes(du: dict) -> dict:
    """{unit attribute: column(s)} for the attributes the spec binds to data columns (data, not engine parameters)."""
    out = {}
    for a, m in ((du or {}).get("engine_attributes") or {}).items():
        if isinstance(m, dict) and ("column" in m or "columns" in m):
            c = m.get("column") or m.get("columns")
            out[a] = c if isinstance(c, str) else ", ".join(c.values() if isinstance(c, dict) else c)
    return out


def unit_rates_problems(s, eng) -> list[str]:
    """hb3 fix 5: the engine's per-unit sampling (`unit_sampling()`, on its card) is drawn by the source unit_rates or
    excluded with a reason, so an interval never silently holds measured unit rates at their point values; the source
    is refused for an engine that declares none, or off direct evaluation (an emulator does not take drawn values)."""
    u = s.raw["uncertainty"]
    if not isinstance(u, dict):          # _validate_uncertainty reports it
        return []
    inc = [x.get("kind") for x in u.get("sources") or [] if isinstance(x, dict)]
    exc = {x.get("kind"): x.get("reason") for x in u.get("excluded") or [] if isinstance(x, dict) and "kind" in x}
    fn = getattr(eng, "unit_sampling", None)
    dec = fn() if callable(fn) else None
    bad = []
    if UNIT_RATES in inc and UNIT_RATES in exc:
        bad.append(f"uncertainty: {UNIT_RATES} is both a source and excluded")
    if UNIT_RATES in inc and dec is None:
        bad.append(f"uncertainty: source {UNIT_RATES} is declared but the engine declares no per-unit sampling "
                   "(for the package-rate engine: engine.config.baseline.sampling)")
    if dec is not None and UNIT_RATES not in inc and not str(exc.get(UNIT_RATES) or "").strip():
        bad.append(f"uncertainty: source {UNIT_RATES} (the engine's per-unit sampling of {dec['attribute']} "
                   f"{dec['outcomes']}) is neither declared in `sources` nor excluded with a reason")
    if dec is not None:      # Codex r1 P2-5 of #377: every unit's count for every sampled outcome, before sign-off
        ev = dec.get("events_attribute")
        try:
            for row in s.units():
                a = s.unit_attributes(row).get(ev)
                miss = [o for o in dec["outcomes"] if not (isinstance(a, dict) and _isnum(a.get(o)) and a[o] > 0)]
                if miss:
                    bad.append(f"decision_units.engine_attributes.{ev}: unit {row.get(s.uid)} has no count > 0 for "
                               f"{miss} (the per-unit sampling draws each of {dec['outcomes']} from its count)")
                    break
        except (OSError, KeyError, TypeError, ValueError) as e:
            bad.append(f"decision_units.engine_attributes.{ev}: the counts cannot be read ({type(e).__name__}: {e})")
    return bad


def _validate_uncertainty(s, pnames) -> list[str]:
    u = s.raw["uncertainty"]
    if not isinstance(u, dict):
        return ["uncertainty: a mapping"]
    bad = []
    inc = [x.get("kind") for x in u.get("sources") or [] if isinstance(x, dict)]
    exc = {x.get("kind"): x.get("reason") for x in u.get("excluded") or [] if isinstance(x, dict) and "parameter" not in x}
    for k in inc + list(exc):
        if k not in UNCERTAINTY_KINDS and k not in OPTIONAL_KINDS and k not in CONDITIONAL_KINDS:
            bad.append(f"uncertainty: unknown source kind {k!r} (known: "
                       f"{', '.join(list(UNCERTAINTY_KINDS) + list(OPTIONAL_KINDS) + list(CONDITIONAL_KINDS))})")
    if "burden_anchor" in inc and "burden_anchor" in exc:       # F6 S2-5d review r3
        bad.append("uncertainty: burden_anchor is both a source and excluded")
    if "burden_anchor" in inc and not ((s.raw.get("validation") or {}).get("burden") if isinstance(
            s.raw.get("validation"), dict) else None):
        bad.append("uncertainty: source burden_anchor needs validation.burden anchor rows")
    for x in u.get("excluded") or []:
        if isinstance(x, dict) and "parameter" in x and "kind" in x:
            bad.append("uncertainty.excluded: an entry names a kind or a parameter, not both")
    for k, why in exc.items():
        if k in OPTIONAL_KINDS and not str(why or "").strip():
            bad.append(f"uncertainty: the exclusion of {k} needs a reason")
        if k in OPTIONAL_KINDS and k in inc:
            bad.append(f"uncertainty: {k} is both a source and excluded")
    for k in UNCERTAINTY_KINDS:
        if k not in inc and not str(exc.get(k) or "").strip():
            bad.append(f"uncertainty: source {k} ({UNCERTAINTY_KINDS[k]}) is neither declared in `sources` nor "
                       "excluded with a reason (spike lesson 6: an interval must say what it covers)")
        if k in inc and k in exc:
            bad.append(f"uncertainty: {k} is both a source and excluded")
    if not inc:
        bad.append("uncertainty.sources: at least one declared source")
    if not (isinstance(u.get("draws"), int) and u["draws"] >= 2):
        bad.append("uncertainty.draws: an integer >= 2")
    lvl = u.get("level", 0.95)
    if not (_isnum(lvl) and 0.5 <= lvl < 1):
        bad.append("uncertainty.level: in [0.5, 1)")
    if "affordability" in u:      # F6 S1-1: the share of draws in which the fixed plan may exceed the budget
        af = u["affordability"]
        if not isinstance(af, dict) or "max_share_over_budget" not in af or not set(af) <= {"max_share_over_budget",
                                                                                            "enforce"}:
            bad.append("uncertainty.affordability: a mapping with max_share_over_budget and optionally enforce")
        elif not (_isnum(af["max_share_over_budget"]) and 0 <= af["max_share_over_budget"] < 1):
            bad.append("uncertainty.affordability.max_share_over_budget: in [0, 1)")
        elif not isinstance(af.get("enforce", False), bool):
            bad.append("uncertainty.affordability.enforce: true or false")
    items = {it.get("id") for it in s.raw["options"].get("cost_items") or []}
    for src in u.get("sources") or []:
        if src.get("kind") == "costs":
            rng = src.get("items") or {}
            if not rng:
                bad.append("uncertainty.sources costs: give items {cost_item_id: [lo_multiplier, hi_multiplier]}")
            for i, v in rng.items():
                if i not in items:
                    bad.append(f"uncertainty.sources costs: unknown cost item {i}")
                elif not (isinstance(v, list) and len(v) == 2 and all(_isnum(x) and x > 0 for x in v) and v[0] <= v[1]):
                    bad.append(f"uncertainty.sources costs.{i}: [lo, hi] positive multipliers")
        if src.get("kind") == "engine_parameters":
            fitted = [p for p in pnames.values() if p.kind == "fitted"]
            if not fitted:
                bad.append("uncertainty.sources engine_parameters: the engine has no fitted parameters with ranges")
        if src.get("kind") == "emulator" and s.raw["analysis"].get("evaluation") != "emulator":
            bad.append("uncertainty.sources emulator: there is no emulator (analysis.evaluation is direct); exclude it")
        if src.get("kind") == UNIT_RATES and s.raw["analysis"].get("evaluation") != "direct":   # also when pending
            bad.append(f"uncertainty.sources {UNIT_RATES}: needs analysis.evaluation: direct (an emulator does not "
                       "take drawn unit values)")
        if src.get("kind") == "calibration" and s.raw["calibration"].get("method") == "none":
            bad.append("uncertainty.sources calibration: calibration.method is none")
    if (s.raw.get("constraints") or {}).get("envelope_exact") is True:
        if "costs" in inc:
            bad.append("constraints.envelope_exact: cannot be combined with uncertainty source costs "
                       "(an exact fixed-supply envelope requires fixed unit costs)")
        if isinstance(u.get("affordability"), dict) and u["affordability"].get("enforce"):
            bad.append("constraints.envelope_exact: cannot be combined with uncertainty.affordability.enforce "
                       "(a reserve would hold back part of an exact envelope)")
        if any(isinstance(it, dict) and it.get("basis") == "per_outcome"
               for it in (s.raw.get("options") or {}).get("cost_items") or []):
            bad.append("constraints.envelope_exact: cannot be combined with per_outcome cost items "
                       "(package costs must stay fixed across draws)")
        if ((s.raw.get("analysis") or {}).get("sensitivity") or {}).get("cost_items"):
            bad.append("constraints.envelope_exact: cannot be combined with analysis.sensitivity.cost_items "
                       "(unit-cost multipliers can make an exact supply infeasible)")
    return bad


# Owner R3 (external review point a): the analysis keys the code reads; any other is refused, so a misspelt or
# unsupported signed field cannot silently do nothing
ANALYSIS_KEYS = {"evaluation", "emulator", "direct", "cost_outputs", "diagnostics", "stability", "sensitivity",
                 "share_threshold", "regret_threshold", "dollars_threshold", "decision_tests", "plan_variants",
                 "value_of_information", "workers", "outcome_adjustments"}


def _validate_analysis(s, card, pnames, cols, pk) -> list[str]:
    an = s.raw["analysis"]
    bad = [f"analysis: unknown key {k} (known: {', '.join(sorted(ANALYSIS_KEYS))})" for k in sorted(set(an) - ANALYSIS_KEYS)]
    if an.get("evaluation") not in EVALUATIONS:
        return [f"analysis.evaluation: one of {EVALUATIONS}"]
    if an["evaluation"] == "emulator":
        em = an.get("emulator") or {}
        if not (isinstance(em.get("points"), int) and em["points"] >= 8):
            bad.append("analysis.emulator.points: an integer >= 8")
        from toolkit.analysis import features as FT      # F6 S2-4: columns, or encoded engine unit attributes
        bad += FT.problems(s, cols)
    from toolkit.analysis import variants as _V      # F6 S1-6: operational plan variants
    bad += _V.validate(s.raw, cols, s.units(), pk, s.uid)
    from toolkit.analysis import value_of_information as _VOI      # F6: analysis.value_of_information
    bad += _VOI.validate(s.raw)
    for name, d in (an.get("diagnostics") or {}).items():
        if (d or {}).get("metric") not in card.outputs.get("windows", []):
            bad.append(f"analysis.diagnostics.{name}: {d!r} is not an engine window metric")
    st = an.get("stability") or {}
    if st:
        if st.get("holdout_group") not in cols:
            bad.append(f"analysis.stability.holdout_group: no column {st.get('holdout_group')}")
        bad += [f"analysis.stability.covariates: no column {c}" for c in st.get("covariates") or [] if c not in cols]
    sens = an.get("sensitivity") or {}
    sp = sens.get("parameters")
    if sp not in (None, "fitted") and not (isinstance(sp, list) and all(p in pnames for p in sp)):
        bad.append("analysis.sensitivity.parameters: `fitted` or a list of engine parameter names")
    comps = set(s.raw["options"]["components"])
    from toolkit.analysis import outcome_adjust as _OA     # F6 run 4 R3: base outcome adjustments (trial ratios)
    try:
        _pk = s.packages()
    except Exception:  # noqa: BLE001 -- package problems are reported elsewhere
        _pk = {}
    bad += _OA.problems(s.raw, _pk, comps, (s.raw.get("comparator") or {}).get("package"))
    seen = set()
    for k in ("share_threshold", "regret_threshold", "dollars_threshold"):     # F6 S1-5: toolkit/analysis/moves.py
        thr = an.get(k, 0.10)
        if not (_isnum(thr) and 0 <= thr <= 1):
            bad.append(f"analysis.{k}: a fraction in [0, 1]")
    rules = {g.get("id") for g in (s.raw.get("constraints") or {}).get("group_rules") or []}
    cost_ids = {it.get("id") for it in s.raw["options"].get("cost_items") or []}
    for dt in an.get("decision_tests") or []:
        i = dt.get("id")
        if not i or i in seen:
            bad.append(f"analysis.decision_tests: id {i!r} missing or duplicated")
        if str(i or "").startswith(("prior_range_", "fitted_range_", "scenario_excluded_", "anchor_range_")) or \
                dt.get("type") in ("prior_range", "fitted_range", "scenario_excluded", "anchor_range"):
            bad.append(f"decision test {i}: prior_range, fitted_range, scenario_excluded and anchor_range tests are "
                       "generated by the harness (toolkit/analysis/decision_tests.py); the spec cannot declare one")
            continue
        seen.add(i)
        t = dt.get("type")
        if t not in DECISION_TEST_TYPES:
            bad.append(f"decision test {i}: type {t!r} not in {DECISION_TEST_TYPES}")
            continue
        if not str(dt.get("text") or "").strip():
            bad.append(f"decision test {i}: text (the question it answers) is empty")
        if "engine_arm_or" in dt:
            bad.append(f"decision test {i}: engine_arm_or cannot be supplied; arm ratios come from the engine's "
                       "verified benchmark results (Codex P1-13)")
        if "with" in dt and t != "parameter_value":      # never ignored on a test that would not apply it
            bad.append(f"decision test {i}: with (companion parameters) is only for a parameter_value test")
        if t == "interaction_rescale":
            if not dt.get("benchmark"):
                bad.append(f"decision test {i}: benchmark (the engine benchmark whose arms set the interaction)")
            for c in list(dt.get("components") or []) + list(dt.get("with_any") or []):
                if c not in comps:
                    bad.append(f"decision test {i}: unknown component {c}")
            if not dt.get("values"):
                bad.append(f"decision test {i}: values (the interaction values to test)")
        elif t == "parameter_value":
            if dt.get("parameter") not in pnames:
                bad.append(f"decision test {i}: {dt.get('parameter')!r} is not an engine parameter")
            if not dt.get("values"):
                bad.append(f"decision test {i}: values")
            if "with" in dt:        # companion engine parameters held in every case of the test (owner, 20 Nov)
                w = dt["with"]
                if not isinstance(w, dict) or not w:
                    bad.append(f"decision test {i}: with must be a mapping {{engine parameter: value}}")
                else:
                    bad += [f"decision test {i}: with: {k!r} is not an engine parameter" for k in w if k not in pnames]
                    bad += [f"decision test {i}: with: {k} must be a finite number" for k, v in w.items()
                            if not _isnum(v) or not math.isfinite(v)]
                    if dt.get("parameter") in w:
                        bad.append(f"decision test {i}: with cannot hold the test's own parameter "
                                   f"{dt.get('parameter')}")
        elif t == "group_rule":
            if dt.get("rule") not in rules and not dt.get("add"):
                bad.append(f"decision test {i}: rule {dt.get('rule')!r} is not a constraints.group_rules id (or give add:)")
        elif t == "envelope":
            if not dt.get("values") or not all(_isnum(v) for v in dt["values"]):
                bad.append(f"decision test {i}: values (envelopes in USD)")
        elif t == "unit_attribute":      # the runner's own planner (column, units, domain, identity), at sign-off
            from toolkit.analysis import decision_tests as DT
            emu = an.get("evaluation") == "emulator"      # #228 r1 P2-5: the design box the pre-run plan reads
            pl = DT.plan_unit_attribute(s, dt, card, an.get("evaluation"), DT.design_features_box(s) if emu else None)
            if pl["status"] != "runs":
                bad.append(f"decision test {i}: {pl['why']}")
        elif t == "calibration_target":      # F6 run 1, owner decision R1 (toolkit/analysis/decision_tests.py)
            bad += _calibration_target_problems(s, dt, i)
        elif t == "outcome_ratio":           # F6 run 2, owner decision DT13 (toolkit/analysis/decision_tests.py)
            orc = dt.get("outcome_ratio")
            if not isinstance(orc, dict) or set(orc) != {"component", "reference", "values"}:
                bad.append(f"decision test {i}: give outcome_ratio {{component, reference, values}}")
                continue
            for k in ("component", "reference"):
                if orc[k] not in comps:
                    bad.append(f"decision test {i}: outcome_ratio.{k}: unknown component {orc[k]!r}")
            if orc["component"] == orc["reference"]:
                bad.append(f"decision test {i}: outcome_ratio component and reference must differ")
            vs = orc["values"]
            from toolkit.analysis import outcome_adjust as OAm      # F6 run 4 R3: "engine" removes a base adjustment
            based = {str(a.get("component")) for a in OAm.config(s.raw) or [] if isinstance(a, dict)}
            bad += [f"decision test {i}: {e}" for e in OAm.dt_problems(orc, OAm.config(s.raw))]   # #251 review
            if isinstance(vs, list) and "engine" in vs and orc.get("component") not in based:
                bad.append(f"decision test {i}: outcome_ratio value \"engine\" needs a base outcome adjustment of "
                           f"{orc.get('component')} (analysis.outcome_adjustments)")
            nums = [v for v in vs if v != "engine"] if isinstance(vs, list) else None
            if not (isinstance(vs, list) and vs and all(_isnum(v) and 0 < v < float("inf") for v in nums)):
                bad.append(f"decision test {i}: outcome_ratio.values: positive ratios (the package's burden over its "
                           "counterpart's), or \"engine\"")
            elif "engine" not in vs and orc.get("component") not in based and all(abs(float(v) - 1.0) <= 1e-12
                                                                                    for v in vs):
                bad.append(f"decision test {i}: outcome_ratio values of 1 only change nothing (an identity)")
            elif orc["component"] in comps and orc["reference"] in comps:
                cpk = s.packages().get(s.raw["comparator"].get("package"), set())
                if orc["component"] in set(cpk):       # #248 P2-1 (Codex review of #245): benefits are measured from it
                    bad.append(f"decision test {i}: the comparator holds {orc['component']}: the test would change "
                               "the baseline every benefit is measured from")
                pk = s.packages()
                if not any(orc["component"] in set(cs) and orc["reference"] not in set(cs)
                           and any(set(c2) == (set(cs) - {orc["component"]}) | {orc["reference"]}
                                   for c2 in pk.values()) for cs in pk.values()):
                    bad.append(f"decision test {i}: no package with {orc['component']} has a counterpart with "
                               f"{orc['reference']} in its place")
        elif t == "boundary_engine":         # F6 run 4 R3 (RIG-002): the boundary check's engine tables, re-solved
            extra_k = sorted(set(dt) - {"id", "type", "text", "finding"})
            if extra_k:
                bad.append(f"decision test {i}: boundary_engine takes no parameters (got {extra_k})")
            if an.get("evaluation") != "emulator":
                bad.append(f"decision test {i}: boundary_engine needs the emulator path (analysis.evaluation emulator)")
        elif t == "burden_scale":            # F6 run 3, owner decision D-c (toolkit/analysis/decision_tests.py)
            bsc = dt.get("burden_scale")
            if not isinstance(bsc, dict) or set(bsc) != {"references", "shares"}:
                bad.append(f"decision test {i}: give burden_scale {{references, shares}}")
                continue
            sh = bsc["shares"]
            if not (isinstance(sh, list) and sh and all(_isnum(v) and 0 < v <= 1 for v in sh)):
                bad.append(f"decision test {i}: burden_scale.shares: numbers in (0, 1] (1 sets each group's burden to "
                           "its reference)")
            from toolkit.analysis import decision_tests as DTm
            bad += [f"decision test {i}: {e}" for e in DTm.burden_scale_refs(s, bsc)[1]]
        elif t == "cost_items":
            m = dt.get("multipliers")
            if not isinstance(m, dict) or not m:
                bad.append(f"decision test {i}: multipliers {{cost_item_id: multiplier}} (one price case: the listed "
                           "unit costs are multiplied together)")
                continue
            for it_id, v in m.items():
                if it_id not in cost_ids:
                    bad.append(f"decision test {i}: unknown cost item {it_id}")
                elif not (_isnum(v) and 0 < v < float("inf")):
                    bad.append(f"decision test {i}: multipliers.{it_id}: a positive multiplier")
    return bad


def _calibration_target_problems(s, dt, i) -> list[str]:
    """calibration_target {units: [ids], scale}: the listed units' calibration targets (calibration.target_column)
    multiplied by scale, their calibrated values redone, the plan re-solved. Fail closed: an emulator-inversion
    calibration, a non-empty list of known unit ids without repeats, and a positive scale other than 1 that keeps every
    listed target inside (0, 1)."""
    bad, ct = [], dt.get("calibration_target")
    if not isinstance(ct, dict):
        return [f"decision test {i}: give calibration_target {{units: [ids], scale}}"]
    cal = s.raw.get("calibration") or {}
    if cal.get("method") != "emulator_inversion":
        bad.append(f"decision test {i}: a calibration_target test needs calibration.method emulator_inversion")
    us, sc = ct.get("units"), ct.get("scale")
    if set(ct) != {"units", "scale"}:
        bad.append(f"decision test {i}: calibration_target holds exactly units and scale")
    if not (isinstance(us, list) and us and len({str(u) for u in us}) == len(us)):
        return bad + [f"decision test {i}: calibration_target.units: a non-empty list of unit ids, each once"]
    try:
        rows = {str(r[s.uid]): r for r in s.units()}
    except Exception as e:  # noqa: BLE001 — the units table is validated elsewhere; this test cannot be checked
        return bad + [f"decision test {i}: the units table cannot be read ({e})"]
    unknown = [str(u) for u in us if str(u) not in rows]
    if unknown:
        bad.append(f"decision test {i}: calibration_target.units not in the units table: {unknown[:5]}")
    if not (_isnum(sc) and sc > 0):
        return bad + [f"decision test {i}: calibration_target.scale: a positive number"]
    if abs(float(sc) - 1.0) <= 1e-12:
        return bad + [f"decision test {i}: a scale of 1 changes no target (an identity)"]
    tc = cal.get("target_column")
    for u in us:
        r = rows.get(str(u))
        try:
            v = float(r[tc]) * float(sc) if r is not None else None
        except (TypeError, ValueError, KeyError):
            bad.append(f"decision test {i}: unit {u}'s calibration target {tc!r} is not a number")
            continue
        if v is not None and not 0.0 < v < 1.0:
            bad.append(f"decision test {i}: unit {u}'s scaled target {v:g} is not inside (0, 1)")
    return bad


def _validate_deliverable(s) -> list[str]:
    d = s.raw["deliverable"]
    if not isinstance(d, dict):
        return ["deliverable: a mapping (kind, allocation_file, columns, headline)"]
    bad = []
    if d.get("kind") not in DELIVERABLE_KINDS:
        bad.append(f"deliverable.kind: one of {DELIVERABLE_KINDS}")
    if d.get("kind") == "cost_effectiveness":
        if d.get("plan") is not None:
            bad.append("deliverable.plan: applies to a package_per_unit deliverable")
        return bad
    if not str(d.get("allocation_file") or "").endswith(".csv"):
        bad.append("deliverable.allocation_file: a .csv path")
    cols = d.get("columns") or {}
    for k in ("unit", "package", "value"):
        if not cols.get(k):
            bad.append(f"deliverable.columns.{k}: the column name")
    hl = d.get("headline") or {}
    if not hl.get("name") or hl.get("outcome") not in s.outcomes():
        bad.append("deliverable.headline: {name, outcome} with outcome in objective/outcomes_reported")
    pr = d.get("plan")
    if pr is not None and pr not in PLAN_RULES:
        bad.append(f"deliverable.plan: one of {PLAN_RULES} (absent: point), got {pr!r}")
    if pr == "expected_value":       # hb1 fix 11: the plan with the best mean over the uncertainty draws
        un = s.raw.get("uncertainty") or {}
        if not un.get("sources"):
            bad.append("deliverable.plan: expected_value needs at least one uncertainty source to average over")
        if (un.get("affordability") or {}).get("enforce"):
            bad.append("deliverable.plan: expected_value is not combined with uncertainty.affordability.enforce (a "
                       "plan solved within a reserve); the expected-value plan already fits the budget in every draw")
        if (s.raw.get("analysis") or {}).get("plan_variants"):
            bad.append("deliverable.plan: expected_value is not combined with analysis.plan_variants (the variants are "
                       "solved and compared at the point values)")
        if d.get("kind") != "package_per_unit":
            bad.append("deliverable.plan: expected_value applies to a package_per_unit deliverable")
    bg = d.get("by_group")
    if bg is not None:
        gc = (bg or {}).get("columns") if isinstance(bg, dict) else None
        if not isinstance(gc, dict) or not all(isinstance(gc.get(k), str) and gc[k] for k in ("group", "quantity",
                                                                                           "value")):
            bad.append("deliverable.by_group.columns: {group, quantity, value} column names")
        qm = (bg or {}).get("quantity") if isinstance(bg, dict) else None
        outs = set((s.raw.get("analysis") or {}).get("cost_outputs") or {})
        groups = set()
        if not s.engine_pending:
            try:
                groups = set(s.engine().describe().age_groups or [])
            except Exception:  # noqa: BLE001 -- reported by the engine checks
                groups = set()
        if not isinstance(qm, dict) or not qm:
            bad.append("deliverable.by_group.quantity: {engine group: analysis.cost_outputs name}")
        else:
            bad += [f"deliverable.by_group.quantity: {g} is not an engine group" for g in qm
                    if groups and g not in groups]
            bad += [f"deliverable.by_group.quantity.{g}: {v!r} is not in analysis.cost_outputs" for g, v in qm.items()
                    if v not in outs]
        if (s.raw.get("analysis") or {}).get("evaluation") != "direct":
            bad.append("deliverable.by_group: needs analysis.evaluation: direct (per-group outcomes)")
    return bad


def toy_spec(d: str, n: int = 12, bench: str | None = None, dt_values=(1.0, 3.0)) -> str:
    """A valid toy spec and units table in directory d (used by the analysis self-tests too). `bench`: a benchmark
    results file for the toy engine (its interaction benchmark T2 drives decision test DT1)."""
    import math
    with open(os.path.join(d, "units.csv"), "w") as f:
        f.write("id,region,grp,cov,pop,elig,tgt\n")
        for i in range(n):
            x = -1.5 + 3.0 * ((i * 7) % n) / max(n - 1, 1)
            prev = 1 - math.exp(-0.5 * math.exp(0.8 * x))
            f.write(f"{i},r{i % 3},g{i % 4},{x + 0.05 * ((i % 5) - 2):.4f},{1000 + 50 * i},{i % 2},{prev:.5f}\n")
    spec = {
        "schema_version": 1, "question": "Which package per unit?", "decision_maker": "programme manager",
        "decision_date": 2026,
        "engine": ({"ref": "toolkit.engines.contract:ToyEngine", "config": {"results_path": bench}} if bench
                   else {"ref": "toolkit.engines.contract:ToyEngine"}),
        "horizon": {"start": 0, "stop": 3},
        "decision_units": {"csv": "units.csv", "id": "id", "region": "region",
                           "populations": {"all": {"column": "pop", "age_groups": ["all"]}}, "engine_attributes": {}},
        "options": {"populations": {"total": "pop"},
                    "components": {"a": {}, "b": {}}, "packages": {"generate": True},
                    "engine_map": {"a": {"a": 1}, "b": {"b": 1}},
                    "prices": {"base_year": "2024", "index": {"2024": 1.0}},
                    "cost_items": [{"id": "ca", "component": "a", "value": 1.0, "price_year": "2024",
                                    "price_rule": "as_published", "basis": "per_person", "includes": ["x"]},
                                   {"id": "cb", "component": "b", "value": 0.6, "price_year": "2024",
                                    "price_rule": "as_published", "basis": "per_person", "includes": ["y"]}]},
        "objective": {"outcome": "dalys", "horizon": [0, 3], "discounting": "none"},
        "constraints": {"envelope_usd": 9000, "eligibility": {"b": {"column": "elig"}}},
        "comparator": {"package": "none", "text": "no package"},
        "outcomes_reported": ["dalys", "cases"],
        "calibration": {"method": "emulator_inversion", "attribute": "x", "scale": "linear", "range": [-2, 2],
                        "target_column": "tgt", "window": [1, 2], "metric": "prev",
                        "target_uncertainty": {"region_sd_logit": 0.05}, "covariates": ["cov"]},
        "uncertainty": {"draws": 6, "seed": 1, "sources": [{"kind": "engine_parameters"}, {"kind": "calibration"}],
                        "excluded": [{"kind": "costs", "reason": "toy"}, {"kind": "emulator", "reason": "toy"},
                                     {"parameter": "synergy", "reason": "toy: no interaction assumed"}]},
        "analysis": {"evaluation": "emulator", "emulator": {"points": 60, "holdout_share": 0.15, "seed": 5,
                                                             "features": []},
                     "stability": {"holdout_group": "grp", "covariates": ["cov"]},
                     "sensitivity": {"parameters": "fitted"},
                     "decision_tests": [{"id": "DT1", "type": "interaction_rescale", "components": ["b"],
                                         "with_any": ["a"], "values": [float(v) for v in dt_values],
                                         "finding": "benchmark_drift:T2", "benchmark": "T2",
                                         "text": "Does the plan change at the trial interaction?"}]},
        "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation_optimized.csv",
                        "columns": {"unit": "unit", "package": "package", "value": "dalys_averted"},
                        "headline": {"name": "total_dalys_averted", "outcome": "dalys"}},
    }
    p = os.path.join(d, "decision_spec.yaml")
    with open(p, "w") as f:
        yaml.safe_dump(spec, f, sort_keys=False)
    return p


def direct_toy(r: dict) -> dict:
    """The toy spec on direct evaluation with no calibration and the engine attribute x bound to column cov (a unit
    attribute the units table sets; the unit-attribute decision-test self-tests)."""
    r["decision_units"]["engine_attributes"] = {"x": {"column": "cov"}}
    r["calibration"] = {"method": "none"}
    r["analysis"]["evaluation"] = "direct"
    r["analysis"].pop("emulator", None)
    r["analysis"]["decision_tests"] = []
    r["uncertainty"]["sources"] = [{"kind": "engine_parameters"}]
    r["uncertainty"]["excluded"] = [{"kind": k, "reason": "toy"} for k in ("costs", "calibration", "emulator")] + [
        {"parameter": "synergy", "reason": "toy: no interaction assumed"}]
    return r


TOY_DERIVE_SCRIPT = ("import argparse\nimport csv\nap = argparse.ArgumentParser()\nap.add_argument('--inputs', nargs='+')\n"
                     "ap.add_argument('--outputs', nargs='+')\na = ap.parse_args()\n"
                     "rows = list(csv.DictReader(open(a.inputs[0], newline='')))\n"
                     "with open(a.outputs[0], 'w', newline='') as f:\n"
                     "    w = csv.DictWriter(f, fieldnames=list(rows[0]) + ['lbl'])\n    w.writeheader()\n"
                     "    for i, r in enumerate(rows):\n        w.writerow(dict(r, lbl='' if i == 0 else 'u' + r['id']))\n")


def derived_toy_spec(d: str, n: int = 12, **kw) -> str:
    """toy_spec with its units table made by a recorded derivation (docs/design/v2-derived-inputs.md): the table is a
    staged input (as stage_inputs records it), a copying script derives data/derived/units.csv (adding a column `lbl`
    blank in one row), and the spec accepts the toy's one default by name and declares its target's frame. Where
    there is no sandbox the derivation runs with the development flag (the record says sandbox none)."""
    import pipeline as P
    p = toy_spec(d, n=n, **kw)
    os.makedirs(os.path.join(d, "data", "inputs"), exist_ok=True)
    os.makedirs(os.path.join(d, "data", "derive"), exist_ok=True)
    raw = os.path.join(d, "data", "inputs", "units_raw.csv")
    os.replace(os.path.join(d, "units.csv"), raw)
    with open(raw, newline="") as f:
        cols = next(csv.reader(f))
    with open(os.path.join(d, "data", "inputs_provenance.json"), "w") as f:
        json.dump({"inputs": {"data/inputs/units_raw.csv": {"source": "fixture/units_raw.csv", "sha256": sha256_file(raw)}},
                   "cited": {}}, f)
    with open(os.path.join(d, "data", "derive", "units.py"), "w") as f:
        f.write(TOY_DERIVE_SCRIPT)
    with open(os.path.join(d, "data", "derivations.yaml"), "w") as f:
        yaml.safe_dump({"derivations": [{"id": "units", "script": "data/derive/units.py",
                                         "inputs": ["data/inputs/units_raw.csv"], "outputs": ["data/derived/units.csv"],
                                         "columns": {"uses": {"data/inputs/units_raw.csv": cols}}}]}, f, sort_keys=False)
    raw_spec = yaml.safe_load(open(p))
    raw_spec["decision_units"]["csv"] = "data/derived/units.csv"
    raw_spec["decision_units"]["defaults_accepted"] = {"group": "the toy units carry no group label"}
    raw_spec["calibration"]["target_frame"] = {"band": "prev", "source": "the toy target measures the engine's own "
                                                                         "window metric"}
    with open(p, "w") as f:
        yaml.safe_dump(raw_spec, f, sort_keys=False)
    rep = P.derive(d, unsandboxed_ok=P.sandbox_kind() is None)
    if not rep["ok"]:
        raise RuntimeError(f"derived_toy_spec: the fixture's derivation failed: {rep['problems']}")
    return p


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(__doc__)
        return 2
    errs = validate(load(argv[0]))
    print("spec valid" if not errs else "spec invalid:\n  " + "\n  ".join(errs))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
