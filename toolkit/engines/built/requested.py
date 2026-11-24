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
PARAM_DOMAIN_TYPES = ("continuous", "integer", "categorical")
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


def _is_str(x) -> bool:
    return isinstance(x, str) and bool(x.strip())


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def _val_in(val, choices: list) -> bool:
    """Membership test that keeps booleans distinct from 0/1 ints and never raises on unhashable values."""
    for c in choices:
        if isinstance(val, bool) or isinstance(c, bool):
            if type(val) is type(c) and val == c:
                return True
        elif val == c:
            return True
    return False


def _check_interval(rng, label: str) -> tuple[tuple[float, float] | None, list[str]]:
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2 and _is_num(rng[0]) and _is_num(rng[1])
            and float(rng[0]) < float(rng[1])):
        return None, [f"{label}: must be [lo, hi] with finite lo < hi"]
    return (float(rng[0]), float(rng[1])), []


def _check_tol_map(tol, label: str, *, max_rel: float | None = None) -> list[str]:
    if not isinstance(tol, Mapping) or not tol:
        return [f"{label}: non-empty {{abs, rel}} tolerance mapping required"]
    bad: list[str] = []
    extra = sorted(str(k) for k in tol if k not in ("abs", "rel"))
    if extra:
        bad.append(f"{label}: unknown tolerance key(s) {extra}")
    if "abs" in tol and not (_is_num(tol["abs"]) and float(tol["abs"]) > 0):
        bad.append(f"{label}.abs {tol['abs']!r}: finite positive number required")
    if "rel" in tol:
        if not (_is_num(tol["rel"]) and float(tol["rel"]) > 0):
            bad.append(f"{label}.rel {tol['rel']!r}: finite positive number required")
        elif max_rel is not None and float(tol["rel"]) > max_rel:
            bad.append(f"{label}.rel {tol['rel']} exceeds harness ceiling {max_rel}")
    if not any(k in tol for k in ("abs", "rel")):
        bad.append(f"{label}: positive abs or rel tolerance required")
    return bad


def _output_path_declared(path, per_cap: set[str], age_groups: set[str],
                          windows: dict[str, set[str]], drivers: set[str]) -> bool:
    """Match toolkit.engines.benchmark.get() paths exactly:
    per_capita.<outcome>.<age>, per_capita_all.<outcome>, windows.<w>.<metric>, cost.<driver>."""
    if not isinstance(path, str):
        return False
    parts = path.split(".")
    if len(parts) == 3 and parts[0] == "per_capita":
        return parts[1] in per_cap and parts[2] in age_groups
    if len(parts) == 2 and parts[0] == "per_capita_all":
        return parts[1] in per_cap
    if len(parts) == 3 and parts[0] == "windows":
        return parts[1] in windows and parts[2] in windows[parts[1]]
    if len(parts) == 2 and parts[0] == "cost":
        return parts[1] in drivers
    return False


def _check_attr_val(val, aspec: Mapping, label: str) -> list[str]:
    at = aspec.get("type")
    rng = aspec.get("range") if "range" in aspec else aspec.get("test_range")
    if at == "continuous":
        if not _is_num(val):
            return [f"{label}: continuous attribute requires finite number, got {val!r}"]
        if isinstance(rng, (list, tuple)) and len(rng) == 2 and _is_num(rng[0]) and _is_num(rng[1]):
            if not (float(rng[0]) <= float(val) <= float(rng[1])):
                return [f"{label}: {val!r} outside {list(rng)}"]
        return []
    if at == "integer":
        if not _is_int(val):
            return [f"{label}: integer attribute requires integer value, got {val!r}"]
        if isinstance(rng, (list, tuple)) and len(rng) == 2 and _is_num(rng[0]) and _is_num(rng[1]):
            if not (float(rng[0]) <= float(val) <= float(rng[1])):
                return [f"{label}: {val!r} outside {list(rng)}"]
        return []
    if at == "categorical":
        vs = aspec.get("values") if isinstance(aspec.get("values"), list) else []
        if not _val_in(val, vs):
            return [f"{label}: {val!r} not in {vs!r}"]
        return []
    if at == "vector":
        ln = aspec.get("length")
        if not (isinstance(val, list) and _is_int(ln) and len(val) == ln and all(_is_num(x) for x in val)):
            return [f"{label}: vector attribute requires list of {ln} finite numbers, got {val!r}"]
        return []
    return []


def _check_param_val(val, ps: Mapping, label: str) -> list[str]:
    dom = ps.get("domain") if isinstance(ps.get("domain"), Mapping) else {}
    dt = dom.get("type")
    bad: list[str] = []
    if dt == "integer":
        if not _is_int(val):
            bad.append(f"{label}: integer parameter requires integer value, got {val!r}")
    elif dt == "categorical":
        pass
    else:
        if not _is_num(val):
            bad.append(f"{label}: finite number required, got {val!r}")
    if "values" in dom and isinstance(dom["values"], list) and dom["values"]:
        if not _val_in(val, dom["values"]):
            bad.append(f"{label}: {val!r} not in domain values {dom['values']!r}")
    if "range" in dom:
        iv, _ = _check_interval(dom["range"], label)
        if iv and _is_num(val) and not (iv[0] <= float(val) <= iv[1]):
            bad.append(f"{label}: {val!r} outside domain range {list(iv)}")
    if _is_num(ps.get("lo")) and _is_num(ps.get("hi")):
        if not _is_num(val) or not (float(ps["lo"]) <= float(val) <= float(ps["hi"])):
            bad.append(f"{label}: {val!r} outside [{ps['lo']}, {ps['hi']}]")
    return bad


