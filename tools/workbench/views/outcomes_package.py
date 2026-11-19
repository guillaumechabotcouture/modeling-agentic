#!/usr/bin/env python3
"""The Outcomes adapter for the deliverable kind `package_per_unit` (docs/design/review-workbench.md §5.7): what a
run's analysis wrote about an allocation plan, mapped into the generic Outcomes model.

    python3 tools/workbench/views/outcomes_package.py --self-test

`read(run, spec)` returns the model (`spec` is outcomes_read.spec's result). Every part is None when its file cannot
be read, with the reason in `why[<file>]`; nothing is guessed. Where each part comes from:

 headline    analysis/point.yaml data.averted[<outcome>] (the point value) and data.comparator_total[<outcome>];
             analysis/uncertainty.yaml data.fixed_plan[<outcome>] (median and interval with the plan held fixed).
             The outcome is the signed spec's deliverable.headline.outcome, its name deliverable.headline.name.
 plan        analysis/point.yaml data.plan ({unit: package}) and data.per_unit[<unit>].cost. A package's components
             are the signed spec's own definition of it where it has one (options.packages.<name>), else the parts
             of its name (generated packages are named by their components joined with "+"; "none" is the empty one).
 groups      computed here from the verified signed spec alone (gap H6): the options of a group are the components
             that share options.components.<c>.group; an option's price is the sum of its cost items that do not
             depend on the model (basis other than per_outcome), each value times its share, times and years and,
             for a per_commodity item, divided by people_per_commodity. Options are compared only when they are
             priced on the same bases (the same basis and population, and the same price year where a price index
             applies); otherwise there is no dearest option to name. toolkit/analysis/red_flag.py is the producer's
             rule; it prices each unit through the harness's cost code, which the workbench does not import.
 tests       the shared loader (checks.decision_tests); question and varied from models/decision_tests/<id>.yaml.
 voi         analysis/value_of_information.yaml data.inputs; prior_dominated from analysis/uncertainty.yaml.
 variants    analysis/plan_variants.yaml data.variants (the optimised plan is not a row).
 burden      analysis/burden_validation.yaml data.references; `tests` on a row are the verified signed spec's
             burden_scale decision tests that name the reference.
 stability   analysis/stability.yaml data.metrics, then the two across-draw measures of analysis/uncertainty.yaml.
 emulator    analysis/emulator.yaml data.accuracy (its numbers), then the boundary check's share of units matched.
 fixed       the inputs analysis/uncertainty.yaml lists as held at one value (interval.spec_fixed, interval.fixed,
             draw_plan.fixed[].name).
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
from views import outcomes_read as R  # noqa: E402
from views.outcomes_read import at, dicts, num, text  # noqa: E402

# Each producer's own words, mapped explicitly (§3); any other word is not_available.
VOI_STATUSES = ("computed", "indicative", "too_few_draws", "no_eligible_plan")     # toolkit/analysis/value_of_information.py STATUSES
STABILITY_VERDICTS = ("ROBUST", "FRAGILE", "UNSTABLE", "NOT_RUN")                  # toolkit/analysis/stability.py
BURDEN_STATUSES = ("within", "outside", "information", "unmatched")                # toolkit/analysis/burden.py STATUSES
BURDEN_ROLES = ("checked", "report_only", "anchor")
VARIANT_STATUSES = ("evaluated", "solved", "not_solved")                           # toolkit/analysis/variants.py


def _summary(d):
    """(median, low, high, level) of a draws summary as toolkit/analysis/uncertainty.py writes one."""
    d = d if isinstance(d, dict) else {}
    return num(d.get("median")), num(d.get("ci_low")), num(d.get("ci_high")), num(d.get("level"))


def _price(item):
    """A cost item's price per head of its basis population, as written in the spec (no price index applied)."""
    v = num(item.get("value"))
    if v is None:
        return None
    for k in ("share", "times", "years"):
        if item.get(k) is not None:
            if num(item[k]) is None:
                return None
            v *= item[k]
    if item.get("basis") == "per_commodity":
        per = num(item.get("people_per_commodity"))
        if not per or per <= 0:
            return None
        v /= per
    return v


