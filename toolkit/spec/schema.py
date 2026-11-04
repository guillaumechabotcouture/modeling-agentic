#!/usr/bin/env python3
"""Decision spec schema and validator (architecture-v2 §3.1), fail closed.

    python3 toolkit/spec/schema.py <decision_spec.yaml>      # prints problems, exit 1 if any
    python3 toolkit/spec/schema.py --self-test

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
        a: {column: col}                   # {column, split}, {columns: [..] or {k: col}}, {value, source}
      populations:                         # population group <- unit column and engine age groups
        all: {column: pop, age_groups: [all]}
      defaults_accepted: {attr: "<reason>"}   # an engine card default taken by name (the card's attribute_binding.yaml);
                                           # every other card attribute is bound or calibrated (v2-derived-inputs)
    options:                              # toolkit/alloc sections (costs.py, packages.py)
      populations: {total: pop}            # alloc population names -> unit columns
      components: {c1: {group: g}, ...}
      packages: {generate: true} | {name: [components]}
      engine_map: {c1: {engine_component: value}}
      prices: {...}
      cost_items: [...]
    objective:
      outcome: dalys                       # an engine per-capita output
      sense: avert                         # benefit = comparator outcome - package outcome
      horizon: [2024.0, 2027.0]            # the decision window (outcomes are counted over it)
      delivery: [2024.0, 2026.0]           # optional: the years whose components the decision delivers and pays
                                           # for, inside horizon (default: horizon); only for an engine that declares
                                           # supports_delivery (toolkit/engines/horizon.py)
      discounting: none | {rate: 0.03, base_year: 2024}
    constraints:
      envelope_usd: 1.0e6
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
    analysis:
      evaluation: emulator | direct        # an emulator for slow engines, the engine itself for cheap ones
      emulator: {points, holdout_share, seed, features, design_seed}
      cost_outputs: {treated: {cost_driver: treated_all}}    # per_outcome cost items <- engine cost drivers
      diagnostics: {name: {window: cal, metric: m, summary: [max, median]}}
      stability: {holdout_group: col, covariates: [..]}
      sensitivity: {parameters: fitted | [names], cost_items: {id: [lo, hi]}}
      share_threshold: 0.10                # decision tests: share of units changing package
      decision_tests: [{id, type, text, finding, ...}]
    deliverable:
      kind: package_per_unit
      allocation_file: allocation_optimized.csv
      columns: {unit: district, package: package, value: cases_averted}
      headline: {name: total_cases_averted, outcome: cases}

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
import os
import re
import sys

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
                     "emulator": "emulator error"}
DECISION_TEST_TYPES = ("interaction_rescale", "parameter_value", "group_rule", "envelope")
DELIVERABLE_KINDS = ("package_per_unit",)
CALIBRATION_FIELDS = {
    "emulator_inversion": ("attribute", "range", "target_column", "window", "metric"),
    "likelihood": ("attribute", "range", "parameters", "targets"),
    "posterior": ("attribute", "range", "parameters", "targets", "priors", "observation"),   # v2d §3.1
    "none": (),
}
TARGET_FIELDS = ("csv", "unit", "start", "package", "observed", "exposure")   # and `stop` (a column) or `period` (years)


class SpecError(ValueError):
    pass


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
    return eng == "auto" or (isinstance(eng, dict) and eng.get("auto") is True)


def _resolve_engine_choice(s: "Spec") -> None:
    """engine: auto (or {auto: true, candidates: [...]}) in the signed spec: the engine is the AI's choice, recorded by
    the harness after sign-off in engine_choice.json beside the spec. Fail closed on a malformed choice or one outside
    the signed candidates. An explicit engine in the spec wins and the choice file is ignored."""
    eng = s.raw.get("engine")
    if not engine_is_auto(eng):
        return
    s.engine_auto = True
    cands = eng.get("candidates") if isinstance(eng, dict) else None
    if cands is not None and (not isinstance(cands, list) or not all(isinstance(c, str) and ":" in c for c in cands)):
        raise SpecError("engine.candidates: a list of 'module:Class' references")
    for c in cands or []:
        check_engine_ref(c)
    cp = os.path.join(s.base, ENGINE_CHOICE)
    if not os.path.exists(cp):
        s.engine_pending = True
        return
    try:
        doc = json.load(open(cp, encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SpecError(f"{ENGINE_CHOICE}: unreadable: {e}") from e
    e = doc.get("engine") if isinstance(doc, dict) else None
    if not isinstance(e, dict) or not isinstance(e.get("ref"), str) or ":" not in e["ref"] \
            or not isinstance(e.get("config") or {}, dict):
        raise SpecError(f"{ENGINE_CHOICE}: engine must be {{ref: 'module:Class', config: {{...}}}}")
    if cands and e["ref"] not in cands:
        raise SpecError(f"{ENGINE_CHOICE}: engine {e['ref']} is not among the signed candidates {cands}")
    check_engine_ref(e["ref"])
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
        self.derived = None          # derived-input obligations: None = when the run declares derivations
        if engine_is_auto(raw.get("engine")) and path is None:
            self.engine_auto = self.engine_pending = True      # a mapping with no file beside it: no choice to read
        self._engine = None
        self._units = None

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
    def engine(self):
        if self.engine_pending:
            raise SpecError(f"engine: auto, and the harness has not written {ENGINE_CHOICE} yet")
        if self._engine is None:
            self._engine = load_engine(self.raw["engine"])
        return self._engine

    def horizon(self, windows: dict | None = None) -> Horizon:
        """The engine horizon; a DeliveryHorizon when the spec gives objective.delivery (else the contract's own)."""
        h, o = self.raw["horizon"], self.raw["objective"]
        return make_horizon(h["start"], h["stop"], tuple(float(x) for x in o["horizon"]), windows, o.get("delivery"))

    # ---- units
    def units(self) -> list[dict]:
        if self._units is None:
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
                out[attr] = [float(x) for x in str(v).split(m["split"])] if "split" in m else _num(v)
            elif isinstance(m["columns"], dict):
                out[attr] = {k: float(row[c]) for k, c in m["columns"].items()}
            else:
                out[attr] = [float(row[c]) for c in m["columns"]]
        return out

    # ---- options and constraints, in toolkit/alloc form
    def alloc_spec(self) -> dict:
        """The toolkit/alloc spec: options plus the constraints (one place to read eligibility and caps)."""
        o, c = self.raw["options"], self.raw["constraints"]
        a = {k: copy.deepcopy(v) for k, v in o.items() if k in ("components", "packages", "prices", "cost_items")}
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
        return acosts.packages(self.raw["options"])

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


def check_engine_ref(ref) -> tuple[str, str]:
    """(module, class) of an allowed engine reference, or SpecError. Imports nothing."""
    from toolkit import import_guard as G
    try:
        return G.check_ref("engine", ref)
    except G.ImportRefused as e:
        raise SpecError(f"engine {str(ref)[:80]}: {e}") from None


def engine_class(ref: str):
    """The engine class an allowed reference names, checked against the Engine contract; never instantiated."""
    from toolkit import import_guard as G
    check_engine_ref(ref)
    try:
        k = G.resolve("engine", ref)
    except G.ImportRefused as e:
        raise SpecError(str(e)) from None
    if not isinstance(k, type) or not all(callable(getattr(k, m, None)) for m in ENGINE_METHODS):
        raise SpecError(f"engine {ref}: not a class implementing the Engine contract ({', '.join(ENGINE_METHODS)})")
    return k


def load_engine(ref):
    if isinstance(ref, str):
        ref = {"ref": ref}
    if not isinstance(ref, dict):
        raise SpecError("engine: {ref: 'module:Class', config: {...}}")
    config = ref.get("config") or {}
    if not isinstance(config, dict):
        raise SpecError("engine.config must be a mapping")
    return engine_class(ref.get("ref"))(**config)


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
        pres = presentation_refusal(spec.raw)         # every path, sign-off with engine: auto pending included
        if spec.engine_pending:
            if not pending_ok:
                return [f"engine: auto, and the harness has not written {ENGINE_CHOICE} yet (engine-dependent sections "
                        "cannot be checked)"] + pres
            return pres + _validate_pending(spec) + (_derived_checks(spec, None) if obligations(spec) else [])
        return pres + _validate(spec) + (_derived_checks(spec, spec.engine()) if obligations(spec) else [])
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
            out[str(k)] = v
        else:
            bad.append(f"engine_attribute_unbound: {path}: {k} must be `required` or {{default, meaning}}")
    return out, bad


def load_binding(engine) -> tuple[dict, list[str]]:
    """The engine card's attribute binding ({attribute or glob: required | {default, meaning}}): `binding_path` on the
    engine, else attribute_binding.yaml beside the module of the engine's class or of its nearest base class that has
    one (the priors.yaml convention). No file: every attribute is required."""
    import inspect
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
    if m == "emulator_inversion":
        compared, fr, where = cal.get("metric"), cal.get("target_frame"), "calibration.target_frame"
    else:
        t = cal.get("targets") or {}
        compared = t.get("outcome", (s.raw.get("objective") or {}).get("outcome"))
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


def with_engine(s: Spec, ref: str, config: dict | None = None) -> Spec:
    """The spec with one candidate engine in place of `auto` (the pre-sign check of each candidate)."""
    raw = copy.deepcopy(s.raw)
    raw["engine"] = {"ref": ref, **({"config": dict(config)} if config else {})}
    out = Spec(raw, s.base, s.path)
    out.settings_path, out.derived = s.settings_path, s.derived
    return out


def _validate_pending(s: Spec) -> list[str]:
    """engine: auto before the choice: everything that does not need the engine card."""
    r = s.raw
    bad = [f"missing section {k}" for k in REQUIRED if k not in r]
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
        if dt.get("type") not in DECISION_TEST_TYPES:
            bad.append(f"decision test {dt.get('id')}: type {dt.get('type')!r} not in {DECISION_TEST_TYPES}")
        if any(ch.isdigit() for ch in str(dt.get("text") or "")):
            bad.append(f"decision test {dt.get('id')}: text must be descriptive, with no numbers")
    bad += _validate_deliverable(s)
    return bad


def _validate(s: Spec) -> list[str]:
    r = s.raw
    bad = [f"missing section {k}" for k in REQUIRED if k not in r]
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
    a = s.alloc_spec()
    bad += [f"options: {e}" for e in acosts.validate(a, cols)]
    bad += [f"constraints: {e}" for e in apackages.validate(a, units)]
    c = r["constraints"]
    if not _isnum(c.get("envelope_usd")):
        bad.append("constraints.envelope_usd: a number")
    elif s.budget() <= 0:
        bad.append("constraints: envelope_usd must exceed the fixed costs")
    for g in c.get("group_rules") or []:
        if not isinstance(g, dict) or not g.get("id"):
            bad.append("constraints.group_rules: each rule needs an id")
    if bad:
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
    drivers = card.outputs.get("cost_drivers", [])
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
    from toolkit.spec import priors as PRI
    bad += PRI.problems(s)
    bad += _validate_analysis(s, card, pnames, cols, pk)
    bad += _validate_deliverable(s)
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


def _validate_calibration(s, card, pnames, cols) -> list[str]:
    cal = s.raw["calibration"]
    m = cal.get("method") if isinstance(cal, dict) else None
    if m not in CALIBRATION_METHODS:
        return [f"calibration.method: one of {CALIBRATION_METHODS}"]
    bad = [f"calibration.{k}: required for {m}" for k in CALIBRATION_FIELDS[m] if k not in cal]
    if bad or m == "none":
        return bad
    if cal["attribute"] not in card.unit_attributes:
        bad.append(f"calibration.attribute: {cal['attribute']} is not declared by the engine")
    rg = cal["range"]
    if not (isinstance(rg, list) and len(rg) == 2 and all(_isnum(x) for x in rg) and rg[0] < rg[1]):
        bad.append("calibration.range: [lo, hi] on the attribute's scale (log by default)")
    if cal.get("scale", "log") not in ("log", "linear"):
        bad.append("calibration.scale: log or linear")
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


def _validate_uncertainty(s, pnames) -> list[str]:
    u = s.raw["uncertainty"]
    if not isinstance(u, dict):
        return ["uncertainty: a mapping"]
    bad = []
    inc = [x.get("kind") for x in u.get("sources") or [] if isinstance(x, dict)]
    exc = {x.get("kind"): x.get("reason") for x in u.get("excluded") or [] if isinstance(x, dict) and "parameter" not in x}
    for k in inc + list(exc):
        if k not in UNCERTAINTY_KINDS and k not in OPTIONAL_KINDS:
            bad.append(f"uncertainty: unknown source kind {k!r} (known: {', '.join(list(UNCERTAINTY_KINDS) + list(OPTIONAL_KINDS))})")
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
        if src.get("kind") == "calibration" and s.raw["calibration"].get("method") == "none":
            bad.append("uncertainty.sources calibration: calibration.method is none")
    return bad


def _validate_analysis(s, card, pnames, cols, pk) -> list[str]:
    an = s.raw["analysis"]
    bad = []
    if an.get("evaluation") not in EVALUATIONS:
        return [f"analysis.evaluation: one of {EVALUATIONS}"]
    if an["evaluation"] == "emulator":
        em = an.get("emulator") or {}
        if not (isinstance(em.get("points"), int) and em["points"] >= 8):
            bad.append("analysis.emulator.points: an integer >= 8")
        bad += [f"analysis.emulator.features: no column {f}" for f in em.get("features") or [] if f not in cols]
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
    seen = set()
    thr = an.get("share_threshold", 0.10)
    if not (_isnum(thr) and 0 <= thr <= 1):
        bad.append("analysis.share_threshold: a fraction in [0, 1]")
    rules = {g.get("id") for g in s.raw["constraints"].get("group_rules") or []}
    for dt in an.get("decision_tests") or []:
        i = dt.get("id")
        if not i or i in seen:
            bad.append(f"analysis.decision_tests: id {i!r} missing or duplicated")
        if str(i or "").startswith("prior_range_") or dt.get("type") == "prior_range":
            bad.append(f"decision test {i}: prior_range tests are generated by the harness for every drawn prior "
                       "(toolkit/analysis/decision_tests.py); the spec cannot declare one")
            continue
        seen.add(i)
        t = dt.get("type")
        if t not in DECISION_TEST_TYPES:
            bad.append(f"decision test {i}: type {t!r} not in {DECISION_TEST_TYPES}")
            continue
        if not str(dt.get("text") or "").strip():
            bad.append(f"decision test {i}: text (the question it answers) is empty")
        elif any(ch.isdigit() for ch in str(dt["text"])):
            bad.append(f"decision test {i}: text must be descriptive, with no numbers (Codex P1-4): the values tested "
                       "and the results are rendered from computed claims")
        if "engine_arm_or" in dt:
            bad.append(f"decision test {i}: engine_arm_or cannot be supplied; arm ratios come from the engine's "
                       "verified benchmark results (Codex P1-13)")
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
        elif t == "group_rule":
            if dt.get("rule") not in rules and not dt.get("add"):
                bad.append(f"decision test {i}: rule {dt.get('rule')!r} is not a constraints.group_rules id (or give add:)")
        elif t == "envelope":
            if not dt.get("values") or not all(_isnum(v) for v in dt["values"]):
                bad.append(f"decision test {i}: values (envelopes in USD)")
    return bad


def _validate_deliverable(s) -> list[str]:
    d = s.raw["deliverable"]
    if not isinstance(d, dict):
        return ["deliverable: a mapping (kind, allocation_file, columns, headline)"]
    bad = []
    if d.get("kind") not in DELIVERABLE_KINDS:
        bad.append(f"deliverable.kind: one of {DELIVERABLE_KINDS}")
    if not str(d.get("allocation_file") or "").endswith(".csv"):
        bad.append("deliverable.allocation_file: a .csv path")
    cols = d.get("columns") or {}
    for k in ("unit", "package", "value"):
        if not cols.get(k):
            bad.append(f"deliverable.columns.{k}: the column name")
    hl = d.get("headline") or {}
    if not hl.get("name") or hl.get("outcome") not in s.outcomes():
        bad.append("deliverable.headline: {name, outcome} with outcome in objective/outcomes_reported")
    return bad


# --------------------------------------------------------------------------- self-test
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
                        "excluded": [{"kind": "costs", "reason": "toy"}, {"kind": "emulator", "reason": "toy"}]},
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


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as d:
        p = toy_spec(d)
        s = load(p)
        ok(validate(s) == [], f"SP1 a well-formed toy spec validates: {validate(s)}")

        def bad(mut, needle, label):
            r = copy.deepcopy(s.raw)
            mut(r)
            errs = validate(Spec(r, d))
            ok(any(needle in e for e in errs), f"{label}: {errs[:3]}")
        bad(lambda r: r["decision_units"]["engine_attributes"].update(zz={"column": "cov"}), "not declared",
            "SP2 an engine attribute the engine does not declare is refused")
        bad(lambda r: r["options"]["engine_map"].update(b={"b": 7}), "not an engine component value",
            "SP3 a component mapped to an unknown engine value is refused")
        bad(lambda r: r["calibration"].update(target_column="nope"), "no column nope",
            "SP4 a missing calibration target column is refused")
        bad(lambda r: r["comparator"].update(package="x"), "comparator", "SP5 an unknown comparator is refused")
        bad(lambda r: r["options"]["cost_items"].pop(), "no cost item",
            "SP6 a packaged component without a cost item is refused (toolkit/alloc)")
        bad(lambda r: r["constraints"].update(fixed_costs_usd={"f": 1e5}), "exceed the fixed costs",
            "SP7 an envelope below its fixed costs is refused")
        bad(lambda r: r["uncertainty"].update(excluded=[{"kind": "costs", "reason": "toy"}]), "neither declared",
            "SP8 lesson 6: an uncertainty source neither declared nor excluded with a reason is refused")
        bad(lambda r: r["uncertainty"]["excluded"][0].update(reason=""), "neither declared",
            "SP9 an exclusion without a reason is refused")
        bad(lambda r: r["constraints"].update(group_rules=[{"id": "g", "components": ["a"], "by": "nocol"}]),
            "not a column", "SP10 lesson 10: a group rule on an unknown column is refused")
        bad(lambda r: r["analysis"]["decision_tests"].append({"id": "DT2", "type": "magic", "text": "t"}),
            "not in", "SP11 an unknown decision-test type is refused")
        bad(lambda r: r["analysis"]["decision_tests"].append({"id": "prior_range_eff_a", "type": "parameter_value",
                                                               "parameter": "eff_a", "values": [0.3], "text": "t"}),
            "generated by the harness", "SP11b the spec cannot declare (or shadow) an automatic prior_range test")
        bad(lambda r: r["objective"].update(outcome="nope"), "per-capita output", "SP12 an unknown objective outcome")
        # owner 2026-11-02: display labels are not part of the signed spec (an unsigned presentation.yaml instead)
        bad(lambda r: r.update(presentation={"labels": {"components": {"a": "Service A"}}}), "presentation.yaml",
            "SP-P1 a presentation block in the signed spec is refused, with a pointer to the presentation file")
        # Codex r2 P1-3: the refusal also holds for engine: auto before the choice (the sign-off path)
        r_pend = copy.deepcopy(s.raw)
        r_pend["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]}
        r_pend["presentation"] = {"labels": {"components": {"a": "Service A"}}}
        sp_pend = Spec(r_pend, d)
        errs_pend = validate(sp_pend, pending_ok=True)
        ok(sp_pend.engine_pending and any("presentation.yaml" in e for e in errs_pend),
           f"SP-P5 a presentation block is refused at sign-off too (engine pending): {errs_pend[:3]}")
        eng = s.engine()
        pn, card = {p.name for p in eng.parameters()}, eng.describe()
        lab_ok = {"components": {"a": "Service A"}, "parameters": {"eff_a": "Effect of A"}, "outcomes": {"dalys": "DALYs"},
                  "metrics": {"prev": "prevalence"}, "units_title": "districts"}
        ok(labels_problems(s.raw, lab_ok, pn, card) == [], f"SP-P1b valid labels: {labels_problems(s.raw, lab_ok, pn, card)}")
        for lab, needle, label in (({"components": {"zz": "Z"}}, "labels.components", "SP-P2 a label for an unknown component"),
                                   ({"colours": {}}, "unknown key", "SP-P3 an unknown labels key"),
                                   ({"outcomes": {"dalys": ""}}, "non-empty display name", "SP-P4 an empty display name")):
            got = labels_problems(s.raw, lab, pn, card)
            ok(any(needle in e for e in got), f"{label} is refused: {got}")
        bad(lambda r: r.pop("deliverable"), "missing section deliverable", "SP13 a missing section is refused")
        bad(lambda r: r["objective"].update(delivery=[0, 4]), "objective.delivery",
            "SP-D1 a delivery period outside the decision window is refused")
        bad(lambda r: r["objective"].update(delivery="2024-2027"), "objective.delivery",
            "SP-D1b a delivery period that is not [t0, t1] with t0 < t1 is refused")
        bad(lambda r: r["objective"].update(delivery=[0, 2]), "does not honour",
            "SP-D2 a delivery period for an engine that does not declare supports_delivery is refused (fail closed)")
        from toolkit.engines.contract import ToyEngine
        r = copy.deepcopy(s.raw)
        r["objective"]["delivery"] = [0, 2]
        ToyEngine.supports_delivery = True
        try:
            sd = Spec(r, d)
            errs = validate(sd)
            ok(errs == [] and getattr(sd.horizon(), "delivery", None) == (0.0, 2.0)
               and getattr(s.horizon(), "delivery", None) is None and tuple(sd.horizon().window) == (0.0, 3.0),
               f"SP-D3 a delivery period inside the window validates and reaches the engine horizon: {errs[:3]}")
        finally:
            del ToyEngine.supports_delivery
        bad(lambda r: r["uncertainty"]["sources"].append({"kind": "emulator"}) or r["uncertainty"].update(excluded=[
            {"kind": "costs", "reason": "toy"}]) or r["analysis"].update(evaluation="direct"),
            "there is no emulator", "SP14 an emulator source on the direct path is refused")
        bad(lambda r: r["calibration"].update(method="likelihood", parameters=["eff_a"], targets={}), "not calibrated",
            "SP15 likelihood calibration of a parameter the engine card does not mark calibrated is refused")
        r = copy.deepcopy(s.raw)
        r["constraints"]["coverage"] = {"b": {"max_units_share": 0.25}}
        r["constraints"]["group_rules"] = [{"id": "camp", "components": ["a"], "by": "region", "rule": "same_choice"}]
        s2 = Spec(r, d)
        a = s2.alloc_spec()
        ok(validate(s2) == [] and a["caps"]["b"]["max_units"] == 3.0 and "camp" in a["group_rules"],
           f"SP16 coverage becomes a toolkit/alloc cap and group rules pass through: {validate(s2)}")
        sc = s.uncertainty_scope()
        ok(sc["label"] == "95% UI over engine parameters, calibration; held fixed: synergy and 3 other engine parameters; "
                         "not covered: costs, emulator", f"SP17 the interval label states what is covered and what is not: {sc['label']}")
        ok(validate({"schema_version": 1}) and validate(Spec({"engine": 3}, d)), "SP18 malformed specs fail closed")
        r = copy.deepcopy(s.raw)
        draws = r["uncertainty"].pop("draws")
        pts = r["analysis"]["emulator"].pop("points")
        with open(p, "w") as f:
            yaml.safe_dump(r, f)
        with open(os.path.join(d, SETTINGS), "w") as f:
            yaml.safe_dump({"uncertainty": {"draws": draws}, "analysis": {"emulator": {"points": pts}}}, f)
        s3 = load(p)
        ok(validate(s3) == [] and s3["uncertainty"]["draws"] == draws and s3["analysis"]["emulator"]["points"] == pts
           and s3.settings_sha256, f"SP19 computational settings (draws, design size) can live in {SETTINGS}")
        ok(s3.path == p and s3.sha256 == sha256_file(p),
           f"SP19b loading with a settings file keeps the spec's own path (Codex r2 P1-1): {s3.path}")
        for bad_set, label in (({"objective": {"outcome": "cases"}}, "a decision section"),
                               ({"calibration": {"range": [-9, 9]}}, "a calibration setting"),
                               ({"analysis": {"decision_tests": []}}, "the decision tests"),
                               ({"uncertainty": {"sources": []}}, "the uncertainty sources"),
                               ({"analysis": {"cost_outputs": {}}}, "a cost mapping"),
                               ({"uncertainty": {"draws": 9}, "analysis": {"emulator": {"points": 9, "seed": 1}}}
                                if False else {"analysis": {"emulator": {"holdout_share": 0.5}}}, "the hold-out share")):
            with open(os.path.join(d, SETTINGS), "w") as f:
                yaml.safe_dump(bad_set, f)
            try:
                load(p)
                ok(False, f"SP20 {label} in the settings file is refused (Codex P1-1)")
            except SpecError:
                ok(True, f"SP20 {label} in the settings file is refused (Codex P1-1)")
        with open(os.path.join(d, SETTINGS), "w") as f:
            yaml.safe_dump({"uncertainty": {"draws": draws, "seed": 3}}, f)
        r2 = copy.deepcopy(r)
        r2["uncertainty"]["draws"] = draws
        with open(p, "w") as f:
            yaml.safe_dump(r2, f)
        try:
            load(p)
            ok(False, "SP21 a leaf in both files is refused")
        except SpecError:
            ok(True, "SP21 a leaf in both files is refused")
        os.remove(os.path.join(d, SETTINGS))
        r2["analysis"]["emulator"]["points"] = pts
        with open(p, "w") as f:
            yaml.safe_dump(r2, f)
        r = copy.deepcopy(s.raw)
        r["constraints"]["caps"] = {"b": {"max_units": 2}}
        r["constraints"]["coverage"] = {"b": {"max_units_share": 0.5}}
        ok(Spec(r, d).alloc_spec()["caps"]["b"]["max_units"] == 2,
           "SP22 a max_units cap and a coverage share: the binding (smaller) limit is kept (Codex P1-2)")
        r["constraints"]["caps"] = {"b": {"max_units": 9}}
        ok(Spec(r, d).alloc_spec()["caps"]["b"]["max_units"] == 6, "SP22b ... whichever is smaller")
        r = copy.deepcopy(s.raw)
        r["analysis"]["decision_tests"][0]["text"] = "Does the plan change at an interaction of 2.43?"
        ok(any("no numbers" in e for e in validate(Spec(r, d))), "SP23 a decision-test text with numbers is refused "
                                                                 "(Codex P1-4)")
        r = copy.deepcopy(s.raw)
        r["analysis"]["decision_tests"][0]["engine_arm_or"] = {"a11": 0.4, "a10": 0.5, "a01": 0.7}
        ok(any("cannot be supplied" in e for e in validate(Spec(r, d))),
           "SP24 arm ratios supplied in the spec are refused (Codex P1-13)")
    with tempfile.TemporaryDirectory() as d:     # engine: auto (the engine is the AI's choice, made after sign-off)
        p = toy_spec(d)
        raw = yaml.safe_load(open(p))
        toy = raw["engine"]
        raw["engine"] = {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]}
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        try:
            s = load(p)
            errs, pend = validate(s), validate(s, pending_ok=True)
            ok(s.engine_pending and any(ENGINE_CHOICE in e for e in errs) and pend == [],
               f"SP25 engine auto before the choice: the decision sections validate, the engine-dependent ones wait: "
               f"{errs[:2]} {pend[:2]}")
        except Exception as e:  # noqa: BLE001
            ok(False, f"SP25 engine auto before the choice validates its decision sections: {type(e).__name__}: {e}")
        cp = os.path.join(d, ENGINE_CHOICE) if "ENGINE_CHOICE" in globals() else os.path.join(d, "engine_choice.json")
        import json
        json.dump({"engine": toy, "why": "the only eligible engine", "candidates": [
            {"ref": toy["ref"], "eligible": True, "why_not": ""}], "cross_check": {}}, open(cp, "w"))
        try:
            s = load(p)
            ok(s["engine"] == toy and validate(s) == [] and s.engine_choice_sha256 == sha256_file(cp),
               "SP26 engine auto: the engine comes from engine_choice.json and the spec validates in full")
        except Exception as e:  # noqa: BLE001
            ok(False, f"SP26 engine auto reads engine_choice.json: {type(e).__name__}: {e}")
        json.dump({"engine": {"ref": "toolkit.engines.empirical.engine:PackageRateEngine"}, "why": "x"}, open(cp, "w"))
        try:
            load(p)
            ok(False, "SP27 a chosen engine outside the signed candidates is refused")
        except Exception as e:  # noqa: BLE001
            ok(isinstance(e, SpecError) and "candidate" in str(e), f"SP27 a chosen engine outside the signed candidates "
                                                                   f"is refused: {e}")
        raw["engine"] = toy
        yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
        s = load(p)
        ok(s["engine"] == toy and getattr(s, "engine_choice_path", None) is None,
           "SP28 an explicit engine in the signed spec wins; engine_choice.json is ignored")
    try:
        _self_test_derived(ok)
    except Exception as e:  # noqa: BLE001 -- before the checks exist (step α) this is the failure
        ok(False, f"DI spec checks: {type(e).__name__}: {e}")
    _self_test_engine_refs(ok)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


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


