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
GROUPS_LISTED = 40      # results.md lists at most this many groups' choices for a group-rule variant
GROUPS_SHOWN = 3        # results.md names the groups with the most units changing package in each decision test


def _scripts():
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import build_claims_ledger
    import render_claims
    return build_claims_ledger, render_claims


def _cid(test_id) -> str:
    """A decision test's id in claim ids ([A-Za-z0-9_.-]): a test id may carry a parameter name such as z:<unit>."""
    import re
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(test_id))


def _c(cid, kind, art, field, units="", **kw):
    return {"id": cid, "claim_kind": kind, "units": units, "source_artifact": art, "source_field": field,
            "produced_by": "harness", **kw}


def claim_spec(A, arts: dict, point: dict, uq: dict, stab: dict, dts: list, cal: dict, burden=None) -> list[dict]:
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
    if (uq.get("affordability") or {}).get("draws"):     # F6 S1-1: the fixed plan's cost across the draws
        cl += [_c("uq.draws_over_budget", "count", arts["uncertainty"], "data.affordability.draws_over_budget", "draws"),
               _c("uq.share_over_budget", "scalar", arts["uncertainty"], "data.affordability.share_over_budget",
                  "share of draws"),
               _c("uq.fixed_plan_cost.median", "currency", arts["uncertainty"],
                  "data.affordability.fixed_plan_cost.median", "USD", interval=uq["interval"]),
               _c("uq.fixed_plan_cost.high", "currency", arts["uncertainty"],
                  "data.affordability.fixed_plan_cost.ci_high", "USD", interval=uq["interval"])]
    if point.get("reserve"):     # F6 S1-1b: the plan solved within a reserve so that it meets the affordability rule
        cl += [_c("reserve.fraction", "scalar", arts["point"], "data.reserve.fraction", "share of budget"),
               _c("reserve.usd", "currency", arts["point"], "data.reserve.reserve_usd", "USD"),
               _c("reserve.value_given_up", "scalar", arts["point"], "data.reserve.value_given_up", o)]
        if point["reserve"].get("value_given_up_pct") is not None:      # undefined when the objective is not positive
            cl.append(_c("reserve.value_given_up_pct", "percentage", arts["point"], "data.reserve.value_given_up_pct",
                         "%"))
    if (point.get("cost_floors") or {}).get("items"):     # F6 S1-3: incremental per-outcome costs floored at zero
        cl.append(_c("cost.floored_unit_packages.point", "count", arts["point"], "data.cost_floors.n_unit_packages",
                     "unit x package combinations"))
        if uq.get("cost_floors"):
            cl += [_c("cost.floored_unit_packages.draws_with_floors", "count", arts["uncertainty"],
                      "data.cost_floors.draws_with_floors", "draws"),
                   _c("cost.floored_unit_packages.draw_max", "count", arts["uncertainty"],
                      "data.cost_floors.max_unit_packages", "unit x package combinations")]
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
        m = stab["metrics"]       # F6 S1-5: what moves in the worst fold; stable provision apart from stable absence
        cl += [_c("stability.dollars_worst", "currency", arts["stability"], "data.metrics.dollars_moved_worst_fold", "USD"),
               _c("stability.population_worst", "scalar", arts["stability"],
                  "data.metrics.population_changed_worst_fold", "people")]
        if m.get("regret_pct_worst_fold") is not None:
            cl.append(_c("stability.regret_worst", "percentage", arts["stability"], "data.metrics.regret_pct_worst_fold",
                         "%"))
        if stab.get("components"):
            cl += [_c(f"stability.{k}", "count", arts["stability"], f"data.components.{k}", "unit-components")
                   for k in ("stable_provision", "stable_absence", "changed")]
        if stab["metrics"].get("rank_correlation_worst_fold") is not None:
            cl.append(_c("stability.rank_worst", "scalar", arts["stability"], "data.metrics.rank_correlation_worst_fold"))
    for j, d in enumerate(dts):
        tid = _cid(d["test_id"])
        cl.append(_c(f"dt.{tid}.verdict", "label", arts["decision_tests"], f"data.tests[{j}].verdict"))
        cl.append(_c(f"dt.{tid}.threshold", "percentage", arts["decision_tests"],
                     f"data.tests[{j}].threshold_pct", "%"))
        if d.get("status") != "not_run":
            f = f"data.tests[{j}]"
            cl += [_c(f"dt.{tid}.share", "percentage", arts["decision_tests"], f"{f}.share_pct", "%"),
                   _c(f"dt.{tid}.dollars", "currency", arts["decision_tests"], f"{f}.dollars_moved", "USD"),
                   _c(f"dt.{tid}.reallocated", "currency", arts["decision_tests"], f"{f}.dollars_reallocated", "USD"),
                   _c(f"dt.{tid}.population", "scalar", arts["decision_tests"], f"{f}.population_changed", "people")]
            if d.get("regret_share") is not None:      # undefined when the re-optimised objective is not positive
                cl.append(_c(f"dt.{tid}.regret_pct", "percentage", arts["decision_tests"], f"{f}.regret_pct", "%"))
            if d.get("regret") is not None:            # none when every value makes the point plan infeasible
                cl.append(_c(f"dt.{tid}.regret", "scalar", arts["decision_tests"], f"{f}.regret", A.primary))
            for k, _g in enumerate((d.get("units_changed_by_group") or [])[:GROUPS_SHOWN]):
                cl.append(_c(f"dt.{tid}.group{k}", "count", arts["decision_tests"],
                             f"{f}.units_changed_by_group[{k}].changed", "units"))
    for r in _spec_rows(A, claimable=True):     # spec-declared engine parameter values and the card values they replace
        n, f = r["name"], f"data.spec_values.{r['name']}"
        cl.append(_c(f"spec_value.{n}.value", "scalar", arts["engine"], f"{f}.value", r["units"]))
        cl.append(_c(f"spec_value.{n}.card_value", "scalar", arts["engine"], f"{f}.card_value", r["units"]))
        if r["card_range"]:
            cl.append(_c(f"spec_value.{n}.card_lo", "scalar", arts["engine"], f"{f}.card_range[0]", r["units"]))
            cl.append(_c(f"spec_value.{n}.card_hi", "scalar", arts["engine"], f"{f}.card_range[1]", r["units"]))
    if cal.get("method") == "emulator_inversion":
        cl.append(_c("calibration.clamped", "count", arts["calibration"], "data.diagnostics.clamped_units", "units"))
    elif cal.get("method") == "likelihood":
        cl.append(_c("calibration.dispersion", "scalar", arts["calibration"], "data.diagnostics.dispersion"))
    if burden:      # F6 S2-5: absolute burden against external estimates
        from toolkit.analysis import burden as BV
        cl += BV.claims(_c, arts["burden_validation"], burden)
    return cl


