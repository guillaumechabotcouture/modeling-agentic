#!/usr/bin/env python3
"""Toolkit, "budget allocation across units" template: costs (T1).

    python3 toolkit/alloc/costs.py <spec.yaml> [--units <csv>] [--json]
    python3 toolkit/alloc/costs.py --self-test

E7 attempt 7's `models/costs.py` was the most finding-dense file of the run:
- the SMC drug counted twice beside an all-in cost (R-024);
- case-management cost charged on a population the effect did not reach
  (R-026, D-021, R-031);
- service costs left in mixed price years (R-039, D-030).
Each run rewrote this. Here the costs are declared in the question spec, and
those error classes fail validation before any number is computed.

Spec (the `units`, `components`, `packages`, `prices` and `cost_items`
sections of the template spec):

    units:
      csv: data/units.csv
      id: lga_id
      populations: {total: pop_2024, u5: pop_u5_2024, smc_eligible: pop_smc_3_59m}
    components:
      net_dual_ai: {group: net}          # at most one component per group in a package
      smc: {eligible: smc_eligible}       # the population the component reaches
      cm_u5: {eligible: u5, reach_share: 0.54}
    packages:                             # explicit, or {generate: true} for every combination
      none: []
      dual: [net_dual_ai]
    prices:
      base_year: 2024
      index_name: US CPI-U annual average
      index: {2015: 237.0, 2021: 271.0, 2024: 313.7}
      local:                              # for price_rule local_index (Turner et al. 2019, method 3)
        currency: NGN
        exchange_rate: {2021: 401.2, 2024: 1479.0}   # local currency per USD
        index: {2021: 354.3, 2024: 699.4}            # local consumer price index
    cost_items:
      - id: smc_all_in
        component: smc
        value: 0.94
        price_year: 2021
        price_rule: index                 # index: base-currency index; local_index: through the local currency
                                          # (source-year rate, local inflation, base-year rate); as_published
        basis: per_eligible               # per_person | per_eligible | per_reached | per_commodity | fixed
        times: 4                          # e.g. cycles per year
        years: 3                          # charged per year over the window (default 1: once)
        share: 1.0                        # share of the basis population charged (e.g. sprayed structures)
        includes: [drug, delivery]        # what the value covers: two items of one component may not overlap
        people_per_commodity: 1.8         # per_commodity only

Validation (fail closed, before computing):
- unknown components and missing population fields;
- an index price rule without index entries for its price year and the base year;
- two items of one component whose `includes` overlap (a double count);
- per_eligible and per_reached items charged on a population other than the
  component's own eligible population, so a targeted intervention cannot be
  costed on everyone;
- a packaged component with no cost item, unless it declares
  `cost_free: <reason>`;
- a package with two components of one group.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import os
import sys
import tempfile

import yaml

BASES = ("per_person", "per_eligible", "per_reached", "per_commodity", "fixed")
RULES = ("index", "local_index", "as_published")


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def packages(spec: dict) -> dict[str, frozenset]:
    """Package name -> its components (explicit, or every combination with at most one per group)."""
    comps = spec.get("components") or {}
    p = spec.get("packages") or {}
    if isinstance(p, dict) and p.get("generate") is True:
        groups: dict[str, list] = {}
        for c, d in comps.items():
            groups.setdefault(str((d or {}).get("group") or c), []).append(c)
        out = {}
        for pick in itertools.product(*[[None] + sorted(v) for _k, v in sorted(groups.items())]):
            cs = frozenset(c for c in pick if c)
            out["+".join(sorted(cs)) or "none"] = cs
        return out
    return {str(k): frozenset(str(c) for c in (v or [])) for k, v in (p or {}).items()}


def validate(spec: dict, unit_fields: set[str] | None = None) -> list[str]:
    errs = []
    comps = spec.get("components")
    if not isinstance(comps, dict) or not comps:
        return ["components: a mapping of every intervention component is required"]
    pops = ((spec.get("units") or {}).get("populations") or {})
    if not isinstance(pops, dict) or "total" not in pops:
        errs.append("units.populations: map population names to unit columns, including `total`")
        pops = {}
    if unit_fields is not None:
        errs += [f"units.populations.{k}: column {v} is not in the units table" for k, v in pops.items()
                 if v not in unit_fields]
    for c, d in comps.items():
        d = d or {}
        if d.get("eligible") and d["eligible"] not in pops:
            errs.append(f"components.{c}: eligible population {d['eligible']} is not in units.populations")
        if d.get("reach_share") is not None and not (_num(d["reach_share"]) is not None and 0 < d["reach_share"] <= 1):
            errs.append(f"components.{c}: reach_share must be in (0, 1]")
    pk = packages(spec)
    if not pk:
        errs.append("packages: at least one package is required")
    for name, cs in pk.items():
        unknown = sorted(cs - set(comps))
        if unknown:
            errs.append(f"packages.{name}: unknown components {', '.join(unknown)}")
        grp = [str((comps.get(c) or {}).get("group") or c) for c in cs]
        dup = sorted({g for g in grp if grp.count(g) > 1})
        if dup:
            errs.append(f"packages.{name}: more than one component of group {', '.join(dup)}")
    pr = spec.get("prices") or {}
    base, idx = pr.get("base_year"), {str(k): v for k, v in (pr.get("index") or {}).items()}
    items = spec.get("cost_items")
    if not isinstance(items, list) or not items:
        return errs + ["cost_items: a non-empty list is required"]
    seen, by_comp = set(), {}
    for it in items:
        if not isinstance(it, dict):
            errs.append("cost_items: an entry is not a mapping")
            continue
        iid, c = str(it.get("id") or "?"), str(it.get("component") or "")
        if iid in seen:
            errs.append(f"cost_items.{iid}: duplicate id")
        seen.add(iid)
        if c not in comps:
            errs.append(f"cost_items.{iid}: unknown component {c or '(none)'}")
            continue
        by_comp.setdefault(c, []).append(it)
        if _num(it.get("value")) is None or it["value"] < 0:
            errs.append(f"cost_items.{iid}: value must be a non-negative number")
        basis, rule = it.get("basis"), it.get("price_rule")
        if basis not in BASES:
            errs.append(f"cost_items.{iid}: basis must be one of {', '.join(BASES)}")
        if rule not in RULES:
            errs.append(f"cost_items.{iid}: price_rule must be one of {', '.join(RULES)}")
        if rule == "index":
            py = str(it.get("price_year"))
            if py not in idx or str(base) not in idx:
                errs.append(f"cost_items.{iid}: price_rule index needs prices.index entries for its price year "
                            f"{py} and the base year {base}")
        elif rule == "local_index":
            py, loc = str(it.get("price_year")), (pr.get("local") or {})
            xr = {str(k): v for k, v in (loc.get("exchange_rate") or {}).items()}
            li = {str(k): v for k, v in (loc.get("index") or {}).items()}
            miss = [f"{n} {y}" for n, t in (("exchange_rate", xr), ("index", li)) for y in (py, str(base)) if y not in t]
            if miss:
                errs.append(f"cost_items.{iid}: price_rule local_index needs prices.local entries: {', '.join(miss)}")
        elif it.get("price_year") is None:
            errs.append(f"cost_items.{iid}: price_year is required (as published: the year the price is quoted for)")
        cd = comps[c] or {}
        if basis in ("per_eligible", "per_reached"):
            if not cd.get("eligible"):
                errs.append(f"cost_items.{iid}: {basis} needs components.{c}.eligible")
            elif it.get("population") not in (None, cd["eligible"]):
                errs.append(f"cost_items.{iid}: charged on {it['population']}, but {c} reaches {cd['eligible']}: "
                            "cost and benefit must be on the same population")
        if basis == "per_reached" and cd.get("reach_share") is None:
            errs.append(f"cost_items.{iid}: per_reached needs components.{c}.reach_share")
        if basis in ("per_person", "per_commodity") and it.get("population", "total") not in pops:
            errs.append(f"cost_items.{iid}: population {it.get('population')} is not in units.populations")
        if basis == "per_commodity" and not (_num(it.get("people_per_commodity")) or 0) > 0:
            errs.append(f"cost_items.{iid}: per_commodity needs people_per_commodity > 0")
        for k in ("times", "years", "share"):
            if it.get(k) is not None and not (_num(it[k]) is not None and it[k] > 0):
                errs.append(f"cost_items.{iid}: {k} must be > 0")
        if not isinstance(it.get("includes"), list) or not it["includes"]:
            errs.append(f"cost_items.{iid}: includes must list what the value covers (e.g. [drug, delivery])")
    for c, its in by_comp.items():
        for a, b in itertools.combinations(its, 2):
            both = sorted(set(map(str, a.get("includes") or [])) & set(map(str, b.get("includes") or [])))
            if both:
                errs.append(f"cost_items.{a.get('id')} and {b.get('id')} both include {', '.join(both)} for {c}: "
                            "a double count")
    used = set().union(*pk.values()) if pk else set()
    for c in sorted(used - set(by_comp)):
        if not str((comps.get(c) or {}).get("cost_free") or "").strip():
            errs.append(f"components.{c}: no cost item; give one, or `cost_free: <reason>`")
    return errs


def _factor(it: dict, prices: dict) -> float:
    if it["price_rule"] == "as_published":
        return 1.0
    b, py = str(prices["base_year"]), str(it["price_year"])
    if it["price_rule"] == "local_index":
        loc = prices["local"]
        xr = {str(k): float(v) for k, v in loc["exchange_rate"].items()}
        li = {str(k): float(v) for k, v in loc["index"].items()}
        return xr[py] / xr[b] * li[b] / li[py]
    idx = {str(k): float(v) for k, v in prices["index"].items()}
    return idx[b] / idx[py]


def item_cost(it: dict, unit: dict, spec: dict) -> float:
    """One item's cost for one unit over the window, in base-year prices."""
    comps, pops = spec["components"], spec["units"]["populations"]
    cd = comps[it["component"]] or {}
    basis = it["basis"]
    if basis == "fixed":
        q = 1.0
    else:
        pop_name = cd["eligible"] if basis in ("per_eligible", "per_reached") else it.get("population", "total")
        q = float(unit[pops[pop_name]])
        if basis == "per_reached":
            q *= float(cd["reach_share"])
        if basis == "per_commodity":
            q /= float(it["people_per_commodity"])
    return (float(it["value"]) * _factor(it, spec["prices"]) * q * float(it.get("share", 1.0))
            * float(it.get("times", 1.0)) * float(it.get("years", 1.0)))