def _self_test_engine_refs(ok) -> None:
    """SP-RW1..4 (run console review round 2, P1-1): a spec names its engine by "module:Class"; the spec is drafted by
    an agent, so a reference outside the registered engines is refused before any import or instantiation."""
    import tempfile
    seen, real = [], importlib.import_module

    def spy(name, *a, **k):
        seen.append(name)
        return real(name, *a, **k)
    with tempfile.TemporaryDirectory() as d:
        marker = os.path.join(d, "ran")
        importlib.import_module = spy
        try:
            refused = []
            for ref, cfg in (("subprocess:Popen", {"args": ["touch", marker]}), ("os:system", {}),
                             ("json.decoder:JSONDecoder", {}), ("toolkit.alloc.costs:Nope", {})):
                try:
                    load_engine({"ref": ref, "config": cfg})
                    refused.append(False)
                except (SpecError, ValueError):
                    refused.append(True)
                except Exception:  # noqa: BLE001 -- imported and failed later: not refused up front
                    refused.append(False)
        finally:
            importlib.import_module = real
        bad = [m for m in seen if m.split(".")[0] in ("subprocess", "os", "json") or m.startswith("toolkit.alloc")]
        ok(all(refused) and not bad and not os.path.exists(marker),
           f"SP-RW1 subprocess:Popen, os:system and modules outside toolkit.engines are refused without an import "
           f"({refused}; imported {bad})")
        p = toy_spec(d)
        raw = yaml.safe_load(open(p))
        raw["engine"] = {"ref": "subprocess:Popen", "config": {"args": ["touch", marker]}}
        yaml.safe_dump(raw, open(p, "w"))
        probs = validate(load(p))
        ok(probs and not os.path.exists(marker) and any("engine" in x for x in probs),
           f"SP-RW2 validating a spec with such an engine reports it and runs nothing ({probs[:1]})")
        raw["engine"] = {"auto": True, "candidates": ["os:system", "toolkit.engines.contract:ToyEngine"]}
        yaml.safe_dump(raw, open(p, "w"))
        try:
            load(p)
            ok(False, "SP-RW3 an unregistered engine among the signed candidates is refused")
        except SpecError as e:
            ok("os:system" in str(e), f"SP-RW3 an unregistered engine among the signed candidates is refused ({e})")
    # SP-RW5/6 (round 3, P1-1): the binding lookup and the legacy spike loader are guarded too
    seen = []
    importlib.import_module = spy
    try:
        refused = []
        for fn in (lambda: binding_for_ref("os:system"), lambda: binding_for_ref("subprocess:Popen")):
            try:
                fn()
                refused.append(False)
            except (SpecError, ValueError):
                refused.append(True)
            except Exception:  # noqa: BLE001
                refused.append(False)
        try:
            from spike.analysis import spec as _spike
            _spike.load_engine("os:system")
            refused.append(False)
        except (SpecError, ValueError):
            refused.append(True)
        except Exception:  # noqa: BLE001
            refused.append(False)
    finally:
        importlib.import_module = real
    bad = [m for m in seen if m.split(".")[0] in ("subprocess", "os")]
    ok(all(refused) and not bad, f"SP-RW5 binding_for_ref and the spike loader refuse os:system and subprocess:Popen "
                                 f"before an import ({refused}; imported {bad})")
    try:
        k = engine_class("toolkit.engines.contract:ToyEngine")
        ok(k.__name__ == "ToyEngine" and load_engine("toolkit.engines.contract:ToyEngine") is not None,
           "SP-RW4 a registered engine still loads")
    except Exception as e:  # noqa: BLE001
        ok(False, f"SP-RW4 a registered engine still loads: {type(e).__name__}: {e}")


