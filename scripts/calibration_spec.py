#!/usr/bin/env python3
"""Phase 43 α (B1): calibration spec. Parameter roles and target metadata.

    python3 scripts/calibration_spec.py <run_dir> [--round N]
    python3 scripts/calibration_spec.py --self-test

A parameter being editable does not make it a calibration parameter (the
stisim.ai `calibration-strategy` skill). E7 attempt 8 freed two national
multipliers, watched them run to their bounds against the archetype
transmission levels, and fixed them at 1.0; the reasoning lived only in
prose. A run that calibrates writes `models/calibration_spec.yaml`:

    question: "<the decision this calibration serves>"
    parameters:                      # every candidate parameter, one role each
      - name: transmission_level
        matches: ["m0_*"]            # optional: globs for the fitted names it covers
        role: country_specific_uncertain
        calibrated: true
        reason: "<why it is fitted (or, when calibrated: false, why not)>"
        bounds: [0.6, 110.0]         # optional: the declared search range
        bounds_basis: empirical      # optional: empirical | pragmatic
        bound_changes:               # optional: each change of the range (B4)
          - round: 2
            from: [1.0, 50.0]
            to: [0.6, 110.0]
            diagnosis:               # every check before `bounds`, in this order
              missing_data: "<what was checked, what was found>"
              missing_mechanism: "..."
              structural_constraint: "..."
              inconsistent_targets: "..."
              mismatched_target_definition: "..."
    targets:
      - id: survey_2018
        quantity: "parasite prevalence by microscopy"
        population: "children in each state"
        age: "6-59 months"
        time: "2018"
        units: proportion
        source: "<survey or registry, with year>"
        uncertainty: "<CI / effective sample size / 'not reported'>"
        provenance: observed         # observed | modelled_by_others
        n: 37                        # optional: number of data points

Roles: data_informed (set from data, not fitted), fixed_literature,
country_specific_uncertain, behavioural, tuning, scenario_assumption (a
future or hypothetical input; never fitted to history). Parameters that were
considered and not calibrated are listed with `calibrated: false` and the
reason.

The fitted set is read from the run's calibration artifacts: the
`parameters[].name` of `models/identifiability.yaml` (the post-hoc
identifiability manifest) and the `parameters` / `fitted_parameters` of
`models/calibration_result.yaml`. A missing spec in a run that fits
parameters is reported by the manifest (`required_artifact_missing`,
`required_for_accept: fitted_parameters`), not here.

Findings:
  calibration_spec_invalid               HIGH  unreadable / malformed spec or entry
  calibration_spec_missing               HIGH  no spec, and the fit is recorded only
                                               in calibration_result.yaml (outside
                                               the manifest's fitted_parameters
                                               trigger)
  calibration_fitted_set_unreadable      HIGH  the spec exists but no fitted
                                               parameter can be read from the
                                               run's calibration artifacts
  calibration_scenario_assumption_fitted HIGH  a scenario_assumption parameter
                                               is among the fitted ones
  calibration_fitted_not_in_spec         HIGH  a fitted parameter has no entry,
                                               or only `calibrated: false` ones
                                               (MEDIUM at round 1, the drafting
                                               window)
  calibration_spec_reason_missing        MEDIUM an entry gives no reason
  calibration_target_metadata_missing    MEDIUM no targets, or a target without
                                               the required metadata
  calibration_bounds_widened_undiagnosed MEDIUM a recorded widening without the
                                               B4 diagnosis, or the manifest's
                                               bounds wider than the spec's
None are scope-declarable.

Bound widening between rounds is detected from what the run records: the
spec's `bound_changes` and its `bounds` against the identifiability
manifest's current bounds. The gate keeps no per-round copy of the bounds,
so a widening made in both files with no record is not seen. A harness-side
snapshot of the bounds at each round is the follow-up (B4, fix ledger S3).
"""
from __future__ import annotations

import argparse
import fnmatch
import math
import os
import sys
import tempfile

import yaml

PATH = os.path.join("models", "calibration_spec.yaml")
IDENT = os.path.join("models", "identifiability.yaml")
CALRES = os.path.join("models", "calibration_result.yaml")
ROLES = ("data_informed", "fixed_literature", "country_specific_uncertain",
         "behavioural", "tuning", "scenario_assumption")
