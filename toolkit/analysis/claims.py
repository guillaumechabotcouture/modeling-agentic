"""Claims ledger and results.md, rendered from the analysis artifacts (design §2).

The harness writes `models/claims_spec.yaml` (each claim names its source artifact and
field, never a value), builds `models/claims_ledger.yaml` with scripts/build_claims_ledger.py
(values resolved from the artifacts by scripts/lib/provenance.py), and renders `results.md`
from a harness template with scripts/render_claims.py. Interval claims carry
`interval: {level, covers, excludes}` from the spec, from which render_claims derives the label (lesson 6).
The writer's narrative cites the same claim ids.
"""
from __future__ import annotations

import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRODUCER = "toolkit/analysis/claims.py"
SPEC_REL = os.path.join("models", "claims_spec.yaml")


def _scripts():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import build_claims_ledger
    import render_claims
    return build_claims_ledger, render_claims


def _c(cid, kind, art, field, units="", **kw):
    return {"id": cid, "claim_kind": kind, "units": units, "source_artifact": art, "source_field": field,
            "produced_by": "harness", **kw}


def claim_spec(A, arts: dict, point: dict, uq: dict, stab: dict, dts: list, cal: dict) -> list[dict]:
    o = A.primary
    cl = [_c("headline.point", "scalar", arts["point"], f"data.averted.{o}", o),
          _c("headline.median", "scalar", arts["uncertainty"], f"data.fixed_plan.{o}.median", o,
             interval=uq["interval"]),
          _c("cost.allocated", "currency", arts["point"], "data.cost_allocated", "USD"),
          _c("cost.allocatable", "currency", arts["point"], "data.budget_allocatable", "USD"),
          _c("units.total", "count", arts["point"], "data.n_units", "units"),
          _c("units.no_package", "count", arts["point"], "data.mix.no_package", "units"),
          _c("uq.draws", "count", arts["uncertainty"], "data.draws", "draws"),
          _c("uq.units_stable_90", "count", arts["uncertainty"], "data.units_same_package_in_90pct_of_draws", "units")]
    for oc in A.outcomes[1:]:
        cl.append(_c(f"averted.{oc}.point", "scalar", arts["point"], f"data.averted.{oc}", oc))
        cl.append(_c(f"averted.{oc}.median", "scalar", arts["uncertainty"], f"data.fixed_plan.{oc}.median", oc,
                     interval=uq["interval"]))
    for c in A.spec["options"]["components"]:
        cl.append(_c(f"mix.{c}", "count", arts["point"], f"data.mix.{c}", "units"))
    if stab.get("folds"):
        cl += [_c("stability.value_lost_worst", "percentage", arts["stability"],
                  "data.metrics.total_benefit_loss_pct_worst_fold", "%"),
               _c("stability.value_verdict", "label", arts["stability"], "data.value_verdict"),
               _c("stability.identity_verdict", "label", arts["stability"], "data.verdict"),
               _c("stability.concordance_mean", "percentage", arts["stability"],
                  "data.metrics.package_concordance_pct_mean", "%")]
        if stab["metrics"].get("rank_correlation_worst_fold") is not None:
            cl.append(_c("stability.rank_worst", "scalar", arts["stability"], "data.metrics.rank_correlation_worst_fold"))
    for j, d in enumerate(dts):
        cl.append(_c(f"dt.{d['test_id']}.verdict", "label", arts["decision_tests"], f"data.tests[{j}].verdict"))
        cl.append(_c(f"dt.{d['test_id']}.threshold", "percentage", arts["decision_tests"],
                     f"data.tests[{j}].threshold_pct", "%"))
        if d.get("status") != "not_run":
            cl.append(_c(f"dt.{d['test_id']}.share", "percentage", arts["decision_tests"],
                         f"data.tests[{j}].share_pct", "%"))
    if cal.get("method") == "emulator_inversion":
        cl.append(_c("calibration.clamped", "count", arts["calibration"], "data.diagnostics.clamped_units", "units"))
    elif cal.get("method") == "likelihood":
        cl.append(_c("calibration.dispersion", "scalar", arts["calibration"], "data.diagnostics.dispersion"))
    return cl


