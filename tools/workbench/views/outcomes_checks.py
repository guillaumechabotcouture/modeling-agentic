#!/usr/bin/env python3
"""The Outcomes checks (docs/design/review-workbench.md §5.7), each on the generic model an adapter fills
(views/outcomes_package.py, views/outcomes_estimates.py). The check against another run is views/outcomes_compare.py.

    python3 tools/workbench/views/outcomes_checks.py --self-test

`run_all(m)` returns the eleven checks of one run in the order of CHECKS (run_shift is added by the view). A check
never answers ok or look on something it could not read: a missing file, a word no producer writes, a kind that does
not record the thing, or no verified signed spec where the rule rests on one are `not_available` with the reason.

Thresholds (§3: a run's own records first; the workbench's own are labelled in the sentence):
 from the run    each test's three thresholds (analysis/decision_tests.yaml); the value of information's min_draws,
                 informative_draws and noise floors; each reference's band; the budget allowance of the verified signed
                 spec (uncertainty.affordability.max_share_over_budget).
 from code       dearest_wins: SHARE and MIN_UNITS of toolkit/analysis/red_flag.py.
 workbench       share_of_burden 50% of the comparator; point_vs_draws 5% of the median; a near miss is within 25% of
                 a threshold that was not passed (dearest_wins, the tests, variant_budget, share_of_burden,
                 point_vs_draws).
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
from views import outcomes_package as K  # noqa: E402
from views import outcomes_read as R  # noqa: E402
from views.outcomes_read import g, near, num, pct, plural, r3, some  # noqa: E402

# check -> scorecard case (§8)
CHECKS = {"dearest_wins": "C8", "share_of_burden": "C7", "point_vs_draws": None, "tests_that_move": None,
          "burden_refs": None, "voi_support": "C14", "boundary_coverage": None, "run_shift": "C6",
          "stability_agreement": None, "infeasible_tests": None, "variant_budget": None, "fixed_inputs": None}
TITLES = {"dearest_wins": "The most expensive option of a group chosen in nearly every unit",
          "share_of_burden": "The headline as a share of the comparator's burden",
          "point_vs_draws": "The point value against the median and interval of the draws",
          "tests_that_move": "Decision tests that change the plan, near misses and tests that did not run",
          "burden_refs": "The model's burden against external references",
          "voi_support": "Value of information: inputs above their noise floor, and whether the data constrain them",
          "boundary_coverage": "Components covered by the emulator's boundary check",
          "run_shift": "Shift in the plan or the headline against another run of the same question",
          "stability_agreement": "The plan's stability verdict against the value's",
          "infeasible_tests": "Decision tests under which the point plan cannot be afforded or applied",
          "variant_budget": "Plan variants over budget in more draws than the signed spec allows",
          "fixed_inputs": "Inputs held at one value that change the plan when a test varies them"}
DEAREST_SHARE, DEAREST_MIN_UNITS, RED_FLAG = 0.9, 5, "toolkit/analysis/red_flag.py"      # the producer's SHARE and MIN_UNITS
BURDEN_SHARE, POINT_GAP = 0.5, 0.05                                                       # workbench thresholds (§5.7)
WB = "workbench threshold"


def _r(name, status, says, evidence, rows=None):
    return checks.result(f"outcomes.{name}", "outcomes", TITLES[name], status, says, evidence, rows, CHECKS[name])


def _na(name, why, evidence):
    return _r(name, "not_available", f"Not available: {why}.", evidence)


def _file(m, name, rel, field):
    """The not_available answer for a part whose file could not be read."""
    return _na(name, m["why"].get(rel) or f"{rel} does not hold {field}", [(rel, field)])


def _unsigned(m, name, what, evidence):
    return _na(name, f"no signed spec could be verified ({m['spec']['why']}), so there is no signed {what} to read", evidence)


def _table(columns, rows):
    return {"columns": columns, "rows": rows} if rows else None


def check_dearest_wins(m):
    ev = [(R.SIGNED, "options.components[].group, .eligible, .reach_share; options.cost_items; options.populations; options.prices"),
          (R.SIGNED, "decision_units.csv, decision_units.id (the units table)"), (R.POINT, "data.plan, data.per_unit, data.n_units"),
          (RED_FLAG, "SHARE, MIN_UNITS, flags()"), ("toolkit/alloc/costs.py", "item_cost()")]
    if m["plan_clash"]:
        return _r("dearest_wins", "problem", f"The point file's plan contradicts its own record of the units: {m['plan_clash']}. No share is counted "
                                             "from a plan that does not agree with the units recorded beside it.", ev)
    if m["plan"] is None:
        return _file(m, "dearest_wins", R.POINT, "data.plan")
    if not m["spec"]["verified"]:
        return _unsigned(m, "dearest_wins", "cost to rank the options by", ev)
    if m["dearest"] is None:
        unknown = sorted(p for p in set(m["plan"].values()) if K.parts(p, m["options"]) is None)
        return _na("dearest_wins", f"the plan names {some(unknown)}, which the verified signed spec does not define as a package of its "
                                   "components, so the plan cannot be priced" if m["options"] else "the verified signed spec lists no components (options.components)", ev)
    groups = m["options"]["groups"]
    left = [f"group {name}: {grp['why']}" for name, grp in sorted(groups.items()) if grp["why"]]
    rule = (f"the producer flags a share of {pct(DEAREST_SHARE)} or more in at least {DEAREST_MIN_UNITS} units ({RED_FLAG} SHARE, MIN_UNITS); "
            "an option's cost in a unit is computed as the producer computes it, from the verified signed spec's cost items that do not depend "
            "on the model and the units table")
    if left:
        return _na("dearest_wins", "the cost of an option in a unit cannot be reproduced from the recorded inputs, and none is guessed (" + "; ".join(left) + ")", ev)
    rows = _table(["group", "dearest_option", "units_holding_the_group", "units_with_the_dearest", "share"],
                  [[d["group"], d["option"], d["units"], d["hits"], d["share"]] for d in m["dearest"]])
    if not m["dearest"]:
        return _r("dearest_wins", "ok", "No group of options to judge: no group has two or more options whose costs differ in a unit whose plan holds one "
                                        f"({rule}).", ev)
    said = "; ".join(f"group {d['group']}: the dearest option, most often option {d['option']}, is the one chosen in {d['hits']} of the {d['units']} units whose plan "
                     f"holds the group ({pct(d['share'])})" for d in m["dearest"])
    enough = [d for d in m["dearest"] if d["units"] >= DEAREST_MIN_UNITS]
    if any(d["share"] >= DEAREST_SHARE for d in enough):
        return _r("dearest_wins", "look", f"The dearest option wins nearly everywhere: {said}; {rule}. Either its extra effect is real and large "
                                          "everywhere or the engine overstates it; for you to judge (a flag, never a finding).", ev, rows)
    if any(near(d["share"], DEAREST_SHARE) for d in enough):
        return _r("dearest_wins", "look", f"Near miss (within 25% of the share, a {WB}): {said}; {rule}.", ev, rows)
    return _r("dearest_wins", "ok", f"No dearest option wins nearly everywhere: {said}; {rule}.", ev, rows)


def check_share_of_burden(m):
    h, ev = m["headline"], [(R.POINT, f"data.averted.{m['outcome']}, data.comparator_total.{m['outcome']}")]
    if m["point"] is None or h["share"] is None:
        return _file(m, "share_of_burden", R.POINT, f"the point value and the comparator total of {m['outcome']}")
    said = f"The plan averts {g(h['point'])} of the comparator's {g(h['comparator_total'])} {m['outcome']} ({pct(h['share'])})"
    if h["share"] > BURDEN_SHARE:
        return _r("share_of_burden", "look", f"{said}, above {pct(BURDEN_SHARE)} ({WB}): is a share this large plausible for these packages?", ev)
    if near(h["share"], BURDEN_SHARE):
        return _r("share_of_burden", "look", f"{said}, a near miss: within 25% of the {pct(BURDEN_SHARE)} above which it is raised ({WB}s).", ev)
    return _r("share_of_burden", "ok", f"{said}, not above {pct(BURDEN_SHARE)} ({WB}).", ev)


def check_point_vs_draws(m):
    h, key = m["headline"], m["outcome"]
    ev = [(R.POINT, f"data.averted.{key}"), (R.UNC, f"data.fixed_plan.{key}.median, .ci_low, .ci_high")]
    if m["point"] is None or h["point"] is None:
        return _file(m, "point_vs_draws", R.POINT, f"a point value for {key}")
    if None in (h["median"], h["low"], h["high"]):
        return _na("point_vs_draws", m["why"].get(R.UNC) or f"{R.UNC} holds no median or no interval end for {key} (data.fixed_plan), so the point "
                                                                "value cannot be placed among the draws", ev)
    span = f"the {pct(h['level']) if h['level'] else ''} interval of the draws ({g(h['low'])} to {g(h['high'])})".replace("the  interval", "the interval")
    if h["low"] > h["high"]:
        return _r("point_vs_draws", "problem", f"The interval of the draws has its ends reversed: its lower end {g(h['low'])} is above its upper end "
                                               f"{g(h['high'])}, so the record contradicts itself.", ev)
    if not h["low"] <= h["point"] <= h["high"]:
        return _r("point_vs_draws", "problem", f"The point value {g(h['point'])} lies outside {span}: the headline the report quotes is not among "
                                               "the values the uncertainty analysis found plausible.", ev)
    gap = abs(h["point"] - h["median"]) / abs(h["median"]) if h["median"] else None
    said = f"The point value {g(h['point'])} is inside {span} and differs from the median {g(h['median'])} by {pct(gap)} of the median"
    more = " No run stores per-draw values or quartiles (gap H17), so the point cannot be placed more finely."
    if gap is None or gap > POINT_GAP:
        return _r("point_vs_draws", "look", f"{said}, more than {pct(POINT_GAP)} ({WB}): the point analysis and the draws do not centre on the same value.{more}", ev)
    if near(gap, POINT_GAP):
        return _r("point_vs_draws", "look", f"{said}, a near miss: within 25% of the {pct(POINT_GAP)} above which it is raised ({WB}s).{more}", ev)
    return _r("point_vs_draws", "ok", f"{said}, within {pct(POINT_GAP)} ({WB}).", ev)


def _tests_file(m):
    return R.EST if m["kind"] == "estimates" else R.DT


def _linked(rows):
    """Tests by their link: those linked to a register row with the row, then those linked to none."""
    tied, loose = [f"{r['id']} (register row {r['issue']})" for r in rows if r["issue"]], [r["id"] for r in rows if not r["issue"]]
    return " ".join(x for x in (f"linked to a register row: {some(tied, 12)};" if tied else "",
                                f"linked to no register row: {some(loose, 16)}" if loose else "") if x).rstrip(";")


def _tied(m, name, ev):
    """The answer of a check that reads the decision tests when their file does not record the verified signed spec
    (§3: a decision test counts only when its file records the same signed spec), or None when it does. The word and
    the sentence are the shared loader's (checks.decision_tests, checks.estimates_tests), never judged again here.
    Another spec's tests beside the signed spec are two files that contradict each other: `problem`, said once, by
    tests_that_move; no recorded hash, or no verified signed spec, is `not_available`."""
    word, says = m["tie"] or ("same", None)
    if word == "same":
        return None
    if word == "other" and name == "tests_that_move":
        return _r(name, "problem", f"The {len(m['tests'])} decision tests are not this spec's: {says}. None of them is counted or passed here.", ev)
    return _na(name, f"{says}, so no decision test is counted or passed here", ev)


def check_tests_that_move(m):
    rel = _tests_file(m)
    ev = [(rel, "data.decision_tests[].status, .decision_changes" if m["kind"] == "estimates" else "data.tests[].status, .verdict and the shares beside their thresholds"),
          ("the science register (views/register_rows.py)", "rows[].tests")]
    t = m["tests"]
    if t is None:
        return _file(m, "tests_that_move", rel, "a list of decision tests")
    if _tied(m, "tests_that_move", ev):
        return _tied(m, "tests_that_move", ev)
    table = _table(R.TEST_COLUMNS, [[r[c] for c in R.TEST_COLUMNS] for r in t if r["state"] != "ran" or r["verdict"] == "CHANGES_PLAN" or r["near_miss"]])
    clash = [r for r in t if r["state"] == "contradiction"]
    if clash:
        return _r("tests_that_move", "problem", f"{len(clash)} decision test(s) contradict their own record: "
                  + "; ".join(f"{r['id']} ({r['clash'] or f'status {r['status']} with verdict {r['verdict']}, a pair the analysis does not write'})" for r in clash[:R.SHOWN])
                  + (f" and {len(clash) - R.SHOWN} more" if len(clash) > R.SHOWN else "") + ". Whether they ran, or what they found, cannot be said.", ev, table)
    odd = [f"{r['id']} (status {r['status']!r}, verdict {r['verdict']!r})" for r in t if r["state"] == "unknown"]
    if odd:
        return _na("tests_that_move", f"{len(odd)} decision test(s) hold a status or verdict the producer does not write, or ran with no verdict: {some(odd)}", ev)
    moving, close = [r for r in t if r["verdict"] == "CHANGES_PLAN"], [r for r in t if r["near_miss"]]
    idle = [r for r in t if r["state"] == "not_run" and not r["excused"]]
    excused = [r["id"] for r in t if r["excused"]]
    what = "decision" if m["kind"] == "estimates" else "plan"
    listed = (f" Listed, and not raised: {some(excused)} did not run and is the producer's own scenario for an input the verified signed spec "
              "excludes from the draws.") if excused else ""
    if not (moving or close or idle):
        return _r("tests_that_move", "ok", f"None of the {len(t)} decision tests changes the {what}, none is a near miss and none that should have run "
                                           f"did not.{listed}", ev, table)
    said = []
    if moving:
        said.append(f"{len(moving)} of {len(t)} decision tests change the {what}, {_linked(moving)}")
    if close:
        said.append(f"{plural(len(close), 'near miss', 'near misses')} (no change, with a share, regret or budget moved within 25% of its own "
                    f"threshold; {WB}), {_linked(close)}")
    if idle:
        said.append(f"{plural(len(idle), 'test')} did not run, so nothing is known under {'it' if len(idle) == 1 else 'them'}: {some((r['id'] for r in idle), 12)}")
    return _r("tests_that_move", "look", ". ".join(x[0].upper() + x[1:] for x in said) + f". For you to judge whether each is carried by an issue in the register.{listed}"
              + (f" The register link could not be derived ({m['link_why']})." if m.get("link_why") else ""), ev, table)


def check_burden_refs(m):
    ev = [(R.BV, "data.references[].status, .role, .ratio, .tolerance.ratio_band"), (R.SIGNED, "analysis.decision_tests[].burden_scale.references"),
          (R.DT, "data.tests[].status, .verdict")]
    b = m["burden"]
    if b is None:
        return _file(m, "burden_refs", R.BV, "a list of references")
    clash = [r for r in b if r["clash"]]
    if clash:
        return _r("burden_refs", "problem", f"{len(clash)} of {len(b)} external references contradict their own numbers: "
                  + "; ".join(f"{r['id']} ({r['clash']})" for r in clash[:R.SHOWN]) + ". The producer judges a ratio band on the model's point value over "
                  "the reference's value (toolkit/analysis/burden.py verdict).", ev)
    odd = [f"{r['id']} ({r['state']!r}, role {r['role']!r})" for r in b if r["state"] not in K.BURDEN_STATUSES or r["role"] not in K.BURDEN_ROLES]
    if odd:
        return _na("burden_refs", f"{len(odd)} reference(s) hold a status or role the producer does not write: {some(odd)}", ev)
    tests = {r["id"]: r for r in m["tests"] or []}

    def covered(r):       # report-only, and named by a decision test that ran for the verified signed spec
        return r["report_only"] and any(tests.get(t, {}).get("counts") for t in r["tests"])
    outside = [r for r in b if r["state"] == "outside"]
    table = _table(["id", "group", "outcome", "ratio", "band_low", "band_high", "state", "role", "tests"],
                   [[r["id"], r["group"], r["outcome"], r["ratio"], r["band_low"], r["band_high"], r["state"], r["role"], ", ".join(r["tests"])] for r in b])
    bare = [r for r in outside if not covered(r)]
    if bare:
        why = "; ".join(f"{r['id']} (ratio {r3(r['ratio'])} against {g(r['band_low'])} to {g(r['band_high'])}, {r['role'].replace('_', '-')}"
                        + (", named by no decision test" if not r["tests"] else f", its test {', '.join(r['tests'])} did not run for the verified signed spec"
                           if r["report_only"] else "") + ")" for r in bare[:R.SHOWN])
        return _r("burden_refs", "problem", f"{len(outside)} of {len(b)} external references are outside their band, and {len(bare)} of them "
                                            f"with nothing that covers it: {why}. Only a report-only reference named by a decision test that ran is for you to judge.", ev, table)
    if outside:
        why = "; ".join(f"{r['id']} (ratio {r3(r['ratio'])} against {g(r['band_low'])} to {g(r['band_high'])}; "
                        + ", ".join(f"{t} {tests[t]['verdict']}" for t in r["tests"] if tests.get(t, {}).get("counts")) + ")" for r in outside[:R.SHOWN])
        return _r("burden_refs", "look", f"{len(outside)} of {len(b)} external references are outside their band; each is report-only and named by a "
                                         f"decision test that ran: {why}. For you to judge whether the model's burden level can carry the decision.", ev, table)
    lost = [r["id"] for r in b if r["state"] == "unmatched" and r["role"] == "checked"]
    if lost:
        return _na("burden_refs", f"{len(lost)} checked reference(s) could not be matched to the model ({some(lost)}), so they were not compared", ev)
    edge = [f"{r['id']} ({r3(r['ratio'])} in {g(r['band_low'])} to {g(r['band_high'])})" for r in b if r["state"] == "within" and r["ratio"] is not None
            and ((r["band_high"] is not None and near(r["ratio"], r["band_high"]))
                 or (r["band_low"] is not None and r["band_low"] <= r["ratio"] <= (1 + R.NEAR) * r["band_low"]))]
    if edge:
        return _r("burden_refs", "look", f"No reference is outside its band, and {len(edge)} of {len(b)} are a near miss, within 25% of an end of the "
                                         f"band ({WB}): {some(edge)}.", ev, table)
    return _r("burden_refs", "ok", f"None of the {len(b)} external references is outside its band.", ev, table)


def check_voi_support(m):
    ev = [(R.VOI, "data.status, data.draws, data.informative_draws, data.inputs[].above_noise_floor"), (R.UNC, "data.prior_dominated")]
    v = m["voi"]
    if v is None:
        return _file(m, "voi_support", R.VOI, "a value of information")
    if v["status"] not in K.VOI_STATUSES:
        return _na("voi_support", f"{R.VOI} has the status {v['status']!r}, which the producer does not write", ev)
    if v["status"] != "no_eligible_plan":
        # The producer's rule for the word, recomputed from the recorded counts (toolkit/analysis/value_of_information.py:
        # `if n < th["min_draws"]: return dict(out, status="too_few_draws")`, then
        # `status="computed" if n >= th["informative_draws"] else "indicative"`).
        n, least, enough = v["draws"], v["min_draws"], v["informative_draws"]
        if n is None or least is None or (enough is None and n >= least):
            return _na("voi_support", f"{R.VOI} records the status {v['status']} without the draws, min_draws or informative_draws it follows from, so the word cannot be checked", ev)
        word = "too_few_draws" if n < least else "computed" if n >= enough else "indicative"
        if word != v["status"]:
            return _r("voi_support", "problem", f"The value of information contradicts its own record: its status is {v['status']}, and {g(n)} draws against a "
                                                f"minimum of {g(least)} and {g(enough)} for an informative result is {word} by the producer's rule. Nothing is read from it.", ev)
    if v["status"] in ("too_few_draws", "no_eligible_plan"):
        return _na("voi_support", f"the value of information was not computed (status {v['status']}: {g(v['draws'])} draws against the method's "
                                  f"minimum of {g(v['min_draws'])}), so nothing can be said of which inputs carry value", ev)
    if m["dominated"] is None:
        return _na("voi_support", f"{m['why'].get(R.UNC) or R.UNC + ' could not be read'}, so which inputs the data do not constrain is not known", ev)
    above = [r for r in v["rows"] if r["above_noise"]]
    carried = [r["input"] for r in above if r["prior_dominated"]]
    table = _table(["input", "kind", "evppi", "noise_floor", "above_noise", "prior_dominated"],
                   [[r["input"], r["kind"], r["evppi"], r["noise_floor"], r["above_noise"], r["prior_dominated"]] for r in above])
    said = []
    if v["status"] == "indicative":
        said.append(f"the analysis calls its result indicative: {g(v['draws'])} draws against the {g(v['informative_draws'])} it needs to be "
                    "informative, so every value is a rough size only")
    if not above:
        said.append(f"none of the {len(v['rows'])} inputs is above its noise floor, so no input's value can be told from noise")
    if carried:
        said.append(f"{len(carried)} of them {'is' if len(carried) == 1 else 'are'} prior-dominated, so value rests on an input the data do "
                    f"not constrain: {some(carried)}")
    head = f"{len(above)} of {len(v['rows'])} inputs are above their noise floor ({some(r['input'] for r in above)})" if above else "Value of information"
    if said:
        return _r("voi_support", "look", f"{head}; " + "; ".join(said) + ".", ev, table)
    return _r("voi_support", "ok", f"{head}, none of them prior-dominated, on {g(v['draws'])} draws (status {v['status']}).", ev, table)


def check_boundary_coverage(m):
    ev = [(R.EMB, "data.rows[].chosen, .runner_up"), (R.SIGNED, "options.components")]
    b = m["boundary"]
    if b is None:
        return _file(m, "boundary_coverage", R.EMB, "the boundary check's rows")
    if not m["spec"]["verified"] or m["options"] is None:
        return _unsigned(m, "boundary_coverage", "list of components", ev)
    held = [K.parts(p, m["options"]) for p in set(b["packages"])]
    if any(h is None for h in held):
        return _na("boundary_coverage", f"a row of {R.EMB} names a package the verified signed spec does not define", ev)
    seen = {c for h in held for c in h}
    names = m["options"]["components"]
    out = [c for c in names if c not in seen]
    tail = (f" The check ran the engine on {b['units']} units and matched the emulator's order in {pct(b['share_matched'])} of them"
            f" (the method's minimum {pct(b['threshold'])}).") if b["share_matched"] is not None else ""
    if out:
        return _r("boundary_coverage", "look", f"{len(out)} of the {len(names)} components are in no row of the boundary check, as the chosen "
                                               f"package or as the runner-up: {some(out, 12)}. The emulator's ranking of packages that hold them was "
                                               f"not compared with the engine.{tail}", ev)
    return _r("boundary_coverage", "ok", f"Each of the {len(names)} components is in a row of the boundary check, as the chosen package or the runner-up.{tail}", ev)


def check_stability_agreement(m):
    ev = [(R.STAB, "data.verdict, data.value_verdict")]
    s = m["stability"]
    if s is None:
        return _file(m, "stability_agreement", R.STAB, "the two verdicts")
    plan, value = s["verdict"], s["value_verdict"]
    odd = [repr(x) for x in (plan, value) if x not in K.STABILITY_VERDICTS]
    if odd:
        return _na("stability_agreement", f"{R.STAB} holds the verdict {' and '.join(odd)}, which the producer does not write", ev)
    if "NOT_RUN" in (plan, value):
        return _na("stability_agreement", f"the stability analysis did not run (plan verdict {plan}, value verdict {value}"
                                          + (f": {s['reason']}" if s["reason"] else "") + "), so there is nothing to compare", ev)
    if plan != value:
        return _r("stability_agreement", "look", f"The two stability verdicts disagree: {plan} for the plan (which package each unit keeps when a group "
                                                 f"is left out) and {value} for the value (the worst fold's loss, {s['headline_metric']}). A plan that "
                                                 "moves while the value holds means near-equal packages; for you to judge which verdict the report should carry.", ev)
    return _r("stability_agreement", "ok", f"The plan's stability verdict and the value's agree: {plan}.", ev)


def check_infeasible_tests(m):
    ev = [(R.DT, "data.tests[].point_plan_feasible")]
    t = m["tests"]
    if t is None:
        return _file(m, "infeasible_tests", R.DT, "a list of decision tests")
    if _tied(m, "infeasible_tests", ev):
        return _tied(m, "infeasible_tests", ev)
    known = [r for r in t if r["feasible"] is not None]
    if not known:
        return _na("infeasible_tests", f"no test of {R.DT} records whether the point plan stays feasible (point_plan_feasible), so it was not computed", ev)
    bad = [r for r in known if r["feasible"] is False]
    if bad:
        return _r("infeasible_tests", "look", f"Under {len(bad)} of {len(known)} decision tests the point plan cannot be afforded or applied: "
                                              f"{some((r['id'] for r in bad), 12)}. The producer counts each as changing the plan; what was varied is in the tests table.",
                  ev, _table(["id", "varied", "verdict"], [[r["id"], r["varied"], r["verdict"]] for r in bad]))
    return _r("infeasible_tests", "ok", f"The point plan stays feasible under each of the {len(known)} decision tests that record it.", ev)


def check_variant_budget(m):
    ev = [(R.PV, "data.variants[].uncertainty.affordability.share_over_budget"), (R.SIGNED, "uncertainty.affordability.max_share_over_budget")]
    v = m["variants"]
    if v is None:
        return _file(m, "variant_budget", R.PV, "a list of variants")
    if not m["spec"]["verified"]:
        return _unsigned(m, "variant_budget", "budget allowance", ev)
    if m["allowance"] is None:
        return _na("variant_budget", "the verified signed spec sets no budget allowance (uncertainty.affordability.max_share_over_budget), so there is nothing to compare with", ev)
    odd = [f"{r['id']} ({r['status']!r})" for r in v if r["status"] not in K.VARIANT_STATUSES]
    if odd:
        return _na("variant_budget", f"{some(odd)} hold a status the producer does not write", ev)
    allow = f"the {pct(m['allowance'])} of draws the verified signed spec allows"
    table = _table(["id", "status", "cost", "over_budget_share"], [[r["id"], r["status"], r["cost"], r["over_budget_share"]] for r in v])
    over = [r for r in v if r["over_budget_share"] is not None and r["over_budget_share"] > m["allowance"]]
    blank = [r["id"] for r in v if r["cost"] is None or r["over_budget_share"] is None]
    tail = f" Not judged: {some(blank)} has no cost or no draws." if blank else ""
    if over:
        return _r("variant_budget", "look", f"{len(over)} of {len(v)} plan variants cost more than the allocatable budget in more draws than {allow}: "
                                            + "; ".join(f"{r['id']} in {pct(r['over_budget_share'])} of draws" for r in over) + f".{tail}", ev, table)
    if blank:
        return _na("variant_budget", f"{some(blank)} has no cost or no draws (status {some(sorted({str(r['status']) for r in v if r['id'] in blank}))}), so it "
                                     "cannot be held against the allowance", ev)
    close = [r for r in v if near(r["over_budget_share"], m["allowance"])]
    if close:
        return _r("variant_budget", "look", "Near miss: " + "; ".join(f"{r['id']} is over budget in {pct(r['over_budget_share'])} of draws" for r in close)
                  + f", within 25% of {allow} ({WB}).", ev, table)
    return _r("variant_budget", "ok", f"None of the {len(v)} plan variants is over budget in more draws than {allow}.", ev, table)


def check_fixed_inputs(m):
    ev = [(R.UNC, "data.interval.spec_fixed, data.interval.fixed, data.draw_plan.fixed[].name"), (R.SIGNED, "analysis.decision_tests[].parameter"),
          (R.DT, "data.tests[].verdict")]
    if m["fixed"] is None:
        return _file(m, "fixed_inputs", R.UNC, "the inputs held at one value")
    if m["tests"] is None:
        return _file(m, "fixed_inputs", R.DT, "a list of decision tests")
    if not m["spec"]["verified"]:
        return _unsigned(m, "fixed_inputs", "list of tests to say which input each varies", ev)
    if _tied(m, "fixed_inputs", ev):
        return _tied(m, "fixed_inputs", ev)
    varied = [r for r in m["tests"] if r["parameter"] in m["fixed"]]
    odd = [r["id"] for r in varied if r["state"] in ("contradiction", "unknown")]
    if odd:
        return _na("fixed_inputs", f"{some(odd)} varies an input held at one value and its status and verdict cannot be read", ev)
    moving = [r for r in varied if r["state"] == "ran" and r["verdict"] == "CHANGES_PLAN"]
    table = _table(["input", "test", "verdict"], [[r["parameter"], r["id"], r["verdict"]] for r in varied])
    head = f"{plural(len(m['fixed']), 'input')} held at one value in the draws; {plural(len(varied), 'decision test varies', 'decision tests vary')} one of them"
    if moving:
        return _r("fixed_inputs", "look", f"{head}; {len(moving)} of those change the plan: "
                                          + "; ".join(f"{r['parameter']} (test {r['id']})" for r in moving)
                                          + ". The interval does not carry these inputs, so the headline's interval is narrower than what is known.", ev, table)
    return _r("fixed_inputs", "ok", f"{head}; none of those changes the plan.", ev, table)


RULES = (check_dearest_wins, check_share_of_burden, check_point_vs_draws, check_tests_that_move, check_burden_refs, check_voi_support,
         check_boundary_coverage, check_stability_agreement, check_infeasible_tests, check_variant_budget, check_fixed_inputs)
ESTIMATES = ("tests_that_move",)        # what the estimates kind has something for


def unknown(kind_says, names=None):
    """Every check not_available with one reason (no signed spec to read a kind from, or a kind the view does not know)."""
    return [_na(n, kind_says, [(R.SIGNED, "deliverable.kind")]) for n in (names or [k for k in CHECKS if k != "run_shift"])]


def run_all(m):
    """The eleven checks of one run, in the order of CHECKS; a check that fails on the run's files says so and the rest run."""
    out = []
    for fn in RULES:
        name = fn.__name__[6:]
        if m["kind"] == "estimates" and name not in ESTIMATES:
            out.append(_na(name, f"the deliverable kind estimates records nothing for this check (it has {R.LACKS})", [(R.SIGNED, "deliverable.kind")]))
            continue
        try:
            out.append(fn(m))
        except Exception as e:      # a check must not take the page down; say which one failed and why
            out.append(_r(name, "not_available", f"This check failed on the run's files ({type(e).__name__}: {e}).", [(R.POINT, "")]))
    return out