def _claimable(name: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_\-]+", name))


def _spec_rows(A, claimable=False) -> list[dict]:
    """The spec's engine parameter values (toolkit/spec/engine_parameters.py); `claimable`: only those whose name a
    claim id can carry (the others are rendered with their values in results.md)."""
    from toolkit.spec import engine_parameters as PV
    return [r for r in PV.rows(A.spec, A.eng) if not claimable or _claimable(r["name"])]


def write(A, run_dir, arts, point, uq, stab, sens, dts, cal, emu_acc, pv=None, burden=None) -> dict:
    B, R = _scripts()
    spec = {"producer": PRODUCER, "produced_by": "harness",
            "claims": claim_spec(A, arts, point, uq, stab, dts, cal, burden) + variant_claims(A, arts, pv)}
    from toolkit.analysis.artifacts import _safe_io
    _safe_io().write_nofollow(run_dir, SPEC_REL.replace(os.sep, "/"),
                              yaml.safe_dump(spec, sort_keys=False, width=120).encode(), atomic=True)
    doc, errs = B.write(run_dir)
    if errs or doc is None:
        raise RuntimeError(f"claims ledger: {errs}")
    if doc.get("unresolved"):
        raise RuntimeError(f"claims ledger: unresolved harness claims {doc['unresolved'][:3]}")
    claims = R.load_claims(os.path.join(run_dir, "models", "claims_ledger.yaml"))
    text = results_template(A, point, uq, stab, sens, dts, cal, emu_acc, burden) + variants_markdown(A, pv)
    rendered, unresolved = R.render_text(text, claims)
    if unresolved:
        raise RuntimeError(f"results.md: unresolved claims {unresolved}")
    _safe_io().write_nofollow(run_dir, "results.md", rendered.encode("utf-8"), atomic=True)
    return {"n_claims": len(claims), "unresolved": doc.get("unresolved") or [], "ledger": "models/claims_ledger.yaml",
            "spec": SPEC_REL, "results": "results.md"}


