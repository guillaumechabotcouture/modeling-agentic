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
    unit_column: district                          # joins the allocation to the units table
    package_column: package                        # allocated package per unit
    benefit_column: dalys_averted                  # the allocation's benefit per unit
    units_csv: models/units.csv                    # unit_column + the inputs unit_fn takes
    package_input: package                         # the unit_fn input that selects a package
    benefit_output: dalys_averted                  # unit_fn output matching benefit_column
    burden_output: dalys_covered                   # unit_fn output whose plan/comparator ratio is the effect:
                                                   # burden over the period the intervention covers (a window
                                                   # that includes uncovered time dilutes the effect)
    effects:
      - id: E1
        intervention: "<package component>"
        registry: <name in the citations.md Parameter Registry>   # trial effect, with its CI
        trial_comparator: "<what the trial's control arm received>"
        trial_use: 0.8                              # use in the trial's intervention arm
        comparator_id: CF1                          # the decision brief's primary comparator
        comparator_delivers: "<what CF1 already delivers in these units for this intervention>"
        on_package: <package: what the comparator delivers + this component>
        off_package: <package: what the comparator delivers>
        model_use: 0.63                             # use the model attains (capped: see below)
    not_bounding:                                   # every other trial-effect registry entry, with a reason
      - {registry: <name>, reason: "<e.g. a hut-trial input to the mechanism; not allocated>"}

1. Binding. For the allocated units, the harness evaluates unit_fn with each
   unit's allocated package; the sum of `benefit_output` must match the
   allocation CSV's `benefit_column` within 2%. Otherwise the function is not
   the one behind the allocation and nothing it says can be trusted: HIGH,
   not declarable.
2. Effect. In the units allocated `on_package`, the harness computes
   sum burden(on_package) / sum burden(off_package). The modeled reduction may
   not exceed the trial's largest plausible reduction (1 - ci_low for a relative
   risk, rate ratio, hazard ratio or ratio; ci_high for an efficacy; an odds
   ratio must be converted first), scaled by model_use / trial_use capped at 1,
   by more than 10%: HIGH (scope-declarable). Overstating model_use can at most
   restore the unscaled trial bound.

Every registry entry whose kind is a trial effect is checked under `effects` or
listed under `not_bounding`. Whether the trial's control arm matches what the
comparator delivers is a judgement: the red-team critic checks each pairing.
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
        "package_input", "benefit_output", "burden_output")
