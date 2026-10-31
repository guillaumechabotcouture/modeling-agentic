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
    engine: {ref: "pkg.module:Class", config: {...}}       # constructor keyword arguments
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
    options:                               # toolkit/alloc sections (costs.py, packages.py)
      populations: {total: pop}            # alloc population names -> unit columns
      components: {c1: {group: g}, ...}
      packages: {generate: true} | {name: [components]}
      engine_map: {c1: {engine_component: value}}
      prices: {...}
      cost_items: [...]
    objective:
      outcome: dalys                       # an engine per-capita output
      sense: avert                         # benefit = comparator outcome - package outcome
      horizon: [2024.0, 2027.0]            # the decision window
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
      method: emulator_inversion | likelihood | none
      ...                                  # see CALIBRATION_FIELDS
    uncertainty:                           # spike lesson 6: sources declared up front
      level: 0.95
      draws: 64
      seed: 42
      sources: [{kind: engine_parameters}, {kind: calibration}, {kind: costs, items: {id: [0.8, 1.2]}}]
      excluded: [{kind: emulator, reason: "..."}]
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
"""
from __future__ import annotations

import copy
import csv
import hashlib
import importlib
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.alloc import costs as acosts, packages as apackages  # noqa: E402
from toolkit.engines.contract import Engine, Horizon  # noqa: E402

SCHEMA_VERSION = 1
REQUIRED = ("schema_version", "question", "decision_maker", "decision_date", "engine", "horizon", "decision_units",
            "options", "objective", "constraints", "comparator", "outcomes_reported", "calibration", "uncertainty",
            "analysis", "deliverable")
CALIBRATION_METHODS = ("emulator_inversion", "likelihood", "none")
EVALUATIONS = ("emulator", "direct")
# Spike lesson 6: every kind is either a declared source or excluded with a reason.
UNCERTAINTY_KINDS = {
    "engine_parameters": "fitted engine parameters, uniform within their engine-card ranges",
    "calibration": "calibration (target error for emulator inversion; parameter covariance for likelihood)",
    "costs": "unit costs (a multiplier range per cost item)",
    "emulator": "emulator error (held-out residuals)",
}
UNCERTAINTY_SHORT = {"engine_parameters": "engine parameters", "calibration": "calibration", "costs": "unit costs",
                     "emulator": "emulator error"}
DECISION_TEST_TYPES = ("interaction_rescale", "parameter_value", "group_rule", "envelope")
DELIVERABLE_KINDS = ("package_per_unit",)
CALIBRATION_FIELDS = {
    "emulator_inversion": ("attribute", "range", "target_column", "window", "metric"),
    "likelihood": ("attribute", "range", "parameters", "targets"),
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


def load(path: str) -> "Spec":
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
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
        for path, v in _leaves(st):
            name = ".".join(map(str, path))
            if path not in SETTINGS_PATHS:
                raise SpecError(f"{SETTINGS}: {name} is not a computational setting "
                                f"({', '.join('.'.join(x) for x in sorted(SETTINGS_PATHS))}); it belongs in the signed "
                                "decision spec")
            node = raw
            for k in path[:-1]:
                node = node.setdefault(k, {})
                if not isinstance(node, dict):
                    raise SpecError(f"{SETTINGS}: {name}: the signed spec's {k} is not a mapping")
            if path[-1] in node:
                raise SpecError(f"{SETTINGS}: {name} is also in the signed spec; keep it in one place")
            node[path[-1]] = v
        settings_path = sp
    s = Spec(raw, base, path)
    s.settings_path = settings_path
    return s


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

    # ---- engine
    def engine(self):
        if self._engine is None:
            self._engine = load_engine(self.raw["engine"])
        return self._engine

    def horizon(self, windows: dict | None = None) -> Horizon:
        h, o = self.raw["horizon"], self.raw["objective"]
        return Horizon(float(h["start"]), float(h["stop"]), tuple(float(x) for x in o["horizon"]), dict(windows or {}))

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
        inc = [s["kind"] for s in u.get("sources") or []]
        exc = [(e["kind"], e.get("reason", "")) for e in u.get("excluded") or []]
        lvl = float(u.get("level", 0.95))
        meta = {"level": lvl, "covers": inc, "excludes": [k for k, _ in exc]}
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import render_claims    # one rule for the label, from structured metadata only (Codex P1-5)
        return {"level": lvl, "covers": inc, "excludes": [{"kind": k, "reason": r} for k, r in exc],
                "interval": meta, "label": render_claims.interval_label(meta)}


def load_engine(ref):
    if isinstance(ref, str):
        ref = {"ref": ref}
    mod, cls = str(ref["ref"]).split(":")
    return getattr(importlib.import_module(mod), cls)(**(ref.get("config") or {}))


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# --------------------------------------------------------------------------- validation
def validate(spec: "Spec | dict", base: str | None = None) -> list[str]:
    """Problems with the spec (empty = valid). Never raises on a malformed spec."""
    if isinstance(spec, dict):
        spec = Spec(spec, base or os.getcwd())
    try:
        return _validate(spec)
    except Exception as e:  # noqa: BLE001 -- a malformed section must be a finding, not a crash
        return [f"spec could not be checked: {type(e).__name__}: {e}"]


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
    bad += _validate_analysis(s, card, pnames, cols, pk)
    bad += _validate_deliverable(s)
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
    return bad


def _validate_uncertainty(s, pnames) -> list[str]:
    u = s.raw["uncertainty"]
    if not isinstance(u, dict):
        return ["uncertainty: a mapping"]
    bad = []
    inc = [x.get("kind") for x in u.get("sources") or [] if isinstance(x, dict)]
    exc = {x.get("kind"): x.get("reason") for x in u.get("excluded") or [] if isinstance(x, dict)}
    for k in inc + list(exc):
        if k not in UNCERTAINTY_KINDS:
            bad.append(f"uncertainty: unknown source kind {k!r} (known: {', '.join(UNCERTAINTY_KINDS)})")
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
        bad(lambda r: r["objective"].update(outcome="nope"), "per-capita output", "SP12 an unknown objective outcome")
        bad(lambda r: r.pop("deliverable"), "missing section deliverable", "SP13 a missing section is refused")
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
        ok(sc["label"] == "95% UI over engine parameters, calibration; not covered: costs, emulator", f"SP17 the interval label states what is covered and what is not: {sc['label']}")
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
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


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