def _fmt(x, d=3):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:,.{d}g}" if abs(x) < 1e4 else f"{x:,.0f}"
    return str(x)


def attribute_simplifications(A) -> list[str]:
    """The engine card's limitation (and its sourced prior beside the run's) for the unit attribute the calibration
    moves (toolkit/spec/schema.py attribute_notes); [] when the card gives none. A card binding that cannot be read
    is stated in the section, not dropped (Codex r3 P2-2)."""
    from toolkit.spec import schema
    b, bad = schema.load_binding(A.eng)
    return schema.attribute_notes_markdown(schema.attribute_notes(A.spec.raw.get("calibration") or {}, b), problems=bad)


def variant_claims(A, arts, pv) -> list[dict]:
    """F6 S1-6: each solved plan variant's value lost, dollars moved, unfunded units and units changing package."""
    out = []
    for j, v in enumerate((pv or {}).get("variants") or []):
        if v.get("status") not in ("solved", "evaluated"):
            continue
        f, c = f"data.variants[{j}]", f"variant.{v['id']}"
        out += [_c(f"{c}.value_lost", "scalar", arts["plan_variants"], f"{f}.value_lost", A.primary),
                _c(f"{c}.dollars", "currency", arts["plan_variants"], f"{f}.dollars_moved", "USD"),
                _c(f"{c}.reallocated", "currency", arts["plan_variants"], f"{f}.dollars_reallocated", "USD"),
                _c(f"{c}.no_package", "count", arts["plan_variants"], f"{f}.units_no_package", "units"),
                _c(f"{c}.units_changed", "count", arts["plan_variants"], f"{f}.units_changed", "units")]
        if v.get("value_lost_pct") is not None:      # undefined when the unconstrained objective is not positive
            out.append(_c(f"{c}.value_lost_pct", "percentage", arts["plan_variants"], f"{f}.value_lost_pct", "%"))
        if v.get("group_choices") is not None:
            out.append(_c(f"{c}.groups_none", "count", arts["plan_variants"], f"{f}.groups_none", "groups"))
        if v.get("status") == "evaluated":       # F6 S2-6: a fixed plan's cost against the budget and eligibility
            out += [_c(f"{c}.cost", "currency", arts["plan_variants"], f"{f}.cost", "USD"),
                    _c(f"{c}.budget_difference", "currency", arts["plan_variants"], f"{f}.cost_minus_budget", "USD"),
                    _c(f"{c}.ineligible", "count", arts["plan_variants"], f"{f}.units_ineligible", "units")]
            un = v.get("uncertainty")
            if un:
                out += [_c(f"{c}.averted_median", "scalar", arts["plan_variants"],
                           f"{f}.uncertainty.averted.{A.primary}.median", A.primary, interval=un["interval"]),
                        _c(f"{c}.cost_median", "currency", arts["plan_variants"],
                           f"{f}.uncertainty.affordability.fixed_plan_cost.median", "USD", interval=un["interval"]),
                        _c(f"{c}.draws_over_budget", "count", arts["plan_variants"],
                           f"{f}.uncertainty.affordability.draws_over_budget", "draws")]
    if pv:
        out.append(_c("variant.unconstrained.no_package", "count", arts["plan_variants"],
                      "data.unconstrained.units_no_package", "units"))
    return out


