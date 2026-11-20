#!/usr/bin/env python3
"""What the Outcomes modules share (docs/design/review-workbench.md §5.7): the file names, the signed spec as the
view reads it, the decision-test rows of the generic model, and small readers of numbers and words.

    python3 tools/workbench/views/outcomes_read.py --self-test

Read by views/outcomes_package.py and views/outcomes_estimates.py (one adapter per deliverable kind), which fill the
generic model; views/outcomes_checks.py and views/outcomes_compare.py judge that model; views/outcomes_page.py turns it
into tables, charts and notes; views/outcomes.py is the view.

Rules held here (§3, §5.7):
- The sign-off record and the decision tests come from the shared loaders (checks.verified_spec,
  checks.decision_tests, checks.estimates_tests); nothing is loaded a second way.
- `spec(run)` gives the verified signed spec (`doc`, None when it cannot be verified) and, apart from it, the signed
  copy as read (`raw`): names and the deliverable kind are read from `raw` so the page can say what it is looking at,
  and nothing that excuses or defines anything (costs, an allowance, excluded inputs, the tests' entries, the
  question) is ever taken from it.
- A test's link to a register row is the register's own derivation (views/register_rows.py derive): the first row,
  other than the test's own `test:<id>` row, whose tests hold the test.
- A near miss (§3): a value at or above 75% of a threshold it did not pass (workbench threshold).
"""
from __future__ import annotations

import math
import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402

POINT, UNC, DT, STAB, VOI, PV, BV, EMU, EMB, EST = (f"analysis/{n}.yaml" for n in (
    "point", "uncertainty", "decision_tests", "stability", "value_of_information", "plan_variants", "burden_validation",
    "emulator", "emulator_boundary", "estimates"))
SIGNED, FIGS, TESTS_DIR = checks.SIGNED_COPY, checks.FIGURES, "models/decision_tests/"
KINDS = ("package_per_unit", "estimates")              # toolkit/spec/schema.py DELIVERABLE_KINDS
NEAR = 0.25                                            # §3: within 25% of a threshold (workbench threshold)
SCENARIO = "scenario_excluded_"                        # toolkit/analysis/decision_tests.py SCENARIO_EXCLUDED_PREFIX
TEST_COLUMNS = ["id", "question", "varied", "status", "verdict", "share_pct", "share_threshold_pct", "regret_pct",
                "regret_threshold_pct", "moved_pct", "moved_threshold_pct", "near_miss", "issue"]
# what the estimates kind does not have (§5.7), in one line for its notes and checks
LACKS = "no allocation plan, comparator, point value, plan variant, external reference, value of information, stability measure or emulator"
SHOWN = 8                                              # names listed in a sentence before "and n more"


