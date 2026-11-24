#!/usr/bin/env python3
"""RequestedEngine stand-in and build-request validation (docs/design/agent-built-engines.md §2.1).

    python3 toolkit/engines/built/requested.py --self-test

Before an agent-built engine is written or admitted, the owner signs the spec containing
`engine.build`. `schema.validate` validates every engine-dependent section (unit attributes,
options, engine_map, calibration, priors, decision inputs) against `RequestedEngine`, a
harness stand-in built solely from the signed request. Calling `run()` raises
`EngineNotBuilt`. Nothing here names a disease.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from toolkit.engines.contract import EngineCard, KINDS, Outcomes, Package, ParamSpec, Horizon, UnitSpec  # noqa: E402

ID_RE = re.compile(r"^[a-z0-9_]{3,24}$")
BUILD_KINDS = ("starsim_module", "closed_form")
CONSERVATION_MODES = ("closed", "births_deaths", "open_with_flows")
ATTR_TYPES = ("continuous", "integer", "categorical", "vector")
EFFECT_SIGNS = ("+", "-", "nonzero")
PARAM_ROLES = ("observation", "cost")
RESERVED_CONFIG_KEYS = frozenset({"run_dir", "run_engine", "snapshot_sha256", "admission_sha256", "config_sha256"})
MAX_REFERENCE_REL_TOL = 0.05
OPEN_ENDPOINTS = frozenset({"birth", "death", "external"})

# Oracle library signatures validated at SIGN (PR gamma implements the wrappers in toolkit/engines/built/oracles.py).
KNOWN_ORACLES: dict[str, tuple[str, ...]] = {
    "final_size_lambert": ("r0",),
    "herd_immunity": ("r0",),
    "wallinga_lipsitch": ("r0", "gi"),
    "branching_single": ("r0",),
    "seir_growth_r0": ("r", "latent_days", "infectious_days"),
    "final_size_multigroup": ("r0", "contact", "s0"),
    "ode_final_size": ("r0", "s0"),
    "cohort_susceptible": ("coverage", "seroconversion"),
    "sis_endemic": ("r0",),
    "stable_age_dist": ("mortality",),
    "constant_hazard_survival": ("hazard", "dt"),
    "seir_multigroup_ode": ("r0", "contact", "s0"),
}


class EngineNotBuilt(RuntimeError):
    """Raised when code attempts to execute a RequestedEngine stand-in before build and admission."""


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _check_interval(rng, label: str) -> tuple[tuple[float, float] | None, list[str]]:
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2 and _is_num(rng[0]) and _is_num(rng[1])
            and float(rng[0]) < float(rng[1])):
        return None, [f"{label}: must be [lo, hi] with finite lo < hi"]
    return (float(rng[0]), float(rng[1])), []


def _output_path_declared(path: str, per_cap: set[str], windows: dict[str, set[str]], drivers: set[str]) -> bool:
    parts = str(path).split(".")
    if len(parts) >= 2 and parts[0] == "per_capita":
        return parts[1] in per_cap
    if len(parts) == 3 and parts[0] == "windows":
        return parts[1] in windows and parts[2] in windows[parts[1]]
    if len(parts) == 2 and parts[0] == "cost_drivers":
        return parts[1] in drivers
    return False


def validate_build(build: Mapping) -> list[str]:
    """Validate `engine.build` at SPEC/SIGN (fail closed). Returns a list of problems (empty = valid)."""
    from toolkit.engines import benchmark as BM
    if not isinstance(build, Mapping):
        return ["engine.build: must be a mapping"]
    bad: list[str] = []
    bid = build.get("id")
    if not isinstance(bid, str) or not ID_RE.fullmatch(bid):
        bad.append(f"engine.build.id {bid!r}: must match {ID_RE.pattern}")
    kind = build.get("kind")
    if kind not in BUILD_KINDS:
        bad.append(f"engine.build.kind {kind!r}: must be one of {list(BUILD_KINDS)}")
    if not (isinstance(build.get("why"), str) and build["why"].strip()):
        bad.append("engine.build.why: non-empty justification required")
    mechs = build.get("mechanisms")
    if not (isinstance(mechs, list) and mechs and all(isinstance(m, str) and m.strip() for m in mechs)):
        bad.append("engine.build.mechanisms: non-empty list of strings required")
    req = build.get("request")
    if not isinstance(req, Mapping):
        return bad + ["engine.build.request: must be a mapping"]

    # Components
    comps_raw = req.get("components")
    comp_vals: dict[str, list] = {}
    if not isinstance(comps_raw, Mapping) or not comps_raw:
        bad.append("engine.build.request.components: non-empty mapping required")
    else:
        for cname, cspec in comps_raw.items():
            if not isinstance(cspec, Mapping):
                bad.append(f"engine.build.request.components.{cname}: must be {{values, absent}}")
                continue
            vals = cspec.get("values")
            if not isinstance(vals, list) or not vals:
                bad.append(f"engine.build.request.components.{cname}.values: non-empty list required")
                continue
            if "absent" not in cspec or cspec["absent"] not in vals:
                bad.append(f"engine.build.request.components.{cname}.absent: must be one of values {vals!r}")
            comp_vals[str(cname)] = list(vals)

    # Unit attributes
    attrs_raw = req.get("unit_attributes")
    attr_ranges: dict[str, tuple[float, float]] = {}
    attr_types: dict[str, str] = {}
    attr_cats: dict[str, list] = {}
    if not isinstance(attrs_raw, Mapping) or not attrs_raw:
        bad.append("engine.build.request.unit_attributes: non-empty mapping required")
    else:
        for aname, aspec in attrs_raw.items():
            an = str(aname)
            if not isinstance(aspec, Mapping):
                bad.append(f"engine.build.request.unit_attributes.{an}: must be a mapping")
                continue
            at = aspec.get("type")
            if at not in ATTR_TYPES:
                bad.append(f"engine.build.request.unit_attributes.{an}.type {at!r}: must be one of {list(ATTR_TYPES)}")
                continue
            attr_types[an] = at
            if at in ("continuous", "integer"):
                rng = aspec.get("range") if "range" in aspec else aspec.get("test_range")
                if rng is None:
                    bad.append(f"engine.build.request.unit_attributes.{an}: continuous/integer needs range or test_range")
                else:
                    iv, errs = _check_interval(rng, f"engine.build.request.unit_attributes.{an}")
                    bad += errs
                    if iv:
                        attr_ranges[an] = iv
            elif at == "categorical":
                vs = aspec.get("values")
                if not isinstance(vs, list) or not vs:
                    bad.append(f"engine.build.request.unit_attributes.{an}.values: non-empty list required")
                else:
                    attr_cats[an] = list(vs)
            elif at == "vector":
                ln = aspec.get("length")
                if not isinstance(ln, int) or isinstance(ln, bool) or ln < 1:
                    bad.append(f"engine.build.request.unit_attributes.{an}.length: positive integer required")

    # Outputs & age groups
    outs_raw = req.get("outputs")
    per_cap: list[str] = []
    windows_map: dict[str, set[str]] = {}
    cost_drivers: list[str] = []
    if not isinstance(outs_raw, Mapping):
        bad.append("engine.build.request.outputs: must be a mapping")
    else:
        pc = outs_raw.get("per_capita")
        if not (isinstance(pc, list) and pc and all(isinstance(x, str) and x.strip() for x in pc)):
            bad.append("engine.build.request.outputs.per_capita: non-empty list of strings required")
        else:
            per_cap = list(pc)
        wm = outs_raw.get("windows") or {}
        if not isinstance(wm, Mapping):
            bad.append("engine.build.request.outputs.windows: must be a mapping")
        else:
            for wname, metrics in wm.items():
                if not (isinstance(metrics, list) and metrics and all(isinstance(m, str) and m.strip() for m in metrics)):
                    bad.append(f"engine.build.request.outputs.windows.{wname}: non-empty list of metric names required")
                else:
                    windows_map[str(wname)] = set(metrics)
        cd = outs_raw.get("cost_drivers") or []
        if not (isinstance(cd, list) and all(isinstance(x, str) and x.strip() for x in cd)):
            bad.append("engine.build.request.outputs.cost_drivers: must be a list of strings")
        else:
            cost_drivers = list(cd)
    ag = req.get("age_groups")
    if not (isinstance(ag, list) and ag and all(isinstance(g, str) and g.strip() for g in ag)):
        bad.append("engine.build.request.age_groups: non-empty list of strings required")

    # Parameters
    params_raw = req.get("parameters")
    param_specs: dict[str, dict] = {}
    if not isinstance(params_raw, list):
        bad.append("engine.build.request.parameters: must be a list")
    else:
        for idx, p in enumerate(params_raw):
            if not isinstance(p, Mapping):
                bad.append(f"engine.build.request.parameters[{idx}]: must be a mapping")
                continue
            pn = p.get("name")
            if not isinstance(pn, str) or not pn.strip():
                bad.append(f"engine.build.request.parameters[{idx}].name: non-empty string required")
                continue
            if pn in param_specs:
                bad.append(f"engine.build.request.parameters: duplicate name {pn!r}")
                continue
            pk = p.get("kind")
            if pk not in KINDS:
                bad.append(f"engine.build.request.parameters.{pn}.kind {pk!r}: must be one of {list(KINDS)}")
            if not _is_num(p.get("value")):
                bad.append(f"engine.build.request.parameters.{pn}.value: finite number required")
            lo, hi = p.get("lo"), p.get("hi")
            if pk in ("fitted", "calibrated") and (not _is_num(lo) or not _is_num(hi)):
                bad.append(f"engine.build.request.parameters.{pn}: kind {pk} requires finite lo and hi")
            if (lo is not None or hi is not None):
                if not (_is_num(lo) and _is_num(hi) and float(lo) < float(hi)):
                    bad.append(f"engine.build.request.parameters.{pn}: lo < hi finite required")
                elif _is_num(p.get("value")) and not (float(lo) <= float(p["value"]) <= float(hi)):
                    bad.append(f"engine.build.request.parameters.{pn}: value {p['value']} outside [{lo}, {hi}]")
            if not (isinstance(p.get("source"), str) and p["source"].strip()):
                bad.append(f"engine.build.request.parameters.{pn}.source: dossier id or 'assumption' required")
            param_specs[pn] = dict(p)

    # Attribute binding
    ab_raw = req.get("attribute_binding")
    if not isinstance(ab_raw, Mapping):
        bad.append("engine.build.request.attribute_binding: must be a mapping")
    else:
        for an in attr_types:
            if an not in ab_raw:
                bad.append(f"engine.build.request.attribute_binding: missing unit attribute {an!r}")
        for an, rule in ab_raw.items():
            if rule != "required" and not (isinstance(rule, Mapping) and "default" in rule
                                           and isinstance(rule.get("meaning"), str) and rule["meaning"].strip()):
                bad.append(f"engine.build.request.attribute_binding.{an}: must be 'required' or {{default, meaning}}")

    # Decision inputs
    di_raw = req.get("decision_inputs")
    if not isinstance(di_raw, Mapping):
        bad.append("engine.build.request.decision_inputs: must be a mapping")
    else:
        for cname in comp_vals:
            if cname not in di_raw:
                bad.append(f"engine.build.request.decision_inputs: missing component {cname!r}")
        for cname, items in di_raw.items():
            if not isinstance(items, list):
                bad.append(f"engine.build.request.decision_inputs.{cname}: must be a list")
                continue
            for j, item in enumerate(items):
                if not isinstance(item, Mapping):
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}]: must be a mapping")
                    continue
                ik, iname, iwhy = item.get("kind"), item.get("name"), item.get("why")
                if ik not in ("parameter", "unit_attribute"):
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}].kind {ik!r}: parameter or unit_attribute")
                elif ik == "parameter" and iname not in param_specs:
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}]: unknown parameter {iname!r}")
                elif ik == "unit_attribute" and iname not in attr_types:
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}]: unknown unit_attribute {iname!r}")
                if not (isinstance(iwhy, str) and iwhy.strip()):
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}].why: non-empty text required")

    # Inert & parameter_roles
    all_inputs = set(param_specs) | set(comp_vals) | set(attr_types)
    inert_raw = req.get("inert") or {}
    if not isinstance(inert_raw, Mapping):
        bad.append("engine.build.request.inert: must be a mapping")
        inert_raw = {}
    else:
        for iname, ispec in inert_raw.items():
            if iname not in all_inputs:
                bad.append(f"engine.build.request.inert.{iname}: unknown input")
            if not (isinstance(ispec, Mapping) and isinstance(ispec.get("reason"), str) and ispec["reason"].strip()):
                bad.append(f"engine.build.request.inert.{iname}: requires non-empty reason")
    roles_raw = req.get("parameter_roles") or {}
    if not isinstance(roles_raw, Mapping):
        bad.append("engine.build.request.parameter_roles: must be a mapping")
    else:
        for pn, role in roles_raw.items():
            if pn not in param_specs:
                bad.append(f"engine.build.request.parameter_roles.{pn}: unknown parameter")
            elif role not in PARAM_ROLES:
                bad.append(f"engine.build.request.parameter_roles.{pn}: role {role!r} not in {list(PARAM_ROLES)}")

    # Expected effects
    ee_raw = req.get("expected_effects")
    valid_effect_outs = (set(per_cap) | set(cost_drivers)
                         | {m for ms in windows_map.values() for m in ms}
                         | {f"windows.{w}.{m}" for w, ms in windows_map.items() for m in ms})
    if not isinstance(ee_raw, Mapping):
        bad.append("engine.build.request.expected_effects: must be a mapping")
    else:
        for iname in sorted(all_inputs):
            if iname not in ee_raw and iname not in inert_raw:
                bad.append(f"engine.build.request.expected_effects: missing input {iname!r} (declare effects or inert)")
        for iname, outs in ee_raw.items():
            if iname not in all_inputs:
                bad.append(f"engine.build.request.expected_effects.{iname}: unknown input")
            if not isinstance(outs, Mapping) or not outs:
                bad.append(f"engine.build.request.expected_effects.{iname}: non-empty {{output: sign}} mapping required")
                continue
            for oname, sign in outs.items():
                if oname not in valid_effect_outs:
                    bad.append(f"engine.build.request.expected_effects.{iname}.{oname}: undeclared output")
                if sign not in EFFECT_SIGNS:
                    bad.append(f"engine.build.request.expected_effects.{iname}.{oname}: sign {sign!r} not in {list(EFFECT_SIGNS)}")

    # Compartments (required for starsim_module)
    cmps = req.get("compartments")
    if kind == "starsim_module" and cmps is None:
        bad.append("engine.build.request.compartments: required for kind starsim_module")
    if cmps is not None:
        if not isinstance(cmps, Mapping):
            bad.append("engine.build.request.compartments: must be a mapping")
        else:
            states = cmps.get("states")
            if not (isinstance(states, list) and states and all(isinstance(s, str) and s.strip() for s in states)):
                bad.append("engine.build.request.compartments.states: non-empty list of strings required")
                state_set: set[str] = set()
            else:
                state_set = set(states)
                if len(state_set) != len(states):
                    bad.append("engine.build.request.compartments.states: duplicate state names")
            cons = cmps.get("conservation")
            if cons not in CONSERVATION_MODES:
                bad.append(f"engine.build.request.compartments.conservation {cons!r}: must be one of {list(CONSERVATION_MODES)}")
            allowed_nodes = state_set | (OPEN_ENDPOINTS if cons != "closed" else set())
            trans = cmps.get("transitions")
            counted: set[str] = set()
            if not isinstance(trans, list) or not trans:
                bad.append("engine.build.request.compartments.transitions: non-empty list required")
            else:
                for idx, tr in enumerate(trans):
                    if not isinstance(tr, Mapping):
                        bad.append(f"engine.build.request.compartments.transitions[{idx}]: must be a mapping")
                        continue
                    if tr.get("from") not in allowed_nodes:
                        bad.append(f"engine.build.request.compartments.transitions[{idx}].from {tr.get('from')!r}: unknown state")
                    if tr.get("to") not in allowed_nodes:
                        bad.append(f"engine.build.request.compartments.transitions[{idx}].to {tr.get('to')!r}: unknown state")
                    cnts = tr.get("counts") or []
                    if not isinstance(cnts, list):
                        bad.append(f"engine.build.request.compartments.transitions[{idx}].counts: must be a list")
                    else:
                        for c in cnts:
                            if c not in per_cap:
                                bad.append(f"engine.build.request.compartments.transitions[{idx}].counts: undeclared output {c!r}")
                            else:
                                counted.add(c)
            for out_name in per_cap:
                if out_name not in counted:
                    bad.append(f"engine.build.request.compartments: per_capita output {out_name!r} is not counted by any transition")

    # Reductions
    reds = req.get("reductions") or []
    if not isinstance(reds, list):
        bad.append("engine.build.request.reductions: must be a list")
    else:
        seen_r: set[str] = set()
        per_cap_set, driver_set = set(per_cap), set(cost_drivers)
        for idx, red in enumerate(reds):
            if not isinstance(red, Mapping):
                bad.append(f"engine.build.request.reductions[{idx}]: must be a mapping")
                continue
            rid = red.get("id")
            if not isinstance(rid, str) or not rid.strip() or rid in seen_r:
                bad.append(f"engine.build.request.reductions[{idx}].id {rid!r}: unique non-empty string required")
            else:
                seen_r.add(rid)
            orc = red.get("oracle")
            if orc not in KNOWN_ORACLES:
                bad.append(f"engine.build.request.reductions.{rid}: unknown oracle {orc!r}")
            else:
                oargs = red.get("oracle_args")
                if not isinstance(oargs, Mapping):
                    bad.append(f"engine.build.request.reductions.{rid}.oracle_args: mapping required")
                else:
                    miss = [a for a in KNOWN_ORACLES[orc] if a not in oargs]
                    if miss:
                        bad.append(f"engine.build.request.reductions.{rid}.oracle_args: missing {miss}")
            rset = red.get("set") or {}
            if not isinstance(rset, Mapping):
                bad.append(f"engine.build.request.reductions.{rid}.set: must be a mapping")
            else:
                for k, v in rset.items():
                    if k in param_specs:
                        ps = param_specs[k]
                        dom = ps.get("domain") if isinstance(ps.get("domain"), Mapping) else {}
                        if "values" in dom and v not in dom["values"]:
                            bad.append(f"engine.build.request.reductions.{rid}.set.{k}: {v!r} not in domain values {dom['values']!r}")
                        elif _is_num(ps.get("lo")) and _is_num(ps.get("hi")) and (not _is_num(v) or not (float(ps["lo"]) <= float(v) <= float(ps["hi"]))):
                            bad.append(f"engine.build.request.reductions.{rid}.set.{k}: {v!r} outside [{ps['lo']}, {ps['hi']}]")
                    elif k in comp_vals:
                        if v not in comp_vals[k]:
                            bad.append(f"engine.build.request.reductions.{rid}.set.{k}: {v!r} not in component values {comp_vals[k]!r}")
                    else:
                        bad.append(f"engine.build.request.reductions.{rid}.set.{k}: unknown parameter or component")
            runit = red.get("unit") or {}
            if not isinstance(runit, Mapping):
                bad.append(f"engine.build.request.reductions.{rid}.unit: must be a mapping")
            else:
                for k, v in runit.items():
                    if k not in attr_types:
                        bad.append(f"engine.build.request.reductions.{rid}.unit.{k}: unknown unit attribute")
                    elif k in attr_ranges and _is_num(v) and not (attr_ranges[k][0] <= float(v) <= attr_ranges[k][1]):
                        bad.append(f"engine.build.request.reductions.{rid}.unit.{k}: {v!r} outside {list(attr_ranges[k])}")
                    elif k in attr_cats and v not in attr_cats[k]:
                        bad.append(f"engine.build.request.reductions.{rid}.unit.{k}: {v!r} not in {attr_cats[k]!r}")
            comp_map = red.get("compare")
            if not isinstance(comp_map, Mapping) or not comp_map:
                bad.append(f"engine.build.request.reductions.{rid}.compare: non-empty mapping required")
            else:
                for path, tol in comp_map.items():
                    if not BM.value_path_ok(path) or not _output_path_declared(path, per_cap_set, windows_map, driver_set):
                        bad.append(f"engine.build.request.reductions.{rid}.compare.{path}: invalid or undeclared output path")
                    if not isinstance(tol, Mapping) or not any(k in tol and _is_num(tol[k]) and float(tol[k]) > 0 for k in ("abs", "rel")):
                        bad.append(f"engine.build.request.reductions.{rid}.compare.{path}: positive abs or rel tolerance required")

    # Trials
    trials = req.get("trials") or []
    if not isinstance(trials, list):
        bad.append("engine.build.request.trials: must be a list")
    else:
        seen_t: set[str] = set()
        for idx, tr in enumerate(trials):
            if not isinstance(tr, Mapping):
                bad.append(f"engine.build.request.trials[{idx}]: must be a mapping")
                continue
            tid = tr.get("id")
            if not isinstance(tid, str) or not tid.strip() or tid in seen_t:
                bad.append(f"engine.build.request.trials[{idx}].id {tid!r}: unique non-empty string required")
            else:
                seen_t.add(tid)
            if not (isinstance(tr.get("dossier"), str) and tr["dossier"].strip()):
                bad.append(f"engine.build.request.trials.{tid}.dossier: non-empty dossier entry id required")
            if tr.get("component") not in comp_vals:
                bad.append(f"engine.build.request.trials.{tid}.component {tr.get('component')!r}: unknown component")
            if not (isinstance(tr.get("arms"), Mapping) and len(tr["arms"]) >= 2):
                bad.append(f"engine.build.request.trials.{tid}.arms: mapping with at least 2 arms required")
            if not isinstance(tr.get("setting"), Mapping):
                bad.append(f"engine.build.request.trials.{tid}.setting: mapping required")
            _, werr = _check_interval(tr.get("window"), f"engine.build.request.trials.{tid}.window")
            bad += werr
            if tr.get("estimand_type") not in BM.TYPES:
                bad.append(f"engine.build.request.trials.{tid}.estimand_type {tr.get('estimand_type')!r}: not in {list(BM.TYPES)}")
            if not (isinstance(tr.get("estimand_mapping"), Mapping) and tr["estimand_mapping"]):
                bad.append(f"engine.build.request.trials.{tid}.estimand_mapping: non-empty mapping required")
            pub = tr.get("published")
            if not isinstance(pub, Mapping):
                bad.append(f"engine.build.request.trials.{tid}.published: {{lower, upper, level}} required")
            else:
                if not (_is_num(pub.get("lower")) and _is_num(pub.get("upper")) and float(pub["lower"]) < float(pub["upper"])):
                    bad.append(f"engine.build.request.trials.{tid}.published: finite lower < upper required")
                if not (_is_num(pub.get("level")) and 0.0 < float(pub["level"]) < 1.0):
                    bad.append(f"engine.build.request.trials.{tid}.published.level: must be in (0, 1)")

    # Reference tolerance & configs
    rtol = req.get("reference_tolerance")
    if rtol is not None:
        if not isinstance(rtol, Mapping) or not any(k in rtol and _is_num(rtol[k]) and float(rtol[k]) > 0 for k in ("rel", "abs")):
            bad.append("engine.build.request.reference_tolerance: positive rel or abs required")
        elif "rel" in rtol and _is_num(rtol["rel"]) and float(rtol["rel"]) > MAX_REFERENCE_REL_TOL:
            bad.append(f"engine.build.request.reference_tolerance.rel {rtol['rel']} exceeds harness ceiling {MAX_REFERENCE_REL_TOL}")
    cfgs = req.get("configs")
    if cfgs is not None:
        if not isinstance(cfgs, Mapping):
            bad.append("engine.build.request.configs: must be a mapping")
        else:
            for cname, cmap in cfgs.items():
                if not isinstance(cmap, Mapping):
                    bad.append(f"engine.build.request.configs.{cname}: must be a mapping")
                else:
                    res_hits = sorted(RESERVED_CONFIG_KEYS & set(cmap))
                    if res_hits:
                        bad.append(f"engine.build.request.configs.{cname}: reserved harness keywords refused: {res_hits}")
    return bad


def request_sha256(build: Mapping) -> str:
    """Canonical sha256 over the signed engine.build declaration."""
    return hashlib.sha256(json.dumps(build, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class RequestedEngine:
    """Harness stand-in Engine built solely from a signed `engine.build` request (never runs)."""

    def __init__(self, build: Mapping):
        probs = validate_build(build)
        if probs:
            raise ValueError("invalid engine.build request: " + "; ".join(probs[:6]))
        self.build = dict(build)
        self.request = dict(build["request"])
        self.id = str(build["id"])
        self.kind = str(build["kind"])

    def describe(self) -> EngineCard:
        r = self.request
        comps = {str(k): list(v["values"]) for k, v in r["components"].items()}
        uattrs = {str(k): str(v.get("meaning") or v.get("type") or "") for k, v in r["unit_attributes"].items()}
        vr = {}
        for k, v in r["unit_attributes"].items():
            rng = v.get("range") if "range" in v else v.get("test_range")
            if isinstance(rng, (list, tuple)) and len(rng) == 2:
                vr[str(k)] = [float(rng[0]), float(rng[1])]
        for p in r.get("parameters") or []:
            if p.get("lo") is not None and p.get("hi") is not None:
                vr[str(p["name"])] = [float(p["lo"]), float(p["hi"])]
        outs = r["outputs"]
        win_metrics = sorted({m for ms in (outs.get("windows") or {}).values() for m in ms})
        card_outs = {"per_capita": list(outs["per_capita"]), "windows": win_metrics,
                     "cost_drivers": list(outs.get("cost_drivers") or [])}
        fitted = {str(p["name"]): {"value": float(p["value"]), "lo": float(p["lo"]), "hi": float(p["hi"]),
                                   "benchmarks": []}
                  for p in r.get("parameters") or [] if p.get("kind") == "fitted"}
        return EngineCard(
            name=f"built:{self.id}", version="requested", mechanisms=list(self.build["mechanisms"]),
            components=comps, unit_attributes=uattrs, outputs=card_outs,
            age_groups=list(r["age_groups"]), valid_ranges=vr, fitted=fitted, benchmarks=[],
            notes=[f"requested agent-built engine ({self.kind}); not yet built or admitted"],
        )

    def parameters(self) -> list[ParamSpec]:
        out = []
        for p in self.request.get("parameters") or []:
            out.append(ParamSpec(
                name=str(p["name"]), value=float(p["value"]), kind=str(p["kind"]),
                units=str(p.get("units") or ""),
                lo=float(p["lo"]) if p.get("lo") is not None else None,
                hi=float(p["hi"]) if p.get("hi") is not None else None,
                source=str(p["source"]),
            ))
        return out

    def attribute_binding(self) -> dict:
        return dict(self.request.get("attribute_binding") or {})

    def decision_inputs_declaration(self) -> dict:
        """Decision-input declaration in toolkit/engines/decision_inputs.py shape."""
        out = {}
        comps = self.request.get("components") or {}
        di = self.request.get("decision_inputs") or {}
        for cname, cspec in comps.items():
            out[str(cname)] = {
                "inactive": [cspec["absent"]],
                "inputs": [dict(i) for i in di.get(cname) or []],
            }
        return out

    def check_plan_summary(self) -> dict:
        """Summary of generated admission checks and coverage gaps for the pre-sign sheet."""
        r = self.request
        n_inputs = len(r.get("parameters") or []) + len(r.get("components") or {}) + len(r.get("unit_attributes") or {})
        reds = r.get("reductions") or []
        trials = r.get("trials") or []
        reached = set()
        for red in reds:
            for path in (red.get("compare") or {}):
                parts = str(path).split(".")
                if len(parts) >= 2 and parts[0] == "per_capita":
                    reached.add(parts[1])
        uncovered_outputs = [o for o in r["outputs"]["per_capita"] if o not in reached]
        trial_comps = {tr.get("component") for tr in trials}
        uncovered_components = [c for c in r["components"] if c not in trial_comps]
        checks = {
            "A1_conformance": len(r["components"]) + 4,
            "A2_determinism": 9,
            "A4_reductions": len(reds) * 3,
            "A5_mass_balance": 6 if self.kind == "starsim_module" else 0,
            "A6_A7_wiring": n_inputs * 12,
            "A8_trials": len(trials),
            "A11_reference": 1,
        }
        return {"checks": checks, "uncovered_outputs": uncovered_outputs,
                "uncovered_components": uncovered_components}

    def run(self, params: dict, unit: UnitSpec, package: Package, horizon: Horizon, seed: int) -> Outcomes:
        raise EngineNotBuilt(f"requested engine {self.id!r} has not been built and admitted")


def sheet_lines(build: Mapping, full_registry_rows: list[dict] | None = None) -> list[str]:
    """Render the 'Engine to be built' section of decision_spec.signoff_sheet.md."""
    if not isinstance(build, Mapping):
        return []
    lines = ["## Engine to be built", ""]
    probs = validate_build(build)
    if probs:
        lines.append("Build request has validation errors: " + "; ".join(probs[:5]))
        return lines + [""]
    eng = RequestedEngine(build)
    card = eng.describe()
    plan = eng.check_plan_summary()
    lines += [
        f"- **Engine id:** `{eng.id}` (`{eng.kind}`, request sha256 `{request_sha256(build)[:16]}`)",
        f"- **Justification:** {build['why']}",
        f"- **Mechanisms:** {', '.join(card.mechanisms)}",
        f"- **Components:** {', '.join(f'{k} {v}' for k, v in card.components.items())}",
        f"- **Per-capita outputs:** {', '.join(card.outputs['per_capita'])}",
        "",
        "| Parameter | Kind | Value | Range | Source |",
        "|---|---|---|---|---|",
    ]
    for p in eng.parameters():
        rng = f"[{p.lo}, {p.hi}]" if p.lo is not None and p.hi is not None else "-"
        lines.append(f"| `{p.name}` | {p.kind} | {p.value} | {rng} | {p.source} |")
    lines += ["", "### Generated check plan", "", "| Check | Planned cases |", "|---|---|"]
    for ck, n in plan["checks"].items():
        lines.append(f"| {ck} | {n} |")
    lines += [
        "",
        "- **Per-capita outputs not reached by any reduction:** "
        + (", ".join(f"`{o}`" for o in plan["uncovered_outputs"]) or "none"),
        "- **Components with no trial declaration:** "
        + (", ".join(f"`{c}`" for c in plan["uncovered_components"]) or "none"),
    ]
    if full_registry_rows:
        lines += ["", "### Full-registry eligibility check", ""]
        for r in full_registry_rows:
            st = "eligible" if r.get("eligible") else "ineligible: " + "; ".join(str(w) for w in (r.get("why_not") or [])[:2])
            lines.append(f"- `{r.get('ref')}`: {st}")
    return lines + [""]


def example_build() -> dict:
    """Minimal valid disease-agnostic build request for unit tests."""
    return {
        "id": "two_stage_mod",
        "kind": "starsim_module",
        "why": "no registered card implements stage-dependent secondary severity with multi-strain cross-immunity",
        "mechanisms": ["multi-strain cross-immunity", "stage-dependent secondary severity"],
        "request": {
            "components": {"comp_a": {"values": [0, 1], "absent": 0}},
            "unit_attributes": {
                "N": {"type": "continuous", "test_range": [1.0e4, 1.0e6], "meaning": "unit population"},
                "rate_in": {"type": "continuous", "range": [0.01, 0.5], "meaning": "baseline hazard rate"},
            },
            "outputs": {
                "per_capita": ["cases", "severe"],
                "windows": {"w0": ["prev"]},
                "cost_drivers": ["units_delivered"],
            },
            "age_groups": ["all"],
            "parameters": [
                {"name": "eff_a", "kind": "fitted", "value": 0.6, "lo": 0.2, "hi": 0.9,
                 "units": "fraction", "source": "dossier:E1"},
                {"name": "sev_mult", "kind": "fitted", "value": 4.0, "lo": 1.5, "hi": 10.0,
                 "units": "ratio", "source": "dossier:S1"},
                {"name": "n_strains", "kind": "fixed", "value": 4, "domain": {"type": "integer", "values": [1, 4]},
                 "units": "count", "source": "assumption"},
            ],
            "attribute_binding": {"N": "required", "rate_in": "required"},
            "decision_inputs": {
                "comp_a": [{"kind": "parameter", "name": "eff_a", "why": "sets protective effect of comp_a"}],
            },
            "expected_effects": {
                "comp_a": {"cases": "-", "units_delivered": "+"},
                "eff_a": {"cases": "-"},
                "sev_mult": {"severe": "+"},
                "rate_in": {"cases": "+"},
                "n_strains": {"cases": "+"},
            },
            "inert": {"N": {"reason": "per-capita rates are invariant to population size",
                            "outputs_unaffected": ["cases", "severe"]}},
            "compartments": {
                "states": ["S", "I1", "R1", "I2", "R2"],
                "transitions": [
                    {"from": "S", "to": "I1", "counts": ["cases"]},
                    {"from": "R1", "to": "I2", "counts": ["cases", "severe"]},
                ],
                "conservation": "closed",
            },
            "per_capita_invariant_to_N": True,
            "stochastic": False,
            "reductions": [
                {"id": "R1", "oracle": "final_size_lambert", "oracle_args": {"r0": 2.0},
                 "set": {"n_strains": 1, "comp_a": 0}, "unit": {"rate_in": 0.1},
                 "horizon": {"start": 0.0, "stop": 5.0},
                 "compare": {"per_capita.cases": {"abs": 1.0e-3}}},
            ],
            "trials": [
                {"id": "T1", "dossier": "E1", "component": "comp_a",
                 "arms": {"control": {"comp_a": 0}, "treated": {"comp_a": 1}},
                 "setting": {"rate_in": 0.1}, "window": [0.0, 5.0],
                 "estimand_type": "ratio", "estimand_mapping": {"value": "per_capita.cases"},
                 "published": {"lower": 0.25, "upper": 0.65, "level": 0.95}},
            ],
            "reference_tolerance": {"rel": 0.01},
            "configs": {"default": {}},
        },
    }


def _self_test() -> int:
    import copy
    import tempfile
    import yaml
    from toolkit.spec import schema

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    b0 = example_build()
    ok(validate_build(b0) == [], f"BE24a valid build request passes ({validate_build(b0)})")
    req_eng = RequestedEngine(b0)
    try:
        req_eng.run({}, UnitSpec("u1", {}), Package("none", {}), Horizon(0, 1, (0, 1)), 0)
        ok(False, "BE24b RequestedEngine.run raises EngineNotBuilt")
    except EngineNotBuilt:
        ok(True, "BE24b RequestedEngine.run raises EngineNotBuilt")

    # BE24: build spec validates through RequestedEngine; missing component, bad trial, bad reduction refused
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "units.csv"), "w") as fh:
            fh.write("id,pop,rate_in\nu1,100000,0.1\nu2,200000,0.2\n")
        spec_raw = {
            "schema_version": 1, "question": "synthetic build test", "decision_maker": "planner",
            "decision_date": 2026, "engine": {"build": copy.deepcopy(b0)},
            "horizon": {"start": 2026.0, "stop": 2031.0},
            "decision_units": {
                "csv": "units.csv", "id": "id",
                "engine_attributes": {"N": {"column": "pop"}, "rate_in": {"column": "rate_in"}},
                "populations": {"all": {"column": "pop", "age_groups": ["all"]}},
            },
            "options": {
                "populations": {"all": "pop"},
                "components": {"c_a": {"group": "g1"}},
                "packages": {"none": [], "act": ["c_a"]},
                "engine_map": {"c_a": {"comp_a": 1}},
                "prices": {"base_year": "2026", "index_name": "none", "index": {"2026": 1.0}},
                "cost_items": [{"id": "ci1", "component": "c_a", "value": 2.0, "price_year": "2026",
                                "price_rule": "as_published", "basis": "per_person", "population": "all",
                                "includes": ["delivery"]}],
            },
            "objective": {"outcome": "cases", "sense": "avert", "horizon": [2026.0, 2031.0], "discounting": "none"},
            "constraints": {"envelope_usd": 500000},
            "comparator": {"package": "none", "text": "no action"},
            "outcomes_reported": ["cases"],
            "calibration": {"method": "none"},
            "uncertainty": {"level": 0.95, "sources": [{"kind": "engine_parameters"}],
                            "excluded": [{"kind": "calibration", "reason": "none"},
                                         {"kind": "costs", "reason": "fixed"},
                                         {"kind": "emulator", "reason": "direct"}]},
            "analysis": {"evaluation": "direct", "decision_tests": []},
            "deliverable": {"kind": "package_per_unit", "allocation_file": "alloc.csv",
                            "columns": {"unit": "id", "package": "pkg", "value": "val"},
                            "headline": {"name": "total_averted", "outcome": "cases"}},
        }
        sp_path = os.path.join(d, "decision_spec.yaml")
        yaml.safe_dump(spec_raw, open(sp_path, "w"), sort_keys=False)
        s_ok = schema.load(sp_path)
        e_ok = schema.validate(s_ok, pending_ok=True)
        # Missing component in request (options.engine_map references comp_a)
        r_miss = copy.deepcopy(spec_raw)
        r_miss["options"]["engine_map"]["c_a"] = {"missing_comp": 1}
        e_miss = schema.validate(schema.Spec(r_miss, d), pending_ok=True)
        # Incomplete trial declaration
        r_trial = copy.deepcopy(spec_raw)
        r_trial["engine"]["build"]["request"]["trials"][0]["published"] = {"lower": 0.8, "upper": 0.2, "level": 0.95}
        e_trial = schema.validate(schema.Spec(r_trial, d), pending_ok=True)
        # Invalid reduction (unknown oracle + undeclared compare path)
        r_red = copy.deepcopy(spec_raw)
        r_red["engine"]["build"]["request"]["reductions"][0]["oracle"] = "not_an_oracle"
        r_red["engine"]["build"]["request"]["reductions"][0]["compare"] = {"per_capita.no_such": {"abs": 0.01}}
        e_red = schema.validate(schema.Spec(r_red, d), pending_ok=True)
        # Direct naming of toolkit.engines.built:BuiltEngine outside build form is refused
        r_dir = copy.deepcopy(spec_raw)
        r_dir["engine"] = {"ref": "toolkit.engines.built:BuiltEngine"}
        e_dir = schema.validate(schema.Spec(r_dir, d), pending_ok=True)
        # Reserved config keyword refused
        r_res = copy.deepcopy(spec_raw)
        r_res["engine"]["build"]["request"]["configs"] = {"default": {"run_dir": "/tmp"}}
        e_res = schema.validate(schema.Spec(r_res, d), pending_ok=True)
        ok(e_ok == [] and bool(e_miss) and any("published" in x for x in e_trial)
           and any("oracle" in x for x in e_red) and any("undeclared output" in x for x in e_red)
           and bool(e_dir) and any("reserved" in x for x in e_res),
           f"BE24 build spec validates via RequestedEngine and refuses missing component, bad trial, bad reduction, "
           f"direct BuiltEngine ref, and reserved config key ({e_ok}, {e_miss[:1]}, {e_trial[:1]}, {e_red[:2]}, {e_dir[:1]}, {e_res[:1]})")

    # Starsim spike (§3.2): confirm an ss.Analyzer attaches to an unrun ss.Sim on the pinned Starsim version
    import starsim as ss

    class _SpikeAnalyzer(ss.Analyzer):
        def __init__(self):
            super().__init__()
            self.steps: list[int] = []

        def step(self):
            self.steps.append(int(self.sim.ti))

    sim = ss.Sim(n_agents=10, dur=2, verbose=0)
    an = _SpikeAnalyzer()
    sim.pars.rand_seed = 7
    existing = sim.pars.analyzers
    sim.pars.analyzers = (list(existing) if isinstance(existing, (list, tuple)) else ([existing] if existing else [])) + [an]
    sim.run()
    ok(bool(sim.initialized) and len(an.steps) >= 2 and int(sim.pars.rand_seed) == 7,
       f"BE_SPIKE Starsim unrun-Sim analyzer attachment works ({an.steps})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
