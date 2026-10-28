#!/usr/bin/env python3
"""Phase 38 β: each intervention's modeled effect, measured against the run's
primary comparator, is held to the trial evidence for that comparison.

    python3 scripts/effect_concordance.py <run_dir>
    python3 scripts/effect_concordance.py --self-test

E7 attempt 7: every single-trial benchmark passed, yet the plan's net effect
against the comparator (round 2, finding R-021) was an 83% burden reduction in
covered seasons where trials of the same net type against the nets the
comparator still had support about 45%; the critics found it in round 2 and the
fix was a cap added after the library had been run. The modeler writes
`models/effect_concordance.yaml` with the first model:

    effects:
      - id: E1
        intervention: "<package component>"
        registry: <name in the citations.md Parameter Registry>   # trial effect, with its CI
        trial_comparator: "<what the trial's control arm received>"
        trial_use: 0.8          # use / coverage in the trial's intervention arm (default 1)
        comparator_id: CF1      # decision-brief comparator the model effect is measured against
        comparator_delivers: "<what CF1 already delivers in these units, for this intervention>"
        units: "<the units and period measured, e.g. units receiving only this component, covered season>"
        model_ratio: 0.62       # outcome under the plan / outcome under the comparator, in those units
        model_use: 0.63         # use / coverage the model attains there

The registry effect must be the trial comparison that matches what the
comparator already delivers (e.g. a new product against the product the
comparator keeps, not against nothing). The check: the modeled reduction
1 - model_ratio may not exceed the trial's largest plausible reduction
(1 - ci_low for a relative risk, rate ratio, hazard ratio or ratio; ci_high for
an efficacy), scaled by model_use / trial_use (capped at 1), by more than 10%.
An odds ratio must be converted to a risk ratio first.
Exceeding it is HIGH (scope-declarable, so the write-up must say why).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

import yaml

PATH = os.path.join("models", "effect_concordance.yaml")
TOL = 0.10
RATIO_KINDS = ("relative_risk", "incidence_rate_ratio", "hazard_ratio", "ratio")
REDUCTION_KINDS = ("efficacy",)


def _max_reduction(entry):
    """(largest plausible reduction, point reduction) from a registry entry, or
    (None, reason)."""
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
_REQ = ("id", "intervention", "registry", "trial_comparator", "comparator_id", "comparator_delivers", "units",
        "model_ratio")


def _registry(run_dir: str) -> dict:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import effect_size_registry as esr
    try:
        params = esr.load_priors(os.path.join(run_dir, "citations.md")).get("parameters") or []
    except Exception:  # noqa: BLE001 — a missing or broken registry fails each effect closed below
        return {}
    return {str(p.get("name")): p for p in params if isinstance(p, dict) and p.get("name")}


def _comparator_ids(run_dir: str) -> set[str] | None:
    """CF ids of the decision brief, or None when the brief has none (older runs)."""
    try:
        doc = yaml.safe_load(open(os.path.join(run_dir, "decision_brief.yaml"))) or {}
    except (OSError, yaml.YAMLError):
        return None
    cfs = doc.get("comparators") if isinstance(doc, dict) else None
    if not isinstance(cfs, list) or not cfs:
        return None
    return {str(c.get("id")) for c in cfs if isinstance(c, dict)}


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def evaluate(run_dir: str) -> tuple[list[dict], list[str]]:
    """(rows, errors). A row has the effect's numbers, its bound and whether it exceeds it."""
    p = os.path.join(run_dir, PATH)
    try:
        doc = yaml.safe_load(open(p))
    except OSError:
        return [], []
    except yaml.YAMLError as e:
        return [], [f"{PATH} does not parse: {str(e)[:150]}"]
    effects = doc.get("effects") if isinstance(doc, dict) else None
    if not isinstance(effects, list) or not effects:
        return [], [f"{PATH}: expected a non-empty `effects:` list"]
    reg, cfs, rows, errors = _registry(run_dir), _comparator_ids(run_dir), [], []
    for e in effects:
        if not isinstance(e, dict):
            errors.append(f"{PATH}: an entry is not a mapping")
            continue
        eid = str(e.get("id") or "?")
        missing = [k for k in _REQ if e.get(k) in (None, "")]
        if missing:
            errors.append(f"{eid}: missing {', '.join(missing)}")
            continue
        if cfs is not None and str(e["comparator_id"]) not in cfs:
            errors.append(f"{eid}: comparator_id {e['comparator_id']} is not a decision-brief comparator "
                          f"({', '.join(sorted(cfs))})")
            continue
        r = reg.get(str(e["registry"]))
        if r is None:
            errors.append(f"{eid}: registry entry {e['registry']} not found in the citations.md Parameter Registry")
            continue
        trial_max, trial_point = _max_reduction(r)
        if trial_max is None:
            errors.append(f"{eid}: registry entry {e['registry']} {trial_point}")
            continue
        mr, mu, tu = _num(e.get("model_ratio")), _num(e.get("model_use", 1.0)), _num(e.get("trial_use", 1.0))
        if mr is None or mr <= 0 or mu is None or not 0 < mu <= 1 or tu is None or not 0 < tu <= 1:
            errors.append(f"{eid}: model_ratio must be > 0 and model_use, trial_use in (0, 1]")
            continue
        bound = trial_max * min(1.0, mu / tu)
        red = 1 - mr
        rows.append({"id": eid, "intervention": e["intervention"], "registry": e["registry"],
                     "trial_reduction": round(trial_point, 4), "trial_max_reduction": round(trial_max, 4),
                     "trial_comparator": e["trial_comparator"],
                     "comparator_id": e["comparator_id"], "comparator_delivers": e["comparator_delivers"],
                     "units": e["units"], "model_ratio": mr, "model_use": mu, "trial_use": tu,
                     "model_reduction": round(red, 4), "max_reduction": round(bound, 4),
                     "exceeds": red > bound * (1 + TOL) + 1e-9})
    return rows, errors