def _check_horizon_map(hor, label: str) -> list[str]:
    if not isinstance(hor, Mapping):
        return [f"{label}: must be a mapping with start < stop"]
    s0, s1 = hor.get("start"), hor.get("stop")
    if not (_is_num(s0) and _is_num(s1) and float(s0) < float(s1)):
        return [f"{label}: finite start < stop required"]
    lo, hi = float(s0), float(s1)
    bad: list[str] = []
    if "window" in hor and hor["window"] is not None:
        iv, errs = _check_interval(hor["window"], f"{label}.window")
        bad += errs
        if iv and not (lo <= iv[0] and iv[1] <= hi):
            bad.append(f"{label}.window {list(iv)} must lie within [{lo}, {hi}]")
    if "windows" in hor and hor["windows"] is not None:
        if not isinstance(hor["windows"], Mapping):
            bad.append(f"{label}.windows: must be a mapping")
        else:
            for wn, wiv in hor["windows"].items():
                iv, errs = _check_interval(wiv, f"{label}.windows.{wn}")
                bad += errs
                if iv and not (lo <= iv[0] and iv[1] <= hi):
                    bad.append(f"{label}.windows.{wn} {list(iv)} must lie within [{lo}, {hi}]")
    return bad


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
    if not isinstance(kind, str) or kind not in BUILD_KINDS:
        bad.append(f"engine.build.kind {kind!r}: must be one of {list(BUILD_KINDS)}")
    if not _is_str(build.get("why")):
        bad.append("engine.build.why: non-empty justification required")
    mechs = build.get("mechanisms")
    if not (isinstance(mechs, list) and mechs and all(_is_str(m) for m in mechs)):
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
            if not _is_str(cname):
                bad.append(f"engine.build.request.components: component name {cname!r} must be a non-empty string")
                continue
            if not isinstance(cspec, Mapping):
                bad.append(f"engine.build.request.components.{cname}: must be {{values, absent}}")
                continue
            vals = cspec.get("values")
            if not isinstance(vals, list) or not vals:
                bad.append(f"engine.build.request.components.{cname}.values: non-empty list required")
                continue
            if "absent" not in cspec or not _val_in(cspec["absent"], vals):
                bad.append(f"engine.build.request.components.{cname}.absent: must be one of values {vals!r}")
            comp_vals[cname] = list(vals)

    # Unit attributes
    attrs_raw = req.get("unit_attributes")
    attr_specs: dict[str, Mapping] = {}
    if not isinstance(attrs_raw, Mapping) or not attrs_raw:
        bad.append("engine.build.request.unit_attributes: non-empty mapping required")
    else:
        for aname, aspec in attrs_raw.items():
            if not _is_str(aname):
                bad.append(f"engine.build.request.unit_attributes: attribute name {aname!r} must be a non-empty string")
                continue
            if not isinstance(aspec, Mapping):
                bad.append(f"engine.build.request.unit_attributes.{aname}: must be a mapping")
                continue
            at = aspec.get("type")
            if not isinstance(at, str) or at not in ATTR_TYPES:
                bad.append(f"engine.build.request.unit_attributes.{aname}.type {at!r}: must be one of {list(ATTR_TYPES)}")
                continue
            attr_specs[aname] = aspec
            if at in ("continuous", "integer"):
                rng = aspec.get("range") if "range" in aspec else aspec.get("test_range")
                if rng is None:
                    bad.append(f"engine.build.request.unit_attributes.{aname}: continuous/integer needs range or test_range")
                else:
                    _, errs = _check_interval(rng, f"engine.build.request.unit_attributes.{aname}")
                    bad += errs
            elif at == "categorical":
                vs = aspec.get("values")
                if not isinstance(vs, list) or not vs:
                    bad.append(f"engine.build.request.unit_attributes.{aname}.values: non-empty list required")
            elif at == "vector":
                ln = aspec.get("length")
                if not _is_int(ln) or ln < 1:
                    bad.append(f"engine.build.request.unit_attributes.{aname}.length: positive integer required")

    # Outputs & age groups
    outs_raw = req.get("outputs")
    per_cap: list[str] = []
    windows_map: dict[str, set[str]] = {}
    cost_drivers: list[str] = []
    if not isinstance(outs_raw, Mapping):
        bad.append("engine.build.request.outputs: must be a mapping")
    else:
        pc = outs_raw.get("per_capita")
        if not (isinstance(pc, list) and pc and all(_is_str(x) for x in pc)):
            bad.append("engine.build.request.outputs.per_capita: non-empty list of strings required")
        else:
            per_cap = list(pc)
        wm = outs_raw.get("windows") or {}
        if not isinstance(wm, Mapping):
            bad.append("engine.build.request.outputs.windows: must be a mapping")
        else:
            for wname, metrics in wm.items():
                if not _is_str(wname):
                    bad.append(f"engine.build.request.outputs.windows: window name {wname!r} must be a non-empty string")
                    continue
                if not (isinstance(metrics, list) and metrics and all(_is_str(m) for m in metrics)):
                    bad.append(f"engine.build.request.outputs.windows.{wname}: non-empty list of metric names required")
                else:
                    windows_map[wname] = set(metrics)
        cd = outs_raw.get("cost_drivers") or []
        if not (isinstance(cd, list) and all(_is_str(x) for x in cd)):
            bad.append("engine.build.request.outputs.cost_drivers: must be a list of strings")
        else:
            cost_drivers = list(cd)
    ag = req.get("age_groups")
    age_group_set: set[str] = set()
    if not (isinstance(ag, list) and ag and all(_is_str(g) for g in ag)):
        bad.append("engine.build.request.age_groups: non-empty list of strings required")
    else:
        age_group_set = set(ag)

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
            if not _is_str(pn):
                bad.append(f"engine.build.request.parameters[{idx}].name: non-empty string required")
                continue
            if pn in param_specs:
                bad.append(f"engine.build.request.parameters: duplicate name {pn!r}")
                continue
            pk = p.get("kind")
            if not isinstance(pk, str) or pk not in KINDS:
                bad.append(f"engine.build.request.parameters.{pn}.kind {pk!r}: must be one of {list(KINDS)}")
            if not _is_num(p.get("value")):
                bad.append(f"engine.build.request.parameters.{pn}.value: finite number required")
            lo, hi = p.get("lo"), p.get("hi")
            if pk in ("fitted", "calibrated") and (not _is_num(lo) or not _is_num(hi)):
                bad.append(f"engine.build.request.parameters.{pn}: kind {pk} requires finite lo and hi")
            if lo is not None or hi is not None:
                if not (_is_num(lo) and _is_num(hi) and float(lo) < float(hi)):
                    bad.append(f"engine.build.request.parameters.{pn}: lo < hi finite required")
                elif _is_num(p.get("value")) and not (float(lo) <= float(p["value"]) <= float(hi)):
                    bad.append(f"engine.build.request.parameters.{pn}: value {p['value']} outside [{lo}, {hi}]")
            dom = p.get("domain")
            if dom is not None:
                if not isinstance(dom, Mapping):
                    bad.append(f"engine.build.request.parameters.{pn}.domain: must be a mapping")
                else:
                    dt = dom.get("type")
                    if dt is not None and (not isinstance(dt, str) or dt not in PARAM_DOMAIN_TYPES):
                        bad.append(f"engine.build.request.parameters.{pn}.domain.type {dt!r}: must be one of {list(PARAM_DOMAIN_TYPES)}")
                    if "values" in dom:
                        dvs = dom["values"]
                        if not isinstance(dvs, list) or not dvs:
                            bad.append(f"engine.build.request.parameters.{pn}.domain.values: non-empty list required")
                        elif dt == "integer" and not all(_is_int(x) for x in dvs):
                            bad.append(f"engine.build.request.parameters.{pn}.domain.values: integers required")
                    if "range" in dom:
                        bad += _check_interval(dom["range"], f"engine.build.request.parameters.{pn}.domain.range")[1]
                    if "value" in p:
                        bad += _check_param_val(p["value"], p, f"engine.build.request.parameters.{pn}.value")
            if not _is_str(p.get("source")):
                bad.append(f"engine.build.request.parameters.{pn}.source: dossier id or 'assumption' required")
            param_specs[pn] = dict(p)

    # Attribute binding
    ab_raw = req.get("attribute_binding")
    if not isinstance(ab_raw, Mapping):
        bad.append("engine.build.request.attribute_binding: must be a mapping")
    else:
        for an in attr_specs:
            if an not in ab_raw:
                bad.append(f"engine.build.request.attribute_binding: missing unit attribute {an!r}")
        for an, rule in ab_raw.items():
            if not _is_str(an):
                bad.append(f"engine.build.request.attribute_binding: key {an!r} must be a non-empty string")
                continue
            if rule != "required" and not (isinstance(rule, Mapping) and "default" in rule
                                           and _is_str(rule.get("meaning"))):
                bad.append(f"engine.build.request.attribute_binding.{an}: must be 'required' or {{default, meaning}}")

    # Decision inputs
    di_raw = req.get("decision_inputs")
    if not isinstance(di_raw, Mapping):
        bad.append("engine.build.request.decision_inputs: must be a mapping")
    else:
        for cname in comp_vals:
            if cname not in di_raw:
                bad.append(f"engine.build.request.decision_inputs: missing component {cname!r}")
        for cname, spec_or_items in di_raw.items():
            if not _is_str(cname):
                bad.append(f"engine.build.request.decision_inputs: component name {cname!r} must be a non-empty string")
                continue
            if isinstance(spec_or_items, list):
                if not spec_or_items:
                    bad.append(f"engine.build.request.decision_inputs.{cname}: empty list; declare inputs or "
                               "{inputs: [], none_reason: '...'}")
                    continue
                items = spec_or_items
            elif isinstance(spec_or_items, Mapping):
                items = spec_or_items.get("inputs") or []
                if not isinstance(items, list):
                    bad.append(f"engine.build.request.decision_inputs.{cname}.inputs: must be a list")
                    continue
                if not items and not _is_str(spec_or_items.get("none_reason")):
                    bad.append(f"engine.build.request.decision_inputs.{cname}: no inputs requires non-empty none_reason")
                    continue
            else:
                bad.append(f"engine.build.request.decision_inputs.{cname}: must be a non-empty list or "
                           "{{inputs, none_reason}}")
                continue
            for j, item in enumerate(items):
                if not isinstance(item, Mapping):
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}]: must be a mapping")
                    continue
                ik, iname, iwhy = item.get("kind"), item.get("name"), item.get("why")
                if not isinstance(ik, str) or ik not in ("parameter", "unit_attribute"):
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}].kind {ik!r}: parameter or unit_attribute")
                elif not _is_str(iname):
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}].name {iname!r}: non-empty string required")
                elif ik == "parameter" and iname not in param_specs:
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}]: unknown parameter {iname!r}")
                elif ik == "unit_attribute" and iname not in attr_specs:
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}]: unknown unit_attribute {iname!r}")
                if not _is_str(iwhy):
                    bad.append(f"engine.build.request.decision_inputs.{cname}[{j}].why: non-empty text required")

    # Inert & parameter_roles
    all_inputs = set(param_specs) | set(comp_vals) | set(attr_specs)
    inert_raw = req.get("inert") or {}
    if not isinstance(inert_raw, Mapping):
        bad.append("engine.build.request.inert: must be a mapping")
        inert_raw = {}
    else:
        for iname, ispec in inert_raw.items():
            if not _is_str(iname) or iname not in all_inputs:
                bad.append(f"engine.build.request.inert.{iname}: unknown input")
            if not (isinstance(ispec, Mapping) and _is_str(ispec.get("reason"))):
                bad.append(f"engine.build.request.inert.{iname}: requires non-empty reason")
    roles_raw = req.get("parameter_roles") or {}
    if not isinstance(roles_raw, Mapping):
        bad.append("engine.build.request.parameter_roles: must be a mapping")
    else:
        for pn, role in roles_raw.items():
            if not _is_str(pn) or pn not in param_specs:
                bad.append(f"engine.build.request.parameter_roles.{pn}: unknown parameter")
            elif not isinstance(role, str) or role not in PARAM_ROLES:
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
            if not _is_str(iname) or iname not in all_inputs:
                bad.append(f"engine.build.request.expected_effects.{iname}: unknown input")
            if not isinstance(outs, Mapping) or not outs:
                bad.append(f"engine.build.request.expected_effects.{iname}: non-empty {{output: sign}} mapping required")
                continue
            for oname, sign in outs.items():
                if not _is_str(oname) or oname not in valid_effect_outs:
                    bad.append(f"engine.build.request.expected_effects.{iname}.{oname}: undeclared output")
                if not isinstance(sign, str) or sign not in EFFECT_SIGNS:
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
            if not (isinstance(states, list) and states and all(_is_str(s) for s in states)):
                bad.append("engine.build.request.compartments.states: non-empty list of strings required")
                state_set: set[str] = set()
            else:
                state_set = set(states)
                if len(state_set) != len(states):
                    bad.append("engine.build.request.compartments.states: duplicate state names")
            cons = cmps.get("conservation")
            if not isinstance(cons, str) or cons not in CONSERVATION_MODES:
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
                    frm, to = tr.get("from"), tr.get("to")
                    if not isinstance(frm, str) or frm not in allowed_nodes:
                        bad.append(f"engine.build.request.compartments.transitions[{idx}].from {frm!r}: unknown state")
                    if not isinstance(to, str) or to not in allowed_nodes:
                        bad.append(f"engine.build.request.compartments.transitions[{idx}].to {to!r}: unknown state")
                    cnts = tr.get("counts") or []
                    if not isinstance(cnts, list):
                        bad.append(f"engine.build.request.compartments.transitions[{idx}].counts: must be a list")
                    else:
                        for c in cnts:
                            if not isinstance(c, str) or c not in per_cap:
                                bad.append(f"engine.build.request.compartments.transitions[{idx}].counts: undeclared output {c!r}")
                            else:
                                counted.add(c)
            for out_name in per_cap:
                if out_name not in counted:
                    bad.append(f"engine.build.request.compartments: per_capita output {out_name!r} is not counted by any transition")

    # seed_test_setting (optional)
    sts = req.get("seed_test_setting")
    if sts is not None:
        if not isinstance(sts, Mapping):
            bad.append("engine.build.request.seed_test_setting: must be a mapping")
        elif "unit" in sts:
            if not isinstance(sts["unit"], Mapping):
                bad.append("engine.build.request.seed_test_setting.unit: must be a mapping")
            else:
                for k, v in sts["unit"].items():
                    if not _is_str(k) or k not in attr_specs:
                        bad.append(f"engine.build.request.seed_test_setting.unit.{k}: unknown unit attribute")
                    else:
                        bad += _check_attr_val(v, attr_specs[k], f"engine.build.request.seed_test_setting.unit.{k}")

    # Reductions
    reds = req.get("reductions") or []
    per_cap_set, driver_set = set(per_cap), set(cost_drivers)
    if not isinstance(reds, list):
        bad.append("engine.build.request.reductions: must be a list")
    else:
        seen_r: set[str] = set()
        for idx, red in enumerate(reds):
            if not isinstance(red, Mapping):
                bad.append(f"engine.build.request.reductions[{idx}]: must be a mapping")
                continue
            rid = red.get("id")
            if not _is_str(rid) or rid in seen_r:
                bad.append(f"engine.build.request.reductions[{idx}].id {rid!r}: unique non-empty string required")
            else:
                seen_r.add(rid)
            orc = red.get("oracle")
            if not isinstance(orc, str) or orc not in KNOWN_ORACLES:
                bad.append(f"engine.build.request.reductions.{rid}: unknown oracle {orc!r}")
            else:
                oargs = red.get("oracle_args")
                if not isinstance(oargs, Mapping):
                    bad.append(f"engine.build.request.reductions.{rid}.oracle_args: mapping required")
                else:
                    need_args = set(KNOWN_ORACLES[orc])
                    miss = [a for a in KNOWN_ORACLES[orc] if a not in oargs]
                    extra = sorted(str(a) for a in oargs if a not in need_args)
                    if miss:
                        bad.append(f"engine.build.request.reductions.{rid}.oracle_args: missing {miss}")
                    if extra:
                        bad.append(f"engine.build.request.reductions.{rid}.oracle_args: unknown argument(s) {extra}")
            rset = red.get("set") or {}
            if not isinstance(rset, Mapping):
                bad.append(f"engine.build.request.reductions.{rid}.set: must be a mapping")
            else:
                for k, v in rset.items():
                    if _is_str(k) and k in param_specs:
                        bad += _check_param_val(v, param_specs[k], f"engine.build.request.reductions.{rid}.set.{k}")
                    elif _is_str(k) and k in comp_vals:
                        if not _val_in(v, comp_vals[k]):
                            bad.append(f"engine.build.request.reductions.{rid}.set.{k}: {v!r} not in component values {comp_vals[k]!r}")
                    else:
                        bad.append(f"engine.build.request.reductions.{rid}.set.{k}: unknown parameter or component")
            runit = red.get("unit") or {}
            if not isinstance(runit, Mapping):
                bad.append(f"engine.build.request.reductions.{rid}.unit: must be a mapping")
            else:
                for k, v in runit.items():
                    if not _is_str(k) or k not in attr_specs:
                        bad.append(f"engine.build.request.reductions.{rid}.unit.{k}: unknown unit attribute")
                    else:
                        bad += _check_attr_val(v, attr_specs[k], f"engine.build.request.reductions.{rid}.unit.{k}")
            if "horizon" in red and red["horizon"] is not None:
                bad += _check_horizon_map(red["horizon"], f"engine.build.request.reductions.{rid}.horizon")
            comp_map = red.get("compare")
            if not isinstance(comp_map, Mapping) or not comp_map:
                bad.append(f"engine.build.request.reductions.{rid}.compare: non-empty mapping required")
            else:
                for path, tol in comp_map.items():
                    if not (isinstance(path, str) and BM.value_path_ok(path)
                            and _output_path_declared(path, per_cap_set, age_group_set, windows_map, driver_set)):
                        bad.append(f"engine.build.request.reductions.{rid}.compare.{path}: invalid or undeclared output path")
                    bad += _check_tol_map(tol, f"engine.build.request.reductions.{rid}.compare.{path}")

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
            if not _is_str(tid) or tid in seen_t:
                bad.append(f"engine.build.request.trials[{idx}].id {tid!r}: unique non-empty string required")
            else:
                seen_t.add(tid)
            if not _is_str(tr.get("dossier")):
                bad.append(f"engine.build.request.trials.{tid}.dossier: non-empty dossier entry id required")
            tcomp = tr.get("component")
            if not _is_str(tcomp) or tcomp not in comp_vals:
                bad.append(f"engine.build.request.trials.{tid}.component {tcomp!r}: unknown component")
            arms = tr.get("arms")
            arm_names: set[str] = set()
            if not (isinstance(arms, Mapping) and len(arms) >= 2):
                bad.append(f"engine.build.request.trials.{tid}.arms: mapping with at least 2 arms required")
            else:
                for aname, amap in arms.items():
                    if not _is_str(aname):
                        bad.append(f"engine.build.request.trials.{tid}.arms: arm name {aname!r} must be a non-empty string")
                        continue
                    arm_names.add(aname)
                    if not isinstance(amap, Mapping):
                        bad.append(f"engine.build.request.trials.{tid}.arms.{aname}: must be a component mapping")
                        continue
                    for ck, cv in amap.items():
                        if not _is_str(ck) or ck not in comp_vals:
                            bad.append(f"engine.build.request.trials.{tid}.arms.{aname}.{ck}: unknown component")
                        elif not _val_in(cv, comp_vals[ck]):
                            bad.append(f"engine.build.request.trials.{tid}.arms.{aname}.{ck}: {cv!r} not in {comp_vals[ck]!r}")
            tset = tr.get("setting")
            if not isinstance(tset, Mapping):
                bad.append(f"engine.build.request.trials.{tid}.setting: mapping required")
            else:
                nested = (bool(tset) and set(tset) <= {"unit", "params"}
                          and "unit" not in attr_specs and "params" not in attr_specs)
                if nested:
                    tu = tset.get("unit")
                    if tu is not None:
                        if not isinstance(tu, Mapping):
                            bad.append(f"engine.build.request.trials.{tid}.setting.unit: must be a mapping")
                        else:
                            for k, v in tu.items():
                                if not _is_str(k) or k not in attr_specs:
                                    bad.append(f"engine.build.request.trials.{tid}.setting.unit.{k}: unknown unit attribute")
                                else:
                                    bad += _check_attr_val(v, attr_specs[k], f"engine.build.request.trials.{tid}.setting.unit.{k}")
                    tp = tset.get("params")
                    if tp is not None:
                        if not isinstance(tp, Mapping):
                            bad.append(f"engine.build.request.trials.{tid}.setting.params: must be a mapping")
                        else:
                            for k, v in tp.items():
                                if not _is_str(k) or k not in param_specs:
                                    bad.append(f"engine.build.request.trials.{tid}.setting.params.{k}: unknown parameter")
                                elif param_specs[k].get("kind") == "fitted":
                                    bad.append(f"engine.build.request.trials.{tid}.setting.params.{k}: cannot override fitted parameter")
                                else:
                                    bad += _check_param_val(v, param_specs[k], f"engine.build.request.trials.{tid}.setting.params.{k}")
                else:
                    for k, v in tset.items():
                        if not _is_str(k) or k not in attr_specs:
                            bad.append(f"engine.build.request.trials.{tid}.setting.{k}: unknown unit attribute")
                        else:
                            bad += _check_attr_val(v, attr_specs[k], f"engine.build.request.trials.{tid}.setting.{k}")
            _, werr = _check_interval(tr.get("window"), f"engine.build.request.trials.{tid}.window")
            bad += werr
            et = tr.get("estimand_type")
            if not isinstance(et, str) or et not in BM.TYPES:
                bad.append(f"engine.build.request.trials.{tid}.estimand_type {et!r}: not in {list(BM.TYPES)}")
            em = tr.get("estimand_mapping")
            if not (isinstance(em, Mapping) and em):
                bad.append(f"engine.build.request.trials.{tid}.estimand_mapping: non-empty mapping required")
            else:
                def _check_vpath(vp, lbl):
                    if not (isinstance(vp, str) and BM.value_path_ok(vp)
                            and _output_path_declared(vp, per_cap_set, age_group_set, windows_map, driver_set)):
                        bad.append(f"{lbl}: invalid or undeclared output path {vp!r}")

                def _check_arm_ref(val, lbl):
                    names = val if isinstance(val, list) else [val]
                    if not names or not all(_is_str(x) and x in arm_names for x in names):
                        bad.append(f"{lbl}: references unknown arm {val!r} (declared: {sorted(arm_names)})")

                if "value" in em:
                    _check_vpath(em["value"], f"engine.build.request.trials.{tid}.estimand_mapping.value")
                if "values" in em:
                    vs = em["values"]
                    if not (isinstance(vs, list) and vs):
                        bad.append(f"engine.build.request.trials.{tid}.estimand_mapping.values: non-empty list required")
                    else:
                        for vi, vitem in enumerate(vs):
                            if not isinstance(vitem, Mapping):
                                bad.append(f"engine.build.request.trials.{tid}.estimand_mapping.values[{vi}]: mapping required")
                                continue
                            _check_vpath(vitem.get("path"),
                                         f"engine.build.request.trials.{tid}.estimand_mapping.values[{vi}].path")
                            if not (_is_num(vitem.get("weight")) and float(vitem["weight"]) > 0):
                                bad.append(f"engine.build.request.trials.{tid}.estimand_mapping.values[{vi}].weight: positive number required")
                if et in ("ratio", "odds_ratio"):
                    if "value" not in em and "values" not in em:
                        bad.append(f"engine.build.request.trials.{tid}.estimand_mapping: {et} requires value or values")
                    for ak in ("num", "den"):
                        if ak in em:
                            _check_arm_ref(em[ak], f"engine.build.request.trials.{tid}.estimand_mapping.{ak}")
                elif et == "interaction_or":
                    if "value" not in em:
                        bad.append(f"engine.build.request.trials.{tid}.estimand_mapping: interaction_or requires value")
                    for ak in ("a11", "a10", "a01", "a00"):
                        _check_arm_ref(em.get(ak), f"engine.build.request.trials.{tid}.estimand_mapping.{ak}")
                elif et == "cost_per_outcome":
                    if "value" not in em:
                        bad.append(f"engine.build.request.trials.{tid}.estimand_mapping: cost_per_outcome requires value")
                    for ak in ("num", "den"):
                        _check_arm_ref(em.get(ak), f"engine.build.request.trials.{tid}.estimand_mapping.{ak}")
                elif et == "curve_rules":
                    _check_vpath(em.get("x"), f"engine.build.request.trials.{tid}.estimand_mapping.x")
                    _check_vpath(em.get("y"), f"engine.build.request.trials.{tid}.estimand_mapping.y")
                    if not (isinstance(em.get("rules"), list) and em["rules"]):
                        bad.append(f"engine.build.request.trials.{tid}.estimand_mapping.rules: non-empty list required")
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
        bad += _check_tol_map(rtol, "engine.build.request.reference_tolerance", max_rel=MAX_REFERENCE_REL_TOL)
    cfgs = req.get("configs")
    if cfgs is not None:
        if not isinstance(cfgs, Mapping):
            bad.append("engine.build.request.configs: must be a mapping")
        else:
            for cname, cmap in cfgs.items():
                if not isinstance(cmap, Mapping):
                    bad.append(f"engine.build.request.configs.{cname}: must be a mapping")
                else:
                    res_hits = sorted(k for k in RESERVED_CONFIG_KEYS if k in cmap)
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
            raw_entry = di.get(cname)
            if isinstance(raw_entry, Mapping):
                ins = [dict(i) for i in (raw_entry.get("inputs") or [])]
                entry = {"inactive": [cspec["absent"]], "inputs": ins}
                if not ins and _is_str(raw_entry.get("none_reason")):
                    entry["none_reason"] = str(raw_entry["none_reason"]).strip()
            else:
                entry = {"inactive": [cspec["absent"]], "inputs": [dict(i) for i in (raw_entry or [])]}
            out[str(cname)] = entry
        return out

    def validate_unit_values(self, units: list[dict], engine_attributes: Mapping) -> list[str]:
        """Validate bound decision-unit attribute values against the signed request's typed attribute domains."""
        specs = self.request.get("unit_attributes") or {}
        bad: list[str] = []
        for aname, m in (engine_attributes or {}).items():
            aspec = specs.get(aname)
            if not isinstance(aspec, Mapping) or not isinstance(m, Mapping):
                continue
            at = aspec.get("type")
            strict_rng = aspec.get("range")
            has_rng = isinstance(strict_rng, (list, tuple)) and len(strict_rng) == 2

            def _check_one(raw_v, where: str) -> None:
                if at == "continuous":
                    try:
                        f = float(raw_v)
                    except (TypeError, ValueError):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): expected continuous number, got {raw_v!r}")
                        return
                    if isinstance(raw_v, bool) or not math.isfinite(f):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): non-finite continuous value {raw_v!r}")
                    elif has_rng and not (float(strict_rng[0]) <= f <= float(strict_rng[1])):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): {f} outside declared range {list(strict_rng)}")
                elif at == "integer":
                    if isinstance(raw_v, bool):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): expected integer, got {raw_v!r}")
                        return
                    try:
                        f = float(raw_v)
                    except (TypeError, ValueError):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): expected integer, got {raw_v!r}")
                        return
                    if not (math.isfinite(f) and f.is_integer()):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): expected integer, got {raw_v!r}")
                    elif has_rng and not (float(strict_rng[0]) <= f <= float(strict_rng[1])):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): {int(f)} outside declared range {list(strict_rng)}")
                elif at == "categorical":
                    vs = aspec.get("values") or []
                    if not (_val_in(raw_v, vs) or any(str(raw_v) == str(x) for x in vs)):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): {raw_v!r} not in {vs!r}")
                elif at == "vector":
                    ln = aspec.get("length")
                    if not (isinstance(raw_v, (list, tuple)) and len(raw_v) == ln):
                        bad.append(f"decision_units.engine_attributes.{aname} ({where}): expected vector of length {ln}, got {raw_v!r}")
                    else:
                        for x in raw_v:
                            try:
                                fx = float(x)
                            except (TypeError, ValueError):
                                fx = float("nan")
                            if isinstance(x, bool) or not math.isfinite(fx):
                                bad.append(f"decision_units.engine_attributes.{aname} ({where}): vector element {x!r} not finite")
                                break

            if "value" in m:
                _check_one(m["value"], "constant value")
                continue
            for idx, u in enumerate(units):
                uid = str(u.get("id", f"row {idx}"))
                if "column" in m:
                    cell = u.get(m["column"])
                    if "split" in m and isinstance(cell, str):
                        if "empty" in m and cell.strip() == m["empty"]:
                            val = []
                        else:
                            val = [p.strip() for p in cell.split(m["split"]) if p.strip()]
                    else:
                        val = cell
                    _check_one(val, f"unit {uid}")
                elif "columns" in m:
                    cols = list(m["columns"].values()) if isinstance(m["columns"], Mapping) else list(m["columns"] or [])
                    _check_one([u.get(c) for c in cols], f"unit {uid}")
                if len(bad) >= 20:
                    return bad
        return bad

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
                if (len(parts) == 3 and parts[0] == "per_capita") or (len(parts) == 2 and parts[0] == "per_capita_all"):
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
                 "compare": {"per_capita.cases.all": {"abs": 1.0e-3}}},
            ],
            "trials": [
                {"id": "T1", "dossier": "E1", "component": "comp_a",
                 "arms": {"control": {"comp_a": 0}, "treated": {"comp_a": 1}},
                 "setting": {"rate_in": 0.1}, "window": [0.0, 5.0],
                 "estimand_type": "ratio",
                 "estimand_mapping": {"value": "per_capita.cases.all", "num": "treated", "den": "control"},
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

    # BE24c: strict validation of paths, trials, reductions, discrete domains, tolerances, unhashable names
    b_paths = copy.deepcopy(b0)
    b_paths["request"]["reductions"][0]["compare"] = {
        "per_capita_all.cases": {"abs": 1e-3},
        "cost.units_delivered": {"rel": 0.01},
    }
    ok(validate_build(b_paths) == [], f"BE24c1 per_capita_all and cost.* paths accepted ({validate_build(b_paths)})")
    b_bad_age = copy.deepcopy(b0)
    b_bad_age["request"]["reductions"][0]["compare"] = {"per_capita.cases.missing": {"abs": 1e-3}}
    ok(any("undeclared output" in x for x in validate_build(b_bad_age)),
       "BE24c2 undeclared age group in per_capita path refused")
    b_bad_trial = copy.deepcopy(b0)
    b_bad_trial["request"]["trials"][0]["arms"] = {"control": None, "treated": {"comp_a": 17}}
    b_bad_trial["request"]["trials"][0]["estimand_mapping"] = {"value": "per_capita.imaginary.all"}
    e_bt = validate_build(b_bad_trial)
    ok(len(e_bt) >= 3, f"BE24c3 invalid trial arms/values/path all refused ({e_bt})")
    b_bad_red = copy.deepcopy(b0)
    b_bad_red["request"]["reductions"][0]["unit"] = {"rate_in": "missing"}
    b_bad_red["request"]["reductions"][0]["horizon"] = {"start": 5, "stop": 1}
    b_bad_red["request"]["reductions"][0]["oracle_args"] = {"r0": 2.0, "bogus": 1}
    e_br = validate_build(b_bad_red)
    ok(any("finite number" in x for x in e_br) and any("start < stop" in x for x in e_br)
       and any("unknown argument" in x for x in e_br), f"BE24c4 bad reduction inputs refused ({e_br})")
    b_bad_dom = copy.deepcopy(b0)
    b_bad_dom["request"]["parameters"][2]["value"] = 2.75
    ok(bool(validate_build(b_bad_dom)), "BE24c5 parameter default outside discrete integer domain refused")
    b_bad_tol = copy.deepcopy(b0)
    b_bad_tol["request"]["reference_tolerance"] = {"abs": 0.01, "rel": float("inf")}
    ok(bool(validate_build(b_bad_tol)), "BE24c6 non-finite rel tolerance alongside abs refused")
    b_empty_di = copy.deepcopy(b0)
    b_empty_di["request"]["decision_inputs"]["comp_a"] = []
    b_none_di = copy.deepcopy(b0)
    b_none_di["request"]["decision_inputs"]["comp_a"] = {"inputs": [], "none_reason": "fixed structural rule"}
    b_unhash = copy.deepcopy(b0)
    b_unhash["request"]["decision_inputs"]["comp_a"] = [{"kind": "parameter", "name": [], "why": "x"}]
    ok(bool(validate_build(b_empty_di)) and validate_build(b_none_di) == []
       and bool(validate_build(b_unhash)) and "validation errors" in "\n".join(sheet_lines(b_unhash)),
       "BE24c7 empty decision_inputs refused without none_reason; unhashable name returns error instead of TypeError")

    # BE24: build spec validates through RequestedEngine; missing component, bad trial, bad reduction, bad unit type refused
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
                "populations": {"total": "pop", "all": "pop"},
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
            "uncertainty": {"draws": 20, "level": 0.95, "sources": [{"kind": "engine_parameters"}],
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
        # Integer unit attribute bound to float CSV values (0.1, 0.2) refused before sign-off
        r_int = copy.deepcopy(spec_raw)
        r_int["engine"]["build"]["request"]["unit_attributes"]["rate_in"] = {
            "type": "integer", "range": [1, 6], "meaning": "integer tier",
        }
        r_int["engine"]["build"]["request"]["reductions"][0]["unit"] = {"rate_in": 2}
        r_int["engine"]["build"]["request"]["trials"][0]["setting"] = {"rate_in": 2}
        e_int = schema.validate(schema.Spec(r_int, d), pending_ok=True)
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
        r_red["engine"]["build"]["request"]["reductions"][0]["compare"] = {"per_capita.no_such.all": {"abs": 0.01}}
        e_red = schema.validate(schema.Spec(r_red, d), pending_ok=True)
        # Direct naming of toolkit.engines.built:BuiltEngine outside build form is refused
        r_dir = copy.deepcopy(spec_raw)
        r_dir["engine"] = {"ref": "toolkit.engines.built:BuiltEngine"}
        e_dir = schema.validate(schema.Spec(r_dir, d), pending_ok=True)
        # Reserved config keyword refused
        r_res = copy.deepcopy(spec_raw)
        r_res["engine"]["build"]["request"]["configs"] = {"default": {"run_dir": "/tmp"}}
        e_res = schema.validate(schema.Spec(r_res, d), pending_ok=True)
        # Plain build with engine_choice.json naming unrelated registered engine is refused
        ch_path = os.path.join(d, schema.ENGINE_CHOICE)
        json.dump({"engine": {"ref": "toolkit.engines.contract:ToyEngine"}}, open(ch_path, "w"))
        try:
            schema.load(sp_path)
            plain_choice_refused = False
        except schema.SpecError:
            plain_choice_refused = True
        finally:
            os.remove(ch_path)
        ok(e_ok == [] and any("integer" in x for x in e_int) and bool(e_miss)
           and any("published" in x for x in e_trial)
           and any("oracle" in x for x in e_red) and any("undeclared output" in x for x in e_red)
           and bool(e_dir) and any("reserved" in x for x in e_res) and plain_choice_refused,
           f"BE24 build spec validates via RequestedEngine and refuses bad int unit attr, missing component, bad trial, "
           f"bad reduction, direct BuiltEngine ref, reserved config, and stale choice "
           f"(ok={e_ok}, int={e_int[:1]}, miss={e_miss[:1]}, trial={e_trial[:1]}, red={e_red[:2]}, dir={e_dir[:1]}, res={e_res[:1]})")

    # Starsim 3.3.2 spike (§3.2): harness can attach an ss.Analyzer to an unrun ss.Sim via s.pars.analyzers and set rand_seed
    try:
        import starsim as ss

        class _SpikeAnalyzer(ss.Analyzer):
            def __init__(self):
                super().__init__()
                self.steps = []

            def step(self):
                self.steps.append(self.sim.ti)

        sim = ss.Sim(n_agents=50, start=0, stop=2, dt=1, diseases="sir", networks="random", verbose=0)
        an = _SpikeAnalyzer()
        sim.pars.analyzers += [an]
        sim.pars.rand_seed = 7
        sim.run()
        ok(sim.initialized and len(an.steps) >= 2 and sim.pars.rand_seed == 7,
           f"BE_SPIKE starsim unrun Sim accepts harness ss.Analyzer via s.pars.analyzers and rand_seed (steps={an.steps})")
    except ImportError:
        ok(True, "BE_SPIKE starsim not installed in this environment (skipped)")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