def cost_matrix(spec: dict, units: list[dict]) -> dict:
    """{unit_id: {package: {"total": $, "items": {item_id: $}}}} in base-year prices."""
    errs = validate(spec, set(units[0]) if units else None)
    if errs:
        raise ValueError("invalid cost spec: " + "; ".join(errs))
    uid = spec["units"]["id"]
    pk = packages(spec)
    out = {}
    for u in units:
        per_item = {it["id"]: item_cost(it, u, spec) for it in spec["cost_items"]}
        comp_of = {it["id"]: it["component"] for it in spec["cost_items"]}
        out[str(u[uid])] = {name: {"total": sum(v for i, v in per_item.items() if comp_of[i] in cs),
                                   "items": {i: v for i, v in per_item.items() if comp_of[i] in cs}}
                            for name, cs in pk.items()}
    return out


def load_units(spec: dict, base_dir: str, csv_path: str | None = None) -> list[dict]:
    with open(csv_path or os.path.join(base_dir, spec["units"]["csv"]), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------- self-test

def _planted() -> tuple[dict, list[dict]]:
    """A synthetic world with known costs and planted traps (design doc §3.5, planted-truth test)."""
    units = [{"id": f"u{i}", "pop": 10000 + 1000 * i, "elig": (2000 + 100 * i) if i % 2 == 0 else 0} for i in range(40)]
    spec = {
        "units": {"id": "id", "populations": {"total": "pop", "eligible": "elig"}},
        "components": {"c1": {}, "c2": {"eligible": "eligible"}},
        "packages": {"generate": True},
        "prices": {"base_year": 2024, "index": {"2015": 237.0, "2024": 313.7}},
        "cost_items": [
            {"id": "c1_delivered", "component": "c1", "value": 2.0, "price_year": 2015, "price_rule": "index",
             "basis": "per_commodity", "people_per_commodity": 2.0, "includes": ["commodity", "delivery"]},
            {"id": "c2_all_in", "component": "c2", "value": 1.0, "price_year": 2024, "price_rule": "index",
             "basis": "per_eligible", "times": 4, "includes": ["drug", "delivery"]},
        ]}
    return spec, units


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    spec, units = _planted()
    ok(validate(spec, set(units[0])) == [], f"TC1 the planted spec is valid: {validate(spec, set(units[0]))}")
    ok(set(packages(spec)) == {"none", "c1", "c2", "c1+c2"}, f"TC2 generated packages: {sorted(packages(spec))}")
    m = cost_matrix(spec, units)
    f15 = 313.7 / 237.0
    ok(abs(m["u0"]["c1"]["total"] - 2.0 * f15 * 10000 / 2.0) < 1e-6,
       f"TC3 a 2015 price converted with the index, per commodity: {m['u0']['c1']}")
    ok(abs(m["u0"]["c2"]["total"] - 1.0 * 2000 * 4) < 1e-6 and m["u1"]["c2"]["total"] == 0.0,
       f"TC4 a targeted component costed on its eligible population only: {m['u0']['c2']} {m['u1']['c2']}")
    ok(abs(m["u0"]["c1+c2"]["total"] - (m["u0"]["c1"]["total"] + m["u0"]["c2"]["total"])) < 1e-9
       and m["u0"]["none"]["total"] == 0.0, "TC5 a bundle costs the sum of its components; none costs nothing")
    total = sum(m[u["id"]]["c1+c2"]["total"] for u in units)
    want = sum(2.0 * f15 * u["pop"] / 2.0 + 4.0 * u["elig"] for u in units)
    ok(abs(total - want) < 1e-6, f"TC6 the planted total is recovered exactly: {total} vs {want}")
    # The planted traps, each must fail validation.
    dup = {**spec, "cost_items": spec["cost_items"] + [
        {"id": "c2_drug", "component": "c2", "value": 0.26, "price_year": 2024, "price_rule": "as_published",
         "basis": "per_eligible", "times": 4, "includes": ["drug"]}]}
    ok(any("double count" in e for e in validate(dup)), "TC7 a drug line beside an all-in cost is a double count (R-024)")
    wrong_pop = {**spec, "cost_items": [spec["cost_items"][0], {**spec["cost_items"][1], "population": "total"}]}
    ok(any("same population" in e for e in validate(wrong_pop)),
       "TC8 a targeted component charged on everyone fails (R-026 / D-021)")
    no_idx = {**spec, "cost_items": [{**spec["cost_items"][0], "price_year": 2019}, spec["cost_items"][1]]}
    ok(any("price year 2019" in e for e in validate(no_idx)), "TC9 a price year with no index entry fails (R-039)")
    free = {**spec, "cost_items": [spec["cost_items"][0]]}
    ok(any("components.c2: no cost item" in e for e in validate(free)), "TC10 an allocated component with no cost fails")
    ok(validate({**free, "components": {"c1": {}, "c2": {"eligible": "eligible", "cost_free": "donated"}}}) == [],
       "TC11 ... unless it is declared cost-free with a reason")
    reach = {**spec, "cost_items": [spec["cost_items"][0], {**spec["cost_items"][1], "basis": "per_reached"}]}
    ok(any("reach_share" in e for e in validate(reach)), "TC12 per_reached needs the component's reach share")
    reach["components"] = {"c1": {}, "c2": {"eligible": "eligible", "reach_share": 0.5}}
    ok(abs(cost_matrix(reach, units)["u0"]["c2"]["total"] - 0.5 * 2000 * 4) < 1e-6,
       "TC13 per_reached charges only the reached share")
    grp = {**spec, "components": {"c1": {"group": "net"}, "c1b": {"group": "net"}, "c2": {"eligible": "eligible"}},
           "packages": {"bad": ["c1", "c1b"]}}
    ok(any("more than one component of group net" in e for e in validate(grp)), "TC14 one component per group")
    ok("bad" not in packages({**grp, "packages": {"generate": True}}), "TC15 generated packages respect groups")
    ok(any("column elig" in e for e in validate(spec, {"id", "pop"})), "TC16 a population column missing from the units fails")
    loc = {**spec, "prices": {**spec["prices"], "local": {"currency": "NGN", "exchange_rate": {"2021": 401.2, "2024": 1479.0},
                                                       "index": {"2021": 354.3, "2024": 699.4}}},
           "cost_items": [spec["cost_items"][0], {**spec["cost_items"][1], "price_year": 2021, "price_rule": "local_index"}]}
    f3 = 401.2 / 1479.0 * 699.4 / 354.3      # E7 attempt 7's Turner method-3 factor for 2021, 0.535
    ok(validate(loc) == [] and abs(cost_matrix(loc, units)["u0"]["c2"]["total"] - f3 * 2000 * 4) < 1e-6,
       f"TC17 local_index converts through the local currency (Turner method 3): {validate(loc)}")
    ok(any("prices.local entries" in e for e in validate({**loc, "prices": spec["prices"]})),
       "TC18 local_index without local rates and index fails")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--units")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.spec:
        ap.error("spec required")
    spec = yaml.safe_load(open(a.spec))
    units = load_units(spec, os.path.dirname(os.path.abspath(a.spec)), a.units)
    errs = validate(spec, set(units[0]) if units else None)
    if errs:
        print("\n".join("ERROR: " + e for e in errs))
        return 1
    m = cost_matrix(spec, units)
    if a.json:
        print(json.dumps(m, indent=1))
    else:
        tot = {p: sum(m[u][p]["total"] for u in m) for p in packages(spec)}
        for p, v in sorted(tot.items(), key=lambda kv: kv[1]):
            print(f"{p}: {v:,.0f} (all units)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