TARGET_FIELDS = ("quantity", "population", "age", "time", "units", "source", "uncertainty", "provenance")
PROVENANCE = ("observed", "modelled_by_others")
# B4: the checks to run, in order, before widening a bound.
DIAGNOSIS_ORDER = ("missing_data", "missing_mechanism", "structural_constraint",
                   "inconsistent_targets", "mismatched_target_definition")


def _sibling(name: str):
    """scripts/<name>.py by path, under a private module name."""
    import importlib.util
    key = f"_cs_p43_{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                    f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    try:
        spec.loader.exec_module(mod)
    except BaseException:
        sys.modules.pop(key, None)
        raise
    return mod


def _f(kind, sev, claim):
    return {"kind": kind, "severity": sev, "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": False, "claim": claim}


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _interval(x):
    """(lo, hi) from a [lo, hi] pair of finite numbers with lo <= hi, else None."""
    if isinstance(x, (list, tuple)) and len(x) == 2:
        lo, hi = _num(x[0]), _num(x[1])
        if lo is not None and hi is not None and lo <= hi:
            return lo, hi
    return None


def _wider(new, old) -> bool:
    return new[0] < old[0] or new[1] > old[1]


def _load_yaml(run_dir, rel):
    """(doc, error, exists)."""
    p = os.path.join(run_dir, rel)
    if not os.path.exists(p):
        return None, None, False
    try:
        with open(p, encoding="utf-8") as f:
            return yaml.safe_load(f), None, True
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, f"`{rel}` cannot be parsed: {e}", True


def _names(x) -> tuple[list[str], int]:
    """(parameter names, unreadable entries) from a list of names / {name: ...}
    dicts, or a mapping's keys. Anything else is unreadable (fail closed)."""
    if isinstance(x, dict):
        return [str(k) for k in x], 0
    if not isinstance(x, list):
        return [], 1
    out, bad = [], 0
    for e in x:
        if isinstance(e, str) and e.strip():
            out.append(e.strip())
        elif isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip():
            out.append(e["name"].strip())
        else:
            bad += 1
    return out, bad


def fitted_parameters(run_dir: str) -> tuple[dict[str, tuple | None], list[str], bool]:
    """({fitted name: manifest bounds or None}, errors, calibration evidence)."""
    fitted: dict[str, tuple | None] = {}
    errs, evidence = [], False
    doc, err, exists = _load_yaml(run_dir, IDENT)
    evidence |= exists
    if err:
        errs.append(err)
    elif exists:
        ps = doc.get("parameters") if isinstance(doc, dict) else None
        if not isinstance(ps, list):
            errs.append(f"`{IDENT}` has no `parameters:` list.")
        else:
            for i, e in enumerate(ps):
                if isinstance(e, dict) and isinstance(e.get("name"), str) and e["name"].strip():
                    fitted[e["name"].strip()] = _interval([e.get("lower_bound"), e.get("upper_bound")])
                else:
                    errs.append(f"`{IDENT}` parameters[{i}] has no `name:`.")
    doc, err, exists = _load_yaml(run_dir, CALRES)
    evidence |= exists
    if err:
        errs.append(err)
    elif exists and not isinstance(doc, dict):
        errs.append(f"`{CALRES}` is not a mapping.")
    elif exists:
        for key in ("parameters", "fitted_parameters"):
            if doc.get(key) is None:
                continue
            names, bad = _names(doc[key])
            if bad:
                errs.append(f"`{CALRES}` `{key}:` has {bad} entry(ies) without a readable parameter name.")
            for n in names:
                fitted.setdefault(n, None)
    return fitted, errs, evidence


