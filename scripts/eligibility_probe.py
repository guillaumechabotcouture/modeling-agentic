#!/usr/bin/env python3
"""
Phase 21 δ — eligible-population consistency probe.

Error class: an intervention's BENEFIT is computed on a different
population than the one that can receive it (and that its COST is
charged for). Example found in a live run: a chemoprevention package
delivered only to children under 5 was costed on the under-5 population
but its cases-averted term multiplied by the TOTAL population, so the
optimizer credited it with averting adult cases it never touches. The
same bug shape appears for any targeted intervention — age-restricted
vaccines, school-based deworming, ART for diagnosed people only,
pregnancy-only interventions, facility-catchment programs.

This is a behavioral (black-box) check, not a code read: it calls the
modeler's own outcome function with perturbed inputs and checks three
invariants per declared intervention:

  (a) eligible population → 0          ⇒ marginal benefit → 0
  (b) eligible population × k (k < 1),  ⇒ marginal benefit shrinks
      total held fixed                    (it must respond at all)
  (c) total population × m,             ⇒ marginal benefit ~unchanged
      eligible held fixed                 (unless spillover is declared
                                           and justified — herd/indirect
                                           effects can legitimately
                                           reach non-recipients)

"Marginal benefit" = benefit(on_inputs) − benefit(off_inputs) on the
same baseline record, so outcome functions that return absolute burden
and ones that return benefit-vs-nothing both work.

The modeler writes `models/eligibility_manifest.yaml`:

```yaml
outcome_fn: models/outcome_fn.py::evaluate_unit  # same path::fn convention
                                                  # as models/outcome_fn.py
call_style: dict          # dict: fn(inputs) (default) | kwargs: fn(**inputs)
baseline_inputs:          # one representative unit record (inline), OR
  total_population: 120000
  eligible_children: 21000
  baseline_rate: 0.3
  package: none
# baseline_inputs_csv: models/units.csv   # first data row (or baseline_row: N)
tolerance: 0.05           # relative tolerance for (a) and (b)
total_scale: 2.0          # m in (c)
total_tolerance: 0.10     # relative tolerance for (c)
eligible_scale: 0.5       # k in (b)
interventions:
  - name: chemoprevention_children
    on_inputs:  {package: chemo}       # merged over baseline_inputs
    off_inputs: {package: none}
    eligible_population_field: eligible_children   # str or list of str
    total_population_field: total_population       # str or list of str
    benefit_output: cases_averted      # key in the returned dict
    spillover: false                   # true requires spillover_justification
    spillover_justification: ""
```

Findings (validator dict shape {kind, severity, claim, ...}):
  eligibility_benefit_without_eligible   HIGH    (a) fails
  eligibility_benefit_scales_with_total  HIGH    (c) fails, no spillover
                                         LOW     (c) fails, spillover justified
  eligibility_insensitive                MEDIUM  (b) fails, or zero baseline
                                                 marginal benefit (probe
                                                 uninformative)
  eligibility_manifest_invalid           HIGH    schema problems
  eligibility_probe_error                HIGH    outcome_fn raised / non-
                                                 deterministic / non-numeric
  eligibility_manifest_missing           HIGH    only when check(require=True)

Usage:
    python3 scripts/eligibility_probe.py <run_dir>     # writes models/eligibility_probe.yaml
    python3 scripts/eligibility_probe.py --self-test

Exit: 0 no HIGH findings · 1 any HIGH · 2 usage error
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import math
import os
import sys
from typing import Any, Callable, Optional

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed.", file=sys.stderr)
    sys.exit(2)


MANIFEST_REL = os.path.join("models", "eligibility_manifest.yaml")
REPORT_REL = os.path.join("models", "eligibility_probe.yaml")
PRODUCER = "scripts/eligibility_probe.py"
VERDICTS = ("PASS", "FAIL", "ABSENT", "NOT_APPLICABLE")

DEFAULTS = {"tolerance": 0.05, "total_scale": 2.0,
            "total_tolerance": 0.10, "eligible_scale": 0.5}


def _finding(kind: str, severity: str, claim: str, **extra) -> dict:
    return {"kind": kind, "severity": severity, "claim": claim, **extra}


def _load_fn(run_dir: str, spec: str) -> Callable:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    try:
        import propagate_uncertainty as pu  # reuse the outcome_fn loader
    finally:
        sys.path.pop(0)
    return pu.load_outcome_fn(run_dir, spec)


def _coerce(v: str) -> Any:
    try:
        f = float(v)
        return int(f) if f.is_integer() and "." not in v else f
    except (TypeError, ValueError):
        return v


def _baseline(run_dir: str, m: dict) -> dict:
    if isinstance(m.get("baseline_inputs"), dict):
        return dict(m["baseline_inputs"])
    rel = m.get("baseline_inputs_csv")
    if not rel:
        raise ValueError("need `baseline_inputs` (mapping) or `baseline_inputs_csv`")
    path = os.path.join(run_dir, rel)
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    idx = int(m.get("baseline_row", 0))
    if not rows or idx >= len(rows):
        raise ValueError(f"{rel}: no row {idx}")
    return {k: _coerce(v) for k, v in rows[idx].items()}


def _as_list(v) -> list:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v)


def _scaled(inputs: dict, fields: list, factor: float) -> dict:
    out = dict(inputs)
    for fld in fields:
        v = out[fld]
        nv = v * factor
        out[fld] = int(round(nv)) if isinstance(v, int) and not isinstance(v, bool) else nv
    return out


def _validate_manifest(m: Any) -> list[str]:
    errs: list[str] = []
    if not isinstance(m, dict):
        return ["manifest must be a YAML mapping"]
    if not m.get("outcome_fn"):
        errs.append("missing `outcome_fn` (path::fn)")
    if m.get("call_style", "dict") not in ("dict", "kwargs"):
        errs.append("`call_style` must be dict or kwargs")
    ivs = m.get("interventions")
    if not isinstance(ivs, list) or not ivs:
        errs.append("`interventions` must be a non-empty list")
        return errs
    for i, iv in enumerate(ivs):
        if not isinstance(iv, dict):
            errs.append(f"interventions[{i}] must be a mapping")
            continue
        tag = iv.get("name", f"[{i}]")
        for key in ("name", "eligible_population_field",
                    "total_population_field", "benefit_output"):
            if not iv.get(key):
                errs.append(f"intervention {tag}: missing `{key}`")
        if not isinstance(iv.get("on_inputs"), dict):
            errs.append(f"intervention {tag}: `on_inputs` must be a mapping")
        if not isinstance(iv.get("off_inputs", {}), dict):
            errs.append(f"intervention {tag}: `off_inputs` must be a mapping")
        if set(_as_list(iv.get("eligible_population_field"))) & \
                set(_as_list(iv.get("total_population_field"))):
            errs.append(f"intervention {tag}: eligible and total fields overlap")
        if iv.get("spillover") and not str(iv.get("spillover_justification") or "").strip():
            errs.append(f"intervention {tag}: `spillover: true` requires a "
                        f"non-empty `spillover_justification`")
    for k in DEFAULTS:
        if k in m:
            try:
                float(m[k])
            except (TypeError, ValueError):
                errs.append(f"`{k}` must be numeric")
    return errs


def probe(run_dir: str) -> dict:
    """Run the probe. Returns {verdict, findings, interventions}."""
    path = os.path.join(run_dir, MANIFEST_REL)
    if not os.path.exists(path):
        return {"verdict": "ABSENT", "findings": [], "interventions": {}}
    try:
        with open(path) as f:
            m = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        return {"verdict": "FAIL", "interventions": {}, "findings": [_finding(
            "eligibility_manifest_invalid", "HIGH",
            f"{MANIFEST_REL} unreadable: {e}")]}
    # A run whose interventions all reach the whole population has no
    # eligible subgroup to probe (Copilot review of PR #14). It must say so
    # explicitly, with a reason, and list no interventions; anything else
    # is validated as a normal manifest.
    if isinstance(m, dict) and m.get("targeted_interventions") == "none":
        why = str(m.get("no_targeted_justification") or "").strip()
        if why and not m.get("interventions"):
            return {"verdict": "NOT_APPLICABLE", "findings": [], "interventions": {},
                    "justification": why}
        return {"verdict": "FAIL", "interventions": {}, "findings": [_finding(
            "eligibility_manifest_invalid", "HIGH",
            f"{MANIFEST_REL}: `targeted_interventions: none` needs a non-empty "
            f"`no_targeted_justification` and no `interventions` list")]}
    errs = _validate_manifest(m)
    if errs:
        return {"verdict": "FAIL", "interventions": {}, "findings": [_finding(
            "eligibility_manifest_invalid", "HIGH",
            f"{MANIFEST_REL}: " + "; ".join(errs))]}

    cfg = {k: float(m.get(k, v)) for k, v in DEFAULTS.items()}
    try:
        fn = _load_fn(run_dir, m["outcome_fn"])
        base = _baseline(run_dir, m)
    except Exception as e:  # noqa: BLE001 — surfaced as a HIGH finding
        return {"verdict": "FAIL", "interventions": {}, "findings": [_finding(
            "eligibility_probe_error", "HIGH",
            f"could not load outcome_fn / baseline: {type(e).__name__}: {e}")]}
    kwargs_style = m.get("call_style", "dict") == "kwargs"

    def call(inputs: dict, out_key: str) -> float:
        res = fn(**copy.deepcopy(inputs)) if kwargs_style else fn(copy.deepcopy(inputs))
        if not isinstance(res, dict) or out_key not in res:
            raise ValueError(f"outcome_fn did not return a dict with "
                             f"`{out_key}` (got {type(res).__name__})")
        v = res[out_key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"`{out_key}` is not a finite number: {v!r}")
        return float(v)

    findings: list[dict] = []
    measurements: dict[str, dict] = {}
    for iv in m["interventions"]:
        name = str(iv["name"])
        elig = _as_list(iv["eligible_population_field"])
        total = _as_list(iv["total_population_field"])
        out_key = str(iv["benefit_output"])
        on = {**base, **iv["on_inputs"]}
        off = {**base, **(iv.get("off_inputs") or {})}
        missing = [f for f in elig + total if f not in on]
        if missing:
            findings.append(_finding(
                "eligibility_manifest_invalid", "HIGH",
                f"intervention {name}: fields {missing} not present in the "
                f"baseline inputs", intervention=name))
            continue

        def marginal(transform) -> float:
            return call(transform(on), out_key) - call(transform(off), out_key)

        try:
            mb = marginal(lambda d: d)
            if abs(marginal(lambda d: d) - mb) > 1e-9 * max(1.0, abs(mb)):
                raise ValueError("outcome_fn is non-deterministic for identical "
                                 "inputs (fix the seed inside the function)")
            mb_zero = marginal(lambda d: _scaled(d, elig, 0.0))
            mb_k = marginal(lambda d: _scaled(d, elig, cfg["eligible_scale"]))
            mb_tot = marginal(lambda d: _scaled(d, total, cfg["total_scale"]))
        except Exception as e:  # noqa: BLE001
            findings.append(_finding(
                "eligibility_probe_error", "HIGH",
                f"intervention {name}: {type(e).__name__}: {e}",
                intervention=name))
            continue

        rec = {"marginal_benefit": mb, "eligible_zero": mb_zero,
               f"eligible_x{cfg['eligible_scale']:g}": mb_k,
               f"total_x{cfg['total_scale']:g}": mb_tot,
               "eligible_fields": elig, "total_fields": total,
               "benefit_output": out_key}
        measurements[name] = rec
        scale = abs(mb)
        if scale <= 1e-12:
            findings.append(_finding(
                "eligibility_insensitive", "MEDIUM",
                f"intervention {name}: marginal `{out_key}` is 0 at the "
                f"baseline record, so the probe is uninformative — choose a "
                f"baseline unit where the intervention has an effect, or "
                f"check that on_inputs actually switches it on.",
                intervention=name, evidence=rec))
            continue
        tol = cfg["tolerance"]
        if abs(mb_zero) > tol * scale:
            findings.append(_finding(
                "eligibility_benefit_without_eligible", "HIGH",
                f"intervention {name}: with {elig} set to 0 the marginal "
                f"`{out_key}` is still {mb_zero:.4g} "
                f"({mb_zero / mb:.0%} of baseline {mb:.4g}). Benefit must come "
                f"only from people who can receive the intervention.",
                intervention=name, evidence=rec))
        shrank = (mb - mb_k) * math.copysign(1.0, mb) > tol * scale
        if not shrank:
            findings.append(_finding(
                "eligibility_insensitive", "MEDIUM",
                f"intervention {name}: scaling {elig} by "
                f"{cfg['eligible_scale']:g} (total fixed) changed marginal "
                f"`{out_key}` from {mb:.4g} to {mb_k:.4g} — benefit does not "
                f"shrink with the eligible population.",
                intervention=name, evidence=rec))
        rel_tot = mb_tot / mb - 1.0
        if abs(rel_tot) > cfg["total_tolerance"]:
            justified = bool(iv.get("spillover"))
            findings.append(_finding(
                "eligibility_benefit_scales_with_total",
                "LOW" if justified else "HIGH",
                f"intervention {name}: scaling {total} by "
                f"{cfg['total_scale']:g} with {elig} held fixed changed marginal "
                f"`{out_key}` by {rel_tot:+.0%} ({mb:.4g} → {mb_tot:.4g}). "
                + ("Declared spillover: "
                   + str(iv.get("spillover_justification")).strip()
                   if justified else
                   "Benefit is being computed on the total population rather "
                   "than the eligible one (or declare `spillover: true` with a "
                   "justification if indirect effects are real)."),
                intervention=name, evidence=rec))

    verdict = ("FAIL" if any(f["severity"] == "HIGH" for f in findings)
               else "PASS")
    return {"verdict": verdict, "findings": findings,
            "interventions": measurements}


def check(run_dir: str, require: bool = False) -> list[dict]:
    """Validator entry point. Returns findings in the validator's dict
    shape. With require=True a missing manifest is a HIGH finding (the
    caller decides when the contract applies, e.g. when an allocation or
    intervention comparison exists)."""
    rep = probe(run_dir)
    if rep["verdict"] == "ABSENT":
        if require:
            return [_finding(
                "eligibility_manifest_missing", "HIGH",
                f"{MANIFEST_REL} is required for runs that compare or "
                f"allocate interventions. Declare each intervention's "
                f"eligible-population and total-population input fields "
                f"(see the eligible-population-consistency skill).")]
        return []
    return rep["findings"]


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    import tempfile
    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    manifest_tpl = """outcome_fn: models/outcome_fn.py::outcome_fn
