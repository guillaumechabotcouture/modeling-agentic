#!/usr/bin/env python3
"""Phase 38 β: each intervention's effect against the run's primary comparator,
computed by the harness from the model's own code, is held to the trial
evidence for that comparison.

    python3 scripts/effect_concordance.py <run_dir>
    python3 scripts/effect_concordance.py --self-test

E7 attempt 7: every single-trial benchmark passed, yet the plan's net effect
against the comparator (round 2, finding R-021) was an 83% burden reduction in
covered seasons where trials of the same net type against the nets the
comparator still had support at most 56%. The critics found it in round 2; the
fix was a cap added after the library had been run.

No number in this check is written by an agent. The modeler declares which
inputs to compare; the harness evaluates the model's per-unit function (the one
the eligibility probe drives, `models/eligibility_manifest.yaml: outcome_fn`):

    unit_fn: models/outcome_fn.py::evaluate_unit   # must be the eligibility manifest's function
    call_style: dict                               # or kwargs
    allocation_csv: models/allocation.csv          # the deliverable: one row per unit
    headline_claim: det_dalys                      # the claims-ledger claim whose value is the summed benefit
    unit_column: district                          # joins the allocation to the units table
    package_column: package                        # allocated package per unit
    benefit_column: dalys_averted                  # the allocation's benefit per unit
    baseline_column: dalys_baseline                # the allocation's burden per unit under the comparator
    units_csv: models/units.csv                    # unit_column + the inputs unit_fn takes
    package_input: package                         # the unit_fn input that selects a package
    benefit_output: dalys_averted                  # unit_fn output matching benefit_column
    burden_output: dalys_covered                   # burden over the period the interventions cover
    comparator_package: n0                         # what the primary comparator delivers (no plan)
    packages:                                      # every allocated package and its components
      n0: []
      n3: [dual_ai]
      n3_s1: [dual_ai, smc]
      n0_s1: [smc]
    effects:
      - id: E1
        intervention: "<package component>"
        component: dual_ai
        registry: <name in the citations.md Parameter Registry>   # trial effect, with its CI
        trial_comparator: "<what the trial's control arm received>"
        trial_use: 0.8                              # use in the trial's intervention arm
        comparator_id: CF1                          # the decision brief's primary comparator
        comparator_delivers: "<what CF1 already delivers in these units for this intervention>"
        model_use: 0.63                             # use the model attains (capped: see below)
    not_bounding:                                   # every other trial-effect registry entry, with a reason
      - {registry: <name>, reason: "<e.g. a hut-trial input to the mechanism; not allocated>"}

1. Binding, unit by unit. For each allocated unit the harness evaluates unit_fn
   with the unit's package: `benefit_output` must match the allocation's
   `benefit_column`, burden(comparator_package) must match its
   `baseline_column`, and burden(comparator_package) - burden(allocated
   package) must equal the benefit, each within 2% in at least 98% of units,
   with the totals within 2%. So the burden the effect is measured on is the
   one the deliverable reports (an all-cause burden would dilute the ratio). Otherwise the function (or the burden measured) is not the
   one behind the allocation: HIGH, not declarable. Once a claims ledger exists,
   the allocation's summed benefit must equal the value of `headline_claim`
   within 0.1%, so the allocation checked is the one whose number the report
   prints.
2. Effect. In every allocated unit whose package contains the component, the
   harness computes sum burden(package) / sum burden(package without the
   component). The modeled reduction may not exceed the trial's largest
   plausible reduction (1 - ci_low for a relative risk, rate ratio, hazard ratio
   or ratio; ci_high for an efficacy; an odds ratio must be converted first),
   scaled by model_use / trial_use capped at 1, by more than 10%: HIGH
   (scope-declarable). Overstating model_use can at most restore the unscaled
   trial bound.

Every registry entry whose kind is a trial effect is checked under `effects` or
listed under `not_bounding`. Judgements stay with the red-team critic: whether
the trial's control arm matches what the comparator delivers, whether the
component table is true, and whether an exemption is the effect of an allocated
intervention.
"""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
import tempfile

import yaml

PATH = os.path.join("models", "effect_concordance.yaml")
TOL = 0.10
BIND_TOL = 0.02
RATIO_KINDS = ("relative_risk", "incidence_rate_ratio", "hazard_ratio", "ratio")
REDUCTION_KINDS = ("efficacy",)
TRIAL_KINDS = RATIO_KINDS + REDUCTION_KINDS + ("odds_ratio",)
_TOP = ("unit_fn", "allocation_csv", "unit_column", "package_column", "benefit_column", "units_csv",
        "package_input", "benefit_output", "burden_output", "comparator_package", "baseline_column", "headline_claim")
_REQ = ("id", "intervention", "component", "registry", "trial_comparator", "comparator_id", "comparator_delivers",
        "model_use", "trial_use")


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _max_reduction(entry):
    """(largest plausible reduction, point reduction) from a registry entry, or (None, reason)."""
    kind, val = str(entry.get("kind") or ""), _num(entry.get("value"))
    if kind in RATIO_KINDS:
        lo = _num(entry.get("ci_low"))
        if lo is None or val is None or not 0 < lo <= val:
            return None, f"a {kind} needs value and ci_low (0 < ci_low <= value)"
        return 1 - lo, 1 - val
    if kind in REDUCTION_KINDS:
        hi = _num(entry.get("ci_high"))
        if hi is None or val is None or not 0 <= val <= hi <= 1:
            return None, f"an {kind} needs value and ci_high (0 <= value <= ci_high <= 1)"
        return hi, val
    if kind == "odds_ratio":
        return None, "is an odds ratio: convert it to a risk ratio for the outcome's baseline risk first"
    return None, (f"has kind {kind or '(none)'}; the trial effect must be one of "
                  f"{', '.join(RATIO_KINDS + REDUCTION_KINDS)}")