def gate_findings(run_dir: str) -> list[dict]:
    """Findings for the STAGE 7 gate. A missing file is the rigor manifest's
    finding (models/effect_concordance.yaml, triggered by an allocation)."""
    rows, errors = evaluate(run_dir)
    out = [{"kind": "effect_concordance_invalid", "severity": "HIGH", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": False, "claim": f"{PATH}: {e}"} for e in errors]
    for r in rows:
        if r["exceeds"]:
            out.append({
                "kind": "effect_exceeds_trial_evidence", "severity": "HIGH", "stage": "MODEL",
                "target_stage": "MODEL", "scope_declarable": True,
                "claim": (f"{r['id']} ({r['intervention']}): the model's effect against {r['comparator_id']} "
                          f"({r['comparator_delivers']}) is a {r['model_reduction']:.0%} reduction in {r['units']}; "
                          f"the trial evidence ({r['registry']}: {r['trial_reduction']:.0%} reduction, at most "
                          f"{r['trial_max_reduction']:.0%} within its CI, vs {r['trial_comparator']}) supports at most "
                          f"{r['max_reduction']:.0%} at the model's use "
                          f"({r['model_use']} vs {r['trial_use']} in the trial). Bring the effect within the evidence "
                          "(constrain it to the trial effect, fix the comparator or the composition), or say in the "
                          "write-up why the trial does not bound it.")})
    return out


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    reg = ("# Citations\n\n## Parameter Registry\n\n```yaml\n"
           "- name: newnet_vs_oldnet_irr\n  value: 0.55\n  ci_low: 0.45\n  ci_high: 0.67\n  kind: incidence_rate_ratio\n"
           "  source: C1\n- name: bad_ratio\n  value: 3.0\n  kind: count\n  source: C2\n"
           "- name: vx_eff\n  value: 0.85\n  ci_low: 0.75\n  ci_high: 0.92\n  kind: efficacy\n  source: C3\n"
           "- name: or_eff\n  value: 0.4\n  ci_low: 0.3\n  ci_high: 0.5\n  kind: odds_ratio\n  source: C4\n```\n")
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, "models"))
        open(os.path.join(d, "citations.md"), "w").write(reg)
        yaml.safe_dump({"comparators": [{"id": "CF1", "role": "primary", "text": "old nets kept"}]},
                       open(os.path.join(d, "decision_brief.yaml"), "w"))
        ok(gate_findings(d) == [], "EC1 no file: nothing here (the manifest reports a missing artifact)")
        base = {"id": "E1", "intervention": "new nets", "registry": "newnet_vs_oldnet_irr",
                "trial_comparator": "old nets", "comparator_id": "CF1", "comparator_delivers": "old nets at 2023 use",
                "units": "new-net-only units, covered season", "trial_use": 0.9}

        def put(*effects):
            yaml.safe_dump({"effects": list(effects)}, open(os.path.join(d, PATH), "w"))
        # E7 attempt 7, R-021: 0.168 (83% reduction) at use 0.63 vs a trial bound of 55% x 0.63/0.9 = 38.5%.
        put({**base, "model_ratio": 0.168, "model_use": 0.63})
        f = gate_findings(d)
        ok(len(f) == 1 and f[0]["kind"] == "effect_exceeds_trial_evidence" and "83%" in f[0]["claim"]
           and ("38%" in f[0]["claim"] or "39%" in f[0]["claim"]), f"EC2 attempt 7's net effect is caught: {f}")
        put({**base, "model_ratio": 0.66, "model_use": 0.63})
        ok(gate_findings(d) == [], f"EC3 an effect within the trial bound passes: {gate_findings(d)}")
        put({**base, "model_ratio": 0.60, "model_use": 0.63})
        ok(gate_findings(d) == [], f"EC4 within the 10% tolerance (40% vs 38.5%) passes: {gate_findings(d)}")
        put({**base, "model_ratio": 0.35, "model_use": 1.0, "trial_use": 0.9})
        ok(len(gate_findings(d)) == 1, "EC5 use above the trial's does not raise the bound past 1 - ci_low")
        put({**base, "comparator_id": "CF9", "model_ratio": 0.9}, {**base, "id": "E2", "registry": "nope", "model_ratio": 0.9},
            {**base, "id": "E3", "registry": "bad_ratio", "model_ratio": 0.9}, {**base, "id": "E4", "model_ratio": 0.9, "units": ""},
            {**base, "id": "E5", "model_ratio": -1})
        f = gate_findings(d)
        claims = " | ".join(x["claim"] for x in f)
        ok(len(f) == 5 and all(x["kind"] == "effect_concordance_invalid" and not x["scope_declarable"] for x in f)
           and "CF9" in claims and "nope" in claims and "bad_ratio" in claims and "missing units" in claims,
           f"EC6 unknown comparator, missing or non-ratio registry entry, missing fields fail closed: {claims}")
        open(os.path.join(d, PATH), "w").write("effects: [\n")
        ok(gate_findings(d)[0]["kind"] == "effect_concordance_invalid", "EC7 an unparseable file fails closed")
        yaml.safe_dump({"effects": []}, open(os.path.join(d, PATH), "w"))
        ok(gate_findings(d)[0]["kind"] == "effect_concordance_invalid", "EC8 an empty effects list fails closed")
        # efficacy: bound = ci_high 0.92 x (0.5 / 0.9) = 51%; 50% passes, 70% fails.
        put({**base, "registry": "vx_eff", "model_ratio": 0.50, "model_use": 0.5, "trial_use": 0.9},
            {**base, "id": "E2", "registry": "vx_eff", "model_ratio": 0.30, "model_use": 0.5, "trial_use": 0.9})
        f = gate_findings(d)
        ok(len(f) == 1 and f[0]["claim"].startswith("E2") and "92% within its CI" in f[0]["claim"],
           f"EC10 an efficacy is bounded by its upper CI, scaled to use: {f}")
        put({**base, "registry": "or_eff", "model_ratio": 0.9})
        ok("odds ratio" in gate_findings(d)[0]["claim"], "EC11 an odds ratio must be converted first")
        os.remove(os.path.join(d, "decision_brief.yaml"))
        put({**base, "comparator_id": "CF7", "model_ratio": 0.66, "model_use": 0.63})
        ok(gate_findings(d) == [], "EC9 a run whose brief has no comparators does not check the comparator id")
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
    rows, errors = evaluate(a.run_dir)
    for r in rows:
        print(f"{r['id']} {r['intervention']}: model reduction {r['model_reduction']:.0%} vs max "
              f"{r['max_reduction']:.0%} ({'EXCEEDS' if r['exceeds'] else 'ok'})")
    for e in errors:
        print("ERROR:", e)
    return 1 if errors or any(r["exceeds"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
