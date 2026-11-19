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
 groups      computed here from the verified signed spec and the units table it names (gap H6), by the producer's
             own arithmetic and without importing it (toolkit/analysis/red_flag.py flags(), toolkit/alloc/costs.py
             item_cost and _factor; the lines are quoted at item_cost and options below): the options of a group are
             the components that share options.components.<c>.group; an option's cost in a unit is the sum of its
             cost items that do not depend on the model (basis other than per_outcome), each value times its price
             factor, the unit's population on the item's basis (the eligible population and the reach share for
             per_eligible and per_reached; people per commodity for per_commodity), its share, times and years.
             Where a recorded input is missing the group has a `why` and dearest_wins is not_available.
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


PER_UNIT = ("per_unit", "fixed")                      # toolkit/alloc/costs.py PER_UNIT: a fixed amount per unit
PER_HEAD = ("per_person", "per_eligible", "per_reached", "per_commodity")
PRICE_RULES = ("as_published", "index", "local_index")


def _float(x):
    """x as the producer reads a number from the spec or a cell of the units table (float(x)), or None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and abs(v) != float("inf") else None


def _factor(item, prices):
    """toolkit/alloc/costs.py _factor: 1 for a price used as published; else the base year's index over the price
    year's (`idx[b] / idx[py]`), or through the local currency (`xr[py] / xr[b] * li[b] / li[py]`). None when the
    signed spec does not hold what the rule needs."""
    rule = item.get("price_rule")
    if rule == "as_published":
        return 1.0
    b, py = str(at(prices, "base_year")), str(item.get("price_year"))
    try:
        if rule == "local_index":
            xr = {str(k): float(v) for k, v in prices["local"]["exchange_rate"].items()}
            li = {str(k): float(v) for k, v in prices["local"]["index"].items()}
            return xr[py] / xr[b] * li[b] / li[py]
        if rule == "index":
            idx = {str(k): float(v) for k, v in prices["index"].items()}
            return idx[b] / idx[py]
    except (KeyError, TypeError, ValueError, ZeroDivisionError, AttributeError):
        pass
    return None


def item_cost(item, unit, opts):
    """(one cost item's cost in one unit, None) or (None, why), by the producer's own arithmetic (toolkit/alloc/costs.py
    item_cost, which toolkit/analysis/red_flag.py calls for each item whose basis is not per_outcome):
        elif basis in PER_UNIT: q = 1.0
        else:
            pop_name = cd["eligible"] if basis in ("per_eligible", "per_reached") else it.get("population", "total")
            q = float(unit[pops[pop_name]])
            if basis == "per_reached": q *= float(cd["reach_share"])
            if basis == "per_commodity": q /= float(it["people_per_commodity"])
        return (float(it["value"]) * _factor(it, spec["prices"]) * q * float(it.get("share", 1.0))
                * float(it.get("times", 1.0)) * float(it.get("years", 1.0)))
    `unit` is the unit's row of the units table; pops is the signed spec's options.populations (population name to
    column), cd the component's entry. Anything the recorded inputs do not hold gives the reason, never a number."""
    iid, basis, cd = item.get("id") or item.get("component"), item.get("basis"), opts["defs_of"].get(str(item.get("component"))) or {}
    value, factor = _float(item.get("value")), _factor(item, opts["prices"])
    if value is None:
        return None, f"cost item {iid} has no value"
    if factor is None:
        return None, f"cost item {iid} has the price rule {item.get('price_rule')!r} and the signed spec's options.prices does not hold what it needs"
    if basis in PER_UNIT:
        q = 1.0
    elif basis in PER_HEAD:
        name = cd.get("eligible") if basis in ("per_eligible", "per_reached") else item.get("population", "total")
        col = opts["pops"].get(name) if isinstance(name, str) else None
        if col is None:
            return None, (f"cost item {iid} is priced {basis.replace('_', ' ')} and " + (f"component {item.get('component')} names no eligible population (eligible)"
                          if name is None else f"the signed spec's options.populations has no population {name}"))
        q = _float(unit.get(col))
        if q is None:
            return None, f"the units table has no number in column {col} for a unit"
        for key, src, on in (("reach_share", cd, basis == "per_reached"), ("people_per_commodity", item, basis == "per_commodity")):
            if on:
                x = _float(src.get(key))
                if x is None or (key == "people_per_commodity" and x == 0):
                    return None, f"cost item {iid} is priced {basis.replace('_', ' ')} and {'component ' + str(item.get('component')) if src is cd else 'it'} has no {key}"
                q = q * x if key == "reach_share" else q / x
    else:
        return None, f"cost item {iid} has the basis {basis!r}, which the producer's cost code does not price per unit"
    for key in ("share", "times", "years"):
        if item.get(key) is not None:
            x = _float(item[key])
            if x is None:
                return None, f"cost item {iid} has no number for {key}"
            q *= x
    return value * factor * q, None


def options(doc, table=None, table_why=None):
    """What the verified signed spec says of the options, or None with no verified spec or no components:
    {components: [names in the spec's order], defs: {package: [components]} or None when packages are generated,
     groups: {group: {options: [components, in the spec's order], cost: {unit: {component: cost}}, why}}}.
    `table` is the units table ({unit id: row}); each option's cost in each unit is the sum of its cost items that do
    not depend on the model, as the producer sums them (red_flag.py: `cost = {c: sum(costs.item_cost(it, u, alloc) for
    it in fixed if it["component"] == c) for c in cs}`; an option with no such item costs nothing). `why` says what
    could not be reproduced from the recorded inputs; such a group has no cost."""
    comps = at(doc, "options", "components")
    if not isinstance(comps, dict) or not comps:
        return None
    packages = at(doc, "options", "packages")
    defs = None
    if isinstance(packages, dict) and packages.get("generate") is not True:
        defs = {str(k): [str(c) for c in (v or [])] for k, v in packages.items() if isinstance(v, (list, type(None)))}
    items = [i for i in dicts(at(doc, "options", "cost_items")) if i.get("basis") != "per_outcome"]
    pops = at(doc, "options", "populations")
    opts = {"components": [str(c) for c in comps], "defs": defs, "groups": {}, "pops": pops if isinstance(pops, dict) else {},
            "prices": at(doc, "options", "prices"), "defs_of": {str(c): d for c, d in comps.items() if isinstance(d, dict)}}
    groups = opts["groups"]
    for c, d in comps.items():
        if isinstance(d, dict) and d.get("group") is not None:
            groups.setdefault(str(d["group"]), {"options": [], "cost": {}, "why": None})["options"].append(str(c))
    for name, grp in list(groups.items()):
        if len(grp["options"]) < 2:
            del groups[name]
            continue
        if table is None:
            grp["why"] = table_why or "the units table was not read"
            continue
        own = {c: [i for i in items if str(i.get("component")) == c] for c in grp["options"]}
        for u, row in table.items():
            cost = {}
            for c in grp["options"]:
                got = [item_cost(i, row, opts) for i in own[c]]
                bad = next((w for v, w in got if v is None), None)
                if bad:
                    grp["why"] = bad
                    break
                cost[c] = sum(v for v, _ in got)
            if grp["why"]:
                grp["cost"] = {}
                break
            grp["cost"][u] = cost
    return opts


def parts(package, opts):
    """A package's components, or None when one of them is not a component of the signed spec."""
    if opts is None:
        return None
    if opts["defs"] is not None:
        got = opts["defs"].get(package)
    else:
        got = [] if package == "none" else package.split("+")
    return got if got is not None and all(c in opts["components"] for c in got) else None


def _top(grp, cost):
    """The dearest option in one unit, as the producer names it (`max(cs, key=lambda c: cost[c])`: the first of the
    spec's order among equals), or None where the unit has no price difference (`len(set(cost.values())) < 2`)."""
    return None if len(set(cost.values())) < 2 else max(grp["options"], key=lambda c: cost[c])


def _rank(package, opts, plan):
    """1 where the package's option is the dearest of its group in every unit given the package (2 the next, by the
    distinct costs in the unit); None where the package holds no option of a group, options of several groups, an
    option of a group that cannot be priced, or where the rank differs between those units."""
    cs = parts(package, opts)
    held = [(name, c) for name, grp in (opts or {"groups": {}})["groups"].items() for c in cs or [] if c in grp["options"]]
    if len(held) != 1 or opts["groups"][held[0][0]]["why"]:
        return None
    grp, c = opts["groups"][held[0][0]], held[0][1]
    costs = [grp["cost"].get(u) for u, p in plan.items() if p == package]
    ranks = {1 + sorted(set(k.values()), reverse=True).index(k[c]) for k in costs if k is not None and len(set(k.values())) > 1}
    return ranks.pop() if len(ranks) == 1 and all(k is not None for k in costs) else None


def dearest(plan, opts):
    """Per group whose options can be priced: {group, option (the dearest in the most units), units (units whose plan
    holds an option of the group and where prices differ), hits (of them, those given their dearest option), share},
    counted as toolkit/analysis/red_flag.py flags() counts (`n += 1; hits += chosen[0] == max(cs, key=...)`). None
    when the plan names a package the signed spec does not define or there is no verified spec. A group gets a `why`,
    and no row, when a unit of the plan is not in the units table or a package holds two of its options."""
    if opts is None or plan is None or any(parts(p, opts) is None for p in set(plan.values())):
        return None
    out = []
    for name, grp in sorted(opts["groups"].items()):
        if grp["why"]:
            continue
        n, hits, tops = 0, 0, {}
        for u, p in plan.items():
            chosen = [c for c in parts(p, opts) if c in grp["options"]]
            if not chosen:
                continue
            if len(chosen) > 1 or u not in grp["cost"]:
                grp["why"] = (f"package {p} holds {len(chosen)} options of the group" if len(chosen) > 1 else
                              f"unit {u} of the plan is not in the units table")
                break
            top = _top(grp, grp["cost"][u])
            if top is None:
                continue
            n, hits = n + 1, hits + (chosen[0] == top)
            tops[top] = tops.get(top, 0) + 1
        if n and not grp["why"]:
            out.append({"group": name, "option": max(tops, key=tops.get), "units": n, "hits": hits, "share": hits / n})
    return out


def plan_clash(P):
    """Why the point file's plan contradicts its own record of the units, or None: data.plan against data.n_units and
    against data.per_unit (the same unit ids, each with the same package). Counts that must agree are checked before
    any rule reads the plan (§3: a record that contradicts itself)."""
    plan, per, n = P.get("plan"), P.get("per_unit"), num(P.get("n_units"))
    if not isinstance(plan, dict):
        return None
    if n is not None and n != len(plan):
        return f"data.plan holds {len(plan)} units and data.n_units is {int(n)}"
    if isinstance(per, dict):
        a, b = {str(u) for u in plan}, {str(u) for u in per}
        if a != b:
            return (f"data.plan holds {len(a)} units and data.per_unit {len(b)}; {len(a ^ b)} are in one and not the other "
                    f"({R.some(sorted(a ^ b), 4)})")
        packs = {str(u): at(v, "package") for u, v in per.items()}
        off = [str(u) for u, p in plan.items() if packs[str(u)] is not None and packs[str(u)] != p]
        if off:
            return f"{len(off)} unit(s) have one package in data.plan and another in data.per_unit ({R.some(off, 4)})"
    return None


def _plan(P, opts):
    plan = P.get("plan")
    if not isinstance(plan, dict) or not plan or not all(isinstance(p, str) for p in plan.values()):
        return None, None, None
    plan = {str(u): p for u, p in plan.items()}
    best = dearest(plan, opts)          # before the ranks: it may find a group that cannot be priced for this plan
    per = {str(k): v for k, v in P["per_unit"].items()} if isinstance(P.get("per_unit"), dict) else {}     # a unit id may be read as a number
    names = sorted(set(plan.values()), key=lambda p: (-sum(1 for x in plan.values() if x == p), p))
    rows = []
    for p in names:
        units = [u for u, x in plan.items() if x == p]
        costs = [num(at(per.get(u), "cost")) for u in units]
        rows.append({"package": p, "units": len(units), "share": len(units) / len(plan),
                     "cost": None if any(c is None for c in costs) else float(sum(costs)), "cost_per_unit_rank": _rank(p, opts, plan)})
    return plan, rows, best


def _tests(run, s):
    D, why = R.data(run, R.DT)          # the adapter boundary: a file of another schema is not handed to the loader
    loaded, why = checks.decision_tests(run, s) if D is not None else (None, why)
    if loaded is None:
        return None, why, None, None

    def ask(tid, t):
        if t["state"] != "ran":
            return None, None
        doc, _ = run.large(f"{R.TESTS_DIR}{tid}.yaml")
        return (text(doc.get("question")), text(doc.get("varied"))) if doc is not None else (None, None)
    rows, link_why = R.test_rows(run, s, loaded, ask)
    return R.order(rows), None, link_why, R.tie(loaded)


def _voi(V, dominated, listed):
    rows = []
    for e in listed:
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


def _variants(PV, listed):
    rows, outcome = [], PV.get("outcome")
    for e in listed:
        _, low, high, _ = _summary(at(e, "uncertainty", "averted", outcome))
        rows.append({"id": text(str(e.get("id")), 200), "outcome": num(e.get("objective")), "low": low, "high": high,
                     "cost": num(e.get("cost")), "value_lost_pct": num(e.get("value_lost_pct")),
                     "over_budget_share": num(at(e, "uncertainty", "affordability", "share_over_budget")),
                     "units_changed": num(e.get("units_changed")), "status": e.get("status"), "what": text(e.get("text"), 600)})
    return rows


def _expected(e, low, high):
    """(the ratio the row's own numbers give, the word the producer's rule gives them) for a reference judged on a
    ratio band, else (None, None). toolkit/analysis/burden.py verdict(): `if row.get("kind") == "reported": return
    "information"`, then for `tol.get("type") == "ratio"`: `return "within" if b[0] <= p / v <= b[1] else "outside"`,
    with p the model's point value (model.point) and v the reference's value."""
    if e.get("kind") == "reported" or at(e, "tolerance", "type") != "ratio" or low is None or high is None:
        return None, None
    p, v = num(at(e, "model", "point")), num(e.get("value"))
    ratio = p / v if p is not None and v is not None and v > 0 else num(e.get("ratio"))
    return (None, None) if ratio is None else (ratio, "within" if low <= ratio <= high else "outside")


def _burden(B, s, listed):
    naming = {}
    for tid, e in R.entries(s).items():
        if e.get("type") == "burden_scale":
            for ref in at(e, "burden_scale", "references") or []:
                naming.setdefault(str(ref), []).append(tid)
    rows = []
    for e in listed:
        band = at(e, "tolerance", "ratio_band")
        low, high = (num(band[0]), num(band[1])) if isinstance(band, list) and len(band) == 2 else (None, None)
        own, word = _expected(e, low, high)
        said = num(e.get("ratio"))
        clash = None
        if word is not None and e.get("status") in ("within", "outside") and e.get("status") != word:
            clash = f"recorded as {e.get('status')}, and its ratio {R.r3(own)} against the band {R.g(low)} to {R.g(high)} is {word}"
        elif own is not None and said is not None and abs(own - said) > 1e-6 * max(1.0, abs(own)):
            clash = f"its recorded ratio {R.r3(said)} is not its model value over its reference value ({R.r3(own)})"
        rows.append({"clash": clash,"id": text(str(e.get("id")), 200), "group": text(str(e.get("group")), 200) if e.get("group") is not None else None,
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
    opts = options(s["doc"], *R.units(run, s["doc"])) if s["verified"] else None
    m = {**R.empty("package_per_unit", s), "why": why, "outcome": key, "headline_name": name, "options": opts, "point": P,
         "allowance": num(at(s["doc"], "uncertainty", "affordability", "max_share_over_budget")) if s["verified"] else None,
         "provenance": R.provenance(run, R.POINT)}
    m["tests"], why[R.DT], m["link_why"], m["tie"] = _tests(run, s)
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
        m["plan_clash"] = plan_clash(P)
        if m["plan_clash"]:
            why[R.POINT] = f"{R.POINT} contradicts itself ({m['plan_clash']}), so no plan is read from it"
        else:
            m["plan"], m["plan_rows"], m["dearest"] = _plan(P, opts)
    if m["plan"] is not None:
        names = opts["components"] if opts else list(dict.fromkeys(c for p in m["plan"].values() for c in ([] if p == "none" else p.split("+"))))
        held = {p: (parts(p, opts) if opts else ([] if p == "none" else p.split("+"))) for p in set(m["plan"].values())}
        m["components"] = [{"component": c, "units": sum(1 for p in m["plan"].values() if c in (held[p] or []))} for c in names]
    if U is not None:
        d = U.get("prior_dominated") if isinstance(U.get("prior_dominated"), list) else at(U, "interval", "prior_dominated")
        m["dominated"] = [str(x) for x in d] if isinstance(d, list) else []
        fixed = [str(x) for k in ("spec_fixed", "fixed") for x in (at(U, "interval", k) or []) if isinstance(at(U, "interval", k), list)]
        fixed += [str(e["name"]) for e in dicts(at(U, "draw_plan", "fixed")) if e.get("name") is not None]
        m["fixed"] = list(dict.fromkeys(fixed))
    def listed(D, key, rel):       # a row list at the adapter boundary: whole, or not read
        got, bad = R.rows(D, key, rel)
        why[rel] = why[rel] or bad
        return got
    if V is not None and listed(V, "inputs", R.VOI) is not None:
        m["voi"] = _voi(V, m["dominated"], V["inputs"])
    if PV is not None and listed(PV, "variants", R.PV) is not None:
        m["variants"] = _variants(PV, PV["variants"])
    if B is not None and listed(B, "references", R.BV) is not None:
        m["burden"] = _burden(B, s, B["references"])
    if EB is not None and listed(EB, "rows", R.EMB) is None:
        EB = None
    if S is not None:
        m["stability"] = {"verdict": S.get("verdict"), "value_verdict": S.get("value_verdict"),
                          "headline_metric": text(S.get("headline_metric"), 200), "method": text(S.get("method"), 200),
                          "reason": text(S.get("reason"), 600)}
    if S is not None or U is not None:
        m["stability_rows"] = _stability(S, U)
    if E is not None or EB is not None:
        m["emulator"] = _emulator(E, EB)
    if EB is not None:
        rows = EB["rows"]
        m["boundary"] = {"packages": [str(r[k]) for r in rows for k in ("chosen", "runner_up") if r.get(k) is not None], "units": len(rows),
                         "share_matched": num(EB.get("share_matched")), "threshold": num(EB.get("threshold")), "passes": EB.get("passes")}
    t, v, b = m["tests"], m["voi"], m["burden"]
    best = max(m["dearest"], key=lambda r: r["share"]) if m["dearest"] else None
    sound = b is not None and not any(r["clash"] for r in b)       # a reference whose word contradicts its numbers is not counted by its word
    m["facts"] = {
        "deliverable_kind": "package_per_unit", "headline_outcome": name, "headline_point": point, "headline_median": med,
        "headline_low": low, "headline_high": high, "interval_level": level, "n_draws": num(at(U, "draws")),
        "comparator_total": total, "share_of_comparator": m["headline"]["share"], "n_units": num(at(P, "n_units")),
        "n_packages_in_plan": None if m["plan_rows"] is None else len(m["plan_rows"]),
        "cost_allocated": num(at(P, "cost_allocated")), "budget_allocatable": num(at(P, "budget_allocatable")),
        "reserve": num(at(P, "reserve", "reserve_usd")), "cost_per_outcome": num(at(P, "cost_per_outcome_averted")),
        **R.test_counts(m), "voi_status": None if v is None else v["status"], "evpi": None if v is None else v["evpi"],
        "evpi_share": None if v is None or v["evpi_pct"] is None else v["evpi_pct"] / 100,
        "n_inputs": None if v is None else len(v["rows"]), "n_inputs_supported": None if v is None else sum(r["supported"] for r in v["rows"]),
        "n_prior_dominated": None if m["dominated"] is None else len(m["dominated"]),
        "stability_verdict": None if m["stability"] is None else m["stability"]["verdict"],
        "units_same_package_share": num(at(U, "units_same_package_share_median")),
        "n_units_stable": num(at(U, "units_same_package_in_90pct_of_draws")),
        "n_variants": None if m["variants"] is None else len(m["variants"]),
        "n_burden_refs": None if b is None else len(b), "n_burden_outside": sum(r["state"] == "outside" for r in b) if sound else None,
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
        spec = {"options": {"populations": {"total": "pop", "few": "elig"},
                            "components": {"A": {}, "B": {"group": "t"}, "C": {"group": "t"}, "D": {"group": "u", "eligible": "few", "reach_share": 0.5},
                                           "E": {"group": "u"}, "F": {"group": "w"}, "G": {"group": "w", "eligible": "few"}},
                            "packages": {"generate": True}, "prices": {"base_year": 2025, "index": {"2020": 100.0, "2025": 125.0}},
                            "cost_items": [{"component": "B", "basis": "per_commodity", "value": 4.0, "people_per_commodity": 2.0, "price_rule": "as_published"},
                                           {"component": "B", "basis": "per_outcome", "value": 99.0},
                                           {"component": "C", "basis": "per_commodity", "value": 3.0, "people_per_commodity": 1.0, "times": 2, "price_rule": "as_published"},
                                           {"component": "D", "basis": "per_reached", "value": 8.0, "price_rule": "index", "price_year": 2020},
                                           {"component": "E", "basis": "per_unit", "value": 450.0, "price_rule": "as_published"},
                                           {"component": "F", "basis": "per_person", "value": 1.0, "price_rule": "as_published"},
                                           {"component": "G", "basis": "per_reached", "value": 1.0, "price_rule": "as_published"}]}}
        table = {"1": {"pop": "100", "elig": "10"}, "2": {"pop": "100", "elig": "10"}, "3": {"pop": "100", "elig": "5"}, "4": {"pop": "100", "elig": "0"}}
        o = options(spec, table)
        need(o["groups"]["t"]["cost"]["1"] == {"B": 200.0, "C": 600.0} and o["groups"]["t"]["why"] is None,
             f"a unit's cost: value x population / people per commodity x times; model-dependent items left out ({o['groups']['t']})")
        need(o["groups"]["u"]["cost"]["1"] == {"D": 50.0, "E": 450.0} and o["groups"]["u"]["cost"]["3"]["D"] == 25.0,
             f"per person reached: value x the price index x the eligible population x the reach share; a fixed amount per unit ({o['groups']['u']['cost']})")
        need("reach_share" in o["groups"]["w"]["why"] and o["groups"]["w"]["cost"] == {}, f"a price per person reached with no reach share is not guessed ({o['groups']['w']['why']})")
        plan = {"1": "A+C", "2": "A+C", "3": "A+B", "4": "A"}
        need((_rank("A+C", o, plan), _rank("A+B", o, plan), _rank("A", o, plan), _rank("B+D", o, {"1": "B+D"}), _rank("A+G", o, {"1": "A+G"})) == (1, 2, None, None, None),
             "rank 1 is the dearest option; none with no option, several groups, or a group that cannot be priced")
        need(parts("none", o) == [] and parts("A+Z", o) is None and parts("A+B", None) is None, "a package's parts; an unknown component is not guessed at")
        got = dearest(plan, o)
        need(len(got) == 1 and got[0]["group"] == "t" and got[0]["option"] == "C" and (got[0]["units"], got[0]["hits"]) == (3, 2),
             f"the dearest option among the units whose plan holds the group ({got})")
        need(dearest({"1": "A+Z"}, o) is None, "a plan naming an unknown package cannot be priced")
        o = options(spec, table)
        got = dearest({"1": "E", "2": "E", "3": "D", "4": "D"}, o)
        need(got[0]["group"] == "u" and (got[0]["units"], got[0]["hits"], got[0]["option"]) == (4, 2, "E"), f"the dearest is judged unit by unit ({got})")
        o = options(spec, table)
        need(dearest({"9": "A+B"}, o) == [] and "not in the units table" in o["groups"]["t"]["why"], "a unit of the plan that the units table does not hold is not priced")
        o = options(spec, None, "the units table x could not be read")
        need(all(g["why"] for g in o["groups"].values()) and dearest(plan, o) == [], "with no units table no group is priced")
        flat = options({"options": {"populations": {"total": "pop"}, "components": {"B": {"group": "t"}, "C": {"group": "t"}}, "cost_items": []}}, table)
        need(flat["groups"]["t"]["why"] is None and dearest({"1": "B"}, flat) == [], "options with no cost item cost nothing each: no price difference, no dearest (as the producer counts)")
        need(options({"options": {"components": {"A": {}}, "packages": {"p": ["A"], "q": None}}})["defs"] == {"p": ["A"], "q": []},
             "explicit package definitions are read from the spec")
        need(plan_clash({"plan": {"1": "A"}, "n_units": 2}) and plan_clash({"plan": {"1": "A"}, "per_unit": {"2": {}}})
             and plan_clash({"plan": {"1": "A"}, "per_unit": {"1": {"package": "B"}}}) and plan_clash({"plan": {"1": "A"}, "n_units": 1, "per_unit": {1: {"package": "A"}}}) is None,
             "a plan against its own record of the units")

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
