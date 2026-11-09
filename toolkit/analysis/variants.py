"""Operational plan variants (F6 S1-6): plans solved beside the unconstrained plan under an operational rule.

Spec `analysis.plan_variants`:
    variants:
      - {id: service_everywhere, type: minimum_service, components: [c1, c2], text: "..."}
                                   # every unit receives at least one of `components` (default: any component)
      - {id: one_choice_per_group, type: group_rule, components: [c1, c2], by: <unit column>, text: "..."}
                                   # one decision per group for those components (toolkit/alloc group_rules,
                                   # rule same_choice)
    headline: service_everywhere   # optional: the headline plan is this variant's (default: the unconstrained plan)
Each variant is solved on the point benefits and costs within the headline plan's budget (the reserved budget when
uncertainty.affordability.enforce is set) and reported with its value lost against the unconstrained plan (absolute
and as a share of its objective), the dollars moved between components, the population and units whose package
changes (toolkit/analysis/moves.py), and the units left with no package. A variant the rule or the budget makes
infeasible is reported as not solved, with why, never dropped. With `headline`, the analysis's feasibility carries
the variant's rule, so every re-optimisation (draws, stability, sensitivity, decision tests) does too.
"""
from __future__ import annotations

import copy
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.alloc import packages as apackages  # noqa: E402

PRODUCER = "toolkit/analysis/variants.py"
TYPES = ("minimum_service", "group_rule")


def config(raw: dict) -> dict:
    return (raw.get("analysis") or {}).get("plan_variants") or {}


def headline_id(raw: dict):
    return config(raw).get("headline")


def validate(raw: dict, cols) -> list[str]:
    """Spec errors in analysis.plan_variants (called by toolkit/spec/schema.py validate)."""
    pv = (raw.get("analysis") or {}).get("plan_variants")
    if pv is None:
        return []
    if not isinstance(pv, dict) or not isinstance(pv.get("variants"), list) or not pv["variants"]:
        return ["analysis.plan_variants: {variants: [...], headline: <variant id>} with at least one variant"]
    bad, ids = [], []
    comps = set((raw.get("options") or {}).get("components") or {})
    for v in pv["variants"]:
        i = v.get("id") if isinstance(v, dict) else None
        if not isinstance(i, str) or not re.fullmatch(r"[A-Za-z0-9_\-]+", i) or i in ids or i == "unconstrained":
            bad.append(f"analysis.plan_variants: id {i!r} missing, duplicated, reserved or not [A-Za-z0-9_-]")
            continue
        ids.append(i)
        if v.get("type") not in TYPES:
            bad.append(f"plan variant {i}: type {v.get('type')!r} not in {TYPES}")
            continue
        cs = v.get("components")
        if (cs is not None or v["type"] == "group_rule") and not (
                isinstance(cs, list) and cs and all(c in comps for c in cs)):
            bad.append(f"plan variant {i}: components must be a non-empty list of known components")
        if v["type"] == "group_rule" and v.get("by") not in cols:
            bad.append(f"plan variant {i}: by {v.get('by')!r} is not a column of the units table")
        txt = str(v.get("text") or "")
        if not txt.strip() or any(ch.isdigit() for ch in txt):
            bad.append(f"plan variant {i}: text (what the rule means, in words, no numbers)")
        for k in sorted(set(v) - {"id", "type", "components", "by", "text"}):
            bad.append(f"plan variant {i}: unknown key {k}")
    for k in sorted(set(pv) - {"variants", "headline"}):
        bad.append(f"analysis.plan_variants: unknown key {k}")
    if pv.get("headline") is not None and pv["headline"] not in ids:
        bad.append(f"analysis.plan_variants.headline: {pv['headline']!r} is not a variant id")
    return bad