baseline_inputs:
  total_population: 100000
  eligible: 18000
  rate: 0.3
  active: []
interventions:
  - name: targeted
    on_inputs: {{active: [targeted]}}
    off_inputs: {{active: []}}
    eligible_population_field: eligible
    total_population_field: total_population
    benefit_output: cases_averted
{extra}
"""

    def run_case(src: str, extra: str = "") -> dict:
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "models"))
            with open(os.path.join(d, "models", "outcome_fn.py"), "w") as f:
                f.write(src)
            with open(os.path.join(d, MANIFEST_REL), "w") as f:
                f.write(manifest_tpl.format(extra=extra))
            return probe(d)

    def kinds(rep):
        return {(f["kind"], f["severity"]) for f in rep["findings"]}

    # S1: correct — benefit scales with the eligible population only.
    correct = ("def outcome_fn(p):\n"
               "    eff = 0.6 if 'targeted' in p['active'] else 0.0\n"
               "    return {'cases_averted': eff * p['rate'] * p['eligible']}\n")
    r1 = run_case(correct)
    ok(r1["verdict"] == "PASS" and not r1["findings"],
       f"S1: correct model passes; got {r1['findings']}")

    # S2: the buggy pattern — benefit × TOTAL population while the
    # intervention only reaches the eligible group.
    buggy = ("def outcome_fn(p):\n"
             "    eff = 0.6 if 'targeted' in p['active'] else 0.0\n"
             "    return {'cases_averted': eff * p['rate'] * p['total_population']}\n")
    r2 = run_case(buggy)
    k2 = kinds(r2)
    ok(r2["verdict"] == "FAIL"
       and ("eligibility_benefit_without_eligible", "HIGH") in k2
       and ("eligibility_benefit_scales_with_total", "HIGH") in k2
       and ("eligibility_insensitive", "MEDIUM") in k2,
       f"S2: total-population benefit flagged; got {k2}")

    # S3: herd/indirect effect — benefit reaches non-recipients in
    # proportion to coverage among the eligible. Justified spillover
    # downgrades (c) to LOW; (a) still holds (no recipients → no benefit).
    herd = ("def outcome_fn(p):\n"
            "    on = 'targeted' in p['active'] and p['eligible'] > 0\n"
            "    direct = 0.6 * p['rate'] * p['eligible'] if on else 0.0\n"
            "    # reduced transmission protects non-recipients too\n"
            "    indirect = (0.05 * p['rate'] * (p['total_population'] - p['eligible'])\n"
            "                if on else 0.0)\n"
            "    return {'cases_averted': direct + indirect}\n")
    r3 = run_case(herd, "    spillover: true\n"
                        "    spillover_justification: \"indirect protection of "
                        "non-recipients via reduced transmission\"")
    ok(r3["verdict"] == "PASS"
       and all(f["severity"] == "LOW" for f in r3["findings"]),
       f"S3: justified spillover passes with LOW info; got {r3['findings']}")
    r3b = run_case(herd)
    ok(("eligibility_benefit_scales_with_total", "HIGH") in kinds(r3b),
       f"S3b: same model without spillover declaration → HIGH; got {kinds(r3b)}")

    # S4: spillover without justification → manifest invalid.
    r4 = run_case(correct, "    spillover: true")
    ok(("eligibility_manifest_invalid", "HIGH") in kinds(r4),
       f"S4: spillover needs justification; got {kinds(r4)}")

    # S5: outcome_fn raising / wrong field → probe error / invalid.
    r5 = run_case("def outcome_fn(p):\n    raise RuntimeError('x')\n")
    ok(("eligibility_probe_error", "HIGH") in kinds(r5),
       f"S5: raising outcome_fn → probe error; got {kinds(r5)}")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "outcome_fn.py"), "w") as f:
            f.write(correct)
        with open(os.path.join(d, MANIFEST_REL), "w") as f:
            f.write(manifest_tpl.format(extra="").replace(
                "eligible_population_field: eligible",
                "eligible_population_field: not_a_field"))
        ok(("eligibility_manifest_invalid", "HIGH") in kinds(probe(d)),
           "S5b: unknown field → manifest invalid")

    # S6: kwargs call style (positional-ish model functions).
    kw = ("def evaluate(package, rate, population, eligible):\n"
          "    eff = 0.6 if package == 'targeted' else 0.0\n"
          "    return {'cases_averted': eff * rate * eligible}\n")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        with open(os.path.join(d, "models", "model.py"), "w") as f:
            f.write(kw)
        with open(os.path.join(d, MANIFEST_REL), "w") as f:
            f.write("outcome_fn: models/model.py::evaluate\n"
                    "call_style: kwargs\n"
                    "baseline_inputs: {package: none, rate: 0.3, "
                    "population: 100000, eligible: 18000}\n"
                    "interventions:\n"
                    "  - name: targeted\n"
                    "    on_inputs: {package: targeted}\n"
                    "    off_inputs: {package: none}\n"
                    "    eligible_population_field: eligible\n"
                    "    total_population_field: population\n"
                    "    benefit_output: cases_averted\n")
        r6 = probe(d)
        ok(r6["verdict"] == "PASS", f"S6: kwargs style passes; got {r6['findings']}")

    # S7: check() contract — absent manifest silent unless required.
    with tempfile.TemporaryDirectory() as d:
        ok(check(d) == [], "S7: absent manifest → [] by default")
        req = check(d, require=True)
        ok(len(req) == 1 and req[0]["kind"] == "eligibility_manifest_missing"
           and req[0]["severity"] == "HIGH", f"S7b: require=True → HIGH; got {req}")

    # S8: zero baseline effect → uninformative MEDIUM, not a silent pass.
    r8 = run_case("def outcome_fn(p):\n    return {'cases_averted': 0.0}\n")
    ok(("eligibility_insensitive", "MEDIUM") in kinds(r8),
       f"S8: zero marginal benefit → MEDIUM; got {kinds(r8)}")

    # S9 (Copilot review): whole-population-only runs declare it explicitly;
    # the declaration needs a reason and may not list interventions.
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        mp = os.path.join(d, MANIFEST_REL)
        for body, want in (
                ("targeted_interventions: none\nno_targeted_justification: every "
                 "intervention is delivered to the whole population\n", "NOT_APPLICABLE"),
                ("targeted_interventions: none\n", "FAIL"),
                ("targeted_interventions: none\nno_targeted_justification: x\n"
                 "interventions: [{name: a}]\n", "FAIL")):
            with open(mp, "w") as f:
                f.write(body)
            got = probe(d)["verdict"]
            ok(got == want, f"S9: {body!r} → {got}, want {want}")
        with open(mp, "w") as f:
            f.write("targeted_interventions: none\nno_targeted_justification: whole population\n")
        ok(check(d, require=True) == [], "S9b: valid declaration satisfies require=True")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--json", action="store_true")
    p.add_argument("--output", default=None)
    args = p.parse_args()
    if args.self_test:
        return _run_self_test()
    if not args.run_dir:
        p.error("run_dir required (or use --self-test)")

    rep = probe(args.run_dir)
    out = {"producer": PRODUCER, **rep}
    if rep["verdict"] != "ABSENT":
        out_path = args.output or os.path.join(args.run_dir, REPORT_REL)
        with open(out_path, "w") as f:
            yaml.safe_dump(out, f, default_flow_style=False, sort_keys=False)
        print(f"eligibility_probe.yaml written: {out_path}", file=sys.stderr)
    print(f"  verdict: {rep['verdict']}", file=sys.stderr)
    for fd in rep["findings"]:
        print(f"  [{fd['severity']}] {fd['kind']}: {fd['claim']}", file=sys.stderr)
    if args.json:
        print(json.dumps(out, indent=2, default=str))
    return 1 if rep["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