def write(A, run_dir, arts, point, uq, stab, sens, dts, cal, emu_acc) -> dict:
    B, R = _scripts()
    spec = {"producer": PRODUCER, "produced_by": "harness",
            "claims": claim_spec(A, arts, point, uq, stab, dts, cal)}
    os.makedirs(os.path.join(run_dir, "models"), exist_ok=True)
    with open(os.path.join(run_dir, SPEC_REL), "w") as f:
        yaml.safe_dump(spec, f, sort_keys=False, width=120)
    doc, errs = B.write(run_dir)
    if errs or doc is None:
        raise RuntimeError(f"claims ledger: {errs}")
    if doc.get("unresolved"):
        raise RuntimeError(f"claims ledger: unresolved harness claims {doc['unresolved'][:3]}")
    claims = R.load_claims(os.path.join(run_dir, "models", "claims_ledger.yaml"))
    text = results_template(A, point, uq, stab, sens, dts, cal, emu_acc)
    rendered, unresolved = R.render_text(text, claims)
    if unresolved:
        raise RuntimeError(f"results.md: unresolved claims {unresolved}")
    with open(os.path.join(run_dir, "results.md"), "w") as f:
        f.write(rendered)
    return {"n_claims": len(claims), "unresolved": doc.get("unresolved") or [], "ledger": "models/claims_ledger.yaml",
            "spec": SPEC_REL, "results": "results.md"}


def _fmt(x, d=3):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:,.{d}g}" if abs(x) < 1e4 else f"{x:,.0f}"
    return str(x)