_REQ = ("id", "intervention", "registry", "trial_comparator", "comparator_id", "comparator_delivers",
        "on_package", "off_package", "model_use", "trial_use")


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
    if not isinstance(effects, list) or not effects:
        return [], [f"{PATH}: expected a non-empty `effects:` list"], {}
    missing = [k for k in _TOP if not str(doc.get(k) or "").strip()]
    if missing:
        return [], [f"{PATH}: missing {', '.join(missing)}"], {}
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
    # Load the deliverable, the units and the model function.
    try:
        alloc = _rows(run_dir, doc["allocation_csv"])
        units = {str(r.get(doc["unit_column"])): r for r in _rows(run_dir, doc["units_csv"])}
        fn = _caller(run_dir, str(doc["unit_fn"]), str(doc.get("call_style") or "dict"))
    except Exception as e:  # noqa: BLE001 — surfaced as a finding
        return [], errors + [f"cannot load the allocation, units or unit_fn: {type(e).__name__}: {str(e)[:200]}"], {}
    uc, pc, bc, pin = doc["unit_column"], doc["package_column"], doc["benefit_column"], doc["package_input"]
    bo, burd = doc["benefit_output"], doc["burden_output"]
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

    # 1. Binding: the function reproduces the allocation's benefits.
    binding = {}
    try:
        got = want = 0.0
        n = 0
        for row in alloc:
            u = str(row.get(uc))
            if u not in units:
                raise KeyError(f"unit {u} of {doc['allocation_csv']} is not in {doc['units_csv']}")
            w = _num(row.get(bc))
            if w is None:
                raise ValueError(f"{doc['allocation_csv']}: {bc} of unit {u} is not a number")
            g = _num(out(u, row.get(pc)).get(bo))
            if g is None:
                raise ValueError(f"unit_fn output {bo} for unit {u} is not a number")
            got, want, n = got + g, want + w, n + 1
        rel = abs(got - want) / max(abs(want), 1e-12)
        binding = {"units": n, "unit_fn_total": round(got, 6), "allocation_total": round(want, 6),
                   "rel_diff": round(rel, 6), "bound": rel <= BIND_TOL}
        if not binding["bound"]:
            return [], errors + [f"unit_fn does not reproduce the allocation: summed {bo} over {n} units is {got:.6g} "
                                 f"against {want:.6g} in {doc['allocation_csv']} ({rel:.1%} apart; at most "
                                 f"{BIND_TOL:.0%}). The checks must run on the model behind the allocation."], binding
    except Exception as e:  # noqa: BLE001
        return [], errors + [f"binding failed: {type(e).__name__}: {str(e)[:200]}"], binding
    # 2. Effects, computed on the units the allocation gives each on_package.
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
        sel = [str(a.get(uc)) for a in alloc if str(a.get(pc)) == str(e["on_package"])]
        if not sel:
            errors.append(f"{eid}: no unit is allocated {e['on_package']}; an intervention the allocation does not "
                          "fund belongs under not_bounding")
            continue
        try:
            on = sum(_num(out(u, e["on_package"]).get(burd)) for u in sel)
            off = sum(_num(out(u, e["off_package"]).get(burd)) for u in sel)
        except TypeError:
            errors.append(f"{eid}: unit_fn output {burd} is not a number for every unit")
            continue
        except Exception as ex:  # noqa: BLE001
            errors.append(f"{eid}: unit_fn failed: {type(ex).__name__}: {str(ex)[:150]}")
            continue
        if not off or off <= 0:
            errors.append(f"{eid}: burden under {e['off_package']} is not positive")
            continue
        mr = on / off
        bound = trial_max * min(1.0, mu / tu)
        rows.append({"id": eid, "intervention": e["intervention"], "registry": e["registry"],
                     "trial_reduction": round(trial_point, 4), "trial_max_reduction": round(trial_max, 4),
                     "trial_comparator": e["trial_comparator"], "comparator_id": e["comparator_id"],
                     "comparator_delivers": e["comparator_delivers"], "units": len(sel),
                     "on_package": e["on_package"], "off_package": e["off_package"],
                     "model_ratio": round(mr, 4), "model_use": mu, "trial_use": tu,
                     "model_reduction": round(1 - mr, 4), "max_reduction": round(bound, 4),
                     "exceeds": (1 - mr) > bound * (1 + TOL) + 1e-9})
    return rows, errors, binding