def _registry(run_dir: str) -> dict:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import effect_size_registry as esr
    try:
        params = esr.load_priors(os.path.join(run_dir, "citations.md")).get("parameters") or []
    except Exception:  # noqa: BLE001 — a missing or broken registry fails each effect closed below
        return {}
    return {str(p.get("name")): p for p in params if isinstance(p, dict) and p.get("name")}


def _primary_comparators(run_dir: str) -> set[str] | None:
    """The decision brief's primary CF id(s), or None when the brief has no comparators (older runs)."""
    try:
        doc = yaml.safe_load(open(os.path.join(run_dir, "decision_brief.yaml"))) or {}
    except (OSError, yaml.YAMLError):
        return None
    cfs = doc.get("comparators") if isinstance(doc, dict) else None
    if not isinstance(cfs, list) or not cfs:
        return None
    return {str(c.get("id")) for c in cfs if isinstance(c, dict) and c.get("role") == "primary"}


def _eligibility_fn(run_dir: str) -> str | None:
    try:
        m = yaml.safe_load(open(os.path.join(run_dir, "models", "eligibility_manifest.yaml"))) or {}
    except (OSError, yaml.YAMLError):
        return None
    return str(m.get("outcome_fn")) if isinstance(m, dict) and m.get("outcome_fn") else None


def _coerce(v):
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in str(v) else f
    except (TypeError, ValueError):
        return v


def _rows(run_dir: str, rel: str) -> list[dict]:
    with open(os.path.join(run_dir, rel), newline="", encoding="utf-8") as f:
        return [{k: _coerce(v) for k, v in r.items()} for r in csv.DictReader(f)]


def _caller(run_dir: str, spec: str, style: str):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import propagate_uncertainty as pu
    fn = pu.load_outcome_fn(run_dir, spec)
    return (lambda d: fn(**d)) if style == "kwargs" else fn


def _ledger_value(run_dir: str, claim_id: str):
    """(value, why) of a claim in models/claims_ledger.yaml; value None when there is no ledger yet."""
    lp = os.path.join(run_dir, "models", "claims_ledger.yaml")
    if not os.path.exists(lp):
        return None, "no ledger"
    try:
        doc = yaml.safe_load(open(lp)) or {}
    except (OSError, yaml.YAMLError) as e:
        return None, f"unreadable ({type(e).__name__})"
    claims = doc.get("claims") if isinstance(doc, dict) else doc
    items = claims if isinstance(claims, list) else list(claims.values()) if isinstance(claims, dict) else []
    for c in items:
        if isinstance(c, dict) and str(c.get("id")) == claim_id:
            v = c.get("value")
            v = v.get("point", v.get("value")) if isinstance(v, dict) else v
            return _num(v), "found"
    return None, "missing"


def _close(a: float, b: float, scale: float) -> bool:
    return abs(a - b) <= BIND_TOL * max(abs(b), scale)