def options(doc):
    """What the verified signed spec says of the options, or None with no verified spec or no components:
    {components: [names in the spec's order], defs: {package: [components]} or None when packages are generated,
     groups: {group: {options: [components], price: {component: number}, why: None or why they cannot be ranked}}}."""
    comps = at(doc, "options", "components")
    if not isinstance(comps, dict) or not comps:
        return None
    packages = at(doc, "options", "packages")
    defs = None
    if isinstance(packages, dict) and packages.get("generate") is not True:
        defs = {str(k): [str(c) for c in (v or [])] for k, v in packages.items() if isinstance(v, (list, type(None)))}
    items = [i for i in dicts(at(doc, "options", "cost_items")) if i.get("basis") != "per_outcome"]
    groups = {}
    for c, d in comps.items():
        if isinstance(d, dict) and d.get("group") is not None:
            groups.setdefault(str(d["group"]), {"options": [], "price": {}, "why": None})["options"].append(str(c))
    for name, grp in list(groups.items()):
        if len(grp["options"]) < 2:
            del groups[name]
            continue
        bases = set()
        for c in grp["options"]:
            own = [i for i in items if str(i.get("component")) == c]
            prices = [_price(i) for i in own]
            if not own or any(p is None for p in prices):
                grp["why"] = f"option {c} has no cost item with a value that does not depend on the model"
                break
            indexed = any(i.get("price_rule") != "as_published" for i in own)
            bases.add(tuple(sorted({(str(i.get("basis")), str(i.get("population", "total")),
                                     str(i.get("price_rule")) if indexed else "", str(i.get("price_year")) if indexed else "") for i in own})))
            grp["price"][c] = sum(prices)
        if grp["why"] is None and len(bases) > 1:
            grp["why"] = "its options are priced on different bases"
        if grp["why"] is None and len(set(grp["price"].values())) < 2:
            grp["why"] = "its options have the same price"
        grp["bases"] = sorted({b[0] for sig in bases for b in sig})
    return {"components": [str(c) for c in comps], "defs": defs, "groups": groups}


def parts(package, opts):
    """A package's components, or None when one of them is not a component of the signed spec."""
    if opts is None:
        return None
    if opts["defs"] is not None:
        got = opts["defs"].get(package)
    else:
        got = [] if package == "none" else package.split("+")
    return got if got is not None and all(c in opts["components"] for c in got) else None


def _rank(package, opts):
    """1 for the dearest option of its group; None where the package holds no option of a group, options of several
    groups, or an option of a group that cannot be ranked."""
    cs = parts(package, opts)
    held = [(name, c) for name, grp in (opts or {"groups": {}})["groups"].items() for c in cs or [] if c in grp["options"]]
    if len(held) != 1 or opts["groups"][held[0][0]]["why"]:
        return None
    price = opts["groups"][held[0][0]]["price"]
    return 1 + sorted(set(price.values()), reverse=True).index(price[held[0][1]])


def dearest(plan, opts):
    """Per group whose options can be ranked: {group, option (the dearest), units (units whose plan holds an option
    of the group), hits (of them, those with the dearest), share}; None when the plan names a package the signed spec
    does not define or there is no verified spec."""
    if opts is None or plan is None or any(parts(p, opts) is None for p in set(plan.values())):
        return None
    out = []
    for name, grp in sorted(opts["groups"].items()):
        if grp["why"]:
            continue
        top = max(grp["options"], key=lambda c: grp["price"][c])
        chosen = [[c for c in parts(p, opts) if c in grp["options"]] for p in plan.values()]
        n, hits = sum(1 for c in chosen if c), sum(1 for c in chosen if c and c[0] == top)
        if n:
            out.append({"group": name, "option": top, "units": n, "hits": hits, "share": hits / n})
    return out