def restrict(feas: dict, pk: dict, comps) -> dict:
    """Feasibility with every package that has none of `comps` removed (minimum service). Fails closed when a unit has
    no package left (a lock, or eligibility, rules every listed component out there)."""
    out = copy.deepcopy(feas)
    cs = set(comps)
    for i, row in out["feasible"].items():
        for p in row:
            if row[p] and not pk[p] & cs:
                row[p] = False
                out["reasons"].setdefault(i, {}).setdefault(p, []).append(
                    f"minimum service: at least one of {sorted(cs)}")
        if not any(row.values()):
            raise ValueError(f"minimum service: unit {i} has no feasible package with any of {sorted(cs)}")
    return out


def feasibility(A, v: dict) -> tuple[dict, dict]:
    """(alloc, feasibility) of variant `v` on top of the unconstrained alloc spec."""
    a = copy.deepcopy(getattr(A, "base_alloc", None) or A.alloc)
    if v["type"] == "group_rule":
        a["group_rules"] = dict(a.get("group_rules") or {})
        a["group_rules"][f"variant_{v['id']}"] = {"components": list(v["components"]), "by": v["by"],
                                                  "rule": "same_choice"}
    feas = apackages.feasibility(a, A.spec.units())
    if v["type"] == "minimum_service":
        feas = restrict(feas, A.pk, v.get("components") or list(A.spec["options"]["components"]))
    return a, feas


def apply_headline(A) -> str | None:
    """With `headline: <id>`, the analysis's alloc and feasibility become the variant's (every later solve carries the
    rule); the unconstrained ones are kept as A.base_alloc and A.base_feas. Returns the id, or None."""
    hid = headline_id(A.spec.raw)
    if hid is None:
        return None
    if getattr(A, "headline_variant", None) == hid:
        return hid
    v = next(x for x in config(A.spec.raw)["variants"] if x["id"] == hid)
    A.base_alloc, A.base_feas = A.alloc, A.feas
    A.alloc, A.feas = feasibility(A, v)
    A.headline_variant = hid
    return hid


def carry(A, feas: dict) -> dict:
    """A feasibility built from A.alloc (a group-rule decision test) with a minimum-service headline's rule applied
    too (a group-rule headline is already in A.alloc)."""
    hid = getattr(A, "headline_variant", None)
    v = next((x for x in config(A.spec.raw).get("variants") or [] if x["id"] == hid), None)
    if v is None or v["type"] != "minimum_service":
        return feas
    return restrict(feas, A.pk, v.get("components") or list(A.spec["options"]["components"]))


def run(A, ben0, cost0, plan) -> dict | None:
    """The plan_variants artifact: the unconstrained plan and every variant, solved on the point tables within the
    headline plan's budget (A.solve: the reserve when enforced). None when the spec asks for none."""
    from toolkit.analysis import moves as MV
    cfg = config(A.spec.raw)
    if not cfg:
        return None
    u = A.solve(ben0, cost0, feas=getattr(A, "base_feas", None) or A.feas)
    up, uo = u["choice"], float(u["objective"])

    def row(p, obj):
        return {"objective": obj, "cost": float(sum(cost0[i][p[i]] for i in A.ids)),
                "units_no_package": sum(p[i] == A.comp for i in A.ids), "recommendation": A.mix_text(p)}
    out = {"headline": cfg.get("headline") or "unconstrained", "outcome": A.primary,
           "unconstrained": dict(row(up, uo), plan=dict(up)), "variants": []}
    for v in cfg["variants"]:
        rec = {"id": v["id"], "type": v["type"], "text": v["text"], "components": v.get("components"),
               "by": v.get("by")}
        try:
            _a, feas = feasibility(A, v)
            sol = A.solve(ben0, cost0, feas=feas)
        except (ValueError, RuntimeError) as e:      # the rule or the budget leaves no feasible plan
            out["variants"].append(dict(rec, status="not_solved", reason=str(e)))
            continue
        vp, vo = sol["choice"], float(sol["objective"])
        A.guard(f"plan variant {v['id']}", [vp])
        m = MV.summarise(A, up, vp, ben0, cost0, vo)
        lost = uo - vo
        rec.update(row(vp, vo), status="solved", plan=dict(vp), value_lost=lost,
                   value_lost_pct=100.0 * lost / uo if uo > 0 else None,
                   **{k: m[k] for k in ("units_changed", "share_units_changed", "population_changed",
                                        "population_changed_share", "dollars_by_component", "dollars_moved",
                                        "dollars_moved_share", "units_changed_by_group")})
        out["variants"].append(rec)
    hv = cfg.get("headline")
    if hv is not None:
        h = next(x for x in out["variants"] if x["id"] == hv)
        if h.get("status") != "solved" or h["plan"] != dict(plan):
            raise RuntimeError(f"plan variant {hv}: the headline plan is not the variant's solved plan")
    return out