def evaluate(run_dir: str) -> tuple[list[dict], list[str], dict]:
    """(rows, errors, binding). Each row: one effect's harness-computed numbers and bound."""
    p = os.path.join(run_dir, PATH)
    if not os.path.lexists(p):
        return [], [], {}     # absent: the rigor manifest reports it when an allocation exists
    try:
        doc = yaml.safe_load(open(p))
    except OSError as e:
        return [], [f"{PATH} cannot be read ({type(e).__name__})"], {}
    except yaml.YAMLError as e:
        return [], [f"{PATH} does not parse: {str(e)[:150]}"], {}
    if not isinstance(doc, dict):
        return [], [f"{PATH} must be a mapping"], {}
    effects = doc.get("effects")
    reason = str(doc.get("no_trial_effects") or "").strip()
    if reason and effects:
        return [], [f"{PATH}: `no_trial_effects` and a non-empty `effects` list contradict each other"], {}
    if reason and isinstance(effects, list) or (reason and effects is None):
        # Phase 40 (smoke): the run's interventions have no trial-based effect to bound (their effects
        # are model assumptions). Allowed only with a reason and with every trial-kind registry entry
        # accounted for; reported as a MEDIUM note the red-team critic challenges, never silent.
        if len(reason) < 20:
            return [], [f"{PATH}: `no_trial_effects` needs a reason (at least a sentence)"], {}
        reg = _registry(run_dir)
        nb = doc.get("not_bounding") or []
        listed = {str(x.get("registry")) for x in nb if isinstance(x, dict)
                  and len(str(x.get("reason") or "").strip()) >= 10}
        unlisted = sorted(n for n, r in reg.items() if str(r.get("kind") or "") in TRIAL_KINDS and n not in listed)
        if unlisted:
            return [], [f"{PATH}: `no_trial_effects` but trial-effect registry entries are not listed under "
                        f"`not_bounding` with a reason: {', '.join(unlisted)}"], {}
        return [], [], {"no_trial_effects": reason, "exempted": sorted(listed)}
    if not isinstance(effects, list) or not effects:
        return [], [f"{PATH}: expected a non-empty `effects:` list, or `no_trial_effects: <reason>` when no "
                    "intervention effect comes from a trial"], {}
    missing = [k for k in _TOP if not str(doc.get(k) or "").strip()]
    if missing:
        return [], [f"{PATH}: missing {', '.join(missing)}"], {}
    pk_table = doc.get("packages")
    if not isinstance(pk_table, dict) or not pk_table or not all(isinstance(v, list) for v in pk_table.values()):
        return [], [f"{PATH}: `packages:` must map every package to its list of components"], {}
    comps = {str(k): frozenset(str(x) for x in v) for k, v in pk_table.items()}
    cmp_pkg = str(doc["comparator_package"])
    if comps.get(cmp_pkg) is None or comps[cmp_pkg]:
        return [], [f"{PATH}: comparator_package {cmp_pkg} must be in `packages` with no components"], {}
    errors: list[str] = []
    elig = _eligibility_fn(run_dir)
    if elig is not None and elig != str(doc["unit_fn"]):
        return [], [f"unit_fn {doc['unit_fn']} is not the eligibility probe's function ({elig}): the harness must "
                    "check the same per-unit model that the probe and the allocation use"], {}
    # Every trial-effect registry entry is checked or explained.
    reg = _registry(run_dir)
    listed = {str(e.get("registry")) for e in effects if isinstance(e, dict)}
    nb = doc.get("not_bounding") or []
    if not isinstance(nb, list):
        errors.append(f"{PATH}: `not_bounding:` must be a list")
        nb = []
    for x in nb:
        if not isinstance(x, dict) or not str(x.get("registry") or "").strip() or len(str(x.get("reason") or "").strip()) < 10:
            errors.append(f"{PATH}: each not_bounding entry needs `registry` and a `reason`")
            continue
        listed.add(str(x["registry"]))
    unlisted = sorted(n for n, r in reg.items() if str(r.get("kind") or "") in TRIAL_KINDS and n not in listed)
    if unlisted:
        errors.append(f"{PATH}: trial-effect registry entries neither checked under `effects` nor listed under "
                      f"`not_bounding` with a reason: {', '.join(unlisted)}")
    try:
        alloc = _rows(run_dir, doc["allocation_csv"])
        units = {str(r.get(doc["unit_column"])): r for r in _rows(run_dir, doc["units_csv"])}
        fn = _caller(run_dir, str(doc["unit_fn"]), str(doc.get("call_style") or "dict"))
    except Exception as e:  # noqa: BLE001 — surfaced as a finding
        return [], errors + [f"cannot load the allocation, units or unit_fn: {type(e).__name__}: {str(e)[:200]}"], {}
    uc, pc, bc, pin = doc["unit_column"], doc["package_column"], doc["benefit_column"], doc["package_input"]
    bo, burd = doc["benefit_output"], doc["burden_output"]
    unknown = sorted({str(a.get(pc)) for a in alloc} - set(comps))
    if unknown:
        return [], errors + [f"allocated packages missing from `packages:`: {', '.join(unknown)[:300]}"], {}
    cache: dict = {}

    def out(unit: str, package) -> dict:
        key = (unit, str(package))
        if key not in cache:
            base = {k: v for k, v in units[unit].items() if k != uc}
            r = fn({**base, pin: package})
            if not isinstance(r, dict):
                raise TypeError(f"unit_fn returned {type(r).__name__}, not a dict")
            cache[key] = r
        return cache[key]

    def num(r: dict, k: str, unit: str) -> float:
        v = _num(r.get(k))
        if v is None:
            raise ValueError(f"unit_fn output {k} for unit {unit} is not a number")
        return v

    # 1. Binding, unit by unit: the function reproduces each allocated unit's benefit, and that
    #    benefit is the burden it removes against the comparator package (so burden_output is the
    #    outcome the allocation counts).
    binding = {}
    try:
        recs = []
        for row in alloc:
            u = str(row.get(uc))
            if u not in units:
                raise KeyError(f"unit {u} of {doc['allocation_csv']} is not in {doc['units_csv']}")
            w, wb = _num(row.get(bc)), _num(row.get(doc["baseline_column"]))
            if w is None or wb is None:
                raise ValueError(f"{doc['allocation_csv']}: {bc} or {doc['baseline_column']} of unit {u} is not a number")
            ra = out(u, row.get(pc))
            g = num(ra, bo, u)
            b0 = num(out(u, cmp_pkg), burd, u)
            recs.append((u, w, g, b0 - num(ra, burd, u), wb, b0))
        scale = max(1e-9, sum(abs(w) for _, w, *_ in recs) / max(len(recs), 1) * 1e-3)
        bscale = max(1e-9, sum(abs(r[4]) for r in recs) / max(len(recs), 1) * 1e-3)
        bad_b = [u for u, w, g, *_ in recs if not _close(g, w, scale)]
        bad_r = [u for u, _, g, rm, *_ in recs if not _close(rm, g, scale)]
        bad_0 = [r[0] for r in recs if not _close(r[5], r[4], bscale)]
        b_got, b_want = sum(r[5] for r in recs), sum(r[4] for r in recs)
        got, want = sum(r[2] for r in recs), sum(r[1] for r in recs)
        rel = abs(got - want) / max(abs(want), 1e-12)
        n = len(recs)
        ok_b = len(bad_b) <= BIND_TOL * n and rel <= BIND_TOL
        rm_tot = sum(r[3] for r in recs)
        ok_r = len(bad_r) <= BIND_TOL * n and abs(rm_tot - got) <= BIND_TOL * max(abs(got), 1e-12)
        ok_0 = len(bad_0) <= BIND_TOL * n and abs(b_got - b_want) <= BIND_TOL * max(abs(b_want), 1e-12)
        binding = {"units": n, "unit_fn_total": round(got, 6), "allocation_total": round(want, 6),
                   "rel_diff": round(rel, 6), "units_off": len(bad_b), "burden_mismatch_units": len(bad_r),
                   "baseline_mismatch_units": len(bad_0), "bound": ok_b and ok_r and ok_0}
        if not ok_b:
            return [], errors + [f"unit_fn does not reproduce the allocation: {len(bad_b)} of {n} units differ by more "
                                 f"than {BIND_TOL:.0%} in {bo} (e.g. {', '.join(bad_b[:5])}); summed {got:.6g} vs "
                                 f"{want:.6g} ({rel:.1%}). The checks must run on the model behind the allocation."], binding
        if not ok_0:
            return [], errors + [f"{burd}({cmp_pkg}) is not the baseline the allocation reports: {len(bad_0)} of {n} "
                                 f"units differ from {doc['baseline_column']} by more than {BIND_TOL:.0%} (summed "
                                 f"{b_got:.6g} vs {b_want:.6g}; e.g. {', '.join(bad_0[:5])})."], binding
        if not ok_r:
            return [], errors + [f"{burd} is not the outcome the allocation counts: in {len(bad_r)} of {n} units, "
                                 f"{burd}({cmp_pkg}) - {burd}(allocated package) differs from {bo} by more than "
                                 f"{BIND_TOL:.0%} (e.g. {', '.join(bad_r[:5])}); summed {rm_tot:.6g} removed vs "
                                 f"{got:.6g} benefit."], binding
    except Exception as e:  # noqa: BLE001
        return [], errors + [f"binding failed: {type(e).__name__}: {str(e)[:200]}"], binding
    # 2. Effects: in every allocated unit whose package contains the component, the package against
    #    the same package without it.
    by_set = {v: k for k, v in comps.items()}
    cfs, rows = _primary_comparators(run_dir), []
    for e in effects:
        if not isinstance(e, dict):
            errors.append(f"{PATH}: an entry is not a mapping")
            continue
        eid = str(e.get("id") or "?")
        miss = [k for k in _REQ if e.get(k) in (None, "")]
        if miss:
            errors.append(f"{eid}: missing {', '.join(miss)}")
            continue
        if cfs is not None and str(e["comparator_id"]) not in cfs:
            errors.append(f"{eid}: comparator_id {e['comparator_id']} is not the decision brief's primary "
                          f"comparator ({', '.join(sorted(cfs)) or 'none marked primary'})")
            continue
        r = reg.get(str(e["registry"]))
        if r is None:
            errors.append(f"{eid}: registry entry {e['registry']} not found in the citations.md Parameter Registry")
            continue
        trial_max, trial_point = _max_reduction(r)
        if trial_max is None:
            errors.append(f"{eid}: registry entry {e['registry']} {trial_point}")
            continue
        mu, tu = _num(e.get("model_use")), _num(e.get("trial_use"))
        if mu is None or not 0 < mu <= 1 or tu is None or not 0 < tu <= 1:
            errors.append(f"{eid}: model_use and trial_use must be in (0, 1]")
            continue
        c = str(e["component"])
        sel = [(str(a.get(uc)), str(a.get(pc))) for a in alloc if c in comps[str(a.get(pc))]]
        if not sel:
            errors.append(f"{eid}: no allocated package contains {c}; an intervention the allocation does not fund "
                          "belongs under not_bounding")
            continue
        lacking = sorted({pk for _, pk in sel if comps[pk] - {c} not in by_set})
        if lacking:
            errors.append(f"{eid}: packages {', '.join(lacking)} contain {c} but no package in `packages:` is the same "
                          f"without it; add it so the harness can evaluate the difference")
            continue
        try:
            on = sum(num(out(u, pk), burd, u) for u, pk in sel)
            off = sum(num(out(u, by_set[comps[pk] - {c}]), burd, u) for u, pk in sel)
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{eid}: unit_fn failed: {type(ex).__name__}: {str(ex)[:150]}")
            continue
        if off <= 0:
            errors.append(f"{eid}: burden without {c} is not positive")
            continue
        mr = on / off
        bound = trial_max * min(1.0, mu / tu)
        rows.append({"id": eid, "intervention": e["intervention"], "component": c, "registry": e["registry"],
                     "trial_reduction": round(trial_point, 4), "trial_max_reduction": round(trial_max, 4),
                     "trial_comparator": e["trial_comparator"], "comparator_id": e["comparator_id"],
                     "comparator_delivers": e["comparator_delivers"], "units": len(sel),
                     "packages": sorted({pk for _, pk in sel}),
                     "model_ratio": round(mr, 4), "model_use": mu, "trial_use": tu,
                     "model_reduction": round(1 - mr, 4), "max_reduction": round(bound, 4),
                     "exceeds": (1 - mr) > bound * (1 + TOL) + 1e-9})
    # Bundles: a package with two or more checked components is held to their combined trial bound,
    # so components cannot mask each other (each is otherwise measured on top of the others).
    bnd = {r["component"]: r["max_reduction"] for r in rows}
    reg_of = {str(e.get("component")): str(e.get("registry")) for e in effects if isinstance(e, dict)}
    for pk in sorted({str(a.get(pc)) for a in alloc}):
        inside = sorted(comps[pk] & set(bnd))
        if len(inside) < 2:
            continue
        rest = comps[pk] - set(inside)
        if rest not in by_set:
            errors.append(f"bundle {pk}: no package in `packages:` is {pk} without {', '.join(inside)}; add it so the "
                          "harness can bound the bundle")
            continue
        us = [str(a.get(uc)) for a in alloc if str(a.get(pc)) == pk]
        try:
            on = sum(num(out(u, pk), burd, u) for u in us)
            off = sum(num(out(u, by_set[rest]), burd, u) for u in us)
        except Exception as ex:  # noqa: BLE001
            errors.append(f"bundle {pk}: unit_fn failed: {type(ex).__name__}: {str(ex)[:150]}")
            continue
        if off <= 0:
            continue
        keep = 1.0
        for c in inside:
            keep *= 1 - bnd[c]
        rows.append({"id": f"bundle {pk}", "intervention": " + ".join(inside), "component": "+".join(inside),
                     "registry": ", ".join(reg_of[c] for c in inside), "trial_reduction": None,
                     "trial_max_reduction": round(1 - keep, 4), "trial_comparator": "each component's trial",
                     "comparator_id": "", "comparator_delivers": by_set[rest], "units": len(us), "packages": [pk],
                     "model_ratio": round(on / off, 4), "model_use": None, "trial_use": None,
                     "model_reduction": round(1 - on / off, 4), "max_reduction": round(1 - keep, 4),
                     "exceeds": (1 - on / off) > (1 - keep) * (1 + TOL) + 1e-9})
    hv, why = _ledger_value(run_dir, str(doc["headline_claim"]))
    if why == "no ledger":
        binding["deliverable_unverified"] = True
    elif hv is None or abs(hv - want) > 1e-3 * max(abs(want), 1e-12):
        errors.append(f"the allocation's summed {bc} ({want:.6g}) is not the report's headline: claim "
                      f"{doc['headline_claim']} in models/claims_ledger.yaml is "
                      f"{'missing or not a number' if hv is None else f'{hv:.6g}'}; the check must bind the allocation "
                      "whose number the report prints")
    return rows, errors, binding