def results_template(A, point, uq, stab, sens, dts, cal, emu_acc) -> str:
    o = A.primary
    s = A.spec
    L = [f"# Results: {s['question'].strip()}", "",
         "Rendered by the harness (toolkit/analysis) from its artifacts; no number below was typed by an agent.", "",
         "## Recommendation", "",
         f"The plan assigns one package to each of [CLAIM:units.total] units within "
         f"[CLAIM:cost.allocatable] (allocated: [CLAIM:cost.allocated]). It averts [CLAIM:headline.point] {o} "
         f"against the comparator ({s['comparator'].get('text', s.comparator)}); median over the uncertainty draws "
         f"[CLAIM:headline.median]. Units with no package: [CLAIM:units.no_package].", ""]
    L += ["| Component | Units |", "|---|---|"]
    L += [f"| {c} | [CLAIM:mix.{c}] |" for c in s["options"]["components"]]
    L += ["", "| Package | Units |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in sorted(point["packages"].items(), key=lambda kv: -kv[1])]
    rule = point.get("rule") or []
    if rule:
        L += ["", "### Decision rule", "",
              f"Units by package, with the range of their {o} per person under the comparator over the window "
              "(the burden the package is chosen against):", "",
              "| Package | Number of units | Comparator per person (min to max) | Units |", "|---|---|---|---|"]
        L += [f"| {r['package']} | {r['n_units']} | {_fmt(r['burden_min'])} to {_fmt(r['burden_max'])} | "
              f"{', '.join(r['units'][:12])}{' ...' if len(r['units']) > 12 else ''} |" for r in rule]
    for oc in A.outcomes[1:]:
        L += ["", f"Also averted: [CLAIM:averted.{oc}.point] {oc} (median [CLAIM:averted.{oc}.median])."]
    L += ["", "## Uncertainty: what the interval covers", "",
          f"Interval over [CLAIM:uq.draws] draws: **{uq['label']}**.", ""]
    for e in uq["excludes"]:
        L.append(f"- Not covered: {e['kind']}: {e['reason']}")
    L += ["", f"[CLAIM:uq.units_stable_90] units keep their package in at least 90% of re-optimised draws.", ""]
    inter = point.get("interactions") or []
    if inter:
        L += ["## Component interactions (computed from the point outcomes)", "",
              f"Interaction ratio = ({o} with both / comparator) / (({o} with the first / comparator) x ({o} with the "
              "second / comparator)), over all units. 1 means the two combine multiplicatively; above 1, the pair "
              "averts less than the product of the single effects (overlap); below 1, more.", "",
              "| Components | Ratio with first | Ratio with second | Ratio with both | Interaction ratio |",
              "|---|---|---|---|---|"]
        L += [f"| {r['a']} + {r['b']} | {_fmt(r['ratio_a'])} | {_fmt(r['ratio_b'])} | {_fmt(r['ratio_ab'])} | "
              f"{_fmt(r['interaction_ratio'])} |" for r in inter]
        L.append("")
    L += ["## Calibration", ""]
    if cal.get("method") == "emulator_inversion":
        d = cal["diagnostics"]
        L += [f"Emulator inversion per unit; [CLAIM:calibration.clamped] of {d['units']} units clamped at the range "
              f"{d['range']}; identifiability: {cal['identifiability']['status']}.", ""]
    elif cal.get("method") == "posterior":
        d, idf = cal["diagnostics"], cal.get("identifiability") or {}
        L += [f"Posterior over {d['n_targets']} targets and {d['n_parameters']} parameters (declared priors, "
              f"{d.get('observation')} observations, sampler {d.get('sampler')}); the plan uses the posterior mode.",
              "", "| Parameter | Class | Contraction |", "|---|---|---|"]
        for p in cal.get("names") or []:
            if not p.startswith("z:"):
                L.append(f"| {p} | {(idf.get('classes') or {}).get(p)} | "
                         f"{_fmt((idf.get('contraction') or {}).get(p))} |")
        L.append("")
    elif cal.get("method") == "likelihood":
        d = cal["diagnostics"]
        L += [f"Poisson quasi-likelihood over {d['n_targets']} targets and {d['n_parameters']} parameters; dispersion "
              f"[CLAIM:calibration.dispersion]; converged: {d['converged']}; parameters at a bound: "
              f"{d['at_bound'] or 'none'}.", "", "| Parameter | Estimate | SE | Identifiability |", "|---|---|---|---|"]
        for p, v in (cal["identifiability"].get("parameters") or {}).items():
            L.append(f"| {p} | {_fmt(v['estimate'])} | {_fmt(v['se'])} | {v['status']} |")
        L.append("")
    if emu_acc and not emu_acc.get("direct"):
        L += [f"Emulator held-out accuracy: {', '.join(f'{k} {_fmt(v)}' for k, v in emu_acc.items())}.", ""]
    L += ["## Allocation stability", ""]
    if stab.get("folds"):
        L += [f"{stab['method']}, {stab['n_folds']} folds. Objective lost when a group's data are withheld (worst "
              f"fold, fold plan judged with the full calibration): [CLAIM:stability.value_lost_worst] "
              f"([CLAIM:stability.value_verdict]). Package identity: mean concordance "
              f"[CLAIM:stability.concordance_mean], verdict [CLAIM:stability.identity_verdict]"
              + (" (worst-fold rank r [CLAIM:stability.rank_worst])." if stab['metrics'].get('rank_correlation_worst_fold')
                 is not None else "."), ""]
    else:
        L += [f"Not run: {stab.get('reason', '')}", ""]
    L += ["## Sensitivity (one at a time)", "", "| Input | Range basis | Low end | High end | Objective swing (% of point) "
          "| Units changing package (max) |", "|---|---|---|---|---|---|"]
    for r in sens["rows"]:
        L.append(f"| {r['name']} | {r['basis']} | {_fmt(r['low']['value'])} | {_fmt(r['high']['value'])} | "
                 f"{_fmt(r['swing_pct_of_point'])} | "
                 f"{max(r['low']['share_units_changed'], r['high']['share_units_changed']):.0%} |")
    L += ["", "## Decision tests", ""]
    for d in dts:   # words from code, numbers from claims (Codex P1-4)
        if d.get("status") == "not_run":
            L += [f"**{d['test_id']}** ({d['question']}) {d['statement']} Verdict: [CLAIM:dt.{d['test_id']}.verdict]. "
                  "No result file was written.", ""]
            continue
        L += [f"**{d['test_id']}** ({d['question']}) {d['statement']} Verdict: [CLAIM:dt.{d['test_id']}.verdict]; "
              f"largest share of units changing package [CLAIM:dt.{d['test_id']}.share] (the plan changes when more "
              f"than [CLAIM:dt.{d['test_id']}.threshold] of units change package). Result file: `{d['path']}`.", ""]
    return "\n".join(L) + "\n"