def _load_spec(run_dir):
    """(entries, targets, doc, findings, exists); entries is None when unusable."""
    doc, err, exists = _load_yaml(run_dir, PATH)
    if not exists:
        return None, None, None, [], False
    if err:
        return None, None, None, [_f("calibration_spec_invalid", "HIGH", err)], True
    if not isinstance(doc, dict) or not isinstance(doc.get("parameters"), list):
        return None, None, None, [_f("calibration_spec_invalid", "HIGH",
                                     f"`{PATH}` must be a mapping with a `parameters:` list.")], True
    out, entries, seen = [], [], set()
    for i, e in enumerate(doc["parameters"]):
        if not isinstance(e, dict) or not isinstance(e.get("name"), str) or not e["name"].strip():
            out.append(_f("calibration_spec_invalid", "HIGH", f"`{PATH}` parameters[{i}] has no `name:`."))
            continue
        name = e["name"].strip()
        if name in seen:
            out.append(_f("calibration_spec_invalid", "HIGH", f"`{PATH}`: parameter `{name}` is listed twice."))
            continue
        seen.add(name)
        role = str(e.get("role", "")).strip().lower()
        if role not in ROLES:
            out.append(_f("calibration_spec_invalid", "HIGH",
                          f"`{PATH}`: parameter `{name}` has role {e.get('role')!r}; it must be one of "
                          f"{', '.join(ROLES)}."))
            continue
        if not isinstance(e.get("calibrated"), bool):
            out.append(_f("calibration_spec_invalid", "HIGH",
                          f"`{PATH}`: parameter `{name}` needs `calibrated: true|false`."))
            continue
        m = e.get("matches", [])
        m = [m] if isinstance(m, str) else m
        if not isinstance(m, list) or not all(isinstance(x, str) and x.strip() for x in m):
            out.append(_f("calibration_spec_invalid", "HIGH",
                          f"`{PATH}`: parameter `{name}` `matches:` must be a list of names or globs."))
            continue
        if e.get("bounds") is not None and _interval(e["bounds"]) is None:
            out.append(_f("calibration_spec_invalid", "HIGH",
                          f"`{PATH}`: parameter `{name}` `bounds: {e['bounds']!r}` is not [lower, upper]."))
            continue
        if not isinstance(e.get("reason"), str) or len(e["reason"].strip()) < 3:
            out.append(_f("calibration_spec_reason_missing", "MEDIUM",
                          f"`{PATH}`: parameter `{name}` ({role}) gives no `reason:` for its role and for "
                          f"being {'calibrated' if e['calibrated'] else 'not calibrated'}."))
        entries.append(dict(e, name=name, role=role, matches=[x.strip() for x in m]))
    targets = doc.get("targets")
    return entries, targets, doc, out, True


def _covers(entry, fitted_name) -> bool:
    return fitted_name == entry["name"] or any(fnmatch.fnmatchcase(fitted_name, g) for g in entry["matches"])


def _check_targets(targets) -> list[dict]:
    if not isinstance(targets, list) or not targets:
        return [_f("calibration_target_metadata_missing", "MEDIUM",
                   f"`{PATH}` lists no `targets:`. Record each calibration target with "
                   f"{', '.join(TARGET_FIELDS)}.")]
    bad = []
    for i, t in enumerate(targets):
        if not isinstance(t, dict):
            bad.append(f"targets[{i}] (not a mapping)")
            continue
        miss = [k for k in TARGET_FIELDS if t.get(k) is None or (isinstance(t.get(k), str) and not t[k].strip())]
        if "provenance" not in miss and str(t["provenance"]).strip().lower() not in PROVENANCE:
            miss.append(f"provenance (must be {' or '.join(PROVENANCE)})")
        if miss:
            bad.append(f"{t.get('id') or f'targets[{i}]'}: {', '.join(miss)}")
    if not bad:
        return []
    return [_f("calibration_target_metadata_missing", "MEDIUM",
               f"`{PATH}`: {len(bad)} calibration target(s) lack required metadata ("
               + "; ".join(bad[:10]) + (" ..." if len(bad) > 10 else "")
               + "). A target modelled by others (an estimate from another model) is recorded as "
                 "`provenance: modelled_by_others`, not as an observation.")]


def _check_bounds(entry, fitted) -> list[dict]:
    out, name = [], entry["name"]
    for j, c in enumerate(entry.get("bound_changes") or []):
        if not isinstance(c, dict):
            out.append(_f("calibration_spec_invalid", "HIGH", f"`{PATH}`: `{name}` bound_changes[{j}] is not a mapping."))
            continue
        new, old = _interval(c.get("to")), _interval(c.get("from"))
        if new is None or old is None:
            out.append(_f("calibration_spec_invalid", "HIGH",
                          f"`{PATH}`: `{name}` bound_changes[{j}] needs `from:` and `to:` as [lower, upper]."))
            continue
        if not _wider(new, old):
            continue
        d = c.get("diagnosis") if isinstance(c.get("diagnosis"), dict) else {}
        miss = [k for k in DIAGNOSIS_ORDER if not (isinstance(d.get(k), str) and d[k].strip())]
        if miss:
            out.append(_f("calibration_bounds_widened_undiagnosed", "MEDIUM",
                          f"`{PATH}`: the bounds of `{name}` were widened from {list(old)} to {list(new)} "
                          f"(round {c.get('round', '?')}) without the diagnosis that comes first: "
                          f"{', '.join(miss)} not recorded. Check, in order, "
                          f"{', '.join(DIAGNOSIS_ORDER)}, and only then the bounds."))
    declared = _interval(entry.get("bounds")) if entry.get("bounds") is not None else None
    if declared is not None:
        wide = sorted(n for n, b in fitted.items() if b is not None and _covers(entry, n) and _wider(b, declared))
        if wide:
            out.append(_f("calibration_bounds_widened_undiagnosed", "MEDIUM",
                          f"`{IDENT}` searches {', '.join(wide[:5])} over a range wider than `{PATH}` declares "
                          f"for `{name}` ({list(declared)}). Record the change under `bound_changes` with its "
                          "diagnosis, or restore the declared bounds."))
    return out