def variants_markdown(A, pv) -> str:
    """results.md section for the plan variants: words from code, numbers from claims."""
    if not pv:
        return ""
    hv = pv["headline"]
    L = ["", "## Plan variants", "",
         "Each rule variant is solved, and each fixed plan evaluated as given, on the point outcomes and costs "
         "within the same budget as the headline plan, and "
         "compared with the unconstrained plan (units with no package there: [CLAIM:variant.unconstrained.no_package]). "
         + ("The headline plan is the unconstrained plan." if hv == "unconstrained" else
            f"The headline plan is the variant {hv}, as the spec states (analysis.plan_variants.headline)."), "",
         f"| Variant | Rule | Value lost ({A.primary}) | Value lost (% of the unconstrained plan) | Dollars moved between "
         "components | Dollars reallocated between units | Units changing package | Units with no package |",
         "|---|---|---|---|---|---|---|---|"]
    for v in pv["variants"]:
        c = f"variant.{v['id']}"
        if v.get("status") not in ("solved", "evaluated"):
            L.append(f"| {v['id']} | {v['text']} | not solved: {v.get('reason', '')} | | | | | |")
            continue
        L.append(f"| {v['id']} | {v['text']} | [CLAIM:{c}.value_lost] | "
                 + (f"[CLAIM:{c}.value_lost_pct]" if v.get("value_lost_pct") is not None else "n/a")
                 + f" | [CLAIM:{c}.dollars] | [CLAIM:{c}.reallocated] | [CLAIM:{c}.units_changed] | [CLAIM:{c}.no_package] |")
    for v in pv["variants"]:        # F6 S2-6: a plan given in full, evaluated as given
        if v.get("status") != "evaluated":
            continue
        c = f"variant.{v['id']}"
        bad = v.get("ineligible_units") or []
        L += ["", f"{v['id']} is evaluated as given, not solved: each unit's package from the units-table column "
              f"{v['column']}, on the same outcomes and costs as the headline plan. Its cost is [CLAIM:{c}.cost]; its cost "
              f"minus the allocatable budget is [CLAIM:{c}.budget_difference] (positive: over the budget; a plan over "
              f"the budget is reported, not refused, and its value lost can then be negative). Units given a package the signed constraints rule out: [CLAIM:{c}.ineligible]"
              + (" (" + "; ".join(f"{x['unit']}: {x['package']}" for x in bad[:GROUPS_LISTED])
                 + (" ..." if len(bad) > GROUPS_LISTED else "") + "; kept in the plan and evaluated as given)"
                 if bad else "") + "."
              + (" It breaks caps or group rules of the signed constraints (constraints_broken in the plan_variants "
                 "artifact)." if v.get("constraints_broken") else "")
              + (f" Over the uncertainty draws: {A.primary} averted [CLAIM:{c}.averted_median], cost "
                 f"[CLAIM:{c}.cost_median], draws over budget [CLAIM:{c}.draws_over_budget]." if v.get("uncertainty")
                 else f" {str(v.get('uncertainty_note') or '').capitalize()}.")]
    for v in pv["variants"]:        # Codex S1-6 r1 P3: what a group rule allows, and what each group chose
        if v.get("status") != "solved" or v.get("group_choices") is None:
            continue
        ch = v["group_choices"]
        L += ["", f"{v['id']}: one decision per {v['by']} on {', '.join(v['components'])}; "
              + ("every group must provide one of them (provide: true). " if v.get("provide") else
                 "a group may share no provision (none); to require provision, set provide: true. ")
              + f"Groups choosing none: [CLAIM:variant.{v['id']}.groups_none]. Choices: "
              + "; ".join(f"{g}: {k}" for g, k in list(ch.items())[:GROUPS_LISTED])
              + (" ..." if len(ch) > GROUPS_LISTED else "") + " (all in the plan_variants artifact)."]
    return "\n".join(L) + "\n"


