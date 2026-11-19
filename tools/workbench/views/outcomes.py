#!/usr/bin/env python3
"""The Outcomes view (docs/design/review-workbench.md §5.7): the headline with its interval, the plan, the decision
tests, stability, value of information, plan variants, external references, emulator accuracy, and the comparison
with another run of the same question.

    python3 tools/workbench/views/outcomes.py --self-test

`view(run, against=None)` returns {checks, facts, tables, charts, notes, images, choose, fact_labels}. `against` is the comparison
run chosen in the page (`&against=<run id>`; workbench.py W55 to W59 hold the server to it). The modules:
 outcomes_read.py       what they share: file names, the signed spec, test rows, the harness figures
 outcomes_package.py    the adapter for the deliverable kind package_per_unit
 outcomes_estimates.py  the adapter for the deliverable kind estimates
 outcomes_checks.py     the eleven checks of one run, on the generic model only
 outcomes_compare.py    the comparison with another run: tables shift and spec_diff, check run_shift
 outcomes_page.py       tables, charts, images and notes, in the owner's reading order
The adapter is chosen by the signed spec's deliverable.kind; a kind this view does not know, or a run with no signed
copy to read one from, gives every check not_available with the reason, never a guess.

What the cases hold the view to, each from §3 or §5.7 (its "Settled" lists win over an earlier line), with what the
specification leaves open marked "assumed":

 A1  facts: the names of FACTS, no other. Check ids are outcomes.<name> for the twelve of CHECKS. A fact the kind or the
     run does not record is null. Assumed: headline_point is analysis/point.yaml data.averted[<outcome>], the median
     and interval are analysis/uncertainty.yaml data.fixed_plan[<outcome>] (the plan held fixed); share_of_comparator
     is the point value over data.comparator_total[<outcome>]; reserve is data.reserve.reserve_usd; evpi_share is
     evpi.pct / 100; n_units_stable is units_same_package_in_90pct_of_draws; headline_outcome is the signed spec's
     deliverable.headline.name (estimates: the output its decision rule names).
 A2  tables: the columns of COLUMNS. A table the kind or the run does not record is present with the value null
     (estimates: plan, components, stability, variants, burden, voi and emulator; package_per_unit: outputs and
     decision; shift and spec_diff with no comparison run). A table `headline` feeds the headline chart (settled).
 A3  tests: `moved_pct` is the row's dollars_share_pct (the share toolkit/analysis/moves.py compares with the dollars
     threshold); `question` and `varied` come from the test's own result file. A near miss is a test with NO_CHANGE
     whose share, regret or moved value is at or above 75% of its threshold. `issue` is the register row whose tests
     hold the test, never the test's own test:<id> row. Assumed: the spec entry's `finding` key links nothing. Rows
     are in reading order (plan-changing, near misses, not run, the rest), each group in the file's order.
 A4  tests_that_move: `look` for a plan-changing test, a near miss or a test that did not run (the sentence names
     each test's register row or says it is linked to none); `problem` for a status and verdict that contradict;
     `not_available` for a file or a word that cannot be read. Settled: the producer's own not-run scenario for an
     input the verified signed spec excludes is counted and listed, and does not raise the check.
 A5  point_vs_draws (settled): outside the interval is `problem`; more than 5% of the median from the median is
     `look` with both numbers; an interval whose ends are reversed contradicts itself, `problem`.
 A6  dearest_wins (settled): `look`, never more; needs the producer's share and its minimum number of units, among
     the units whose plan holds an option of the group. cost_per_unit_rank is 1 for the dearest option. Assumed: a
     plan naming a package the signed spec does not define is `not_available`.
 A7  burden_refs (settled): `look` when every reference outside its band is report-only and named by a decision test
     that ran (the signed spec entry's burden_scale.references); `problem` otherwise.
 A8  voi_support (settled): `ok` with the producer's status `computed` and an input above its noise floor that is not
     prior-dominated; `look` for status `indicative`, no input above its floor, or a prior-dominated input above its
     floor (C14); `not_available` when nothing was computed.
 A9  boundary_coverage: `look` when a component of the signed spec is in no row's chosen or runner-up package.
 A10 stability_agreement, infeasible_tests, variant_budget, fixed_inputs (settled): each `look`. A variant with no
     cost or draws is `not_available`; an input is "held at one value" when analysis/uncertainty.yaml lists it under
     draw_plan.fixed, interval.fixed or interval.spec_fixed, and a test varies it through the signed spec entry's
     `parameter`.
 A11 run_shift: `not_available` with no comparison run, with a comparison whose signed spec cannot be verified, and
     across two questions; `look` past a threshold with a spec key that differs; `problem` past one with none; else
     `ok`. tables.shift: one row per package, then headline, envelope and comparator. Assumed: change is this run
     minus the other for numbers, "differs" for text that differs and null for text that is the same.
 A12 an unknown deliverable kind: every check `not_available`, naming the kind. Assumed: the kind is read from the
     signed copy even when its hash cannot be verified (what it excuses then counts for nothing, F2).
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
from views import outcomes_checks, outcomes_compare, outcomes_estimates, outcomes_package, outcomes_page  # noqa: E402
from views import outcomes_read as R  # noqa: E402

FACTS = ("deliverable_kind", "headline_outcome", "headline_point", "headline_median", "headline_low", "headline_high",
         "interval_level", "n_draws", "comparator_total", "share_of_comparator", "n_units", "n_packages_in_plan",
         "cost_allocated", "budget_allocatable", "reserve", "cost_per_outcome", "n_tests", "n_tests_no_change",
         "n_tests_change_plan", "n_tests_not_run", "n_tests_near_miss", "voi_status", "evpi", "evpi_share", "n_inputs",
         "n_inputs_supported", "n_prior_dominated", "stability_verdict", "units_same_package_share", "n_units_stable",
         "n_variants", "n_burden_refs", "n_burden_outside", "dearest_share", "against")
COLUMNS = outcomes_page.COLUMNS
CHECKS = outcomes_checks.CHECKS      # check -> scorecard case (§8)
PACKAGE_ONLY = ("plan", "variants", "burden", "voi", "emulator")      # null for the estimates kind (§5.7)
ADAPTERS = {"package_per_unit": outcomes_package.read, "estimates": outcomes_estimates.read}


def model(run):
    """(the generic model of one run, why no adapter could fill it or None)."""
    s = R.spec(run)
    if s["kind"] in ADAPTERS:
        return ADAPTERS[s["kind"]](run, s), None
    if s["kind"] is None:
        _, _, why = run.hashed(R.SIGNED)
        return R.empty(None, s), (f"the run has no signed spec to read a deliverable kind from ({why})" if not s["raw"] else
                                  f"the signed spec names no deliverable kind ({R.SIGNED} deliverable.kind)")
    return R.empty(s["kind"], s), (f"the signed spec's deliverable kind is {s['kind']!r}, which this view does not know "
                                   f"({', '.join(ADAPTERS)}), so nothing is read as if it were one of them")


def view(run, against=None):
    m, why = model(run)
    label = None
    if against is not None:
        label = getattr(against, "label", None) or os.path.basename(against.path.rstrip(os.sep)) or "the other run"
    comp = outcomes_compare.compare(m, None if against is None else model(against)[0], label)
    found = outcomes_checks.unknown(why) if why else outcomes_checks.run_all(m)
    by = {c["id"].split(".")[-1]: c for c in found + [comp["check"]]}
    names = (["run_shift"] if against is not None else []) + [n for n in CHECKS if against is None or n != "run_shift"]     # a comparison asked for is read first
    out = outcomes_page.result(m, comp, [by[n] for n in names], R.figures(run))
    out["facts"] = {k: m["facts"].get(k) for k in FACTS[:-1]} | {"against": comp["against"]}
    if why:
        out["notes"]["kind"] = f"Nothing is shown: {why}."
    return out


def _self_test():
    import json
    import re

    import yaml

    import workbench
    from lib.safe_io import write_nofollow
    from views import overview
    from views.fit_bench import Bench, check, close, data, fixture_builder, need, rewrite, says
    fx = fixture_builder()
    b = Bench(lambda run: view(run))        # the module's `view` as it is when a case runs
    folder, case, edited = b.folder, b.case, b.edited
    hm, post = fx.METHODS
    OUT, HELD = fx.OUTCOME, fx.HELD

    def seen(d, against=None):
        v = view(workbench.Run(d), against=None if against is None else workbench.Run(against))
        need(isinstance(v, dict) and set(v) >= {"checks", "facts", "tables", "charts", "notes"},
             "view returns {checks, facts, tables, charts, notes}")
        return v

    def statuses(v):
        return {str(c.get("id")): c.get("status") for c in v["checks"]}

    def status(v, name):
        return check(v, name)["status"]

    def table(v, name):
        t, columns = v["tables"].get(name), COLUMNS[name]
        need(isinstance(t, dict) and t.get("columns") == columns, f"tables.{name} has columns {columns} (got {t.get('columns') if isinstance(t, dict) else t})")
        need(all(isinstance(r, list) and len(r) == len(columns) for r in t["rows"]), f"tables.{name} rows are lists in column order")
        return [dict(zip(columns, r)) for r in t["rows"]]

    def row(v, name, value):
        key = COLUMNS[name][0]
        found = [r for r in table(v, name) if r[key] == value]
        need(len(found) == 1, f"one row of tables.{name} with {key} {value!r} (found {len(found)}: {[r[key] for r in table(v, name)]})")
        return found[0]

    def has(r, **want):
        bad = {k: (r.get(k), w) for k, w in want.items() if not (close(r.get(k), w) if isinstance(w, float) else r.get(k) == w)}
        need(not bad, f"row {r.get(next(iter(r)))}: " + "; ".join(f"{k} is {g!r}, expected {w!r}" for k, (g, w) in bad.items()))

    def every(d):
        """Every check of every view for a run folder, by its full id (the overview gathers them)."""
        return {str(c["id"]): c["status"] for c in overview.view(workbench.Run(d))["checks"]}

    def doc_edit(rel, change):
        return rewrite(rel, lambda doc: change(doc["data"]))

    def without(*rels):
        def edit(d):
            for rel in rels:
                os.remove(os.path.join(d, rel))
        return edit

    def tests_edit(tid, **words):
        return doc_edit(fx.DT, lambda d: next(t for t in d["tests"] if t["test_id"] == tid).update(words))

    # ------------------------------------------------------------------ the generic model, package_per_unit
    def m_shape():
        for method in fx.METHODS:
            v = seen(folder(method))
            need(set(v) - {"images", "choose", "fact_labels"} == {"checks", "facts", "tables", "charts", "notes"}, f"{method}: the five parts, with images and choose (got {sorted(v)})")
            need(v["choose"] == {"param": "against", "label": v["choose"]["label"], "options": "runs"} and v["choose"]["label"],
                 "the view declares how the page chooses the comparison run: the server fills the options with the runs it found")
            need(tuple(sorted(v["facts"])) == tuple(sorted(FACTS)), f"{method}: exactly the facts of §5.7 (differs in {sorted(set(v['facts']) ^ set(FACTS))})")
            need([c["id"] for c in v["checks"]] == [f"outcomes.{n}" for n in CHECKS] or sorted(c["id"] for c in v["checks"]) == sorted(f"outcomes.{n}" for n in CHECKS),
                 f"{method}: the twelve checks, ids outcomes.<name> (got {[c.get('id') for c in v['checks']]})")
            for c in v["checks"]:
                name = c["id"].split(".")[-1]
                need(c.get("step") == "outcomes" and c.get("status") in workbench.checks.STATUSES and c.get("title"), f"{name}: step, status and title")
                need(isinstance(c.get("says"), str) and c["says"].strip() and "\n" not in c["says"], f"{name}: one plain sentence")
                ev = c.get("evidence")
                need(isinstance(ev, list) and ev and all(isinstance(e, dict) and e.get("file") and "field" in e for e in ev), f"{name}: names the file and field read ({ev})")
                need(c.get("case") == CHECKS[name], f"{name}: scorecard case {CHECKS[name]} (got {c.get('case')})")
            need(set(v["tables"]) >= set(COLUMNS), f"{method}: every table of the model is present, null where not recorded (missing {sorted(set(COLUMNS) - set(v['tables']))})")
            need(all(v["tables"][k] is None for k in ("outputs", "decision", "shift", "spec_diff", "outside_spec")),
                 f"{method}: outputs and decision belong to the estimates kind, shift and spec_diff to a comparison: null here")
            need(all(isinstance(v["tables"][k], dict) and v["tables"][k].get("about") for k in COLUMNS if v["tables"][k] is not None), "each table says what its rows are")
            need(all(isinstance(t, str) and t.strip() for t in v["notes"].values()) and v["notes"], "notes are plain text")
            need(any("signed" in t for t in v["notes"].values()), "a note says which copy of the spec was read (§3)")

    def m_clean():
        for method in fx.METHODS:
            v = seen(folder(method))
            want = {n: "not_available" if n == "run_shift" else "ok" for n in CHECKS}
            got = {k.split(".")[-1]: s for k, s in statuses(v).items()}
            need(got == want, f"{method}: on the clean run every check is ok, and run_shift not_available with no comparison ({ {k: s for k, s in got.items() if want.get(k) != s} })")
            all_views = every(folder(method))
            need(not {k: s for k, s in all_views.items() if s in ("look", "problem")}, f"{method}: no view raises anything on the clean run ({ {k: s for k, s in all_views.items() if s in ('look', 'problem')} })")

    def m_facts():
        for method in fx.METHODS:
            d = folder(method)
            f, point, unc, voi = seen(d)["facts"], data(d, fx.POINT), data(d, fx.UNC), data(d, fx.VOI)
            draws, n = unc["fixed_plan"][OUT], len(data(d, fx.DT)["tests"])
            want = {"deliverable_kind": "package_per_unit", "headline_outcome": fx.HEADLINE, "headline_point": point["averted"][OUT],
                    "headline_median": draws["median"], "headline_low": draws["ci_low"], "headline_high": draws["ci_high"],
                    "interval_level": 0.95, "n_draws": fx.DRAWS, "comparator_total": point["comparator_total"][OUT],
                    "share_of_comparator": point["averted"][OUT] / point["comparator_total"][OUT], "n_units": 12, "n_packages_in_plan": 2,
                    "cost_allocated": point["cost_allocated"], "budget_allocatable": fx.ENVELOPE, "reserve": point["reserve"]["reserve_usd"],
                    "cost_per_outcome": point["cost_per_outcome_averted"], "n_tests": n, "n_tests_no_change": n, "n_tests_change_plan": 0,
                    "n_tests_not_run": 0, "n_tests_near_miss": 0, "voi_status": "computed", "evpi": voi["evpi"]["value"],
                    "evpi_share": voi["evpi"]["pct"] / 100, "n_inputs": 3, "n_inputs_supported": 2, "n_prior_dominated": 1,
                    "stability_verdict": "ROBUST", "units_same_package_share": 1.0, "n_units_stable": 12, "n_variants": 1,
                    "n_burden_refs": 4, "n_burden_outside": 0, "dearest_share": 0.25, "against": None}
            bad = {k: (f.get(k), w) for k, w in want.items() if not (close(f.get(k), w) if isinstance(w, float) else f.get(k) == w)}
            need(n == (9 if method == hm else 8) and not bad, f"{method}: " + "; ".join(f"{k} is {g!r}, expected {w!r}" for k, (g, w) in bad.items()))

    def m_tables():
        d = folder(hm)
        v, point, pv = seen(d), data(d, fx.POINT), data(d, fx.PV)["variants"][0]
        cost = {p: sum(x["cost"] for x in point["per_unit"].values() if x["package"] == p) for p in ("A+B", "A+C")}
        need([r["package"] for r in table(v, "plan")] == ["A+B", "A+C"], "plan: one row per package in the plan, most units first")
        has(row(v, "plan", "A+B"), units=9, share=0.75, cost=float(cost["A+B"]), cost_per_unit_rank=2)
        has(row(v, "plan", "A+C"), units=3, share=0.25, cost=float(cost["A+C"]), cost_per_unit_rank=1)
        need({r["component"]: r["units"] for r in table(v, "components")} == {"A": 12, "B": 9, "C": 3}, f"components: units per component of the signed spec ({table(v, 'components')})")
        spec = yaml.safe_load(open(os.path.join(d, fx.SPEC_FILES[1]), encoding="utf-8"))
        asked = {t["id"]: t["text"] for t in spec["analysis"]["decision_tests"]}
        need([r["id"] for r in table(v, "tests")] == list(asked), "tests: one row per test, in the file's order")
        for r in table(v, "tests"):
            has(r, question=asked[r["id"]], status="run", verdict="NO_CHANGE", share_pct=0.0, share_threshold_pct=10.0, regret_pct=0.0,
                regret_threshold_pct=1.0, moved_pct=0.0, moved_threshold_pct=5.0, near_miss=False)
            need(isinstance(r["varied"], str) and r["varied"], f"{r['id']}: what was varied, from the test's own result file")
        has(row(v, "tests", "DT7"), varied=f"engine parameter {HELD} set to each listed value")
        has(row(v, "voi", "p1"), kind="input", evppi=3.6, share_of_evpi=0.6, above_noise=True, prior_dominated=False)
        has(row(v, "voi", "p3"), above_noise=False, prior_dominated=True)
        need([r["input"] for r in table(v, "voi")] == ["p1", "p2", "p3"], "voi: one row per input, largest value first")
        draws = pv["uncertainty"]["averted"][OUT]
        has(row(v, "variants", "one_option"), outcome=float(pv["objective"]), low=draws["ci_low"], high=draws["ci_high"], cost=float(pv["cost"]),
            value_lost_pct=pv["value_lost_pct"], over_budget_share=0.02, units_changed=3)
        need(len(table(v, "variants")) == 1, "variants: the optimised plan is not a row")
        has(row(v, "burden", "ref_G1"), group="G1", outcome=OUT, ratio=1.05, band_low=0.5, band_high=2.0, state="within", report_only=False)
        has(row(v, "burden", "ref_G3"), state="within", report_only=True)
        has(row(v, "stability", "total_benefit_loss_pct_worst_fold"), value=0.4, verdict="ROBUST")
        has(row(v, "emulator", "share_matched"), value=1.0, threshold=fx.BOUNDARY_MIN, passes=True)
        has(row(v, "emulator", "reduction_r2_median"), value=0.97, threshold=None, passes=None)

    def m_charts():
        for d in (folder(hm), folder(post, figures="estimates", kind="estimates")):
            v = seen(d)
            need(workbench.chart_problems(v) == [], f"every chart is one the page can draw ({workbench.chart_problems(v)[:3]})")
            for c in v["charts"]:
                t = v["tables"].get(c.get("table"))
                need(c.get("kind") in workbench.CHART_KINDS and isinstance(t, dict) and c.get("title") and c.get("how_to_read"),
                     f"a chart names a kind, a table the run records, a title and how to read it ({c})")
                need(all(c[k] in t["columns"] for k in workbench.CHART_COLUMNS if k in c and isinstance(c[k], str)), f"a chart's columns are its table's ({c})")
        v = seen(folder(hm))
        drawn = {c["table"] for c in v["charts"]}
        need(any(c.get("low") and c.get("high") and c["table"] not in ("burden", "variants", "tests") for c in v["charts"]),
             "a chart draws the headline with its interval (assumed: from a table the view adds for it)")
        need({"plan", "tests", "voi", "variants", "burden"} <= drawn and len(v["charts"]) >= 6,
             f"charts: the headline with its interval, plan mix, tests against the threshold, value of information, variants, burden ratios (tables drawn: {sorted(drawn)})")
        tests = next(c for c in v["charts"] if c["table"] == "tests")
        need(tests.get("y") == "share_pct" and tests.get("limits"), "the tests chart draws each share against the threshold, as a reference line")
        burden = next(c for c in v["charts"] if c["table"] == "burden")
        need(burden.get("kind") in ("ranges", "interval_points") and {burden.get("y"), burden.get("low"), burden.get("high")} >= {"ratio"},
             "the burden chart draws each ratio against its band")

    # ------------------------------------------------------------------ the estimates kind
    def e_model():
        d = folder(post, figures="estimates", kind="estimates")
        v, est = seen(d), data(d, fx.EST)
        f, q1 = v["facts"], est["outputs"]["q1"]
        need(tuple(sorted(f)) == tuple(sorted(FACTS)), "the same facts for both kinds")
        want = {"deliverable_kind": "estimates", "headline_outcome": "q1", "headline_point": None, "headline_median": q1["median"],
                "headline_low": q1["ci_low"], "headline_high": q1["ci_high"], "interval_level": 0.95, "n_draws": fx.DRAWS,
                "comparator_total": None, "share_of_comparator": None, "n_packages_in_plan": None, "cost_allocated": None,
                "budget_allocatable": None, "reserve": None, "cost_per_outcome": None, "n_tests": 2, "n_tests_no_change": 2,
                "n_tests_change_plan": 0, "n_tests_not_run": 0, "voi_status": None, "evpi": None, "n_variants": None,
                "n_burden_refs": None, "dearest_share": None, "against": None}
        bad = {k: (f.get(k), w) for k, w in want.items() if not (close(f.get(k), w) if isinstance(w, float) else f.get(k) == w)}
        need(not bad, "; ".join(f"{k} is {g!r}, expected {w!r}" for k, (g, w) in bad.items()))
        need(all(v["tables"][k] is None for k in PACKAGE_ONLY), f"the plan, variants, burden, value of information and emulator tables are null ({[k for k in PACKAGE_ONLY if v['tables'][k] is not None]})")
        has(row(v, "outputs", "q1"), median=q1["median"], low=q1["ci_low"], high=q1["ci_high"], scale="ratio")
        need([r["name"] for r in table(v, "outputs")] == ["q1", "q2"], "outputs: one row per output")
        has(row(v, "decision", "q1"), threshold=1.0, direction="below", probability_required=0.9, probability=0.97, verdict="yes")
        need([r["id"] for r in table(v, "tests")] == ["prior_range_p1", "prior_range_p2"], "the kind's decision tests are read from analysis/estimates.yaml")
        has(row(v, "tests", "prior_range_p1"), status="run", verdict="NO_CHANGE", share_pct=None, regret_pct=None, moved_pct=None, near_miss=False)
        got = {k.split(".")[-1]: s for k, s in statuses(v).items()}
        need(got == {n: "ok" if n == "tests_that_move" else "not_available" for n in CHECKS},
             f"tests_that_move is ok and every check the kind has nothing for is not_available ({got})")
        need(any("estimates" in t and "plan" in t for t in v["notes"].values()), "one note says what this kind does not record")

    def e_tests():
        base = dict(figures="estimates", kind="estimates")
        v = seen(folder(post, "estimates_test_changes", **base))
        has(row(v, "tests", "prior_range_p1"), status="run", verdict="CHANGES_PLAN")
        need(status(v, "tests_that_move") == "look" and "prior_range_p1" in check(v, "tests_that_move")["says"] and v["facts"]["n_tests_change_plan"] == 1,
             "a test whose rule's verdict changes at one end changes the decision: look, naming it")
        need(status(seen(folder(post, "estimates_test_unknown_status", **base)), "tests_that_move") == "not_available", "a status word the producer does not write is not_available")

        def no_verdict(d):
            for e in d["decision_tests"][0]["ends"]:
                e.pop("verdict")
        got = status(seen(edited(post, doc_edit(fx.EST, no_verdict), **base)), "tests_that_move")
        need(got in ("not_available", "problem"), f"a test counts only with a real verdict: status run with no verdict at its ends is never ok ({got})")
        got = status(seen(edited(post, doc_edit(fx.EST, lambda d: d["decision_tests"][0].update(status="not_run", decision_changes=None)), **base)), "tests_that_move")
        need(got != "ok", f"a test that did not run is not reported as passed ({got})")

    # ------------------------------------------------------------------ the settled rules, one case each
    def s_point():
        clean = folder(hm)
        need(status(seen(clean), "point_vs_draws") == "ok", "the point value equal to the median, inside the interval: ok")
        c = check(seen(folder(hm, "point_outside_interval")), "point_vs_draws")
        need(c["status"] == "problem" and "interval" in says(c), f"the point value outside the interval is a problem ({c['status']}: {c['says']})")
        d = folder(hm, "point_far_from_median")
        c, h, med = check(seen(d), "point_vs_draws"), data(d, fx.POINT)["averted"][OUT], data(d, fx.UNC)["fixed_plan"][OUT]["median"]
        need(c["status"] == "look" and all(f"{x:g}"[:5] in c["says"].replace(",", "") for x in (h, med)) and "workbench threshold" in says(c),
             f"more than 5% of the median from the median is look, and the sentence gives both values and labels the threshold ({c['status']}: {c['says']})")

        def at(share):
            def change(doc):
                h = data(clean, fx.POINT)["averted"][OUT]
                doc["fixed_plan"][OUT].update(median=h / (1 + share))
            return status(seen(edited(hm, doc_edit(fx.UNC, change))), "point_vs_draws")
        need((at(0.03), at(0.06), at(-0.06)) == ("ok", "look", "look"), "3% from the median is ok; 6% above or below it is look")
        need(status(seen(folder(hm, "interval_reversed")), "point_vs_draws") == "problem", "an interval whose lower end is above its upper end contradicts itself (assumed: problem)")
        for gone in ("ci_low", "median"):
            got = status(seen(edited(hm, doc_edit(fx.UNC, lambda doc, gone=gone: doc["fixed_plan"][OUT].update({gone: None})))), "point_vs_draws")
            need(got == "not_available", f"with no {gone} the point value cannot be placed ({got})")

    def s_dearest():
        v = seen(folder(hm, "C8_dearest_wins"))
        c = check(v, "dearest_wins")
        need(c["status"] == "look" and close(v["facts"]["dearest_share"], 11 / 12) and re.search(r"\bC\b", c["says"]) and "red_flag.py" in str(c["evidence"]) + c["says"],
             f"the dearer option in 11 of 12 units is look, never more, and the threshold is named with its file ({c['status']}: {c['says']})")
        has(row(v, "plan", "A+C"), units=11, cost_per_unit_rank=1)
        v = seen(folder(hm, "C8_dearest_few_units"))
        need(status(v, "dearest_wins") == "ok" and close(v["facts"]["dearest_share"], 1.0),
             "the dearer option in every unit that holds the group, but in four units, under the producer's minimum of five: ok")
        need(row(v, "plan", "A")["cost_per_unit_rank"] is None, "a package with no option of a group has no rank")
        nine = {u: "A+C" if i < 10 else "A+B" for i, u in enumerate(fx.UNITS)}       # 10 of 12: under the share

        def under(d):
            p = d["plan"]
            for u in p:
                p[u] = d["per_unit"][u]["package"] = nine[u]
        got = status(seen(edited(hm, doc_edit(fx.POINT, under))), "dearest_wins")
        need(got != "problem", f"the check is never more than look ({got})")
        need(status(seen(folder(hm, "plan_unknown_package")), "dearest_wins") == "not_available", "a plan naming a package the signed spec does not define cannot be priced (assumed: not_available)")

    def s_tests():
        v = seen(folder(hm, "test_near_miss"))
        has(row(v, "tests", "DT1"), verdict="NO_CHANGE", share_pct=8.0, near_miss=True)
        c = check(v, "tests_that_move")
        need(c["status"] == "look" and v["facts"]["n_tests_near_miss"] == 1 and v["facts"]["n_tests_change_plan"] == 0 and "DT1" in c["says"],
             f"a test with no change within a quarter of its threshold is a near miss: look ({c['status']}: {c['says']})")
        for words, near in (({"dollars_share_pct": 4.5, "dollars_moved_pct": 1.0}, True), ({"dollars_share_pct": 1.0, "dollars_moved_pct": 4.5}, False),
                            ({"regret_pct": 0.8}, True), ({"share_pct": 7.0}, False)):
            v = seen(edited(hm, tests_edit("DT1", **words)))
            need(row(v, "tests", "DT1")["near_miss"] is near and status(v, "tests_that_move") == ("look" if near else "ok"),
                 f"{words}: near miss {near}; moved_pct is the share the producer compares with its threshold (dollars_share_pct)")
        has(row(seen(edited(hm, tests_edit("DT1", dollars_share_pct=4.5, dollars_moved_pct=1.0))), "tests", "DT1"), moved_pct=4.5, moved_threshold_pct=5.0)
        v = seen(folder(hm, "test_changes_linked"))
        has(row(v, "tests", "DT9"), verdict="CHANGES_PLAN", near_miss=False, issue="nodata:unit_attribute:x1")
        c = check(v, "tests_that_move")
        need(c["status"] == "look" and "DT9" in c["says"] and "nodata:unit_attribute:x1" in c["says"] and v["facts"]["n_tests_change_plan"] == 1,
             f"a plan-changing test is look, and the sentence names the register row it is linked to ({c['says']})")
        v = seen(folder(hm, "test_changes_unlinked"))
        has(row(v, "tests", "DT1"), verdict="CHANGES_PLAN", issue=None)
        need(status(v, "tests_that_move") == "look" and "linked to no" in says(check(v, "tests_that_move")),
             f"a plan-changing test linked only to its own test:<id> row has no issue, and the sentence says so ({check(v, 'tests_that_move')['says']})")
        clean = seen(folder(hm))
        need({r["id"]: r["issue"] for r in table(clean, "tests") if r["id"] in ("DT1", "DT7", "DT8", "DT9", "DT10")}
             == {"DT1": None, "DT7": f"excluded:{HELD}", "DT8": "nodata:parameter:p2", "DT9": "nodata:unit_attribute:x1", "DT10": None},
             "issue: the register row whose structured key names the test, on the clean run too")
        v = seen(folder(hm, "test_not_run"))
        has(row(v, "tests", f"scenario_excluded_{HELD}"), status="not_run", verdict="NOT_RUN", question=None, varied=None, share_pct=None, near_miss=False)
        c = check(v, "tests_that_move")
        need(v["facts"]["n_tests_not_run"] == 1 and c["status"] == "ok" and f"scenario_excluded_{HELD}" in c["says"] and "did not run" in c["says"],
             f"settled: the producer's own not-run scenario for an input the verified signed spec excludes is counted and listed, and does not raise the check ({c['status']}: {c['says']})")
        for what, edit in (("a test of the signed spec that did not run", tests_edit("DT1", status="not_run", verdict="NOT_RUN")),
                           ("a not-run scenario for an input the signed spec does not exclude",
                            doc_edit(fx.DT, lambda d: d["tests"].append({"test_id": "scenario_excluded_p9", "status": "not_run", "verdict": "NOT_RUN"})))):
            v = seen(edited(hm, edit))
            need(v["facts"]["n_tests_not_run"] == 1 and status(v, "tests_that_move") == "look", f"{what} is look: it is never reported as passed")

        def unsigned(d):
            with open(os.path.join(d, fx.SPEC_FILES[1]), "a", encoding="utf-8") as f:
                f.write("note: edited after the sign-off\n")
        need(status(seen(edited(hm, unsigned, "test_not_run")), "tests_that_move") == "not_available",
             "with no verified signed spec nothing excuses the scenario that did not run, and no test is tied to a signed spec: never ok (review 1)")
        c = check(seen(folder(hm, "test_pair_contradicts")), "tests_that_move")
        need(c["status"] == "problem" and "DT1" in c["says"], f"a status and a verdict that contradict each other are a problem ({c['status']}: {c['says']})")
        for words in ({"status": "done"}, {"verdict": "MAYBE"}):
            got = status(seen(edited(hm, tests_edit("DT1", **words))), "tests_that_move")
            need(got == "not_available", f"{words}: a word the producer does not write is not_available ({got})")

    def s_stability_burden():
        v = seen(folder(hm, "stability_disagree"))
        need(v["facts"]["stability_verdict"] == "ROBUST" and row(v, "stability", "total_benefit_loss_pct_worst_fold")["verdict"] == "FRAGILE",
             "stability_verdict is the plan's verdict; the value verdict is a row of tables.stability")
        c = check(v, "stability_agreement")
        need(c["status"] == "look" and "ROBUST" in c["says"] and "FRAGILE" in c["says"], f"the two verdicts disagree: look, with both ({c['says']})")
        for words in ({"verdict": "MAYBE"}, {"value_verdict": "NOT_RUN"}):
            got = status(seen(edited(hm, doc_edit(fx.STAB, lambda doc, words=words: doc.update(words)))), "stability_agreement")
            need(got == "not_available", f"{words}: a verdict that is unknown or was not computed is never agreement ({got})")
        v = seen(folder(hm, "burden_report_only_tested"))
        c = check(v, "burden_refs")
        need(c["status"] == "look" and v["facts"]["n_burden_outside"] == 1 and "ref_G3" in c["says"] and "DT10" in c["says"] and "NO_CHANGE" in c["says"].replace("no change", "NO_CHANGE"),
             f"a report-only reference outside its band, named by a test that ran: look, with the test and its verdict ({c['says']})")
        has(row(v, "burden", "ref_G3"), state="outside", report_only=True, ratio=2.6)
        for name in ("burden_report_only_untested", "burden_checked_outside", "burden_test_not_run"):
            need(status(seen(folder(hm, name)), "burden_refs") == "problem", f"{name}: problem")
        got = status(seen(edited(hm, doc_edit(fx.BV, lambda doc: doc["references"][0].update(status="odd")))), "burden_refs")
        need(got == "not_available", f"a status word the producer does not write is not_available ({got})")
        got = status(seen(edited(hm, doc_edit(fx.BV, lambda doc: doc["references"][0].update(status="unmatched", ratio=None)))), "burden_refs")
        need(got not in ("ok", "look"), f"a checked reference the model could not be matched to is never passed ({got})")

    def s_shift():
        clean = folder(hm)
        v = seen(clean, folder(hm, other="second"))
        need(isinstance(v["facts"]["against"], str) and v["facts"]["against"], "facts.against is the comparison run's label")
        need([r["item"] for r in table(v, "shift")] == ["A+B", "A+C", "headline", "envelope", "comparator"], f"shift: one row per package, then the headline, the envelope and the comparator text ({[r['item'] for r in table(v, 'shift')]})")
        has(row(v, "shift", "A+C"), this_run=3, other_run=3, change=0)
        has(row(v, "shift", "envelope"), this_run=100000.0, other_run=110000.0, change=-10000.0)
        has(row(v, "shift", "comparator"), change=None)
        need([r["key"] for r in table(v, "spec_diff")] == ["constraints.envelope_usd"], f"spec_diff lists the one key that differs, to two levels ({table(v, 'spec_diff')})")
        has(row(v, "spec_diff", "constraints.envelope_usd"), this_run=100000.0, other_run=110000.0)
        need(status(v, "run_shift") == "look", "the same plan and headline under a spec that differs in one key: look (a key changed is still a change)")
        v = seen(clean, folder(hm, other="twin"))
        need(status(v, "run_shift") == "ok" and table(v, "spec_diff") == [] and all(r["same"] for r in table(v, "outside_spec")), "the clean pair: ok, no key differs, nothing recorded differs")
        v = seen(clean, folder(hm, other="shifted"))
        has(row(v, "shift", "A+C"), this_run=3, other_run=6, change=-3)
        c = check(v, "run_shift")
        need(c["status"] == "look" and "constraints.envelope_usd" in c["says"] and "workbench threshold" in says(c), f"a shift with a spec key that differs is look, naming the key ({c['says']})")
        v = seen(clean, folder(hm, other="headline"))
        has(row(v, "shift", "comparator"), change="differs")
        need(close(row(v, "shift", "headline")["other_run"] / row(v, "shift", "headline")["this_run"], 1.6) and status(v, "run_shift") == "look",
             "a headline 1.6 times the other is look")
        need([r["key"] for r in table(v, "spec_diff")] == ["comparator.text"], "and the diff names comparator.text")

        def more_tests(spec):
            spec["analysis"]["decision_tests"].append({"id": "DT99", "type": "parameter_value", "parameter": "p1", "values": [0.3], "text": "One more?"})
        other = edited(hm, resign(more_tests), other="second")
        r = row(seen(clean, other), "spec_diff", "analysis.decision_tests")
        need((r["this_run"], r["other_run"]) == (9, 10), f"a cell holds the count of entries where the value is no scalar ({r})")
        need(seen(clean)["tables"]["shift"] is None and seen(clean)["tables"]["spec_diff"] is None, "no comparison chosen: both tables null")

    def resign(change):
        """An edit: the spec changed in both copies and signed again (the sign-off record and every analysis header
        hold the new hash), as a signed amendment followed by the analysis leaves a run."""
        import hashlib

        from views.fit_bench import restamp

        def edit(d):
            with open(os.path.join(d, fx.SPEC_FILES[1]), encoding="utf-8") as f:
                text = f.read()
            spec, old = yaml.safe_load(text), hashlib.sha256(text.encode()).hexdigest()
            change(spec)
            raw = yaml.safe_dump(spec, sort_keys=False).encode()
            for rel in fx.SPEC_FILES:
                write_nofollow(d, rel, raw)
            with open(os.path.join(d, "decision_spec.signoff.json"), encoding="utf-8") as f:
                so = json.load(f)
            so["sha256"]["decision_spec.yaml"] = hashlib.sha256(raw).hexdigest()
            write_nofollow(d, "decision_spec.signoff.json", json.dumps(so).encode())
            restamp(d, old, so["sha256"]["decision_spec.yaml"])
        return edit

    def s_run_shift():
        clean = folder(hm)
        c = check(seen(clean), "run_shift")
        need(c["status"] == "not_available" and "comparison" in says(c), f"not_available until a comparison run is chosen ({c['says']})")
        c = check(seen(clean, folder(hm, other="shifted_same_spec")), "run_shift")
        need(c["status"] == "problem" and "spec" in says(c), f"a shift with no spec key that differs is a problem: something outside the spec changed ({c['says']})")
        v = seen(clean, folder(hm, other="other_question"))
        c = check(v, "run_shift")
        need(c["status"] == "not_available" and "question" in says(c) and v["tables"]["shift"] is None,
             f"two runs whose signed specs name different questions are never compared ({c['status']}: {c['says']})")
        v = seen(clean, folder(hm, other="unverified"))
        c = check(v, "run_shift")
        need(c["status"] == "not_available" and "sign" in says(c) and v["tables"]["shift"] is None and v["tables"]["spec_diff"] is None,
             f"a comparison run whose signed spec cannot be verified: nothing is compared, with the reason ({c['status']}: {c['says']})")
        got = status(seen(folder(hm, other="unverified"), clean), "run_shift")
        need(got == "not_available", f"nor when it is this run's signed spec that cannot be verified ({got})")
        got = status(seen(clean, edited(hm, without(fx.POINT), other="shifted")), "run_shift")
        need(got == "not_available", f"a comparison run with no plan is not_available, never ok ({got})")
        for other, want in (("shifted", "look"), ("second", "look"), ("twin", "ok")):
            need(status(seen(folder(post), folder(post, other=other)), "run_shift") == want, f"posterior, against {other}: {want}")

    def s_new_checks():
        v = seen(folder(hm, "test_infeasible"))
        c = check(v, "infeasible_tests")
        need(c["status"] == "look" and "DT1" in c["says"] and row(v, "tests", "DT1")["verdict"] == "CHANGES_PLAN", f"a test under which the point plan is not feasible: look, with its id ({c['says']})")
        got = status(seen(edited(hm, doc_edit(fx.DT, lambda d: [t.pop("point_plan_feasible") for t in d["tests"]]))), "infeasible_tests")
        need(got == "not_available", f"tests that record no feasibility: not_available, never ok ({got})")
        c = check(seen(folder(hm, "variant_over_budget")), "variant_budget")
        need(c["status"] == "look" and "one_option" in c["says"] and "10%" in c["says"].replace("0.1", "10%"), f"a variant over budget in more draws than the spec allows: look, with the allowance ({c['says']})")
        need(status(seen(folder(hm, "variant_no_cost")), "variant_budget") == "not_available", "a variant with no cost or draws: not_available")
        got = status(seen(edited(hm, resign(lambda spec: spec["uncertainty"].pop("affordability")))), "variant_budget")
        need(got == "not_available", f"a signed spec that sets no allowance: nothing to compare with ({got})")
        v = seen(folder(hm, "fixed_input_changes_plan"))
        c = check(v, "fixed_inputs")
        need(c["status"] == "look" and HELD in c["says"] and "DT7" in c["says"], f"an input held at one value that changes the plan when a test varies it: look, naming both ({c['says']})")
        need(status(seen(folder(hm, "test_changes_linked")), "fixed_inputs") == "ok", "a plan-changing test of an input that is drawn is not this check's")
        need(seen(folder(hm))["facts"]["n_units_stable"] == 12, "n_units_stable: units keeping their package in at least 90% of draws")
        c = check(seen(folder(hm, "boundary_uncovered")), "boundary_coverage")
        need(c["status"] == "look" and re.search(r"\bC\b", c["says"]) and not re.search(r"\bB\b", c["says"]), f"a component in no row of the boundary check: look, naming it ({c['says']})")
        c = check(seen(folder(hm, "C7_share_of_burden")), "share_of_burden")
        need(c["status"] == "look" and "workbench threshold" in says(c), f"averted above half of the comparator: look, with the threshold labelled ({c['says']})")

    def s_voi():
        v = seen(folder(hm, "voi_indicative"))
        need(status(v, "voi_support") == "look" and v["facts"]["voi_status"] == "indicative" and v["facts"]["n_draws"] == 200 and "500" in check(v, "voi_support")["says"],
             "fewer draws than the producer's informative_draws: look, with both numbers")
        v = seen(folder(hm, "voi_too_few_draws"))
        need(status(v, "voi_support") == "not_available" and v["facts"]["evpi"] is None and v["facts"]["voi_status"] == "too_few_draws", "nothing computed is never reported as passed")
        v = seen(folder(hm, "voi_none_above_floor"))
        need(status(v, "voi_support") == "look" and v["facts"]["n_inputs_supported"] == 0, "no input above its noise floor: look")
        v = seen(folder(hm, "C14_dominated_carries_value"))
        c = check(v, "voi_support")
        need(c["status"] == "look" and "p3" in c["says"] and row(v, "voi", "p3")["above_noise"] is True and row(v, "voi", "p3")["prior_dominated"] is True,
             f"a prior-dominated input above its noise floor carries value the data do not constrain (C14): look, naming it ({c['says']})")
        v = seen(folder(hm, "C14_prior_dominated"))
        need(status(v, "voi_support") == "look" and row(v, "voi", "p1")["prior_dominated"] is True and v["facts"]["n_prior_dominated"] == 2,
             "the Fit view's C14 edit (p1 listed as prior-dominated) is raised here too: p1 is above its floor")
        got = status(seen(edited(hm, doc_edit(fx.VOI, lambda doc: doc.update(status="done")))), "voi_support")
        need(got == "not_available", f"a status word the producer does not write is not_available ({got})")

    # ------------------------------------------------------------------ the external review's findings, one case each
    def header_edit(rel, **words):
        """An edit to an analysis file's header (beside `data`): a key set, or removed when its value is None."""
        def change(doc):
            for k, v in words.items():
                doc.pop(k, None) if v is None else doc.update({k: v})
        return rewrite(rel, change)

    def v_other_spec():
        v = seen(edited(hm, header_edit(fx.DT, spec_sha256="0" * 64)))
        c, f = check(v, "tests_that_move"), v["facts"]
        need(c["status"] == "problem" and "another spec" in c["says"], f"tests run for another spec than the signed one: problem, saying so ({c['status']}: {c['says']})")
        need(f["n_tests"] == 9 and all(f[k] is None for k in ("n_tests_no_change", "n_tests_change_plan", "n_tests_near_miss")),
             f"and none of them is counted as a result ({ {k: f[k] for k in f if k.startswith('n_tests')} })")
        need(status(v, "infeasible_tests") == "not_available" and status(v, "fixed_inputs") == "not_available", "nor passed by the checks that read the tests")
        v = seen(edited(hm, header_edit(fx.DT, spec_sha256=None)))
        c = check(v, "tests_that_move")
        need(c["status"] == "not_available" and "spec_sha256" in c["says"] and v["facts"]["n_tests_no_change"] is None,
             f"a tests file that records no spec: not_available, with the loader's reason ({c['status']}: {c['says']})")
        v = seen(edited(post, header_edit(fx.EST, spec_sha256="0" * 64), figures="estimates", kind="estimates"))
        need(status(v, "tests_that_move") == "problem" and v["facts"]["n_tests_no_change"] is None, "the estimates kind too")

    def v_estimates_verdict():
        base = dict(figures="estimates", kind="estimates")
        c = check(seen(edited(post, doc_edit(fx.EST, lambda d: d["decision_tests"][0].update(ends=[])), **base)), "tests_that_move")
        need(c["status"] == "not_available", f"status run with no end: the test ran with no verdict ({c['status']}: {c['says']})")
        c = check(seen(edited(post, doc_edit(fx.EST, lambda d: d["decision_tests"][0]["ends"][1].update(verdict="no")), **base)), "tests_that_move")
        need(c["status"] == "problem" and "prior_range_p1" in c["says"] and "decision_changes" in c["says"],
             f"an accepted end against the decision's verdict with decision_changes false: problem, naming the test and why ({c['status']}: {c['says']})")

    def v_burden_word():
        def high(d):
            d["references"][0].update(ratio=3.0)
            d["references"][0]["model"].update(point=3000.0)
        v = seen(edited(hm, doc_edit(fx.BV, high)))
        c = check(v, "burden_refs")
        need(c["status"] == "problem" and "ref_G1" in c["says"] and "within" in c["says"],
             f"a ratio outside its band recorded as within contradicts itself: problem ({c['status']}: {c['says']})")
        need(v["facts"]["n_burden_outside"] is None, "and the count of references outside is not given from words that contradict the numbers")

        def low(d):
            d["references"][0].update(status="outside")
        need(status(seen(edited(hm, doc_edit(fx.BV, low))), "burden_refs") == "problem", "nor a ratio inside its band recorded as outside")

    def reach(spec):       # two options priced per person reached, on one eligible population: B reaches all of it, C a tenth
        o = spec["options"]
        o["populations"]["elig"] = "pop"
        o["components"]["B"].update(eligible="elig", reach_share=1.0)
        o["components"]["C"].update(eligible="elig", reach_share=0.1)
        for i in o["cost_items"]:
            if i["component"] in ("B", "C"):
                i.update(basis="per_reached", value={"B": 2.0, "C": 3.0}[i["component"]])

    def all_on(package):
        def change(d):
            for u in d["plan"]:
                d["plan"][u] = d["per_unit"][u]["package"] = package
        return doc_edit(fx.POINT, change)

    def v_reach():
        def both(d):
            resign(reach)(d)
            all_on("A+B")(d)
        v = seen(edited(hm, both))
        c = check(v, "dearest_wins")
        need(c["status"] == "look" and close(v["facts"]["dearest_share"], 1.0) and re.search(r"option B\b", c["says"]),
             f"priced as the producer prices a unit (price x eligible population x reach share), B is the dearest and wins in every unit ({c['status']}: {c['says']})")
        has(row(v, "plan", "A+B"), units=12, cost_per_unit_rank=1)

        def no_share(d):
            resign(lambda spec: (reach(spec), spec["options"]["components"]["C"].pop("reach_share")))(d)
        c = check(seen(edited(hm, no_share)), "dearest_wins")
        need(c["status"] == "not_available" and "reach_share" in c["says"], f"a price per person reached with no reach share cannot be reproduced ({c['status']}: {c['says']})")
        c = check(seen(edited(hm, without("data/derived/units.csv"))), "dearest_wins")
        need(c["status"] == "not_available" and "units.csv" in c["says"], f"nor can any price without the units table ({c['status']}: {c['says']})")

    def v_truncated_plan():
        def cut(d):
            for u in d["per_unit"]:
                d["per_unit"][u]["package"] = "A+C"
            d["plan"] = {u: "A+C" for u in list(d["per_unit"])[:4]}
        v = seen(edited(hm, doc_edit(fx.POINT, cut)))
        c = check(v, "dearest_wins")
        need(c["status"] == "problem" and "12" in c["says"] and "4" in c["says"], f"a plan of 4 units beside 12 recorded units contradicts itself: problem ({c['status']}: {c['says']})")
        need(v["facts"]["n_packages_in_plan"] is None and v["facts"]["dearest_share"] is None and v["tables"]["plan"] is None, "and no plan is shown from it")
        c = check(seen(edited(hm, doc_edit(fx.POINT, lambda d: d["per_unit"][fx.UNITS[0]].update(package="A")))), "dearest_wins")
        need(c["status"] == "problem" and fx.UNITS[0] in c["says"], f"a unit whose package differs between plan and per_unit ({c['says']})")
        got = status(seen(folder(hm), edited(hm, doc_edit(fx.POINT, cut), other="second")), "run_shift")
        need(got == "not_available", f"a comparison run whose plan contradicts itself is not compared ({got})")

    def v_voi_draws():
        c = check(seen(edited(hm, doc_edit(fx.VOI, lambda d: d.update(draws=10)))), "voi_support")
        need(c["status"] == "problem" and "10" in c["says"] and str(fx.VOI_MIN_DRAWS) in c["says"],
             f"computed on fewer draws than its own recorded minimum: the record contradicts itself ({c['status']}: {c['says']})")
        c = check(seen(edited(hm, doc_edit(fx.VOI, lambda d: d.update(draws=fx.VOI_INFORMATIVE_DRAWS - 1)))), "voi_support")
        need(c["status"] == "problem", f"computed below informative_draws, where the producer writes indicative ({c['status']}: {c['says']})")
        c = check(seen(edited(hm, doc_edit(fx.VOI, lambda d: d.pop("min_draws")))), "voi_support")
        need(c["status"] == "not_available", f"with no recorded minimum the status word cannot be checked ({c['status']})")

    def v_fired():
        c = check(seen(edited(hm, tests_edit("DT1", share_pct=25.0))), "tests_that_move")
        need(c["status"] == "problem" and "DT1" in c["says"] and "25" in c["says"], f"no change recorded beside a share above its threshold: problem ({c['status']}: {c['says']})")
        got = status(seen(edited(hm, tests_edit("DT1", point_plan_feasible=False))), "tests_that_move")
        need(got == "problem", f"nor beside a point plan that is not feasible ({got})")

    def v_shapes():
        c = check(seen(edited(hm, header_edit(fx.PV, schema="analysis.plan_variants/2"))), "variant_budget")
        need(c["status"] == "not_available" and "analysis.plan_variants/2" in c["says"], f"a schema this view does not know is named, never read as the one it knows ({c['status']}: {c['says']})")
        v = seen(edited(hm, doc_edit(fx.PV, lambda d: d.update(variants=[None]))))
        c = check(v, "variant_budget")
        need(c["status"] == "not_available" and "variants[0]" in c["says"] and v["facts"]["n_variants"] is None and v["tables"]["variants"] is None,
             f"a row that is no mapping is named by its position; no row is dropped and nothing is counted ({c['status']}: {c['says']})")
        for rel, key, name in ((fx.BV, "references", "burden_refs"), (fx.VOI, "inputs", "voi_support"), (fx.EMB, "rows", "boundary_coverage")):
            got = status(seen(edited(hm, doc_edit(rel, lambda d, key=key: d[key].append("not a row")))), name)
            need(got == "not_available", f"{rel} {key}: a row that is no mapping makes {name} not_available ({got})")
            got = status(seen(edited(hm, header_edit(rel, schema="analysis.other/1"))), name)
            need(got == "not_available", f"{rel}: another schema makes {name} not_available ({got})")
        v = seen(edited(hm, header_edit(fx.POINT, schema=None)))
        need({status(v, n) for n in ("dearest_wins", "share_of_burden", "point_vs_draws")} == {"not_available"}, "a point file that names no schema is not read")
        got = status(seen(edited(post, header_edit(fx.EST, schema="analysis.estimates/9"), figures="estimates", kind="estimates")), "tests_that_move")
        need(got == "not_available", f"the estimates file too ({got})")

    # ------------------------------------------------------------------ each planted edit, across every view
    def planted(method, name):
        def run():
            spec = fx.DEFECTS[name]
            base = dict(figures="estimates", kind="estimates") if "estimates" in spec.get("kinds", ()) else {}
            clean = folder(method, **base)
            before = every(clean)
            if "against" in spec:
                v = seen(clean, folder(method, other=spec["against"]))
                after = {**before, **statuses(v)}
            else:
                after = every(folder(method, name, **base))
            want = {f"outcomes.{k}": s for k, s in {spec["check"]: spec["status"], **spec.get("also", {})}.items()}
            want = {k: s for k, s in {**want, **spec.get("across", {})}.items() if before.get(k) != s}
            got = {k: s for k, s in after.items() if before.get(k) != s}
            need(set(before) == set(after) and got == want, f"moves {got}, expected {want}")
        return run

    def others_edits():
        for method in fx.METHODS:
            before = every(folder(method))
            for name, spec in fx.DEFECTS.items():
                across = {k: s for k, s in spec.get("across", {}).items() if k.startswith("outcomes.")}
                if spec.get("view") == "outcomes" or not across or method not in spec["methods"]:
                    continue
                after = every(folder(method, name))
                got = {k: s for k, s in after.items() if k.startswith("outcomes.") and before.get(k) != s}
                need(got == across, f"{method}, {name}: moves {got}, expected {across}")

    # ------------------------------------------------------------------ fail closed
    def f_missing():
        d = os.path.join(b.tmp, "empty")
        os.makedirs(d)
        write_nofollow(d, "run_state.json", b"{}")
        got = {k.split(".")[-1]: s for k, s in statuses(seen(d)).items()}
        need(got == {n: "not_available" for n in CHECKS}, f"a run with none of the files: every check not_available ({got})")
        for stage in ("launch", "sign-off", "calibration"):
            got = set(statuses(seen(folder(hm, upto=stage))).values())
            need(got == {"not_available"}, f"upto {stage}: nothing to judge yet ({got})")
        needs = {fx.POINT: ("dearest_wins", "share_of_burden", "point_vs_draws"), fx.UNC: ("point_vs_draws", "fixed_inputs"),
                 fx.DT: ("tests_that_move", "infeasible_tests", "fixed_inputs"), fx.VOI: ("voi_support",), fx.BV: ("burden_refs",),
                 fx.EMB: ("boundary_coverage",), fx.STAB: ("stability_agreement",), fx.PV: ("variant_budget",)}
        for rel, names in needs.items():
            v = seen(edited(hm, without(rel)))
            got = {n: status(v, n) for n in names}
            need(set(got.values()) == {"not_available"} and all(rel in check(v, n)["says"] + str(check(v, n)["evidence"]) for n in names),
                 f"without {rel}: {names} are not_available and name the file ({got})")
        v = seen(edited(hm, lambda d: write_nofollow(d, fx.POINT, b"data: [1, 2\n")))
        need({status(v, n) for n in needs[fx.POINT]} == {"not_available"}, "a point file that is not YAML is not_available")
        v = seen(folder(hm, "tests_unparsable"))
        need(v["facts"]["n_tests"] is None and v["tables"]["tests"] is None, "a tests file that cannot be parsed: the count is null, never zero, and there is no table")
        v = seen(folder(hm, "unknown_deliverable_kind"))
        need(all("ranking" in c["says"] for c in v["checks"] if c["id"] != "outcomes.run_shift") and v["facts"]["deliverable_kind"] == "ranking",
             "an unknown deliverable kind is named in every check, never guessed at")

    def f_spec():
        def unsigned(d):
            with open(os.path.join(d, fx.SPEC_FILES[1]), "a", encoding="utf-8") as f:
                f.write("note: edited after the sign-off\n")
        v = seen(edited(hm, unsigned, "C8_dearest_wins"))
        need(status(v, "dearest_wins") not in ("ok", "look"), f"with no verified signed spec there are no signed costs to rank by ({status(v, 'dearest_wins')})")
        need(status(v, "variant_budget") not in ("ok", "look") and status(v, "fixed_inputs") not in ("ok", "look"), "nor an allowance or a list of tests to read")
        v = seen(edited(hm, unsigned, "burden_report_only_tested"))
        need(status(v, "burden_refs") == "problem", f"a test counts only for the verified signed spec: with none, a report-only reference outside its band is a problem ({status(v, 'burden_refs')})")

        def other_spec(d):
            p = os.path.join(d, fx.DT)
            with open(p, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            doc["spec_sha256"] = "0" * 64
            write_nofollow(d, fx.DT, yaml.safe_dump(doc, sort_keys=False).encode())
        got = status(seen(edited(hm, other_spec, "burden_report_only_tested")), "burden_refs")
        need(got == "problem", f"a tests file that records another spec excuses nothing (§3) ({got})")

        def disk(d):
            p = os.path.join(d, "decision_spec.yaml")
            with open(p, encoding="utf-8") as f:
                spec = yaml.safe_load(f)
            spec["options"]["cost_items"][2]["value"] = 0.5
            write_nofollow(d, "decision_spec.yaml", yaml.safe_dump(spec, sort_keys=False).encode())
        v = seen(edited(hm, disk, "C8_dearest_wins"))
        need(status(v, "dearest_wins") == "look" and row(v, "plan", "A+C")["cost_per_unit_rank"] == 1, "costs are the signed copy's, whatever the spec on disk says (§3)")

    def f_words():
        words = fx.domain_words()
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            text = f.read().lower()
        need(len(words) > 50 and not [w for w in words if w in text], f"no domain word in this module ({[w for w in words if w in text][:5]})")
        for d in (folder(hm), folder(post, figures="estimates", kind="estimates")):
            out = json.dumps(seen(d)).lower()
            need(not [w for w in words if w in out], "nor in what the view returns for a fixture run: every name comes from the run")
        v = seen(folder(hm))
        need(fx.GROUPING not in json.dumps(v["tables"]["burden"]["columns"]) and row(v, "burden", "ref_G1")["group"] == "G1",
             "the grouping column's own name is never a column of the model; its values are read from the run")

    case("M1 the generic model: the five parts, the facts of §5.7, twelve checks with sentence, evidence and case, every table present", m_shape)
    case("M2 on both clean runs every check is ok (run_shift not_available) and no view raises anything", m_clean)
    case("M3 facts on both clean runs, each from the file that holds it", m_facts)
    case("M4 tables: plan with cost rank, components, tests, value of information, variants, burden, stability, emulator", m_tables)
    case("M5 charts name tables and columns that exist, with the headline, plan, tests, value of information, variants and burden drawn", m_charts)
    case("E1 the estimates kind: the same facts; outputs, decision and tests filled; package tables null; one note says so", e_model)
    case("E2 the estimates kind's tests map to the same verdicts through the loader's rules", e_tests)
    case("S1 point_vs_draws on what is stored: outside the interval problem; more than 5% from the median look, with both", s_point)
    case("S2 dearest_wins is look, never more, needs the producer's minimum of units; rank 1 is the dearest option", s_dearest)
    case("S3 moved_pct, near misses, the register row a test is linked to, and tests that did not run", s_tests)
    case("S4 stability_verdict is the plan's; burden_refs is look only for report-only references named by a test that ran", s_stability_burden)
    case("S5 tables.shift and tables.spec_diff against a comparison run", s_shift)
    case("S6 run_shift: not_available with no comparison, across questions and with an unverified spec; problem with no spec key", s_run_shift)
    case("S7 the four added checks, boundary coverage, share of burden, and n_units_stable", s_new_checks)
    case("S8 voi_support: too few draws, nothing above its floor, and a prior-dominated input that carries value (C14)", s_voi)
    case("V1 review 1: decision tests whose file records another spec, or none, are never passed or counted", v_other_spec)
    case("V2 review 2: an estimates test ran only with an accepted end and a verdict; ends that contradict the recorded flag are a problem", v_estimates_verdict)
    case("V3 review 3: a reference's recorded word against its own ratio and band (toolkit/analysis/burden.py verdict)", v_burden_word)
    case("V4 review 4: an option is priced per unit as the producer prices it: basis, eligible population, reach share", v_reach)
    case("V5 review 5: a plan that disagrees with the recorded units and their packages is a problem, not a short plan", v_truncated_plan)
    case("V6 review 6: the value of information's status word against its own draws and recorded minimum", v_voi_draws)
    case("V7 review 7: a verdict of no change beside a criterion that fired is a problem", v_fired)
    case("V8 review 8: an unknown schema or a row that is no mapping is not_available, naming it", v_shapes)
    n = 0
    for method in fx.METHODS:
        for name in fx.OUTCOMES_DEFECTS:
            spec = fx.DEFECTS[name]
            if method not in spec["methods"]:
                continue
            n += 1
            also = "".join(f", {k} {s}" for k, s in {**spec.get("also", {}), **spec.get("across", {})}.items())
            case(f"P{n} {method}, {name}: {spec['check']} is {spec['status']}{also}, and no other check of any view changes", planted(method, name))
    case("Q1 the other views' edits that touch the decision tests or the prior-dominated list move the Outcomes checks they name, and no other", others_edits)
    case("F1 fail closed: missing files, a file that is not YAML, stages before the analysis, an unknown kind", f_missing)
    case("F2 fail closed: only the verified signed spec gives costs, allowances and tests that count", f_spec)
    case("F3 no domain word in this module or in what it returns; names come from the run", f_words)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: outcomes.py --self-test")