def gate_findings(run_dir: str) -> list[dict]:
    """Findings for the STAGE 7 gate. A missing file is the rigor manifest's
    finding (models/effect_concordance.yaml, triggered by an allocation)."""
    rows, errors, _b = evaluate(run_dir)
    out = [{"kind": "effect_concordance_invalid", "severity": "HIGH", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": False, "claim": f"{PATH}: {e}"} for e in errors]
    for r in rows:
        if r["exceeds"]:
            out.append({
                "kind": "effect_exceeds_trial_evidence", "severity": "HIGH", "stage": "MODEL",
                "target_stage": "MODEL", "scope_declarable": True,
                "claim": (f"{r['id']} ({r['intervention']}): computed from the model, {r['on_package']} against "
                          f"{r['off_package']} ({r['comparator_id']}: {r['comparator_delivers']}) is a "
                          f"{r['model_reduction']:.0%} burden reduction over the {r['units']} units allocated it; the "
                          f"trial evidence ({r['registry']}: {r['trial_reduction']:.0%} reduction, at most "
                          f"{r['trial_max_reduction']:.0%} within its CI, vs {r['trial_comparator']}) supports at most "
                          f"{r['max_reduction']:.0%} at the model's use ({r['model_use']} vs {r['trial_use']}). Bring the "
                          "effect within the evidence (constrain it to the trial effect, fix the comparator or the "
                          "composition), or say in the write-up why the trial does not bound it.")})
    return out


# --------------------------------------------------------------------------- self-test

_MODEL = '''\
BASE = {"none": 0.0, "old": 0.30, "new": 0.70}     # burden reduction per package at full use
def unit(inp):
    b = float(inp["pop"]) * float(inp["rate"])
    red = BASE[inp["package"]] * float(inp.get("use", 1.0))
    return {"dalys": b * (1 - red), "dalys_averted": b * red}
def decoy(inp):
    return {"dalys": 1.0, "dalys_averted": 1.0}
'''


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    reg = ("# Citations\n\n## Parameter Registry\n\n```yaml\n"
           "- name: new_vs_old_irr\n  value: 0.55\n  ci_low: 0.45\n  ci_high: 0.67\n  kind: incidence_rate_ratio\n"
           "  source: C1\n- name: old_eff\n  value: 0.5\n  ci_low: 0.4\n  ci_high: 0.6\n  kind: efficacy\n  source: C2\n"
           "- name: some_or\n  value: 0.4\n  ci_low: 0.3\n  ci_high: 0.5\n  kind: odds_ratio\n  source: C3\n```\n")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "citations.md"), "w").write(reg)
        open(os.path.join(d, "models", "m.py"), "w").write(_MODEL)
        yaml.safe_dump({"comparators": [{"id": "CF1", "role": "primary", "text": "old products kept"},
                                        {"id": "CF2", "role": "sensitivity", "text": "nothing"}]},
                       open(os.path.join(d, "decision_brief.yaml"), "w"))
        with open(os.path.join(d, "models", "units.csv"), "w") as f:
            f.write("unit,pop,rate,use\n" + "".join(f"u{i},{1000 + 100 * i},0.2,0.9\n" for i in range(6)))

        def alloc(pk, scale=1.0):
            with open(os.path.join(d, "models", "alloc.csv"), "w") as f:
                f.write("unit,package,dalys_averted\n")
                for i in range(6):
                    b = (1000 + 100 * i) * 0.2
                    p = pk(i)
                    f.write(f"u{i},{p},{b * {'none': 0, 'old': 0.3, 'new': 0.7}[p] * 0.9 * scale}\n")
        alloc(lambda i: "new" if i < 3 else "old")
        top = {"unit_fn": "models/m.py::unit", "call_style": "dict", "allocation_csv": "models/alloc.csv",
               "unit_column": "unit", "package_column": "package", "benefit_column": "dalys_averted",
               "units_csv": "models/units.csv", "package_input": "package", "benefit_output": "dalys_averted",
               "burden_output": "dalys"}
        e_new = {"id": "E1", "intervention": "new product", "registry": "new_vs_old_irr", "trial_comparator": "old product",
                 "trial_use": 0.9, "comparator_id": "CF1", "comparator_delivers": "the old product",
                 "on_package": "new", "off_package": "old", "model_use": 0.9}
        nb = [{"registry": "old_eff", "reason": "the comparator's own product"},
              {"registry": "some_or", "reason": "kept for reference only"}]

        def put(effects, **kw):
            yaml.safe_dump({**top, **kw, "effects": effects, "not_bounding": kw.pop("nb", nb) if "nb" in kw else nb},
                           open(os.path.join(d, PATH), "w"))
        ok(gate_findings(d) == [], "EC1 no file: nothing here (the manifest reports a missing artifact)")
        # new vs old, computed: (1-0.63)/(1-0.27) = 0.507 -> 49% reduction; bound 55% x 0.9/0.9 = 55%.
        put([e_new])
        rows, errs, b = evaluate(d)
        ok(errs == [] and b["bound"] and rows[0]["units"] == 3 and abs(rows[0]["model_reduction"] - 0.4932) < 1e-3
           and not rows[0]["exceeds"] and gate_findings(d) == [],
           f"EC2 the harness computes the effect from the model on the units allocated it; within the trial: {rows} {errs} {b}")
        # The same model compared against nothing (the lenient comparator) exceeds a new-vs-old trial.
        put([{**e_new, "off_package": "none"}])
        f = gate_findings(d)
        ok(len(f) == 1 and f[0]["kind"] == "effect_exceeds_trial_evidence" and "63%" in f[0]["claim"],
           f"EC3 an effect measured against nothing exceeds the new-vs-old trial: {f}")
        # Overstating attained use cannot lift the bound above the trial's own maximum.
        put([{**e_new, "off_package": "none", "model_use": 1.0, "trial_use": 0.5}])
        ok(len(gate_findings(d)) == 1, "EC4 model_use above trial_use is capped at the unscaled trial bound")
        # Binding: a decoy function, or an allocation the function does not reproduce.
        put([e_new], unit_fn="models/m.py::decoy")
        f = gate_findings(d)
        ok(len(f) == 1 and "does not reproduce the allocation" in f[0]["claim"] and not f[0]["scope_declarable"],
           f"EC5 a decoy function fails the binding, not declarable: {f}")
        alloc(lambda i: "new" if i < 3 else "old", scale=1.2)
        put([e_new])
        ok(any("does not reproduce" in x["claim"] for x in gate_findings(d)), "EC6 an allocation 20% off its model fails the binding")
        alloc(lambda i: "new" if i < 3 else "old")
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        yaml.safe_dump({"outcome_fn": "models/m.py::other"}, open(os.path.join(d, "models", "eligibility_manifest.yaml"), "w"))
        ok(any("eligibility probe's function" in x["claim"] for x in gate_findings(d)),
           "EC7 the checked function must be the eligibility probe's")
        os.remove(os.path.join(d, "models", "eligibility_manifest.yaml"))
        # Accounting and comparator rules.
        put([e_new], nb=[])
        ok(any("neither checked" in x["claim"] and "old_eff" in x["claim"] for x in gate_findings(d)),
           "EC8 a trial-effect registry entry left out fails closed")
        put([{**e_new, "comparator_id": "CF2"}])
        ok(any("primary" in x["claim"] for x in gate_findings(d)), "EC9 a sensitivity comparator cannot replace the primary")
        put([{**e_new, "on_package": "none"}])
        ok(any("no unit is allocated none" in x["claim"] for x in gate_findings(d)),
           "EC10 an intervention no unit is allocated belongs under not_bounding")
        put([{**e_new, "registry": "some_or"}], nb=[{"registry": "old_eff", "reason": "the comparator's own product"},
                                                    {"registry": "new_vs_old_irr", "reason": "not used here at all"}])
        ok(any("odds ratio" in x["claim"] for x in gate_findings(d)), "EC11 an odds ratio must be converted first")
        put([{k: v for k, v in e_new.items() if k != "model_use"}])
        ok(any("missing model_use" in x["claim"] for x in gate_findings(d)), "EC12 attained use is required")
        open(os.path.join(d, PATH), "w").write("effects: [\n")
        ok(gate_findings(d)[0]["kind"] == "effect_concordance_invalid", "EC13 an unparseable file fails closed")
        os.remove(os.path.join(d, PATH)); os.makedirs(os.path.join(d, PATH))
        ok(any("cannot be read" in x["claim"] for x in gate_findings(d)), "EC14 an unreadable path fails closed")
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
        print(f"{r['id']} {r['intervention']}: {r['on_package']} vs {r['off_package']} over {r['units']} units: "
              f"reduction {r['model_reduction']:.0%} vs max {r['max_reduction']:.0%} ({'EXCEEDS' if r['exceeds'] else 'ok'})")
    for e in errors:
        print("ERROR:", e)
    return 1 if errors or any(r["exceeds"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