def _plan(P, opts):
    plan = P.get("plan")
    if not isinstance(plan, dict) or not plan or not all(isinstance(p, str) for p in plan.values()):
        return None, None
    plan = {str(u): p for u, p in plan.items()}
    per = {str(k): v for k, v in P["per_unit"].items()} if isinstance(P.get("per_unit"), dict) else {}     # a unit id may be read as a number
    names = sorted(set(plan.values()), key=lambda p: (-sum(1 for x in plan.values() if x == p), p))
    rows = []
    for p in names:
        units = [u for u, x in plan.items() if x == p]
        costs = [num(at(per.get(u), "cost")) for u in units]
        rows.append({"package": p, "units": len(units), "share": len(units) / len(plan),
                     "cost": None if any(c is None for c in costs) else float(sum(costs)), "cost_per_unit_rank": _rank(p, opts)})
    return plan, rows


def _tests(run, s):
    loaded, why = checks.decision_tests(run, s)
    if loaded is None:
        return None, why, None

    def ask(tid, t):
        if t["state"] != "ran":
            return None, None
        doc, _ = run.large(f"{R.TESTS_DIR}{tid}.yaml")
        return (text(doc.get("question")), text(doc.get("varied"))) if doc is not None else (None, None)
    rows, link_why = R.test_rows(run, s, loaded, ask)
    return R.order(rows), None, link_why


def _voi(V, dominated):
    rows = []
    for e in dicts(V.get("inputs")):
        members = [str(x) for x in e["members"]] if isinstance(e.get("members"), list) else [str(e.get("input"))]
        rows.append({"input": text(e.get("input"), 200), "kind": text(e.get("kind"), 60), "evppi": num(e.get("evppi")),
                     "share_of_evpi": num(e.get("share_of_evpi")),
                     "above_noise": e.get("above_noise_floor") if isinstance(e.get("above_noise_floor"), bool) else None,
                     "prior_dominated": None if dominated is None else any(x in dominated for x in members),
                     "supported": e.get("supported") is True, "noise_floor": num(e.get("noise_floor"))})
    rows.sort(key=lambda r: -(r["evppi"] if r["evppi"] is not None else float("-inf")))
    return {"status": V.get("status"), "draws": num(V.get("draws")), "min_draws": num(V.get("min_draws")),
            "informative_draws": num(V.get("informative_draws")), "evpi": num(at(V, "evpi", "value")),
            "evpi_pct": num(at(V, "evpi", "pct")), "rows": rows}


def _variants(PV):
    rows, outcome = [], PV.get("outcome")
    for e in dicts(PV.get("variants")):
        _, low, high, _ = _summary(at(e, "uncertainty", "averted", outcome))
        rows.append({"id": text(str(e.get("id")), 200), "outcome": num(e.get("objective")), "low": low, "high": high,
                     "cost": num(e.get("cost")), "value_lost_pct": num(e.get("value_lost_pct")),
                     "over_budget_share": num(at(e, "uncertainty", "affordability", "share_over_budget")),
                     "units_changed": num(e.get("units_changed")), "status": e.get("status"), "what": text(e.get("text"), 600)})
    return rows


def _burden(B, s):
    naming = {}
    for tid, e in R.entries(s).items():
        if e.get("type") == "burden_scale":
            for ref in at(e, "burden_scale", "references") or []:
                naming.setdefault(str(ref), []).append(tid)
    rows = []
    for e in dicts(B.get("references")):
        band = at(e, "tolerance", "ratio_band")
        low, high = (num(band[0]), num(band[1])) if isinstance(band, list) and len(band) == 2 else (None, None)
        rows.append({"id": text(str(e.get("id")), 200), "group": text(str(e.get("group")), 200) if e.get("group") is not None else None,
                     "outcome": text(e.get("outcome"), 200), "ratio": num(e.get("ratio")), "band_low": low, "band_high": high,
                     "state": e.get("status"), "report_only": e.get("role") == "report_only", "role": e.get("role"),
                     "tests": naming.get(str(e.get("id")), [])})
    return rows