def results_template(A, point, uq, stab, sens, dts, cal, emu_acc, burden=None) -> str:
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
    fl = point.get("cost_floors") or {}
    if fl.get("items"):
        L += ["", f"Per-outcome cost increments ({', '.join(fl['items'])}) floored at zero at the point values: "
              f"[CLAIM:cost.floored_unit_packages.point] unit x package combinations. {fl['statement']}"]
        uf = uq.get("cost_floors")
        if uf:
            L += ["", "In the uncertainty draws: [CLAIM:cost.floored_unit_packages.draws_with_floors] of "
                  "[CLAIM:uq.draws] draws floored an increment, at most [CLAIM:cost.floored_unit_packages.draw_max] "
                  f"unit x package combinations in one draw. {uf['statement']}"]
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
    from toolkit.spec import engine_parameters as PV
    sv = PV.markdown(_spec_rows(A), ref=lambda n, k: f"[CLAIM:spec_value.{n}.{k}]" if _claimable(n) else None)
    if sv:
        L += [""] + sv[:-1]
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
    L += attribute_simplifications(A)
    if emu_acc and not emu_acc.get("direct"):
        L += [f"Emulator held-out accuracy: {', '.join(f'{k} {_fmt(v)}' for k, v in emu_acc.items())}.", ""]
    if burden:
        from toolkit.analysis import burden as BV
        L += BV.results_lines(burden)
    L += ["## Allocation stability", ""]
    if stab.get("folds"):
        L += [f"{stab['method']}, {stab['n_folds']} folds. Objective lost when a group's data are withheld (worst "
              f"fold, fold plan judged with the full calibration): [CLAIM:stability.value_lost_worst] "
              f"([CLAIM:stability.value_verdict]). Package identity: mean concordance "
              f"[CLAIM:stability.concordance_mean], verdict [CLAIM:stability.identity_verdict]"
              + (" (worst-fold rank r [CLAIM:stability.rank_worst])." if stab['metrics'].get('rank_correlation_worst_fold')
                 is not None else "."), ""]
        L += [f"Across folds, up to [CLAIM:stability.dollars_worst] moves between components and up to "
              f"[CLAIM:stability.population_worst] people live in units whose package changes"
              + ("; keeping the point plan under a fold's calibration loses up to [CLAIM:stability.regret_worst] of "
                 "that fold's re-optimised objective." if stab["metrics"].get("regret_pct_worst_fold") is not None else "."), ""]
        if stab.get("components"):
            L += ["Held-out units, component by component, against the point plan: stable provision (the component "
                  "in both plans) [CLAIM:stability.stable_provision], stable absence (in neither) "
                  "[CLAIM:stability.stable_absence], changed [CLAIM:stability.changed].", ""]
    else:
        L += [f"Not run: {stab.get('reason', '')}", ""]
    L += ["## Sensitivity (one at a time)", "", "| Input | Range basis | Low end | High end | Objective swing (% of point) "
          "| Units changing package (max) | Regret (max, % of objective) | Dollars moved between components (max) "
          "| Population changing (max) | Changes the plan on |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sens["rows"]:
        e2 = (r["low"], r["high"])
        rg = [e["regret_share"] for e in e2 if e.get("regret_share") is not None]
        trig = [k for k in ("infeasible", "share", "regret", "dollars") if any(k in (e.get("triggered_by") or []) for e in e2)]
        L.append(f"| {r['name']} | {r['basis']} | {_fmt(r['low']['value'])} | {_fmt(r['high']['value'])} | "
                 f"{_fmt(r['swing_pct_of_point'])} | "
                 f"{max(r['low']['share_units_changed'], r['high']['share_units_changed']):.0%} | "
                 f"{f'{max(rg):.2%}' if rg else 'n/a'} | {_fmt(max(e.get('dollars_moved') or 0.0 for e in e2))} | "
                 f"{_fmt(max(e.get('population_changed') or 0.0 for e in e2))} | {', '.join(trig) or 'none'} |")
    L += ["", "## Decision tests", ""]
    for d in dts:   # words from code, numbers from claims (Codex P1-4)
        if d.get("status") == "not_run":
            L += [f"**{d['test_id']}** ({d['question']}) {d['statement']} Verdict: [CLAIM:dt.{_cid(d['test_id'])}.verdict]. "
                  "No result file was written.", ""]
            continue
        t = _cid(d["test_id"])
        grp = d.get("units_changed_by_group") or []
        L += [f"**{d['test_id']}** ({d['question']}) {d['statement']} Verdict: [CLAIM:dt.{t}.verdict]; "
              f"largest share of units changing package [CLAIM:dt.{t}.share] (the plan changes when more "
              f"than [CLAIM:dt.{t}.threshold] of units change package, or on regret or dollars moved). "
              + ("At some tested value the point plan is not feasible under the test, so it cannot be kept. "
                 if d.get("point_plan_feasible") is False else "")
              + (f"Keeping the point plan loses up to [CLAIM:dt.{t}.regret_pct] of the re-optimised objective"
                 if d.get("regret_share") is not None else "")
              + ((" and up to" if d.get("regret_share") is not None else "Keeping the point plan loses up to")
                 + f" [CLAIM:dt.{t}.regret] {A.primary} (each at its own worst tested value); "
                 if d.get("regret") is not None else ("; " if d.get("regret_share") is not None else ""))
              + f"up to [CLAIM:dt.{t}.reallocated] is reallocated across units and components and the net change "
              f"between components is up to [CLAIM:dt.{t}.dollars] (each at its own worst tested value), and up to "
              f"[CLAIM:dt.{t}.population] people live in units whose package changes"
              + (" (at the value with the most units changing, most changes in "
                 + ", ".join(f"{g['group']}: [CLAIM:dt.{t}.group{k}]" for k, g in enumerate(grp[:GROUPS_SHOWN]))
                 + ")" if grp else "")
              + ". " + (f"Changes the plan on: {', '.join(d['triggered_by'])}. " if d.get("triggered_by") else "")
              + f"Result file: `{d['path']}`.", ""]
    return "\n".join(L) + "\n"
