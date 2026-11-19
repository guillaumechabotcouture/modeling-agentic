#!/usr/bin/env python3
"""The Outcomes view's tables, charts, images and notes (docs/design/review-workbench.md §5.7, §5.12), built from the
generic model and the comparison. No rule is applied here: the checks are views/outcomes_checks.py and
views/outcomes_compare.py.

    python3 tools/workbench/views/outcomes_page.py --self-test

Reading order (the page draws tables marked `lead` first, each with the charts that name it in `after`):
 package_per_unit   the headline (point, median, interval, share of the comparator) and the plan (mix with cost rank),
                    each with its harness figure under the chart; the decision tests, plan-changing and near-miss ones
                    first, each with what was varied and the register row it is linked to; then components, stability,
                    value of information, variants against the plan, external references and emulator accuracy; then
                    the comparison with another run when one is chosen; then the other decision figures of the harness.
 estimates          the outputs, the threshold decision, its tests.
A table the kind or the run does not record is present with the value null, and a note of its name says why.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
from views import outcomes_read as R  # noqa: E402
from views.outcomes_read import g, pct  # noqa: E402

COLUMNS = {
    "plan": ["package", "units", "share", "cost", "cost_per_unit_rank"],
    "components": ["component", "units"],
    "tests": R.TEST_COLUMNS,
    "voi": ["input", "kind", "evppi", "share_of_evpi", "above_noise", "prior_dominated"],
    "variants": ["id", "outcome", "low", "high", "cost", "value_lost_pct", "over_budget_share", "units_changed"],
    "burden": ["id", "group", "outcome", "ratio", "band_low", "band_high", "state", "report_only"],
    "stability": ["measure", "value", "verdict"],
    "emulator": ["measure", "value", "threshold", "passes"],
    "outputs": ["name", "median", "low", "high", "scale"],
    "decision": ["output", "threshold", "direction", "probability_required", "probability", "verdict"],
    "shift": ["item", "this_run", "other_run", "change"],
    "spec_diff": ["key", "this_run", "other_run"]}
HEADLINE = ["outcome", "point", "median", "low", "high", "comparator_total", "share"]
IMAGES = ["figure", "path", "sha256", "title", "caption"]
ORDER = {"package_per_unit": ("headline", "plan", "tests", "components", "stability", "voi", "variants", "burden", "emulator", "outputs", "decision"),
         "estimates": ("outputs", "decision", "tests", "plan", "components", "stability", "voi", "variants", "burden", "emulator")}
LEAD = ("headline", "plan", "tests", "outputs", "decision")
BESIDE = {"headline": "headline", "plan": "allocation_map", "tests": "decision_tests", "burden": "burden_validation"}     # chart -> harness figure (§5.12)
CHOOSE = {"param": "against", "label": "Compare with another run of the same question", "options": "runs"}


def _table(name, rows, about, columns=None, **more):
    cols = columns or COLUMNS[name]
    if rows is None:
        return None
    return {"columns": list(cols), "rows": [[r[c] for c in cols] for r in rows], "about": about,
            **({"lead": True} if name in LEAD else {}), **more}


def tables(m, comp, figs):
    k, name, key = m["kind"], m.get("headline_name"), m.get("outcome")
    plan_about = (
        f"One row per package the plan gives to at least one unit ({R.POINT} data.plan), most units first. share: the share of the "
        f"plan's units; cost: the sum of those units' costs (data.per_unit[].cost), in the currency of the spec's envelope. "
        "cost_per_unit_rank: where the package holds one option of a group of alternatives, that option's rank by price among the "
        f"group's options, 1 the dearest, from the verified signed spec's cost items ({R.SIGNED} options.cost_items; items priced per "
        "outcome depend on the model and are left out); empty where the package holds no such option, options of several groups, or "
        "options priced on different bases. Needs you when the first check names a dearest option that wins nearly everywhere.")
    groups = (m.get("options") or {}).get("groups") or {}
    ranked = "; ".join(f"group {n}, dearest first: " + ", ".join(sorted(grp["options"], key=lambda c: -grp["price"][c]))
                       for n, grp in sorted(groups.items()) if not grp["why"])
    plan_about += f" The groups ranked in this run: {ranked}." if ranked else ""
    out = {
        "headline": _table("headline", m["outcomes"] if k == "package_per_unit" and m["point"] is not None else None, (
            f"The headline first ({name}: outcome {key}), then each other outcome the run reports. point: the outcome averted by the "
            f"plan in the point analysis ({R.POINT} data.averted); median, low, high: the same with the plan held fixed over the draws "
            f"({R.UNC} data.fixed_plan; the interval's level is in the facts); comparator_total: the outcome under the comparator; "
            "share: point divided by comparator_total. Needs you when the checks on the share or on the point against the draws are raised."),
            HEADLINE),
        "plan": _table("plan", m["plan_rows"], plan_about, labels={"units": "units given this package", "share": "of the plan's units",
                                                                    "cost_per_unit_rank": "1 = the dearest option of its group"}),
        "tests": _table("tests", m["tests"], (
            (f"One row per decision test of the estimate ({R.EST} data.decision_tests): the decision rule evaluated with one parameter at "
             "each end of its declared range. varied: that parameter. verdict: CHANGES_PLAN when the rule's verdict at an end the data do "
             "not reject differs from the estimate's, NO_CHANGE when it does not, NOT_RUN when the producer could not assess it. The "
             "producer records no question, share, regret or budget moved for these tests. ") if k == "estimates" else
            (f"One row per decision test ({R.DT} data.tests): the plan solved again with one thing varied. Order: tests that change the "
             "plan, then near misses, then tests that did not run, then the rest, each in the file's order. question and varied: the "
             f"test's own words ({R.TESTS_DIR}<id>.yaml). share_pct: % of units whose package changes; regret_pct: % of the re-optimised "
             "objective lost by keeping the point plan; moved_pct: % of the allocatable budget moved across units and components "
             "(dollars_share_pct, the share the producer compares with its threshold); each beside the threshold above which the "
             "producer says the plan changes. near_miss: no change, with one of the three at 75% or more of its threshold (workbench "
             "threshold). "))
            + "issue: the register row the test is linked to by a structured key (Science register view); empty when it is linked to "
              "none. Needs you: every row with CHANGES_PLAN or a near miss, and any that did not run.",
            wrap=["question", "varied"], labels={"share_pct": "% of units", "regret_pct": "% of objective", "moved_pct": "% of budget"}),
        "components": _table("components", m["components"], (
            f"One row per component of the signed spec ({R.SIGNED} options.components), with the number of units whose package holds it "
            f"({R.POINT} data.plan). A component in no unit was available and not chosen.")),
        "stability": _table("stability", m["stability_rows"] if k == "package_per_unit" else None, (
            f"One row per stability measure the method records. From {R.STAB} data.metrics: the plan re-solved with one group of units left "
            "out of the calibration at a time (each fold), as means and worst folds; the value verdict (ROBUST, FRAGILE or UNSTABLE, against "
            "the method's own loss thresholds) sits on the row of its headline measure, and the plan's verdict is the fact stability_verdict. "
            f"From {R.UNC}: units_same_package_share_median (the median share of units that keep their package across draws) and "
            "units_same_package_in_90pct_of_draws (units keeping their package in at least 90% of draws). Measures ending in _pct are percentages.")),
        "voi": _table("voi", None if m["voi"] is None else m["voi"]["rows"], (
            f"One row per input or group of inputs ({R.VOI} data.inputs), largest value first. evppi: the expected value of knowing that "
            f"input exactly before deciding, in units of the objective ({key}); share_of_evpi: its share of the value of knowing every input. "
            "above_noise: the value is above its own permutation noise floor. prior_dominated: the calibration did not narrow the input "
            f"({R.UNC} data.prior_dominated; for a group, any member). Needs you: an input that is both above its noise floor and prior-dominated.")),
        "variants": _table("variants", m["variants"], (
            f"One row per plan variant the signed spec asks for ({R.PV} data.variants): the plan under an added rule, or a plan given in "
            f"full. outcome: the objective ({key} averted) under the variant; low, high: its interval over the draws; cost: its cost; "
            "value_lost_pct: % of the optimised plan's objective the variant gives up; over_budget_share: the share of draws in which it "
            "costs more than the allocatable budget; units_changed: units whose package differs from the optimised plan. The optimised "
            "plan itself is the headline, not a row.")),
        "burden": _table("burden", m["burden"], (
            f"One row per external reference ({R.BV} data.references): the model's burden under the comparator against a published "
            "estimate. ratio: model divided by reference; band_low, band_high: the band the ratio must fall in (the reference's own "
            "tolerance); state: the producer's word (within, outside, information, unmatched); report_only: the reference is reported and "
            "does not gate. Needs you: every row outside its band."), plain=["state"]),
        "emulator": _table("emulator", m["emulator"], (
            f"Emulator accuracy as the run records it. From {R.EMU} data.accuracy: one row per number (held-out and training points, errors "
            f"on the calibration and base scales, R² of the reduction). Last row, from {R.EMB}: share_matched, the share of boundary units "
            "(those whose chosen package and runner-up are closest) where the engine ranks the two as the emulator does, against the "
            "method's own minimum; passes is the producer's word. Rows with no threshold are reported only.")),
        "outputs": _table("outputs", m["outputs"], (
            f"One row per output of the estimate ({R.EST} data.outputs), in the file's order: the median and the interval over the "
            "calibration and observation draws (the interval's level is in the facts), on the scale named.")),
        "decision": _table("decision", m["decision"], (
            f"The threshold decision ({R.EST} data.decision): the rule asks whether the output is above or below the threshold "
            "(direction) with at least probability_required; probability is the share of draws on that side, and verdict the rule's "
            "answer (yes, no or undetermined). Needs you when the verdict is undetermined or a test below changes it.")),
    }
    out = {name: out[name] for name in ORDER.get(k, ORDER["package_per_unit"])} | {n: v for n, v in out.items() if n not in ORDER.get(k, ())}
    label = comp["against"]
    out["shift"] = _table("shift", comp["shift"], (
        f"This run against {label}. One row per package of either plan with its number of units in each run (0 where a plan does not "
        "hold it), then the headline value, the envelope and the comparator's text from the two verified signed specs. change: this run "
        "minus the other for numbers; \"differs\" for texts that differ. Needs you when the run_shift check is raised."), wrap=["this_run", "other_run"])
    out["spec_diff"] = _table("spec_diff", comp["spec_diff"], (
        f"Every key, to two levels, on which the two verified signed specs differ ({R.SIGNED} of this run and of {label}). A cell holds a "
        "scalar's value, the number of entries of a list or mapping, or nothing where that spec has no such key. Computed here from the "
        "two documents, not read from the harness's comparison figure." + ("" if comp["spec_diff"] else " No key differs.")))
    used = {BESIDE[c] for c in BESIDE if out.get(c) is not None} | {f"estimates_{r['name']}" for r in m["outputs"] or []}
    rest = None if figs is None else [f for n, f in figs.items() if n not in used]
    out["images"] = _table("images", rest, (
        f"The other harness figures of the decision tier ({R.FIGS}), each with the hash of the image as read. A figure shown under one "
        "of the charts above is not repeated here."), IMAGES) if rest else None
    return out


def charts(m, tabs, figs):
    figs, out, key = figs or {}, [], m.get("outcome")

    def has(name):
        return tabs.get(name) is not None and tabs[name]["rows"]
    if has("headline"):
        h = m["headline"]
        out.append({"kind": "interval_points", "table": "headline", "after": "headline", "x": "outcome", "y": "median", "low": "low", "high": "high",
                    "model": "point", **({"panels": "outcome"} if len(tabs["headline"]["rows"]) > 1 else {}),
                    "labels": {"median": "median over the draws, plan held fixed", "point": "point value"},
                    "title": f"{m['headline_name']}: the point value against the draws" + (f" ({pct(h['level'])} interval)" if h["level"] else ""),
                    "how_to_read": "One panel per outcome, the headline first, each on its own axis. The line is the interval over the uncertainty "
                                   "draws and the dot their median; the diamond is the point value the report quotes. A diamond away from the dot "
                                   "means the point analysis and the draws do not centre on the same value; one off the line is a contradiction. "
                                   "(The harness figure, where shown below, marks the point value with a dot and the median with a tick.)",
                    **R.image(figs.get(BESIDE["headline"]))})
    if has("plan"):
        out.append({"kind": "bars", "table": "plan", "after": "plan", "x": "package", "y": "units", "labels": {"units": "units given the package"},
                    "title": "Plan mix: units per package", **R.image(figs.get(BESIDE["plan"])),
                    "how_to_read": "One bar per package in the plan: the number of units given it. The table above gives each package's cost and "
                                   "the price rank of its option."})
    if has("tests") and any(r["share_pct"] is not None for r in m["tests"]):
        moving = any(r["verdict"] == "CHANGES_PLAN" and r["share_pct"] is not None for r in m["tests"])
        limits = sorted({r["share_threshold_pct"] for r in m["tests"] if r["share_threshold_pct"] is not None})
        out.append({"kind": "bars", "table": "tests", "after": "tests", "x": "id", "y": "share_pct", **({"where": {"verdict": "CHANGES_PLAN"}} if moving else {}),
                    **({"limits": [{"value": limits[-1], "label": "the plan is said to change above this"}]} if limits else {}),
                    "labels": {"share_pct": "% of units whose package changes"}, **R.image(figs.get(BESIDE["tests"])),
                    "title": "Decision tests" + (" that change the plan" if moving else "") + ": share of units whose package changes, against the threshold",
                    "how_to_read": ("Only the tests whose verdict is CHANGES_PLAN are drawn. " if moving else "One bar per test. ")
                                   + "The bar is the share of units whose package changes under the test and the upright line the producer's "
                                   "threshold. A test can also change the plan with a short bar: on regret, on budget moved, or because the point "
                                   "plan cannot be afforded or applied under it; the table gives each."})
    if has("voi") and any(r["evppi"] is not None for r in m["voi"]["rows"]):
        out.append({"kind": "bars", "table": "voi", "after": "voi", "x": "input", "y": "evppi", "labels": {"evppi": f"value of knowing the input ({key})"},
                    "title": "Value of information by input", "how_to_read": (
                        "One bar per input or group of inputs, largest first: the expected gain in the objective from knowing it exactly before "
                        "deciding. Read it with the table's above_noise column: a bar below its noise floor cannot be told from zero.")})
    if has("variants") and any(r["outcome"] is not None for r in m["variants"]):
        point = m["headline"]["point"]
        out.append({"kind": "interval_points", "table": "variants", "after": "variants", "x": "id", "y": "outcome", "low": "low", "high": "high",
                    **({"limits": [{"value": point, "label": "the optimised plan"}]} if point is not None else {}),
                    "labels": {"outcome": f"{key} averted under the variant"}, "title": "Plan variants against the optimised plan",
                    "how_to_read": "One row per variant: the dot is the objective under the variant and the line its interval over the draws. "
                                   "The upright line is the optimised plan's point value; the distance to it is what the variant gives up."})
    if has("burden") and any(r["ratio"] is not None and r["band_low"] is not None for r in m["burden"]):
        log = all(v is None or v > 0 for r in m["burden"] for v in (r["ratio"], r["band_low"], r["band_high"]))
        out.append({"kind": "interval_points", "table": "burden", "after": "burden", "x": "id", "y": "ratio", "low": "band_low", "high": "band_high",
                    **({"scale": "log"} if log else {}), "limits": [{"value": 1.0, "label": "model = reference"}],
                    "labels": {"ratio": "model ÷ reference"}, "title": "Burden against external references: each ratio against its band",
                    **R.image(figs.get(BESIDE["burden"])),
                    "how_to_read": "One row per reference. The line is the band the ratio must fall in and the dot the ratio of the model's burden "
                                   "to the reference: a dot off its line is outside the band, one near the line's end a near miss."})
    d = (m["decision"] or [{}])[0]
    for r in m["outputs"] or []:
        if r["median"] is None or not has("outputs"):
            continue
        rule = r["name"] == d.get("output") and d.get("threshold") is not None
        out.append({"kind": "interval_points", "table": "outputs", "after": "outputs", "x": "name", "y": "median", "low": "low", "high": "high",
                    "where": {"name": r["name"]}, "labels": {"median": f"median ({r['scale'] or 'scale not recorded'})"},
                    **({"limits": [{"value": d["threshold"], "label": f"the rule's threshold ({d.get('direction')})"}]} if rule else {}),
                    "title": f"{r['name']}: median and interval" + (" against the decision threshold" if rule else ""),
                    **R.image(figs.get(f"estimates_{r['name']}")),
                    "how_to_read": "The dot is the median and the line the interval over the draws."
                                   + (" The upright line is the threshold of the decision rule: the rule's probability is the share of draws on "
                                      "the side it names." if rule else "")})
    return out


def notes(m, comp, tabs):
    s, k = m["spec"], m["kind"]
    out = {"reading": (
        "Read in this order: the headline and the plan, then the decision tests (plan-changing and near-miss ones first), then stability, "
        "value of information, plan variants, external references and emulator accuracy, then the comparison with another run when one is "
        "chosen at the top of the page. Point value: the plan's analysis at the central values. Draws: the same analysis over the "
        "uncertainty draws. Comparator: what the plan is measured against. Near miss: a value within 25% of a threshold it did not pass "
        "(workbench threshold). Workbench thresholds are this page's own and are labelled where they are used; every other threshold is "
        "the run's own record." if k != "estimates" else
        "Read in this order: the outputs, the threshold decision, then its tests. Each test evaluates the decision rule with one parameter "
        "at each end of its declared range; a test changes the decision when the rule's verdict there differs from the estimate's.")}
    out["spec"] = (f"The signed copy of the spec, {R.SIGNED} ({str(s['sha'])[:8]}), signed by {s['signed_by']} at {str(s['signed_at'])[:16]} "
                   f"(sign-off record {s['record']}). Costs, the budget allowance, excluded inputs and the tests' entries are read from it alone."
                   if s["verified"] else
                   f"No signed spec could be verified: {s['why']}. The deliverable kind and the names shown are read from the signed copy as it "
                   "is; nothing in it prices an option, sets an allowance or excuses anything here.")
    if k == "estimates":
        out["kind"] = (f"This run's deliverable kind is estimates: it has {R.LACKS}. Those tables are empty and the checks on them are not "
                       "available; the outputs, the decision and its tests are what this kind records.")
    if m.get("link_why"):
        out["register_link"] = f"The tests' link to register rows is not shown: {m['link_why']}."
    groups = (m.get("options") or {}).get("groups") or {}
    if any(grp["why"] for grp in groups.values()):
        out["cost_rank"] = "Groups of options with no price rank: " + "; ".join(f"{n} ({grp['why']})" for n, grp in sorted(groups.items()) if grp["why"]) + "."
    out["comparison"] = (
        f"Compared with {comp['against']}: the tables shift and spec_diff and the check run_shift." if comp["shift"] is not None else
        f"Compared with {comp['against']}: {comp['check']['says']}" if comp["against"] else
        "No comparison run is chosen. Choose another run of the same question at the top of this page to see what moved between the two "
        "plans and headlines and which keys of the two signed specs differ.")
    why = m["why"]

    def gone(rel, what):
        return f"{why[rel]}." if why.get(rel) else f"{rel} holds no {what}."
    lacks = f"The deliverable kind estimates records none: it has {R.LACKS}."
    reasons = {
        "headline": lacks if k == "estimates" else gone(R.POINT, "point value"), "plan": gone(R.POINT, "plan (data.plan)"),
        "components": gone(R.POINT, "plan (data.plan)"), "tests": gone(R.EST if k == "estimates" else R.DT, "list of decision tests"),
        "voi": gone(R.VOI, "value of information"), "variants": gone(R.PV, "list of variants"), "burden": gone(R.BV, "list of references"),
        "stability": gone(R.STAB, "stability measures"), "emulator": gone(R.EMU, "emulator accuracy"),
        "outputs": gone(R.EST, "outputs") if k == "estimates" else "Outputs belong to the deliverable kind estimates; this run's kind is package_per_unit.",
        "decision": (gone(R.EST, "decision rule (the signed spec sets none)") if k == "estimates" else
                     "A threshold decision belongs to the deliverable kind estimates; this run's kind is package_per_unit."),
        "shift": out["comparison"], "spec_diff": out["comparison"],
        "images": f"The run has no readable figure index ({R.FIGS}), or every decision figure is shown under a chart above."}
    for name, t in tabs.items():
        if t is None:
            out.setdefault(name, lacks if k == "estimates" and name in ("plan", "components", "voi", "variants", "burden", "stability", "emulator")
                           else reasons.get(name, "The run's files do not record it."))
    return out


def result(m, comp, checks_, figs):
    tabs = tables(m, comp, figs)
    last = [n for n, t in tabs.items() if t is not None and n != "images"]
    images = [{"table": "images", "path": "path", "hash": "sha256", "title": "title", "caption": "caption",
               **({"after": last[-1]} if last else {})}] if tabs["images"] else []
    return {"checks": checks_, "tables": tabs, "charts": charts(m, tabs, figs), "notes": notes(m, comp, tabs), "images": images, "choose": dict(CHOOSE)}


def _self_test():
    import workbench
    from views import outcomes_compare, outcomes_estimates, outcomes_package
    from views.fit_bench import Bench, fixture_builder, need
    fx = fixture_builder()
    b = Bench(lambda run: None)
    hm, post = fx.METHODS

    def built(d, adapter=outcomes_package, against=None):
        run = workbench.Run(d)
        m = adapter.read(run, R.spec(run))
        other = None
        if against:
            other = outcomes_package.read(workbench.Run(against), R.spec(workbench.Run(against)))
        return result(m, outcomes_compare.compare(m, other, "the other run"), [], R.figures(run))

    def package():
        v = built(b.folder(hm))
        need(list(v["tables"])[:3] == ["headline", "plan", "tests"] and all(v["tables"][n]["lead"] for n in ("headline", "plan", "tests")),
             f"the headline, the plan and the tests are drawn first ({list(v['tables'])})")
        need(workbench.chart_problems(v) == [] and workbench.image_problems(v) == [], f"the page can draw it ({workbench.chart_problems(v)}, {workbench.image_problems(v)})")
        by = {c["table"]: c for c in v["charts"]}
        need(set(by) == {"headline", "plan", "tests", "voi", "variants", "burden"} and all(c["after"] == c["table"] for c in v["charts"]),
             "one chart per part, each directly after its table")
        need(by["headline"]["image"]["path"] == "figures/headline.png" and by["tests"]["image"]["path"] == "figures/decision_tests.png" and "image" not in by["plan"],
             "a chart carries the matching harness figure where the run has one")
        need([r[0] for r in v["tables"]["images"]["rows"]] == ["frontier"] and v["images"][0]["after"] == "emulator",
             "the other decision figures follow the last table, and a figure under a chart is not repeated")
        need(all(v["tables"][n] is None and v["notes"][n] for n in ("outputs", "decision", "shift", "spec_diff")) and v["choose"]["param"] == "against",
             "a table not recorded is null with a note of its name; the page is told how to choose a comparison run")
        v = built(b.folder(hm, "test_changes_linked"))
        need(next(c for c in v["charts"] if c["table"] == "tests")["where"] == {"verdict": "CHANGES_PLAN"}, "with plan-changing tests the chart draws those")
        v = built(b.folder(hm), against=b.folder(hm, other="shifted"))
        need(v["tables"]["shift"]["rows"] and v["tables"]["spec_diff"]["rows"] and "the other run" in v["notes"]["comparison"]
             and list(v["tables"]).index("shift") > list(v["tables"]).index("emulator"), "the comparison comes after the run's own tables")

    def estimates():
        v = built(b.folder(post, figures="estimates", kind="estimates"), outcomes_estimates)
        need(list(v["tables"])[:3] == ["outputs", "decision", "tests"] and workbench.chart_problems(v) == [], f"outputs, decision, tests ({list(v['tables'])})")
        rule = next(c for c in v["charts"] if c["where"] == {"name": "q1"})
        need(rule["limits"][0]["value"] == 1.0 and rule["image"]["path"] == "figures/estimates_q1.png" and len(v["charts"]) == 2,
             "one chart per output with its figure; the decision's output against the threshold")
        need("estimates" in v["notes"]["kind"] and "plan" in v["notes"]["kind"] and all(v["tables"][n] is None and v["notes"][n] for n in ("plan", "voi", "burden")),
             "one note says what the kind does not record, and each null table has its reason")

    b.case("G1 package_per_unit: reading order, charts with their harness figures, the comparison last", package)
    b.case("G2 estimates: outputs, the threshold decision, its tests", estimates)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: outcomes_page.py --self-test")