def _stability(S, U):
    rows = []
    if S is not None:
        metrics = S.get("metrics") if isinstance(S.get("metrics"), dict) else {}
        rows += [{"measure": str(k), "value": num(v), "verdict": S.get("value_verdict") if k == S.get("headline_metric") else None}
                 for k, v in metrics.items()]
    if U is not None:
        rows += [{"measure": k, "value": num(U.get(k)), "verdict": None}
                 for k in ("units_same_package_share_median", "units_same_package_in_90pct_of_draws") if k in U]
    return rows


def _emulator(E, B):
    rows = []
    if E is not None:
        acc = E.get("accuracy") if isinstance(E.get("accuracy"), dict) else {}
        rows += [{"measure": str(k), "value": v, "threshold": None, "passes": None} for k, v in acc.items() if num(v) is not None]
    if B is not None:
        rows.append({"measure": "share_matched", "value": num(B.get("share_matched")), "threshold": num(B.get("threshold")),
                     "passes": B.get("passes") if isinstance(B.get("passes"), bool) else None})
    return rows


def read(run, s):
    why = {}

    def get(rel):
        d, w = R.data(run, rel)
        why[rel] = w
        return d
    P, U, V, PV, B, S, E, EB = (get(rel) for rel in (R.POINT, R.UNC, R.VOI, R.PV, R.BV, R.STAB, R.EMU, R.EMB))
    raw = s["raw"]
    key = text(at(raw, "deliverable", "headline", "outcome")) or text(at(raw, "objective", "outcome")) or text(at(U, "headline", "outcome"))
    name = text(at(raw, "deliverable", "headline", "name")) or text(at(U, "headline", "name")) or key
    opts = options(s["doc"]) if s["verified"] else None
    m = {**R.empty("package_per_unit", s), "why": why, "outcome": key, "headline_name": name, "options": opts, "point": P,
         "allowance": num(at(s["doc"], "uncertainty", "affordability", "max_share_over_budget")) if s["verified"] else None}
    m["tests"], why[R.DT], m["link_why"] = _tests(run, s)
    med, low, high, level = _summary(at(U, "fixed_plan", key))
    point, total = num(at(P, "averted", key)), num(at(P, "comparator_total", key))
    m["headline"] = {"outcome": key, "point": point, "median": med, "low": low, "high": high, "level": level,
                     "comparator_total": total, "share": point / total if point is not None and total else None}
    others = [k for k in (at(U, "fixed_plan") or {}) if k != key and isinstance(at(U, "fixed_plan", k), dict)] if U is not None else []
    m["outcomes"] = [m["headline"]] + [
        {"outcome": str(k), "point": num(at(P, "averted", k)), "median": _summary(at(U, "fixed_plan", k))[0],
         "low": _summary(at(U, "fixed_plan", k))[1], "high": _summary(at(U, "fixed_plan", k))[2], "level": level,
         "comparator_total": num(at(P, "comparator_total", k)),
         "share": (num(at(P, "averted", k)) / num(at(P, "comparator_total", k))
                   if num(at(P, "averted", k)) is not None and num(at(P, "comparator_total", k)) else None)} for k in others]
    if P is not None:
        m["plan"], m["plan_rows"] = _plan(P, opts)
    if m["plan"] is not None:
        m["dearest"] = dearest(m["plan"], opts)
        names = opts["components"] if opts else list(dict.fromkeys(c for p in m["plan"].values() for c in ([] if p == "none" else p.split("+"))))
        held = {p: (parts(p, opts) if opts else ([] if p == "none" else p.split("+"))) for p in set(m["plan"].values())}
        m["components"] = [{"component": c, "units": sum(1 for p in m["plan"].values() if c in (held[p] or []))} for c in names]
    if U is not None:
        d = U.get("prior_dominated") if isinstance(U.get("prior_dominated"), list) else at(U, "interval", "prior_dominated")
        m["dominated"] = [str(x) for x in d] if isinstance(d, list) else []
        fixed = [str(x) for k in ("spec_fixed", "fixed") for x in (at(U, "interval", k) or []) if isinstance(at(U, "interval", k), list)]
        fixed += [str(e["name"]) for e in dicts(at(U, "draw_plan", "fixed")) if e.get("name") is not None]
        m["fixed"] = list(dict.fromkeys(fixed))
    if V is not None:
        m["voi"] = _voi(V, m["dominated"])
    if PV is not None:
        m["variants"] = _variants(PV)
    if B is not None:
        m["burden"] = _burden(B, s)
    if S is not None:
        m["stability"] = {"verdict": S.get("verdict"), "value_verdict": S.get("value_verdict"),
                          "headline_metric": text(S.get("headline_metric"), 200), "method": text(S.get("method"), 200),
                          "reason": text(S.get("reason"), 600)}
    if S is not None or U is not None:
        m["stability_rows"] = _stability(S, U)
    if E is not None or EB is not None:
        m["emulator"] = _emulator(E, EB)
    if EB is not None:
        rows = dicts(EB.get("rows"))
        m["boundary"] = {"packages": [str(r[k]) for r in rows for k in ("chosen", "runner_up") if r.get(k) is not None], "units": len(rows),
                         "share_matched": num(EB.get("share_matched")), "threshold": num(EB.get("threshold")), "passes": EB.get("passes")}
    t, v, b = m["tests"], m["voi"], m["burden"]
    best = max(m["dearest"], key=lambda r: r["share"]) if m["dearest"] else None

    def count(fn):
        return None if t is None else sum(1 for r in t if fn(r))
    m["facts"] = {
        "deliverable_kind": "package_per_unit", "headline_outcome": name, "headline_point": point, "headline_median": med,
        "headline_low": low, "headline_high": high, "interval_level": level, "n_draws": num(at(U, "draws")),
        "comparator_total": total, "share_of_comparator": m["headline"]["share"], "n_units": num(at(P, "n_units")),
        "n_packages_in_plan": None if m["plan_rows"] is None else len(m["plan_rows"]),
        "cost_allocated": num(at(P, "cost_allocated")), "budget_allocatable": num(at(P, "budget_allocatable")),
        "reserve": num(at(P, "reserve", "reserve_usd")), "cost_per_outcome": num(at(P, "cost_per_outcome_averted")),
        "n_tests": None if t is None else len(t), "n_tests_no_change": count(lambda r: r["state"] == "ran" and r["verdict"] == "NO_CHANGE"),
        "n_tests_change_plan": count(lambda r: r["state"] == "ran" and r["verdict"] == "CHANGES_PLAN"),
        "n_tests_not_run": count(lambda r: r["state"] == "not_run"), "n_tests_near_miss": count(lambda r: r["near_miss"]),
        "voi_status": None if v is None else v["status"], "evpi": None if v is None else v["evpi"],
        "evpi_share": None if v is None or v["evpi_pct"] is None else v["evpi_pct"] / 100,
        "n_inputs": None if v is None else len(v["rows"]), "n_inputs_supported": None if v is None else sum(r["supported"] for r in v["rows"]),
        "n_prior_dominated": None if m["dominated"] is None else len(m["dominated"]),
        "stability_verdict": None if m["stability"] is None else m["stability"]["verdict"],
        "units_same_package_share": num(at(U, "units_same_package_share_median")),
        "n_units_stable": num(at(U, "units_same_package_in_90pct_of_draws")),
        "n_variants": None if m["variants"] is None else len(m["variants"]),
        "n_burden_refs": None if b is None else len(b), "n_burden_outside": None if b is None else sum(r["state"] == "outside" for r in b),
        "dearest_share": None if best is None else best["share"]}
    return m