def _self_test_derived(ok) -> None:
    """DI1, DI7, DI11, DI12 and DI13 (docs/design/v2-derived-inputs.md §4)."""
    import tempfile
    sys.path.insert(0, os.path.join(ROOT, "scripts"))

    def has(errs, code, needle=""):
        return any(e.startswith(code) and needle in e for e in errs)

    with tempfile.TemporaryDirectory() as d:          # DI1: the units table written by the task, not derived
        p = toy_spec(d)
        errs = validate(load(p), derived=True)
        ok(has(errs, "units_not_derived"), f"DI1 a units table written by the task (no derivation) gives "
                                           f"units_not_derived: {errs[:3]}")
        ok(validate(load(p)) == [], "DI12a the same spec outside a v2 machine run (no derivation declared) validates "
                                    "unchanged")
    with tempfile.TemporaryDirectory() as d:
        p = derived_toy_spec(d)
        s = load(p)
        ok(validate(s) == [], f"DI7a the derived toy spec (one default accepted by name) validates: {validate(s)[:3]}")

        def errs_of(mut):
            r = copy.deepcopy(s.raw)
            mut(r)
            return validate(Spec(r, d, p))
        e = errs_of(lambda r: r["decision_units"].pop("defaults_accepted"))
        ok(has(e, "engine_attribute_unbound", "group"), f"DI7b an attribute with a card default, neither bound nor "
                                                         f"accepted, gives engine_attribute_unbound: {e[:3]}")
        e = errs_of(lambda r: r["calibration"].update(attribute="group"))
        ok(has(e, "engine_attribute_unbound", "x"), f"DI7c a required attribute left unbound gives "
                                                     f"engine_attribute_unbound: {e[:3]}")
        e = errs_of(lambda r: (r["calibration"].update(attribute="group"),
                               r["decision_units"]["defaults_accepted"].update(x="accept the engine's value")))
        ok(has(e, "engine_attribute_unbound", "x"), f"DI7d a required attribute cannot be accepted as a default: {e[:3]}")
        e = errs_of(lambda r: r["decision_units"]["engine_attributes"].update(group={"column": "lbl"}))
        ok(has(e, "attribute_column_empty", "lbl"), f"DI7e a bound column blank in a row gives attribute_column_empty: "
                                                     f"{e[:3]}")
        e = errs_of(lambda r: r["calibration"].update(target_frame={"band": "prev_young", "source": "a survey"}))
        ok(has(e, "target_frame_unmapped", "prev_young"), f"DI11a a target whose frame band differs from the engine "
                                                           f"output's, with no mapping, gives target_frame_unmapped: {e[:3]}")
        e = errs_of(lambda r: r["calibration"].update(target_frame={
            "band": "prev_young", "source": "a survey",
            "mapping": {"to": "prev", "method": "age-band conversion", "source": "a published conversion"}}))
        ok(not has(e, "target_frame_unmapped"), f"DI11b with a declared mapping it passes: {e[:3]}")
        e = errs_of(lambda r: r["calibration"].pop("target_frame"))
        ok(has(e, "target_frame_unmapped"), f"DI11c a target with no declared frame gives target_frame_unmapped: {e[:3]}")
        os.remove(os.path.join(d, "data", "derivations.yaml"))
        e = validate(load(p), derived=True)
        ok(has(e, "units_not_derived") or has(e, "data_ref_not_derived"),
           f"DI1b a spec citing data/derived/ with no declaration is refused: {e[:3]}")
    # DI12: contract.py and the engines' code are unchanged (the fingerprints still match the benchmark stamps).
    from toolkit.engines.contract import code_fingerprint
    for mod, cls, res in (("toolkit.engines.malaria.engine", "MalariaEngine", "toolkit/engines/malaria/benchmarks/results.yaml"),
                          ("toolkit.engines.compartmental.engine", "CompartmentalEngine",
                           "toolkit/engines/compartmental/benchmarks/results.yaml")):
        try:
            klass = getattr(importlib.import_module(mod), cls)
            stamp = (yaml.safe_load(open(os.path.join(ROOT, res))) or {})["engine_identity"]["code_sha256"]
            ok(code_fingerprint(klass.__new__(klass)) == stamp,
               f"DI12b {cls}: the engine code fingerprint (with contract.py) still matches its benchmark stamp")
        except ImportError as e:
            print(f"SKIP DI12b {cls}: {e}")
    try:
        import pipeline as P
        src = "".join(inspect_source(f) for f in (attribute_problems, frame_problems, load_binding))
        ok(not P.domain_terms(src), f"DI13 the spec checks name no disease, place or intervention: {P.domain_terms(src)}")
    except Exception as e:  # noqa: BLE001
        ok(False, f"DI13 spec checks: {type(e).__name__}: {e}")


def inspect_source(fn) -> str:
    import inspect
    return inspect.getsource(fn)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    if len(argv) != 1:
        print(__doc__)
        return 2
    errs = validate(load(argv[0]))
    print("spec valid" if not errs else "spec invalid:\n  " + "\n  ".join(errs))
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
