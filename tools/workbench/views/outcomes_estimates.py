#!/usr/bin/env python3
"""The Outcomes adapter for the deliverable kind `estimates` (docs/design/review-workbench.md §5.7): outputs with
their intervals, the threshold decision and its tests, mapped into the generic Outcomes model.

    python3 tools/workbench/views/outcomes_estimates.py --self-test

`read(run, spec)` returns the model in the shape views/outcomes_package.py gives, with every part the kind does not
have left None: there is no plan, comparator, point value, variant, external reference, value of information,
stability measure or emulator. Where each part comes from (analysis/estimates.yaml `data`, E):

 outputs    E.outputs[<name>]: median, ci_low, ci_high and scale, in the file's order.
 decision   E.decision: output, threshold, direction, probability_required, probability and the rule's verdict.
 headline   the output the decision rule names (the signed spec's deliverable.decision.output, else E.decision.output;
            with no rule, the first output); the level is E.level; draws from analysis/uncertainty.yaml data.draws,
            else the output's own count.
 tests      the shared loader (checks.estimates_tests), which reads E.decision_tests and maps the producer's words to
            the verdicts of every other test; `varied` is the parameter the test moves; the producer records no
            question, share, regret or budget moved for these tests, so those cells are null.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
from views import outcomes_read as R  # noqa: E402
from views.outcomes_read import at, num, text  # noqa: E402

DECISION_VERDICTS = checks.EST_VERDICTS


def read(run, s):
    why = {}
    E, why[R.EST] = R.data(run, R.EST)
    U, why[R.UNC] = R.data(run, R.UNC)
    m = {**R.empty("estimates", s), "why": why}
    loaded, tests_why = checks.estimates_tests(run, s)
    if loaded is not None:
        rows, m["link_why"] = R.test_rows(run, s, loaded, lambda tid, t: (None, t.get("parameter")))
        m["tests"] = R.order(rows)
    else:
        why[R.EST] = why[R.EST] or tests_why
    outs = at(E, "outputs")
    if isinstance(outs, dict):
        m["outputs"] = [{"name": str(k), "median": num(at(v, "median")), "low": num(at(v, "ci_low")), "high": num(at(v, "ci_high")),
                         "scale": text(at(v, "scale"), 60)} for k, v in outs.items()]
    d = at(E, "decision")
    if isinstance(d, dict):
        m["decision"] = [{"output": text(str(d.get("output")), 200) if d.get("output") is not None else None,
                          "threshold": num(d.get("threshold")), "direction": text(d.get("direction"), 60),
                          "probability_required": num(d.get("probability_required")), "probability": num(d.get("probability")),
                          "verdict": d.get("verdict") if isinstance(d.get("verdict"), str) else None,
                          "statement": text(d.get("statement"), 1200)}]
    name = text(at(s["raw"], "deliverable", "decision", "output")) or text(at(d, "output")) or (m["outputs"][0]["name"] if m["outputs"] else None)
    row = next((r for r in m["outputs"] or [] if r["name"] == name), None) or {}
    level = num(at(E, "level"))
    m["outcome"] = m["headline_name"] = name
    m["headline"] = {"outcome": name, "point": None, "median": row.get("median"), "low": row.get("low"), "high": row.get("high"),
                     "level": level, "comparator_total": None, "share": None}
    if U is not None:
        dom = U.get("prior_dominated") if isinstance(U.get("prior_dominated"), list) else at(U, "interval", "prior_dominated")
        m["dominated"] = [str(x) for x in dom] if isinstance(dom, list) else []
    t = m["tests"]

    def count(fn):
        return None if t is None else sum(1 for r in t if fn(r))
    m["facts"] = {
        "deliverable_kind": "estimates", "headline_outcome": name, "headline_point": None, "headline_median": row.get("median"),
        "headline_low": row.get("low"), "headline_high": row.get("high"), "interval_level": level,
        "n_draws": num(at(U, "draws")) if num(at(U, "draws")) is not None else num(at(outs, name, "n")),
        "n_tests": None if t is None else len(t), "n_tests_no_change": count(lambda r: r["state"] == "ran" and r["verdict"] == "NO_CHANGE"),
        "n_tests_change_plan": count(lambda r: r["state"] == "ran" and r["verdict"] == "CHANGES_PLAN"),
        "n_tests_not_run": count(lambda r: r["state"] == "not_run"), "n_tests_near_miss": count(lambda r: r["near_miss"]),
        "n_prior_dominated": None if m["dominated"] is None else len(m["dominated"])}
    return m


def _self_test():
    import workbench
    from views.fit_bench import Bench, fixture_builder, need, rewrite
    fx = fixture_builder()
    b = Bench(lambda run: None)
    post = fx.METHODS[1]
    base = dict(figures="estimates", kind="estimates")

    def model(d):
        run = workbench.Run(d)
        return read(run, R.spec(run))

    def clean():
        m = model(b.folder(post, **base))
        f = m["facts"]
        need(f["headline_outcome"] == "q1" and f["headline_point"] is None and f["headline_median"] == m["outputs"][0]["median"]
             and f["interval_level"] == 0.95 and f["n_draws"] == fx.DRAWS and f["n_tests"] == f["n_tests_no_change"] == 2, f"facts ({f})")
        need([r["name"] for r in m["outputs"]] == ["q1", "q2"] and m["decision"][0]["verdict"] == "yes" and m["decision"][0]["probability_required"] == 0.9,
             "outputs in the file's order and the decision rule's verdict")
        need([(r["id"], r["varied"], r["question"], r["verdict"]) for r in m["tests"]]
             == [("prior_range_p1", "p1", None, "NO_CHANGE"), ("prior_range_p2", "p2", None, "NO_CHANGE")], f"tests ({m['tests']})")
        need(all(m[k] is None for k in ("plan", "voi", "variants", "burden", "stability", "emulator", "fixed")), "what the kind does not have is null")

    def words():
        m = model(b.folder(post, "estimates_test_changes", **base))
        need(m["tests"][0]["id"] == "prior_range_p1" and m["tests"][0]["verdict"] == "CHANGES_PLAN" and m["facts"]["n_tests_change_plan"] == 1,
             "a test whose rule's verdict changes at one end changes the decision, and is read first")
        m = model(b.folder(post, "estimates_test_unknown_status", **base))
        need(m["tests"][0]["state"] == "unknown" and m["tests"][0]["verdict"] is None, "a status the producer does not write has no verdict")
        m = model(b.edited(post, rewrite(fx.EST, lambda doc: doc["data"]["decision_tests"][0].update(status="not_assessable", decision_changes=None)), **base))
        need(m["tests"][-1]["state"] == "ran" and [r for r in m["tests"] if r["state"] == "not_run"][0]["verdict"] == "NOT_RUN"
             and m["facts"]["n_tests_not_run"] == 1, "the producer's not_assessable is a test that did not run")
        m = model(b.edited(post, lambda folder: os.remove(os.path.join(folder, fx.EST)), **base))
        need(m["tests"] is None and m["outputs"] is None and m["facts"]["n_tests"] is None and R.absent(m["why"][R.EST]), "with no estimates file: nothing, with the reason")

    b.case("T1 the model of the clean estimates run: headline, outputs, decision, tests; the rest null", clean)
    b.case("T2 the producer's words through the shared loader; fail closed with no file", words)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: outcomes_estimates.py --self-test")