def _self_test():
    import workbench
    from views.fit_bench import Bench, close, fixture_builder, need
    fx = fixture_builder()
    b = Bench(lambda run: None)
    hm = fx.METHODS[0]

    def model(*a, **k):
        run = workbench.Run(b.folder(hm, *a, **k))
        return read(run, R.spec(run))

    def priced():
        o = options({"options": {"components": {"A": {}, "B": {"group": "t"}, "C": {"group": "t"}, "D": {"group": "u"}, "E": {"group": "u"}},
                                 "packages": {"generate": True},
                                 "cost_items": [{"component": "B", "basis": "per_commodity", "value": 4.0, "people_per_commodity": 2.0},
                                                {"component": "B", "basis": "per_outcome", "value": 99.0},
                                                {"component": "C", "basis": "per_commodity", "value": 3.0, "people_per_commodity": 1.0, "times": 2},
                                                {"component": "D", "basis": "per_person", "value": 1.0},
                                                {"component": "E", "basis": "per_unit", "value": 5.0}]}})
        need(o["groups"]["t"]["price"] == {"B": 2.0, "C": 6.0} and o["groups"]["t"]["why"] is None,
             f"an option's price per head: value x times / people per commodity, model-dependent items left out ({o['groups']['t']})")
        need("different bases" in o["groups"]["u"]["why"], "options priced on different bases are not ranked")
        need((_rank("A+C", o), _rank("A+B", o), _rank("A", o), _rank("B+D", o), _rank("A+E", o)) == (1, 2, None, None, None),
             "rank 1 is the dearest option; none with no option, several groups, or a group that cannot be ranked")
        need(parts("none", o) == [] and parts("A+Z", o) is None and parts("A+B", None) is None, "a package's parts; an unknown component is not guessed at")
        got = dearest({"1": "A+C", "2": "A+C", "3": "A+B", "4": "A"}, o)
        need(len(got) == 1 and got[0]["group"] == "t" and got[0]["option"] == "C" and (got[0]["units"], got[0]["hits"]) == (3, 2),
             f"the dearest option among the units whose plan holds the group ({got})")
        need(dearest({"1": "A+Z"}, o) is None, "a plan naming an unknown package cannot be priced")
        need(options({"options": {"components": {"A": {}}, "packages": {"p": ["A"], "q": None}}})["defs"] == {"p": ["A"], "q": []},
             "explicit package definitions are read from the spec")

    def clean():
        m = model()
        f = m["facts"]
        need(f["n_units"] == 12 and f["n_packages_in_plan"] == 2 and close(f["dearest_share"], 0.25) and f["n_tests_no_change"] == f["n_tests"] == 9,
             f"facts of the clean run ({f})")
        need([(r["package"], r["units"], r["cost_per_unit_rank"]) for r in m["plan_rows"]] == [("A+B", 9, 2), ("A+C", 3, 1)], f"plan rows ({m['plan_rows']})")
        need(m["fixed"] == [fx.HELD] and m["dominated"] == ["p3"] and m["allowance"] == fx.ALLOWANCE, "fixed inputs, prior-dominated inputs, the allowance")
        need(next(r for r in m["burden"] if r["id"] == "ref_G3")["tests"] == ["DT10"] and all(v is None for v in m["why"].values()),
             "a reference names the burden-scale tests of the signed spec that name it")
        need(m["outcomes"][0] is m["headline"] and m["headline"]["outcome"] == fx.OUTCOME and m["headline_name"] == fx.HEADLINE, "the headline is the first outcome row")

    def missing():
        d = b.edited(hm, lambda folder: [os.remove(os.path.join(folder, rel)) for rel in (R.POINT, R.VOI, R.STAB)])
        run = workbench.Run(d)
        m = read(run, R.spec(run))
        need(m["plan"] is None and m["voi"] is None and m["stability"] is None and m["facts"]["n_units"] is None and m["facts"]["evpi"] is None
             and all(R.absent(m["why"][rel]) for rel in (R.POINT, R.VOI, R.STAB)) and m["stability_rows"] and m["tests"] is not None,
             "a missing file leaves its parts null with the reason; the rest is read")
        m = model("tests_unparsable")
        need(m["tests"] is None and m["facts"]["n_tests"] is None and m["why"][R.DT], "a tests file that cannot be parsed: no rows and no count")

    b.case("K1 an option's price and rank from the signed spec's cost items; a plan's dearest share per group", priced)
    b.case("K2 the model of the clean fixture run", clean)
    b.case("K3 fail closed: a missing or unparsable file leaves null parts with the reason", missing)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: outcomes_package.py --self-test")