def num(x):
    """x when it is a finite number, else None (a value the harness could not compute is no number)."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def g(x):
    """A number for a sentence: whole and grouped from ten thousand, else to six significant digits."""
    if num(x) is None:
        return "none" if x is None else str(x)
    return f"{x:,.0f}" if abs(x) >= 1e4 else f"{x:.6g}"


def r3(x):
    """A ratio or factor for a sentence, to three significant digits."""
    return "none" if num(x) is None else f"{x:.3g}"


def plural(n, one, many=None):
    return f"{n} {one if n == 1 else (many or one + 's')}"


def pct(x):
    return "none" if num(x) is None else f"{100 * x:.3g}%"


def text(x, cap=2000):
    return " ".join(str(x).split())[:cap] if isinstance(x, str) and x.strip() else None


def some(items, n=SHOWN):
    items = [str(i) for i in items]
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def at(doc, *keys):
    """doc[k1][k2]... through mappings only, else None."""
    for k in keys:
        doc = doc.get(k) if isinstance(doc, dict) else None
    return doc


def dicts(x):
    return [e for e in x if isinstance(e, dict)] if isinstance(x, list) else []


def near(value, threshold):
    """A value that did not pass its threshold and is within 25% of it (§3)."""
    v, t = num(value), num(threshold)
    return v is not None and t is not None and t > 0 and (1 - NEAR) * t <= v <= t


def schema(rel):
    """The schema word this view reads an analysis file as (toolkit/analysis: `analysis.<file name>/1`)."""
    return f"analysis.{os.path.basename(rel)[:-len('.yaml')]}/1"


def data(run, rel):
    """(the `data` mapping of an analysis file, None) or (None, why). The adapter boundary (§3a): a file whose header
    names another schema than the one this view reads, or none, is not read as if it were that one."""
    doc, why = run.large(rel)
    if doc is None:
        return None, why
    if doc.get("schema") != schema(rel):
        return None, (f"{rel} has the schema {doc.get('schema')!r}, which this view does not know (it reads {schema(rel)}), so nothing "
                      "in it is read as if it were that one")
    if not isinstance(doc.get("data"), dict):
        return None, f"{rel} holds no data mapping"
    return doc["data"], None


def rows(D, key, rel):
    """(the list of mappings at D[key], None), or (None, why) when it is no list or an entry is no mapping: a row that
    cannot be read is named by its position, never dropped (§6: nothing is read in part)."""
    got = D.get(key)
    if not isinstance(got, list):
        return None, f"{rel} holds no list at data.{key}"
    bad = [i for i, e in enumerate(got) if not isinstance(e, dict)]
    if bad:
        return None, f"{rel} data.{key}[{bad[0]}] is no mapping" + (f" (nor are {len(bad) - 1} more entries)" if len(bad) > 1 else "") + ", so the list is not read in part"
    return got, None


def units(run, doc):
    """(the rows of the units table as {unit id: row}, None) or (None, why): the table the verified signed spec names
    (decision_units.csv, with its id column decision_units.id), read inside the run folder."""
    import csv
    import io
    rel, col = at(doc, "decision_units", "csv"), at(doc, "decision_units", "id")
    if not isinstance(rel, str) or not isinstance(col, str):
        return None, "the verified signed spec names no units table and id column (decision_units.csv, decision_units.id)"
    raw, why = run.read(rel, 32 << 20)
    if raw is None:
        return None, f"the units table {rel} could not be read ({why})"
    try:
        table = list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))
    except csv.Error as e:
        return None, f"the units table {rel} is no CSV table ({e})"
    if not table or col not in table[0]:
        return None, f"the units table {rel} has no rows or no id column {col}"
    return {str(r[col]): r for r in table}, None


def provenance(run, rel):
    """What a run records of itself outside its spec, for a comparison with another run (§12, reader finding 1): the
    launch commit and pipeline (metadata.json), the engine identity in the header of the analysis file `rel`, and
    every input hash an analysis file records, by input path. Recorded files only; a part not recorded is absent."""
    out = {}
    meta, _ = run.json("metadata.json")
    for k, name in (("commit", "launch commit"), ("pipeline", "pipeline")):
        out[name] = meta.get(k) if isinstance(meta, dict) and isinstance(meta.get(k), (str, int, float)) else None
    doc, _ = run.large(rel)
    eng = doc.get("engine") if doc is not None and isinstance(doc.get("engine"), dict) else {}
    for k, v in eng.items():
        out[f"engine {k}"] = v
    held = {}
    for a in run.artifacts():
        for i in a["inputs"]:
            held.setdefault(str(i["path"]), set()).add(str(i["sha256"])[:10] if checks.digest(i["sha256"]) else "no valid hash")
    for path in sorted(held, key=lambda x: (x.startswith("analysis/"), x)):       # the run's inputs first, then what the analysis computed from them
        out[f"input {path}"] = ", ".join(sorted(held[path]))
    return out


def absent(why):
    return str(why or "").endswith("is not in the run folder")


def spec(run):
    """checks.verified_spec's result with `raw` (the signed copy as read, {} when it cannot be) and `kind` (its
    deliverable.kind, or None). `doc` alone may excuse or define anything."""
    s = dict(checks.verified_spec(run))
    raw = s["doc"]
    if raw is None:
        _, raw, _ = run.hashed(SIGNED)
    s["raw"] = raw if isinstance(raw, dict) else {}
    s["kind"] = text(at(s["raw"], "deliverable", "kind"), 60)
    return s


def empty(kind, s):
    """The generic model with nothing in it: what an adapter starts from, and what a run with no kind this view knows
    is shown as."""
    return {"kind": kind, "spec": s, "why": {}, "outcome": None, "headline_name": None, "options": None, "plan": None,
            "plan_rows": None, "components": None, "dearest": None, "point": None, "headline": None, "outcomes": None, "voi": None,
            "variants": None, "burden": None, "stability": None, "stability_rows": None, "emulator": None, "boundary": None,
            "fixed": None, "dominated": None, "outputs": None, "decision": None, "allowance": None, "tests": None, "link_why": None,
            "tie": None, "plan_clash": None, "provenance": None, "facts": {"deliverable_kind": kind}}


def entries(s):
    """{test id: its entry in the verified signed spec's analysis.decision_tests}; {} with no verified spec."""
    return {str(e.get("id")): e for e in dicts(at(s["doc"], "analysis", "decision_tests"))}


def excluded(s):
    """The inputs the verified signed spec excludes from the draws by name (uncertainty.excluded[].parameter)."""
    return [str(e["parameter"]) for e in dicts(at(s["doc"], "uncertainty", "excluded")) if e.get("parameter")]


def issues(run):
    """({test id: the register row it is linked to}, why not). The register's own derivation, never a second one."""
    try:
        from views import register_rows
        rows = register_rows.derive(run)["rows"]
    except Exception as e:      # the register must not take this view down; the link is then unknown, and said
        return {}, f"the register could not be derived ({type(e).__name__}: {e})"
    out = {}
    for r in rows:
        for t in r.get("tests") or []:
            if r.get("id") != f"test:{t}" and r.get("sources") != ["decision_tests"]:
                out.setdefault(str(t), str(r["id"]))
    return out, None


def test_rows(run, s, loaded, ask):
    """The generic model's test rows from a shared loader's result (`loaded`: {id: record}). `ask(id, record)` gives
    (question, varied). Each row has the columns of TEST_COLUMNS and, for the checks, `state`, `counts`, `feasible`,
    `excused` (the producer's own not-run scenario for an input the verified signed spec excludes), `parameter`, and
    the loader's own judgement carried whole: `spec` and `spec_says` (how the file's spec hash stands) and `clash`."""
    linked, why = issues(run)
    left_out, spec_tests = set(excluded(s)), entries(s)
    rows = []
    for tid, t in loaded.items():
        question, varied = ask(tid, t)
        row = {"id": tid, "question": question, "varied": varied, "status": t["status"], "verdict": t["verdict"],
               "share_pct": t["share_pct"], "share_threshold_pct": t["threshold_pct"], "regret_pct": t["regret_pct"],
               "regret_threshold_pct": t["regret_threshold_pct"], "moved_pct": t["dollars_share_pct"],
               "moved_threshold_pct": t["dollars_threshold_pct"], "issue": linked.get(tid),
               "state": t["state"], "counts": t["counts"], "feasible": t["feasible"], "spec": t["spec"], "spec_says": t["spec_says"],
               "clash": t.get("clash"),
               "parameter": t.get("parameter") or text((spec_tests.get(tid) or {}).get("parameter"), 200)}
        row["near_miss"] = t["state"] == "ran" and t["verdict"] == "NO_CHANGE" and any(
            near(row[k], row[k.replace("_pct", "_threshold_pct")]) for k in ("share_pct", "regret_pct", "moved_pct"))
        row["excused"] = t["state"] == "not_run" and tid.startswith(SCENARIO) and tid[len(SCENARIO):] in left_out
        rows.append(row)
    return rows, why


def tie(loaded):
    """(how the tests' file stands against the verified signed spec, the loader's sentence when it is not the same):
    the shared loader's own word, carried into the model so no check judges it again. None with no test."""
    first = next(iter(loaded.values()), None)
    return (first["spec"], first["spec_says"]) if first else None


def test_counts(m):
    """The four counts of test results for the facts: null unless the tests' file records the verified signed spec."""
    t = m["tests"]
    counted = t is not None and (m["tie"] is None or m["tie"][0] == "same")

    def count(fn):
        return sum(1 for r in t if fn(r)) if counted else None
    return {"n_tests": None if t is None else len(t), "n_tests_no_change": count(lambda r: r["state"] == "ran" and r["verdict"] == "NO_CHANGE"),
            "n_tests_change_plan": count(lambda r: r["state"] == "ran" and r["verdict"] == "CHANGES_PLAN"),
            "n_tests_not_run": count(lambda r: r["state"] == "not_run"), "n_tests_near_miss": count(lambda r: r["near_miss"])}


def order(rows):
    """Reading order: what contradicts itself or cannot be read, then plan-changing tests, near misses, tests that did
    not run, and the rest, each group in the file's order."""
    def rank(r):
        return (0 if r["state"] in ("contradiction", "unknown") else 1 if r["verdict"] == "CHANGES_PLAN" else
                2 if r["near_miss"] else 3 if r["state"] == "not_run" else 4)
    return sorted(rows, key=rank)


def figures(run):
    """{figure name: row} of the harness figures that belong beside this view (§5.12: the decision tier, the estimate
    figures, and the burden figure, whose table this view shows), or None with no readable figure index."""
    from views.figures import beside
    figs = checks.harness_figures(run)
    if figs is None:
        return None
    return {n: f for n, f in figs.items() if beside(n, f.get("tier"), f.get("group")) == "outcomes" or f.get("tier") == "decision"
            or n == "burden_validation"}


def image(fig):
    """A chart's `image` for a figure row, or {}."""
    if not fig:
        return {}
    return {"image": {"path": fig["path"], "hash": fig["sha256"], "title": fig["title"] or fig["figure"].replace("_", " "),
                      **({"caption": fig["caption"]} if fig["caption"] else {})}}


def _self_test():
    import workbench
    from views.fit_bench import Bench, fixture_builder, need
    fx = fixture_builder()
    b = Bench(lambda run: None)
    hm, post = fx.METHODS

    def helpers():
        need(num(1.5) == 1.5 and num(True) is None and num(float("nan")) is None and num("1") is None, "num")
        need(g(25000.0) == "25,000" and g(0.125) == "0.125" and g(None) == "none" and r3(2.0) == "2" and plural(1, "test") == "1 test"
             and plural(2, "near miss", "near misses") == "2 near misses", f"g ({g(25000.0)}, {g(0.125)})")
        need(near(8.0, 10.0) and near(7.5, 10.0) and not near(7.4, 10.0) and not near(11.0, 10.0) and not near(0.0, 0.0) and not near(None, 1),
             "a near miss is at or above 75% of a threshold it did not pass")
        need(at({"a": {"b": 1}}, "a", "b") == 1 and at({"a": [1]}, "a", "b") is None and some(range(10), 2) == "0, 1 and 8 more", "at, some")

    def specs():
        s = spec(workbench.Run(b.folder(hm)))
        need(s["verified"] and s["kind"] == "package_per_unit" and s["raw"] is s["doc"] and excluded(s) == [fx.HELD] and "DT1" in entries(s),
             "the verified signed spec, its kind, its excluded inputs and its tests' entries")

        def edit(d):
            with open(os.path.join(d, fx.SPEC_FILES[1]), "a", encoding="utf-8") as f:
                f.write("note: edited after the sign-off\n")
        s = spec(workbench.Run(b.edited(hm, edit)))
        need(not s["verified"] and s["doc"] is None and s["kind"] == "package_per_unit" and excluded(s) == [] and entries(s) == {},
             "with no verified signed spec the kind is still read, and nothing that excuses or defines anything is")
        d = os.path.join(b.tmp, "none")
        os.makedirs(d)
        s = spec(workbench.Run(d))
        need(s["kind"] is None and s["raw"] == {} and not s["verified"], "no signed copy: no kind")

    def tests():
        run = workbench.Run(b.folder(hm, "test_not_run"))
        s = spec(run)
        loaded, _ = checks.decision_tests(run, s)
        rows, why = test_rows(run, s, loaded, lambda tid, t: (None, None))
        by = {r["id"]: r for r in rows}
        need(why is None and [r["id"] for r in rows] == list(loaded) and by["DT7"]["issue"] == f"excluded:{fx.HELD}" and by["DT1"]["issue"] is None,
             f"rows in the file's order, each with the register row its test is linked to ({[(r['id'], r['issue']) for r in rows]})")
        need(by[f"{SCENARIO}{fx.HELD}"]["excused"] and not by["DT1"]["excused"] and by["DT7"]["parameter"] == fx.HELD,
             "the producer's not-run scenario for an input the signed spec excludes is marked; a test's parameter is the spec entry's")
        run = workbench.Run(b.folder(hm, "test_near_miss"))
        s = spec(run)
        rows, _ = test_rows(run, s, checks.decision_tests(run, s)[0], lambda tid, t: (None, None))
        need([r["id"] for r in rows if r["near_miss"]] == ["DT1"] and order(rows)[0]["id"] == "DT1", "a near miss is marked and read first")
        run = workbench.Run(b.folder(post, figures="estimates", kind="estimates"))
        loaded, why = checks.estimates_tests(run)
        need(why is None and [t["state"] for t in loaded.values()] == ["ran", "ran"] and all(t["counts"] for t in loaded.values())
             and loaded["prior_range_p1"]["parameter"] == "p1", f"the estimates kind's tests through the shared loader ({loaded})")

    def figs():
        got = figures(workbench.Run(b.folder(hm)))
        need(set(got) == {"headline", "frontier", "decision_tests"} and image(got["headline"])["image"]["path"] == "figures/headline.png"
             and checks.digest(image(got["headline"])["image"]["hash"]), f"the decision-tier figures, each with the hash of the image as read ({sorted(got)})")
        got = figures(workbench.Run(b.folder(post, figures="estimates", kind="estimates")))
        need(set(got) == {"estimates_q1", "estimates_q2"} and image(None) == {}, f"the estimate figures ({sorted(got)})")

    b.case("R1 numbers, sentences and the near-miss rule", helpers)
    b.case("R2 the signed spec: verified for what it excuses or defines, as read for the kind and names", specs)
    b.case("R3 test rows: file order, the register's link, the excused scenario, near misses, both kinds through the shared loaders", tests)
    b.case("R4 the harness figures that belong beside the view", figs)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: outcomes_read.py --self-test")