def _self_test():
    import workbench
    from views.fit_bench import Bench, fixture_builder, need
    fx = fixture_builder()
    b = Bench(lambda run: None)
    hm = fx.METHODS[0]

    def model(d):
        run = workbench.Run(d)
        return K.read(run, R.spec(run))

    def got(m):
        return {c["id"].split(".")[-1]: c for c in run_all(m)}

    def shape():
        cs = run_all(model(b.folder(hm)))
        need([c["id"] for c in cs] == [f"outcomes.{n}" for n in CHECKS if n != "run_shift"] and all(c["status"] == "ok" for c in cs),
             f"eleven checks in the order of CHECKS, each ok on the clean run ({[(c['id'], c['status']) for c in cs if c['status'] != 'ok']})")
        need(all(c["case"] == CHECKS[c["id"].split(".")[-1]] and c["evidence"] and "\n" not in c["says"] for c in cs), "each with its case, evidence and one sentence")
        need(set(TITLES) == set(CHECKS), "a title per check")

    def changed():
        m = model(b.folder(hm))
        m["headline"] = {**m["headline"], "share": 0.4}
        need(got(m)["share_of_burden"]["status"] == "look" and "near miss" in got(m)["share_of_burden"]["says"], "a share within 25% of the threshold is a near miss")
        m["dearest"] = [{"group": "t", "option": "C", "units": 12, "hits": 9, "share": 0.75}]
        m["options"]["groups"] = {}
        need(got(m)["dearest_wins"]["status"] == "look", "the near-miss rule applies to dearest_wins")
        m["dearest"] = [{"group": "t", "option": "C", "units": 4, "hits": 4, "share": 1.0}]
        need(got(m)["dearest_wins"]["status"] == "ok", "fewer units than the producer's minimum: ok")
        m["variants"][0]["over_budget_share"] = 0.08
        need(got(m)["variant_budget"]["status"] == "look", "a variant within 25% of the allowance is a near miss")
        m["burden"][0].update(state="unmatched")
        need(got(m)["burden_refs"]["status"] == "not_available", "a checked reference that could not be matched is never passed")
        m["burden"][0].update(state="within", ratio=1.9)
        need(got(m)["burden_refs"]["status"] == "look", "a ratio within 25% of an end of its band is a near miss")
        m["tests"][0].update(state="ran", verdict="NO_CHANGE", feasible=False)
        need(got(m)["infeasible_tests"]["status"] == "look", "a test under which the point plan is not feasible")

    def kinds():
        cs = unknown("the signed spec's deliverable kind is 'ranking', which this view does not know")
        need(len(cs) == 11 and all(c["status"] == "not_available" and "ranking" in c["says"] for c in cs), "an unknown kind: every check not_available, naming it")
        from views import outcomes_estimates as E
        run = workbench.Run(b.folder(fx.METHODS[1], figures="estimates", kind="estimates"))
        cs = {c["id"].split(".")[-1]: c["status"] for c in run_all(E.read(run, R.spec(run)))}
        need(cs == {n: "ok" if n == "tests_that_move" else "not_available" for n in CHECKS if n != "run_shift"}, f"the estimates kind ({cs})")

    b.case("C1 the checks of one run: order, case, evidence, one sentence; all ok on the clean run", shape)
    b.case("C2 near misses, the producer's minimum of units, an unmatched reference, an infeasible test", changed)
    b.case("C3 an unknown kind and the estimates kind", kinds)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: outcomes_checks.py --self-test")