def gate_findings(run_dir: str, round_n: int | None = None) -> list[dict]:
    late = "MEDIUM" if round_n is not None and round_n < 2 else "HIGH"
    entries, targets, _doc, out, exists = _load_spec(run_dir)
    if not exists:
        # required_artifact_missing (manifest, fitted_parameters) covers a run
        # with an identifiability manifest or a fitted model comparison. A fit
        # recorded only in calibration_result.yaml is not in that trigger, so
        # report the missing spec here (Codex review).
        fitted, _errs, _ev = fitted_parameters(run_dir)
        if fitted and _sibling("effort_floors")._calibration_evidence(run_dir) is None \
                and not os.path.exists(os.path.join(run_dir, "models", "model_comparison.yaml")):
            return [_f("calibration_spec_missing", "HIGH",
                       f"`{CALRES}` records fitted parameters ({', '.join(sorted(fitted)[:5])}) but `{PATH}` is "
                       "absent. Give every candidate parameter a role and every target its metadata.")]
        return []
    if entries is None:
        return out              # malformed: already HIGH
    fitted, errs, evidence = fitted_parameters(run_dir)
    if errs or (evidence and not fitted):
        why = "; ".join(errs) if errs else (f"`{IDENT}` / `{CALRES}` name no fitted parameter")
        out.append(_f("calibration_fitted_set_unreadable", "HIGH",
                      f"The fitted parameters cannot be read ({why}), so `{PATH}` cannot be checked against "
                      f"them. List them under `parameters:` in `{IDENT}` (name, point_estimate, bounds)."))
    # One finding per spec entry (a grouped entry may cover many fitted names).
    scen: dict[str, list[str]] = {}
    missing, declined = [], []
    for n in sorted(fitted):
        covering = [e for e in entries if _covers(e, n)]
        s = [e["name"] for e in covering if e["role"] == "scenario_assumption"]
        if s:
            scen.setdefault(s[0], []).append(n)
        elif not covering:
            missing.append(n)
        elif not any(e["calibrated"] for e in covering):
            declined.append(f"{n} (`{covering[0]['name']}`)")
    for name, ns in scen.items():
        out.append(_f("calibration_scenario_assumption_fitted", "HIGH",
                      f"Fitted parameter(s) {', '.join(ns[:10])}{' ...' if len(ns) > 10 else ''} are a "
                      f"`scenario_assumption` in `{PATH}` (entry `{name}`). A future or hypothetical input is a "
                      "scenario setting, never fitted to historical data: fix it and vary it across scenarios, "
                      "or give it the role it has."))
    for ns, what in ((missing, "have no entry"), (declined, "are listed only as `calibrated: false`")):
        if ns:
            out.append(_f("calibration_fitted_not_in_spec", late,
                          f"Fitted parameter(s) {', '.join(ns[:10])}{' ...' if len(ns) > 10 else ''} {what} in "
                          f"`{PATH}`. Give every fitted parameter a calibrated entry with a role and a reason "
                          "(an entry's `matches:` may cover a group of names)."))
    if fitted or evidence:
        out += _check_targets(targets)
    for e in entries:
        out += _check_bounds(e, fitted)
    return out


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def w(d, rel, obj):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(obj if isinstance(obj, str) else yaml.safe_dump(obj, sort_keys=False))

    kinds = lambda v: sorted(x["kind"] for x in v)
    tgt = {"id": "t1", "quantity": "prevalence", "population": "all units", "age": "under 5", "time": "2018",
           "units": "proportion", "source": "survey 2018", "uncertainty": "95% CI", "provenance": "observed"}
    ident = {"loss_fn": "models/outcome_fn.py::loss", "loss_type": "sse", "n_obs": 6,
             "parameters": [{"name": "level_1", "point_estimate": 2.0, "lower_bound": 0.5, "upper_bound": 10.0},
                            {"name": "level_2", "point_estimate": 3.0, "lower_bound": 0.5, "upper_bound": 10.0}]}
    good = {"question": "q", "targets": [tgt], "parameters": [
        {"name": "level", "matches": ["level_*"], "role": "country_specific_uncertain", "calibrated": True,
         "reason": "transmission differs by unit", "bounds": [0.5, 10.0]},
        {"name": "use_multiplier", "role": "tuning", "calibrated": False,
         "reason": "ran to its bound and traded off with the level; fixed at 1.0"},
        {"name": "future_coverage", "role": "scenario_assumption", "calibrated": False, "reason": "scenario input"}]}

    def spec(**kw):
        s = yaml.safe_load(yaml.safe_dump(good))
        s.update(kw)
        return s

    with tempfile.TemporaryDirectory() as d:
        ok(gate_findings(d, 2) == [], "CS0 no spec and no calibration: nothing")
        w(d, IDENT, ident)
        ok(gate_findings(d, 2) == [], "CS0b no spec: the manifest's required_artifact_missing covers it, not here")
        os.remove(os.path.join(d, IDENT))
        w(d, CALRES, {"parameters": {"level_1": 2.0}})
        v = gate_findings(d, 2)
        ok(kinds(v) == ["calibration_spec_missing"] and v[0]["severity"] == "HIGH",
           f"CS0c a fit recorded only in calibration_result.yaml still requires the spec (Codex review), got {v}")
        os.remove(os.path.join(d, CALRES))
        w(d, IDENT, ident)
        w(d, PATH, good)
        ok(gate_findings(d, 2) == [], f"CS1 a complete spec passes, got {gate_findings(d, 2)}")
        # scenario assumption fitted
        s = spec()
        s["parameters"][0]["role"] = "scenario_assumption"
        w(d, PATH, s)
        v = gate_findings(d, 2)
        ok(kinds(v) == ["calibration_scenario_assumption_fitted"] and "level_2" in v[0]["claim"] and v[0]["severity"] == "HIGH",
           f"CS2 a fitted scenario_assumption is HIGH, got {v}")
        ok(gate_findings(d, 1)[0]["severity"] == "HIGH", "CS2b ... at round 1 too (no drafting window)")
        # fitted parameter missing from the spec
        s = spec()
        s["parameters"][0]["matches"] = ["level_1"]
        w(d, PATH, s)
        v = gate_findings(d, 2)
        ok(kinds(v) == ["calibration_fitted_not_in_spec"] and "level_2" in v[0]["claim"] and v[0]["severity"] == "HIGH",
           f"CS3 a fitted parameter with no entry is HIGH, got {v}")
        ok(gate_findings(d, 1)[0]["severity"] == "MEDIUM", "CS3b round 1 is the drafting window")
        s = spec()
        s["parameters"][0]["calibrated"] = False
        w(d, PATH, s)
        ok(kinds(gate_findings(d, 2)) == ["calibration_fitted_not_in_spec"],
           "CS3c a fitted parameter listed only as calibrated: false contradicts the fit")
        w(d, CALRES, {"parameters": {"slope": 0.3}})
        w(d, PATH, good)
        v = gate_findings(d, 2)
        ok(kinds(v) == ["calibration_fitted_not_in_spec"] and "slope" in v[0]["claim"],
           f"CS3d calibration_result.yaml parameters count as fitted, got {v}")
        os.remove(os.path.join(d, CALRES))
        # fitted set unreadable: fail closed
        w(d, IDENT, "parameters: [")
        ok(kinds(gate_findings(d, 2)) == ["calibration_fitted_set_unreadable"], "CS4 unparseable manifest fails closed")
        w(d, IDENT, {"loss_fn": "x", "parameters": "level_1"})
        ok(kinds(gate_findings(d, 2)) == ["calibration_fitted_set_unreadable"], "CS4b no parameters list fails closed")
        os.remove(os.path.join(d, IDENT))
        w(d, CALRES, {"method": "nm", "best_loss": 1.0})
        ok(kinds(gate_findings(d, 2)) == ["calibration_fitted_set_unreadable"],
           "CS4c a calibration result naming no parameter fails closed")
        w(d, IDENT, ident)
        w(d, CALRES, {"parameters": ["level_1", {"future_coverage": 0.8}]})
        w(d, PATH, good)
        ok(kinds(gate_findings(d, 2)) == ["calibration_fitted_set_unreadable"],
           "CS4d a calibration result entry without a readable name fails closed (Codex review)")
        os.remove(os.path.join(d, CALRES))
        os.remove(os.path.join(d, IDENT))
        ef = _sibling("effort_floors")
        ok(ef._calibration_evidence(d) is None,
           "CS4e a spec alone is not evidence of fitting (it would require itself; Codex review)")
        w(d, IDENT, ident)
        # target metadata
        s = spec()
        del s["targets"][0]["uncertainty"]
        w(d, PATH, s)
        v = gate_findings(d, 2)
        ok(kinds(v) == ["calibration_target_metadata_missing"] and "uncertainty" in v[0]["claim"]
           and v[0]["severity"] == "MEDIUM", f"CS5 a target without uncertainty is MEDIUM, got {v}")
        s = spec()
        s["targets"][0]["provenance"] = "estimate"
        w(d, PATH, s)
        ok(kinds(gate_findings(d, 2)) == ["calibration_target_metadata_missing"],
           "CS5b provenance must be observed or modelled_by_others")
        w(d, PATH, spec(targets=[]))
        ok(kinds(gate_findings(d, 2)) == ["calibration_target_metadata_missing"], "CS5c no targets")
        # malformed entries fail closed
        s = spec()
        s["parameters"][1]["role"] = "knob"
        w(d, PATH, s)
        ok(kinds(gate_findings(d, 2)) == ["calibration_spec_invalid"], "CS6 unknown role is invalid")
        s = spec()
        del s["parameters"][1]["calibrated"]
        w(d, PATH, s)
        ok(kinds(gate_findings(d, 2)) == ["calibration_spec_invalid"], "CS6b calibrated must be a bool")
        w(d, PATH, "parameters: [")
        ok(kinds(gate_findings(d, 2)) == ["calibration_spec_invalid"], "CS6c unparseable spec fails closed")
        w(d, PATH, {"targets": [tgt]})
        ok(kinds(gate_findings(d, 2)) == ["calibration_spec_invalid"], "CS6d spec without parameters list")
        s = spec()
        s["parameters"][1]["reason"] = ""
        w(d, PATH, s)
        ok(kinds(gate_findings(d, 2)) == ["calibration_spec_reason_missing"], "CS7 an entry with no reason is MEDIUM")
        # bound widening
        s = spec()
        s["parameters"][0]["bound_changes"] = [{"round": 2, "from": [1.0, 5.0], "to": [0.5, 10.0]}]
        w(d, PATH, s)
        v = gate_findings(d, 2)
        ok(kinds(v) == ["calibration_bounds_widened_undiagnosed"] and "missing_data" in v[0]["claim"],
           f"CS8 a recorded widening without a diagnosis is MEDIUM, got {v}")
        s["parameters"][0]["bound_changes"][0]["diagnosis"] = {k: "checked; not the cause" for k in DIAGNOSIS_ORDER}
        w(d, PATH, s)
        ok(gate_findings(d, 2) == [], "CS8b a widening with the full B4 diagnosis passes")
        s["parameters"][0]["bound_changes"] = [{"round": 2, "from": [0.1, 20.0], "to": [0.5, 10.0]}]
        w(d, PATH, s)
        ok(gate_findings(d, 2) == [], "CS8c narrowing needs no diagnosis")
        s = spec()
        s["parameters"][0]["bounds"] = [1.0, 5.0]
        w(d, PATH, s)
        v = gate_findings(d, 2)
        ok(kinds(v) == ["calibration_bounds_widened_undiagnosed"] and "level_1" in v[0]["claim"],
           f"CS9 manifest bounds wider than the spec's declared bounds, got {v}")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--round", type=int, default=None)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    fitted, errs, evidence = fitted_parameters(a.run_dir)
    print(f"fitted parameters ({len(fitted)}): {', '.join(sorted(fitted)) or 'none'}"
          + (f"  [errors: {'; '.join(errs)}]" if errs else ""))
    if not os.path.exists(os.path.join(a.run_dir, PATH)):
        print(f"no {PATH}" + (" (the gate reports required_artifact_missing for a fitting run)" if evidence else ""))
    v = gate_findings(a.run_dir, a.round)
    for x in v:
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 1 if any(x["severity"] == "HIGH" for x in v) else 0


if __name__ == "__main__":
    sys.exit(main())