def gate_findings(run_dir: str) -> list[dict]:
    """Findings for the STAGE 7 gate. A missing file is the rigor manifest's
    finding (models/effect_concordance.yaml, triggered by an allocation)."""
    rows, errors, b = evaluate(run_dir)
    out = [{"kind": "effect_concordance_invalid", "severity": "HIGH", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": False, "claim": f"{PATH}: {e}"} for e in errors]
    if b.get("no_trial_effects"):
        out.append({"kind": "effect_concordance_not_applicable", "severity": "MEDIUM", "stage": "MODEL",
                    "target_stage": "MODEL", "scope_declarable": True,
                    "claim": (f"{PATH}: no intervention effect is checked against trial evidence. Reason given: "
                              f"{b['no_trial_effects'][:400]} Exempted registry entries: "
                              f"{', '.join(b.get('exempted') or []) or 'none'}. The red-team critic checks whether any "
                              "of them is in fact a trial effect of an allocated intervention.")})
    if b.get("deliverable_unverified"):
        out.append({"kind": "effect_concordance_unverified", "severity": "MEDIUM", "stage": "MODEL",
                    "target_stage": "ANALYZE", "scope_declarable": False,
                    "claim": f"{PATH}: no models/claims_ledger.yaml yet, so the allocation checked cannot yet be tied "
                             "to the report's numbers (re-checked at every gate)."})
    for r in rows:
        if r["exceeds"]:
            out.append({
                "kind": "effect_exceeds_trial_evidence", "severity": "HIGH", "stage": "MODEL",
                "target_stage": "MODEL", "scope_declarable": True,
                "claim": (f"{r['id']} ({r['intervention']}): computed from the model over the {r['units']} units "
                          f"whose package includes {r['component']} ({', '.join(r['packages'])[:120]}), each against "
                          f"the same package without it ({r['comparator_id']}: {r['comparator_delivers']}): a "
                          f"{r['model_reduction']:.0%} burden reduction. The trial evidence ({r['registry']}: "
                          + (f"{r['trial_reduction']:.0%} reduction, at most {r['trial_max_reduction']:.0%} within its CI, "
                             f"vs {r['trial_comparator']}) supports at most {r['max_reduction']:.0%} at the model's use "
                             f"({r['model_use']} vs {r['trial_use']}). "
                             if r["trial_reduction"] is not None else
                             f"each component's bound, combined) supports at most {r['max_reduction']:.0%} for the "
                             "bundle. ")
                          + "Bring the effect within the evidence (constrain it "
                          "to the trial effect, fix the comparator or the composition), or say in the write-up why "
                          "the trial does not bound it.")})
    return out


# --------------------------------------------------------------------------- self-test

_MODEL = '''\
RED = {"none": 0.0, "old": 0.30, "new": 0.70, "smc": 0.20, "new_smc": 0.76}   # reduction vs nothing, full use
def unit(inp, base_pkg="old"):
    b = float(inp["pop"]) * float(inp["rate"])
    burden = lambda p: b * (1 - RED[p] * float(inp.get("use", 1.0)))
    return {"dalys": burden(inp["package"]), "dalys_averted": burden(base_pkg) - burden(inp["package"])}
def vs_none(inp):
    return unit(inp, "none")
def decoy(inp):
    return {"dalys": 1.0, "dalys_averted": 1.0}
def allcause(inp):
    r = unit(inp); r["dalys"] = r["dalys"] + 10 * float(inp["pop"]); return r
'''


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    reg = ("# Citations\n\n## Parameter Registry\n\n```yaml\n"
           "- name: new_vs_old_irr\n  value: 0.55\n  ci_low: 0.45\n  ci_high: 0.67\n  kind: incidence_rate_ratio\n"
           "  source: C1\n- name: smc_irr\n  value: 0.75\n  ci_low: 0.7\n  ci_high: 0.8\n  kind: incidence_rate_ratio\n"
           "  source: C2\n- name: new_vs_none_irr\n  value: 0.45\n  ci_low: 0.36\n  ci_high: 0.55\n"
           "  kind: incidence_rate_ratio\n  source: C4\n"
           "- name: some_or\n  value: 0.4\n  ci_low: 0.3\n  ci_high: 0.5\n  kind: odds_ratio\n  source: C3\n```\n")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "citations.md"), "w").write(reg)
        open(os.path.join(d, "models", "m.py"), "w").write(_MODEL)
        yaml.safe_dump({"comparators": [{"id": "CF1", "role": "primary", "text": "old products kept"},
                                        {"id": "CF2", "role": "sensitivity", "text": "nothing"}]},
                       open(os.path.join(d, "decision_brief.yaml"), "w"))
        with open(os.path.join(d, "models", "units.csv"), "w") as f:
            f.write("unit,pop,rate,use\n" + "".join(f"u{i},{1000 + 100 * i},0.2,0.9\n" for i in range(8)))
        red = {"none": 0.0, "old": 0.30, "new": 0.70, "smc": 0.20, "new_smc": 0.76}

        def alloc(pk, base="old", bump=None):
            with open(os.path.join(d, "models", "alloc.csv"), "w") as f:
                f.write("unit,package,dalys_averted,dalys_baseline\n")
                for i in range(8):
                    b, p = (1000 + 100 * i) * 0.2, pk(i)
                    v = b * 0.9 * (red[p] - red[base]) * (bump(i) if bump else 1.0)
                    f.write(f"u{i},{p},{v},{b * (1 - red[base] * 0.9)}\n")
        mix = lambda i: ["new", "new", "old", "new_smc", "smc", "old", "new_smc", "old"][i]   # noqa: E731
        alloc(mix)
        pk_old = {"old": [], "new": ["new_product"], "smc": ["smc"], "new_smc": ["new_product", "smc"]}
        top = {"unit_fn": "models/m.py::unit", "call_style": "dict", "allocation_csv": "models/alloc.csv",
               "unit_column": "unit", "package_column": "package", "benefit_column": "dalys_averted",
               "units_csv": "models/units.csv", "package_input": "package", "benefit_output": "dalys_averted",
               "burden_output": "dalys", "comparator_package": "old", "baseline_column": "dalys_baseline",
               "headline_claim": "det_dalys", "packages": pk_old}
        e_new = {"id": "E1", "intervention": "new product", "component": "new_product", "registry": "new_vs_old_irr",
                 "trial_comparator": "old product", "trial_use": 0.9, "comparator_id": "CF1",
                 "comparator_delivers": "the old product", "model_use": 0.9}
        e_smc = {**e_new, "id": "E2", "intervention": "SMC", "component": "smc", "registry": "smc_irr",
                 "trial_comparator": "no SMC", "comparator_delivers": "no SMC"}
        nb = [{"registry": "some_or", "reason": "kept for reference only"},
              {"registry": "new_vs_none_irr", "reason": "the comparator keeps the old product"}]

        def put(effects, nbl=None, **kw):
            yaml.safe_dump({**top, **kw, "effects": effects, "not_bounding": nb if nbl is None else nbl},
                           open(os.path.join(d, PATH), "w"))

        def ledger(value=None):
            if value is None:
                import csv as _csv
                value = sum(float(r["dalys_averted"]) for r in _csv.DictReader(open(os.path.join(d, "models", "alloc.csv"))))
            yaml.safe_dump({"claims": [{"id": "det_dalys", "source_artifact": "models/derived.yaml", "value": value}]},
                           open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        ok(gate_findings(d) == [], "EC1 no file: nothing here (the manifest reports a missing artifact)")
        ledger()
        # new vs old: (1-0.63)/(1-0.27) -> 49% in "new" units, similar in "new_smc" vs "smc"; bound 55%.
        put([e_new, e_smc])
        rows, errs, b = evaluate(d)
        ok(errs == [] and b["bound"] and b["units"] == 8
           and {r["id"]: r["units"] for r in rows if r["id"].startswith("E")} == {"E1": 4, "E2": 3}
           and not any(r["exceeds"] for r in rows) and gate_findings(d) == [],
           f"EC2 effects computed per component over every unit whose package includes it: {rows} {errs} {b}")
        # The lenient comparator: the same products measured against nothing, held to a new-vs-old trial.
        none_pk = {"none": [], "old": ["old_product"], "new": ["new_product"], "smc": ["smc"],
                   "new_smc": ["new_product", "smc"]}
        alloc(mix, base="none"); ledger()
        put([{**e_new, "registry": "new_vs_old_irr"}, e_smc], unit_fn="models/m.py::vs_none",
            comparator_package="none", packages=none_pk,
            nbl=[{"registry": "some_or", "reason": "kept for reference only"},
                 {"registry": "new_vs_none_irr", "reason": "deliberately the wrong pairing in this test"}])
        f = gate_findings(d)
        ok(any(x["kind"] == "effect_exceeds_trial_evidence" and x["claim"].startswith("E1") for x in f),
           f"EC3 a new-product effect against nothing exceeds a new-vs-old trial: {f}")
        # Overstating attained use cannot lift the bound past the trial's own maximum.
        put([{**e_new, "registry": "new_vs_old_irr", "model_use": 1.0, "trial_use": 0.5}, e_smc],
            unit_fn="models/m.py::vs_none", comparator_package="none", packages=none_pk,
            nbl=[{"registry": "some_or", "reason": "kept for reference only"},
                 {"registry": "new_vs_none_irr", "reason": "deliberately the wrong pairing in this test"}])
        ok(any(x["kind"] == "effect_exceeds_trial_evidence" for x in gate_findings(d)),
           "EC4 model_use above trial_use is capped at the unscaled trial bound")
        alloc(mix); ledger()
        # Binding failures.
        put([e_new, e_smc], unit_fn="models/m.py::decoy")
        f = gate_findings(d)
        ok(len(f) == 1 and "does not reproduce the allocation" in f[0]["claim"] and not f[0]["scope_declarable"],
           f"EC5 a decoy function fails the binding, not declarable: {f}")
        alloc(mix, bump=lambda i: 1.8 if i == 0 else (0.2 if i == 1 else 1.0))
        put([e_new, e_smc])
        ok(any("units differ" in x["claim"] for x in gate_findings(d)),
           "EC6 (Codex) unit errors that cancel in the total still fail the binding")
        alloc(mix); ledger()
        put([e_new, e_smc], unit_fn="models/m.py::allcause")
        ok(any("is not the baseline the allocation reports" in x["claim"] for x in gate_findings(d)),
           f"EC6b (Codex) an all-cause burden that dilutes the ratio fails the binding: {gate_findings(d)}")
        ledger(123.0)
        put([e_new, e_smc])
        ok(any("not the report's headline" in x["claim"] for x in gate_findings(d)),
           "EC6c (Codex) the checked allocation must be the one whose number the report prints")
        os.remove(os.path.join(d, "models", "claims_ledger.yaml"))
        f = gate_findings(d)
        ok([x["kind"] for x in f] == ["effect_concordance_unverified"] and f[0]["severity"] == "MEDIUM",
           f"EC6d before a claims ledger exists the tie to the deliverable is a MEDIUM note: {f}")
        ledger()
        yaml.safe_dump({"outcome_fn": "models/m.py::other"}, open(os.path.join(d, "models", "eligibility_manifest.yaml"), "w"))
        ok(any("eligibility probe's function" in x["claim"] for x in gate_findings(d)),
           "EC7 the checked function must be the eligibility probe's")
        os.remove(os.path.join(d, "models", "eligibility_manifest.yaml"))
        # Bundles: a component allocated only inside a bundle is still checked.
        alloc(lambda i: "new_smc" if i < 4 else "old"); ledger()
        put([e_new, e_smc])
        rows, errs, _b = evaluate(d)
        ok(errs == [] and {r["id"]: r["packages"] for r in rows if r["id"].startswith("E")}
           == {"E1": ["new_smc"], "E2": ["new_smc"]},
           f"EC8 (Codex) a component allocated only inside a bundle is checked in the bundle: {rows} {errs}")
        put([e_new, e_smc], packages={"old": [], "new_smc": ["new_product", "smc"]})
        ok(any("same without it" in x["claim"] for x in gate_findings(d)),
           "EC8b a bundle whose package without the component is not declared fails closed")
        alloc(mix); ledger()
        put([e_new, e_smc], packages={"old": [], "new": ["new_product"], "smc": ["smc"]})
        ok(any("missing from `packages:`" in x["claim"] for x in gate_findings(d)), "EC8c every allocated package is declared")
        # Accounting and comparator rules.
        put([e_new, e_smc], nbl=[])
        ok(any("neither checked" in x["claim"] and "some_or" in x["claim"] for x in gate_findings(d)),
           "EC9 a trial-effect registry entry left out fails closed")
        put([{**e_new, "comparator_id": "CF2"}, e_smc])
        ok(any("primary" in x["claim"] for x in gate_findings(d)), "EC10 a sensitivity comparator cannot replace the primary")
        put([{**e_new, "component": "irs"}, e_smc])
        ok(any("no allocated package contains irs" in x["claim"] for x in gate_findings(d)),
           "EC11 an intervention no unit is allocated belongs under not_bounding")
        put([e_new, {**e_smc, "registry": "some_or"}], nbl=[{"registry": "smc_irr", "reason": "not used here at all"},
                                                             {"registry": "new_vs_none_irr", "reason": "not used here"}])
        ok(any("odds ratio" in x["claim"] for x in gate_findings(d)), "EC12 an odds ratio must be converted first")
        put([{k: v for k, v in e_new.items() if k != "model_use"}, e_smc])
        ok(any("missing model_use" in x["claim"] for x in gate_findings(d)), "EC13 attained use is required")
        # (Codex) Bundles: components that each look small on top of the other cannot hide a large bundle effect.
        # Against the comparator: A alone 80%, B alone 80%, A+B 81%; each marginal is ~5%, but the bundle's
        # 81% exceeds the combined bound 1 - (1 - 0.55)(1 - 0.30) = 68.5%.
        open(os.path.join(d, "models", "mk.py"), "w").write(
            "R = {'old': 0.0, 'new': 0.8, 'smc': 0.8, 'new_smc': 0.81}\n"
            "def unit(inp):\n    b = float(inp['pop']) * float(inp['rate'])\n"
            "    return {'dalys': b * (1 - R[inp['package']]), 'dalys_averted': b * R[inp['package']]}\n")
        with open(os.path.join(d, "models", "alloc.csv"), "w") as f:
            f.write("unit,package,dalys_averted,dalys_baseline\n")
            for i in range(8):
                b = (1000 + 100 * i) * 0.2
                f.write(f"u{i},{'new_smc' if i < 4 else 'old'},{b * (0.81 if i < 4 else 0)},{b}\n")
        ledger()
        put([e_new, e_smc], unit_fn="models/mk.py::unit")
        f = gate_findings(d)
        ok(any(x["claim"].startswith("bundle new_smc") and "for the bundle" in x["claim"] for x in f),
           f"EC16 (Codex) a bundle is held to its components' combined trial bound: {f}")
        # (Codex) Total burden removed must equal total benefit even when one large unit is the only one off.
        with open(os.path.join(d, "models", "units.csv"), "w") as fu:
            fu.write("unit,pop,rate,use\n" + "".join(f"u{i},{1000 + (100000 if i == 0 else 0)},0.2,0.9\n" for i in range(60)))
        open(os.path.join(d, "models", "one.py"), "w").write(
            "def unit(inp):\n    b = float(inp['pop']) * float(inp['rate'])\n"
            "    red = {'old': 0.0, 'new': 0.3}[inp['package']]\n"
            "    big = float(inp['pop']) > 50000 and inp['package'] == 'new'\n"
            "    return {'dalys': b * (1 - red), 'dalys_averted': b * (0.9 if big else red)}\n")
        with open(os.path.join(d, "models", "alloc.csv"), "w") as f:
            f.write("unit,package,dalys_averted,dalys_baseline\n")
            for i in range(60):
                b = (1000 + (100000 if i == 0 else 0)) * 0.2
                f.write(f"u{i},new,{b * (0.9 if i == 0 else 0.3)},{b}\n")
        ledger()
        put([e_new], unit_fn="models/one.py::unit", packages={"old": [], "new": ["new_product"]},
            nbl=nb + [{"registry": "smc_irr", "reason": "no SMC in this test world"}])
        f = gate_findings(d)
        ok(any("summed" in x["claim"] and "removed vs" in x["claim"] for x in f),
           f"EC17 (Codex) one large unit whose benefit is not burden removed fails the total check: {f}")
        # Phase 40 (smoke): no trial effect applies. Allowed with a reason and full registry accounting.
        all_nb = [{"registry": n, "reason": "a model assumption in this run"}
                  for n in ("new_vs_old_irr", "smc_irr", "new_vs_none_irr", "some_or")]
        yaml.safe_dump({"effects": [], "no_trial_effects": "The campaign effect is a transmission-model assumption.",
                        "not_bounding": all_nb}, open(os.path.join(d, PATH), "w"))
        f = gate_findings(d)
        ok([x["kind"] for x in f] == ["effect_concordance_not_applicable"] and f[0]["severity"] == "MEDIUM"
           and "smc_irr" in f[0]["claim"], f"EC18 no_trial_effects is a visible MEDIUM note, not a block: {f}")
        yaml.safe_dump({"effects": [], "no_trial_effects": "The campaign effect is a model assumption.",
                        "not_bounding": all_nb[:2]}, open(os.path.join(d, PATH), "w"))
        ok(any("not listed under `not_bounding`" in x["claim"] for x in gate_findings(d)),
           "EC19 no_trial_effects still requires every trial-effect registry entry to be accounted for")
        yaml.safe_dump({"effects": [], "no_trial_effects": "short", "not_bounding": all_nb}, open(os.path.join(d, PATH), "w"))
        ok(any("needs a reason" in x["claim"] for x in gate_findings(d)), "EC20 no_trial_effects needs a real reason")
        yaml.safe_dump({"effects": [], "not_bounding": all_nb}, open(os.path.join(d, PATH), "w"))
        ok(any("no_trial_effects: <reason>" in x["claim"] for x in gate_findings(d)),
           "EC21 an empty effects list without a reason still fails, and says how to state none applies")
        open(os.path.join(d, PATH), "w").write("effects: [\n")
        ok(gate_findings(d)[0]["kind"] == "effect_concordance_invalid", "EC14 an unparseable file fails closed")
        os.remove(os.path.join(d, PATH)); os.makedirs(os.path.join(d, PATH))
        ok(any("cannot be read" in x["claim"] for x in gate_findings(d)), "EC15 an unreadable path fails closed")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    rows, errors, b = evaluate(a.run_dir)
    if b:
        print(f"binding: unit_fn {b['unit_fn_total']:.6g} vs allocation {b['allocation_total']:.6g} over {b['units']} "
              f"units ({b['rel_diff']:.2%}) {'ok' if b['bound'] else 'NOT BOUND'}")
    for r in rows:
        print(f"{r['id']} {r['intervention']} ({r['component']}) over {r['units']} units: "
              f"reduction {r['model_reduction']:.0%} vs max {r['max_reduction']:.0%} ({'EXCEEDS' if r['exceeds'] else 'ok'})")
    for e in errors:
        print("ERROR:", e)
    return 1 if errors or any(r["exceeds"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