# --------------------------------------------------------------------------- self-test
def _toy():
    """Four units in two regions, two net types (one group) and a third component; costs split by component."""
    from toolkit.analysis.core import Analysis, CostTable

    class _Spec:
        def __init__(self, raw, units):
            self.raw, self._u = raw, units

        def __getitem__(self, k):
            return self.raw[k]

        def units(self):
            return self._u
    units = [{"id": f"u{k}", "r": "NNSS"[k - 1], "pop": 100.0} for k in range(1, 5)]
    raw = {"options": {"components": {"n1": {"group": "net"}, "n2": {"group": "net"}, "s": {}}}, "analysis": {}}
    A = object.__new__(Analysis)
    A.spec = _Spec(raw, units)
    A.ids = [u["id"] for u in units]
    A.by_id = {u["id"]: u for u in units}
    A.pk = {"none": frozenset(), "n1": frozenset({"n1"}), "n2": frozenset({"n2"}), "s": frozenset({"s"})}
    A.comp, A.primary, A.region, A.last_events = "none", "dalys", "r", []
    A.alloc = {"units": {"id": "id", "populations": {"total": "pop"}}, "components": raw["options"]["components"],
               "packages": {k: sorted(v) for k, v in A.pk.items()}}
    A.feas = apackages.feasibility(A.alloc, units)
    A.budget, A.plan_budget, A.reserve_fraction = 2.0, None, None
    price = {"none": {}, "n1": {"n1": 0.5}, "n2": {"n2": 0.5}, "s": {"s": 1.0}}
    cost = CostTable({i: {k: sum(price[k].values()) for k in A.pk} for i in A.ids},
                     {i: {k: dict(price[k]) for k in A.pk} for i in A.ids})
    ben = {"u1": {"none": 0, "n1": 5, "n2": 4, "s": 0.2}, "u2": {"none": 0, "n1": 1, "n2": 3, "s": 0.2},
           "u3": {"none": 0, "n1": 2, "n2": 1, "s": 6}, "u4": {"none": 0, "n1": 0.1, "n2": 0.05, "s": 0.6}}
    ms = {"id": "nets", "type": "minimum_service", "components": ["n1", "n2"], "text": "a net in every unit"}
    gr = {"id": "one_net", "type": "group_rule", "components": ["n1", "n2"], "by": "r", "text": "one net per region"}
    return A, ben, cost, ms, gr


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def attempt(fn, *a):
        try:
            return fn(*a), None
        except Exception as e:  # noqa: BLE001 -- a failing case, not a crash of the self-test
            return None, e
    A, ben, cost, ms, gr = _toy()
    A.spec.raw["analysis"]["plan_variants"] = {"variants": [ms, gr]}
    plan = A.solve(ben, cost)["choice"]
    r, e = attempt(run, A, ben, cost, plan)
    v = {x["id"]: x for x in (r or {}).get("variants", [])}
    u = (r or {}).get("unconstrained") or {}
    ok(e is None and abs(u.get("objective", 0) - 14.0) < 1e-9 and u.get("units_no_package") == 1
       and r.get("headline") == "unconstrained",
       f"PV1 the unconstrained plan is reported with its unfunded units ({e or u})")
    n = v.get("nets") or {}
    ok(n.get("status") == "solved" and abs(n.get("value_lost", 0) - 3.9) < 1e-9
       and abs(n.get("value_lost_pct", 0) - 100 * 3.9 / 14) < 1e-9 and n.get("units_no_package") == 0
       and n.get("plan") == {"u1": "n1", "u2": "n2", "u3": "n1", "u4": "n1"},
       f"PV2 minimum service: a net in every unit, its value lost by hand ({n.get('value_lost')}, {n.get('plan')})")
    ok(n.get("dollars_by_component") == {"n1": 1.0, "n2": 0.0, "s": -1.0} and n.get("dollars_moved") == 1.0
       and n.get("units_changed") == 2 and n.get("population_changed") == 200.0,
       f"PV3 the variant's dollars moved and population changed against the unconstrained plan "
       f"({n.get('dollars_by_component')}, {n.get('units_changed')})")
    g = v.get("one_net") or {}
    ok(g.get("status") == "solved" and abs(g.get("value_lost", 0) - 1.0) < 1e-9
       and g.get("plan", {}).get("u1") == g.get("plan", {}).get("u2") == "n2",
       f"PV4 group rule: one net decision per region (toolkit/alloc group_rules), value lost by hand "
       f"({g.get('value_lost')}, {g.get('plan')})")
    A.budget = 1.5                              # four nets cost 2: no plan gives every unit a net
    r2, e2 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"])
    n2 = {x["id"]: x for x in (r2 or {}).get("variants", [])}.get("nets") or {}
    ok(e2 is None and n2.get("status") == "not_solved" and n2.get("reason"),
       f"PV5 a variant the budget makes infeasible is reported as not solved, with why ({e2!r}, {n2})")
    A.budget, A.plan_budget = 2.0, 1.5          # enforce: variants are solved within the reserved budget
    r3, _ = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"])
    ok(all(x.get("status") != "solved" or x["cost"] <= 1.5 + 1e-9 for x in (r3 or {}).get("variants", [{}]))
       and (r3 or {}).get("unconstrained", {}).get("cost", 9) <= 1.5 + 1e-9,
       "PV6 with a reserve, the unconstrained plan and every variant are solved within the reserved budget")
    A.plan_budget = None
    A.spec.raw["analysis"]["plan_variants"]["headline"] = "one_net"
    h, e4 = attempt(apply_headline, A)
    hp = A.solve(ben, cost)["choice"] if e4 is None else {}
    r4, e5 = attempt(run, A, ben, cost, hp)
    ok(h == "one_net" and hp == g.get("plan") and e5 is None and r4.get("headline") == "one_net"
       and abs(r4["unconstrained"]["objective"] - 14.0) < 1e-9,
       f"PV7 headline: <variant> makes every solve carry the variant's rule; the unconstrained plan is still "
       f"reported ({h}, {e4!r}, {e5!r})")
    _, e6 = attempt(run, A, ben, cost, dict(hp, u4="s"))
    ok(isinstance(e6, RuntimeError) and "headline" in str(e6), f"PV8 a headline plan that is not the variant's solved plan fails closed ({e6!r})")
    lock = copy.deepcopy(A.feas)
    lock["feasible"]["u4"] = {"none": True, "n1": False, "n2": False, "s": False}
    _, e7 = attempt(restrict, lock, A.pk, ["n1", "n2"])
    ok(isinstance(e7, ValueError), f"PV9 a unit that cannot receive any listed component fails closed ({e7!r})")
    raw = {"options": {"components": {"a": {}, "b": {}}}, "analysis": {"plan_variants": {
        "variants": [{"id": "x", "type": "minimum_service", "text": "a service everywhere"},
                     {"id": "y", "type": "group_rule", "components": ["a"], "by": "nocol", "text": "t"},
                     {"id": "x", "type": "magic", "text": "t"},
                     {"id": "z", "type": "minimum_service", "components": ["q"], "text": "rule 2"}],
        "headline": "w"}}}
    errs, e8 = attempt(validate, raw, {"id", "r"})
    errs = errs or []
    want = ("by 'nocol'", "duplicated", "known components", "no numbers", "headline")
    ok(e8 is None and all(any(w in x for x in errs) for w in want) and not any("variant x:" in x for x in errs),
       f"PV10 the spec's variants are validated ({errs}, {e8!r})")
    ok(validate({"analysis": {}}, set()) == [] if e8 is None else False, "PV11 no plan_variants: nothing to validate")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
