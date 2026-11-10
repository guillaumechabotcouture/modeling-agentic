"""Operational plan variants (F6 S1-6): plans solved beside the unconstrained plan under an operational rule.

Spec `analysis.plan_variants`:
    variants:
      - {id: service_everywhere, type: minimum_service, components: [c1, c2], text: "..."}
                                   # every unit receives at least one of `components` (default: any component)
      - {id: one_choice_per_group, type: group_rule, components: [c1, c2], by: <unit column>, text: "..."}
                                   # one decision per group for those components (toolkit/alloc group_rules,
                                   # rule same_choice, under a collision-safe id beside the signed rules). "none"
                                   # is one of the choices: a group may share no provision. `provide: true` adds
                                   # minimum service on the same components, so every group provides one; each
                                   # group's choice is reported (group_choices, groups_none)
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
        if "provide" in v and (v["type"] != "group_rule" or not isinstance(v["provide"], bool)):
            bad.append(f"plan variant {i}: provide (true or false) applies to a group_rule only")
        for k in sorted(set(v) - {"id", "type", "components", "by", "text", "provide"}):
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


def rule_id(rules: dict, vid: str) -> str:
    """A group-rule id for variant `vid` that no rule in `rules` (signed, or a decision test's) already has."""
    base, n = f"__plan_variant__{vid}", 1
    k = base
    while k in rules:
        n += 1
        k = f"{base}_{n}"
    return k


def on_alloc(A, v: dict, alloc: dict) -> tuple[dict, dict]:
    """(alloc, feasibility) of variant `v` added to `alloc`: its group rule under a collision-safe id beside every
    rule already there (never replacing one), its minimum service (or `provide: true`) as a restriction."""
    a = copy.deepcopy(alloc)
    if v["type"] == "group_rule":
        a["group_rules"] = dict(a.get("group_rules") or {})
        a["group_rules"][rule_id(a["group_rules"], v["id"])] = {"components": list(v["components"]), "by": v["by"],
                                                               "rule": "same_choice"}
    feas = apackages.feasibility(a, A.spec.units())
    if v["type"] == "minimum_service" or v.get("provide"):
        feas = restrict(feas, A.pk, v.get("components") or list(A.spec["options"]["components"]))
    return a, feas


def feasibility(A, v: dict) -> tuple[dict, dict]:
    """(alloc, feasibility) of variant `v` on top of the unconstrained alloc spec."""
    return on_alloc(A, v, getattr(A, "base_alloc", None) or A.alloc)


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


def carry(A, alloc: dict) -> dict:
    """The feasibility of a decision test's alloc spec (built from the unconstrained one) with the headline variant's
    rule joined to it: added after the test's own rules under a collision-safe id, so a test can neither replace nor
    remove it (Codex S1-6 r1 P2-3). Without a headline variant: the test's feasibility."""
    hid = getattr(A, "headline_variant", None)
    v = next((x for x in config(A.spec.raw).get("variants") or [] if x["id"] == hid), None)
    return apackages.feasibility(alloc, A.spec.units()) if v is None else on_alloc(A, v, alloc)[1]


def base_alloc(A) -> dict:
    """The unconstrained (signed) alloc spec, before any headline variant."""
    return getattr(A, "base_alloc", None) or A.alloc


def contract_findings(raw: dict, point: dict, pv) -> list[str]:
    """The spec's plan variants, point.yaml's headline_variant, the plan_variants artifact and the headline plan agree
    (Codex S1-6 r1 P2-4). [] when they do."""
    cfg = config(raw)
    hv = cfg.get("headline")
    out = []
    if (point or {}).get("headline_variant") != hv:
        out.append(f"point.yaml headline_variant {(point or {}).get('headline_variant')!r} is not the spec's headline "
                   f"{hv!r}")
    if not cfg:
        if pv is not None:
            out.append("a plan_variants artifact, but the spec asks for no plan variants")
        return out
    if not isinstance(pv, dict):
        return out + ["the spec asks for plan variants, and the run has no plan_variants artifact"]
    if pv.get("headline") != (hv or "unconstrained"):
        out.append(f"the plan_variants artifact's headline {pv.get('headline')!r} is not the spec's {hv or 'unconstrained'!r}")
    got = [x.get("id") for x in pv.get("variants") or [] if isinstance(x, dict)]
    if got != [v["id"] for v in cfg.get("variants") or []]:
        out.append(f"the plan_variants artifact's variants {got} are not the spec's")
    bad = [x.get("id") for x in pv.get("variants") or [] if not isinstance(x, dict)
           or x.get("status") not in ("solved", "not_solved")]
    if bad:
        out.append(f"plan variants {bad}: status is neither solved nor not_solved")
    if hv is not None:
        h = next((x for x in pv.get("variants") or [] if isinstance(x, dict) and x.get("id") == hv), {})
        if h.get("status") != "solved" or h.get("plan") != (point or {}).get("plan"):
            out.append(f"the headline plan is not the solved plan of variant {hv}")
    return out


def group_choices(A, v: dict, plan: dict) -> dict:
    """{group: the class it selected for the rule's components}; "none" (a shared absence) is a valid choice."""
    cs = set(v["components"])
    out = {}
    for i in A.ids:
        out.setdefault(str(A.by_id[i][v["by"]]), set()).add("+".join(sorted(A.pk[plan[i]] & cs)) or "none")
    return {g: "/".join(sorted(c)) for g, c in sorted(out.items())}


def run(A, ben0, cost0, plan, events=None) -> dict | None:
    """The plan_variants artifact: the unconstrained plan and every variant, solved on the point tables within the
    headline plan's budget (A.solve: the reserve when enforced), each guarded against the point evaluation's model
    corrections `events` (Codex S1-6 r1 P1-1). None when the spec asks for none."""
    from toolkit.analysis import moves as MV
    cfg = config(A.spec.raw)
    if not cfg:
        return None
    ev = list(events or [])
    u = A.solve(ben0, cost0, feas=getattr(A, "base_feas", None) or A.feas)
    up, uo = u["choice"], float(u["objective"])
    A.guard("plan variants: the unconstrained plan", [up], events=ev)

    def row(p, obj):        # no package: no component (the comparator package may hold some; Codex S1-6 r1 P2-5)
        return {"objective": obj, "cost": float(sum(cost0[i][p[i]] for i in A.ids)),
                "units_no_package": sum(not A.pk[p[i]] for i in A.ids),
                "units_on_comparator": sum(p[i] == A.comp for i in A.ids), "recommendation": A.mix_text(p)}
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
        A.guard(f"plan variant {v['id']}", [vp], events=ev)
        m = MV.summarise(A, up, vp, ben0, cost0, vo, feas=getattr(A, "base_feas", None) or A.feas)
        lost = uo - vo
        rec.update(row(vp, vo), status="solved", plan=dict(vp), value_lost=lost,
                   value_lost_pct=100.0 * lost / uo if uo > 0 else None,
                   **{k: m[k] for k in ("units_changed", "share_units_changed", "population_changed",
                                        "population_changed_share", "dollars_by_component", "dollars_moved",
                                        "dollars_moved_share", "dollars_reallocated", "dollars_reallocated_share",
                                        "units_changed_by_group")})
        if v["type"] == "group_rule":       # each group's choice; a shared "none" is allowed unless provide: true
            rec["group_choices"] = group_choices(A, v, vp)
            rec["groups_none"] = sum(c == "none" for c in rec["group_choices"].values())
            rec["provide"] = bool(v.get("provide"))
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


def _self_test_round1(ok, attempt):
    """Codex round 1 on S1-6."""
    from toolkit.analysis import moves as MV
    from toolkit.analysis.core import AnalysisBlocked
    # P1-1: the point evaluation's corrections guard the unconstrained plan and every variant
    A, ben, cost, ms, gr = _toy()
    A.spec.raw["analysis"]["plan_variants"] = {"variants": [ms]}
    plan = A.solve(ben, cost)["choice"]
    A.last_events = []                                     # a later step's (clean) events
    _, e1 = attempt(run, A, ben, cost, plan, [{"unit": "u4", "package": "n1", "kind": "extrapolation"}])
    _, e2 = attempt(run, A, ben, cost, plan, [{"unit": "u1", "package": plan["u1"], "kind": "clip"}])
    ok(isinstance(e1, AnalysisBlocked) and isinstance(e2, AnalysisBlocked),
       f"PV12 the point corrections guard each variant and the unconstrained plan ({e1!r}, {e2!r})")
    # P1-2: a signed rule whose name a variant's internal id could take is never replaced
    A, ben, cost, ms, gr = _toy()
    A.alloc["group_rules"] = {"variant_one_net": {"components": ["s"], "by": "r", "rule": "same_choice"}}
    A.feas = apackages.feasibility(A.alloc, A.spec.units())
    signed = A.feas
    A.spec.raw["analysis"]["plan_variants"] = {"variants": [gr], "headline": "one_net"}
    attempt(apply_headline, A)
    hp = A.solve(ben, cost)["choice"]
    net = {i: "+".join(sorted(A.pk[hp[i]] & {"n1", "n2"})) or "none" for i in A.ids}
    ok(MV.infeasible_reasons(A.ids, hp, cost, signed, A.budget) == []
       and all(len({net[i] for i in A.ids if A.by_id[i]["r"] == g}) == 1 for g in "NS"),
       f"PV13 the headline keeps the signed rule of the same name and adds its own ({hp})")
    # P2-3: a group-rule decision test can neither replace nor remove the headline variant's rule
    base_rules = set(getattr(A, "base_alloc", A.alloc).get("group_rules") or {})
    hrule = sorted(set(A.alloc.get("group_rules") or {}) - base_rules)
    test_alloc = copy.deepcopy(getattr(A, "base_alloc", A.alloc))
    test_alloc["group_rules"] = {(hrule or ["x"])[0]: {"components": ["s"], "by": "r", "rule": "same_choice"}}
    f3, e3 = attempt(carry, A, test_alloc)
    ok(e3 is None and len((f3 or {}).get("links") or []) == 2,
       f"PV14 a decision test's rules are joined to the headline variant's ({e3!r}, "
       f"{[x['name'] for x in (f3 or {}).get('links') or []]})")
    # P2-5: units with no package are those with no component, not those on the comparator package
    A, ben, cost, ms, gr = _toy()
    A.comp = "n1"
    A.spec.raw["analysis"]["plan_variants"] = {"variants": [ms]}
    r5, e5 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [])
    n5 = {x["id"]: x for x in (r5 or {}).get("variants", [])}.get("nets") or {}
    ok(e5 is None and n5.get("units_no_package") == 0 and n5.get("units_on_comparator") == 3
       and "units_on_comparator" in (r5 or {}).get("unconstrained", {}),
       f"PV15 a non-empty comparator: no unit is without a package; comparator allocations apart ({e5!r}, {n5})")
    A.outcomes, A.dfac = ["dalys"], 1.0
    out = {i: {"y": {k: {"dalys": 1.0} for k in A.pk}} for i in A.ids}
    s5, e5b = attempt(A.summary, {i: "n1" for i in A.ids}, out, cost)
    ok(e5b is None and s5["mix"]["no_package"] == 0 and s5.get("units_on_comparator") == 4,
       f"PV15b the headline summary counts units with no component ({e5b!r})")
    # P3: each group's selected class is reported; `provide` requires provision beside the rule
    A, ben, cost, ms, gr = _toy()
    gp = dict(gr, id="one_net_p", provide=True, text="one net per region, provided")
    A.spec.raw["analysis"]["plan_variants"] = {"variants": [gr, gp]}
    r6, e6 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [])
    v6 = {x["id"]: x for x in (r6 or {}).get("variants", [])}
    ok(e6 is None and v6.get("one_net", {}).get("group_choices") == {"N": "n2", "S": "none"}
       and v6["one_net"].get("groups_none") == 1 and v6.get("one_net_p", {}).get("status") == "solved"
       and "none" not in v6["one_net_p"].get("group_choices", {"x": "none"}).values()
       and validate({"options": {"components": {"a": {}}}, "analysis": {"plan_variants": {"variants": [
           dict(gp, components=["a"], by="r")]}}}, {"r"}) == [],
       f"PV16 group choices are reported (a shared none is allowed); provide: true requires provision ({e6!r})")
    # P2-4: the contract reconciles the spec, point.headline_variant, the artifact and the headline plan
    raw = {"analysis": {"plan_variants": {"variants": [ms, gr], "headline": "nets"}}}
    good = {"headline": "nets", "unconstrained": {}, "variants": [{"id": "nets", "status": "solved", "plan": {"u": "a"}},
                                                                  {"id": "one_net", "status": "not_solved"}]}
    pt = {"plan": {"u": "a"}, "headline_variant": "nets"}
    cf = lambda r, p, v: attempt(contract_findings, r, p, v)[0]  # noqa: E731
    bad = [cf(raw, pt, None), cf(raw, dict(pt, headline_variant=None), good), cf(raw, pt, dict(good, headline="x")),
           cf(raw, pt, dict(good, variants=good["variants"][:1])),
           cf(raw, pt, dict(good, variants=[dict(good["variants"][0], status="maybe"), good["variants"][1]])),
           cf(raw, dict(pt, plan={"u": "b"}), good), cf({"analysis": {}}, pt, None)]
    ok(cf(raw, pt, good) == [] and all(b for b in bad),
       f"PV17 the contract requires the artifact and agreement on the headline ({[len(b or []) for b in bad]})")


def _self_test_fixed(ok, attempt):
    """F6 S2-6: a plan given in full (a unit column of package ids) is evaluated, never solved."""
    from toolkit.analysis.core import AnalysisBlocked
    g_ = globals().get      # the functions under test, looked up so a missing one fails its case (no crash)
    fx = {"id": "given", "type": "fixed_plan", "column": "gc", "text": "the plan as funded"}
    units = [{"id": f"u{k}", "gc": g} for k, g in enumerate(["s", "n1", "s", "none"], 1)]
    pk = {"none": frozenset(), "n1": frozenset({"n1"}), "n2": frozenset({"n2"}), "s": frozenset({"s"})}
    comps = {"n1": {}, "n2": {}, "s": {}}
    val = lambda pv, us=units, cols=("id", "gc"): attempt(  # noqa: E731
        validate, {"options": {"components": comps}, "analysis": {"plan_variants": pv}}, set(cols), us, pk, "id")
    good, e0 = val({"variants": [fx]})
    bads = [val({"variants": [dict(fx, column="nocol")]}, cols=("id",)),
            val({"variants": [fx]}, us=[dict(units[0], gc=" "), *units[1:]]),
            val({"variants": [fx]}, us=[dict(units[0], gc="zz"), *units[1:]]),
            val({"variants": [fx], "headline": "given"}),
            val({"variants": [dict(fx, components=["s"])]})]
    want = ("nocol", "u1", "zz", "cannot be the headline", "unknown key components")
    ok(e0 is None and good == [] and all(b[1] is None and any(w in x for x in b[0]) for b, w in zip(bads, want)),
       f"PV18 a fixed plan is validated: a known column, every unit's package known and non-empty, never the "
       f"headline ({good}, {[b[0] for b in bads]})")
    A, ben, cost, ms, gr = _toy()
    for u, g in zip(A.spec.units(), ["s", "n1", "s", "none"]):
        u["gc"] = g
    A.spec.raw["analysis"]["plan_variants"] = {"variants": [ms, fx]}
    plan = A.solve(ben, cost)["choice"]
    r, e = attempt(run, A, ben, cost, plan, [])
    f = {x["id"]: x for x in (r or {}).get("variants", [])}.get("given") or {}
    ok(e is None and f.get("status") == "evaluated" and f.get("plan") == {"u1": "s", "u2": "n1", "u3": "s", "u4": "none"}
       and abs(f.get("objective", 0) - 7.2) < 1e-9 and abs(f.get("value_lost", 0) - 6.8) < 1e-9
       and abs(f.get("value_lost_pct", 0) - 100 * 6.8 / 14) < 1e-9 and f.get("units_no_package") == 1,
       f"PV19 the given plan is evaluated on the point benefits, its value lost against the unconstrained plan by "
       f"hand ({e!r}, {f.get('objective')}, {f.get('value_lost')})")
    ok(abs(f.get("cost", 0) - 2.5) < 1e-9 and abs(f.get("cost_minus_budget", 0) - 0.5) < 1e-9
       and f.get("within_budget") is False and f.get("budget_allocatable") == 2.0
       and f.get("dollars_by_component") == {"n1": 0.0, "n2": -0.5, "s": 1.0} and f.get("dollars_moved") == 1.0
       and f.get("units_changed") == 2 and f.get("population_changed") == 200.0,
       f"PV20 a plan over the budget is reported with its cost against the envelope, not refused; dollars moved and "
       f"units changing package against the unconstrained plan ({f.get('cost')}, {f.get('dollars_by_component')})")
    ok(f.get("ineligible_units") == [] and f.get("units_ineligible") == 0 and f.get("uncertainty") is None
       and "point" in str(f.get("uncertainty_note")),
       f"PV21 no draws given: the point estimate only, said so ({f.get('uncertainty_note')})")
    A.feas["feasible"]["u2"]["n1"] = False
    A.feas["reasons"].setdefault("u2", {})["n1"] = ["n1 not eligible here"]
    r2, e2 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [])
    f2 = {x["id"]: x for x in (r2 or {}).get("variants", [])}.get("given") or {}
    ok(e2 is None and f2.get("units_ineligible") == 1 and f2.get("plan", {}).get("u2") == "n1"
       and f2.get("ineligible_units") == [{"unit": "u2", "package": "n1", "reasons": ["n1 not eligible here"]}],
       f"PV22 a unit given a package it is not eligible for is reported with why, and kept in the plan ({e2!r}, "
       f"{f2.get('ineligible_units')})")
    A.spec.units()[0]["gc"] = "zz"
    _, e3 = attempt(run, A, ben, cost, plan, [])
    A.spec.units()[0]["gc"] = ""
    _, e3b = attempt(g_("given_plan"), A, fx)
    _, e3c = attempt(g_("given_plan"), A, dict(fx, column="nocol"))
    ok(all(isinstance(x, ValueError) for x in (e3, e3b, e3c)),
       f"PV23 an unknown or empty package id, or a missing column, fails closed at evaluation ({e3!r}, {e3b!r}, {e3c!r})")
    A.spec.units()[0]["gc"] = "s"
    _, e4 = attempt(run, A, ben, cost, plan, [{"unit": "u2", "package": "n1", "kind": "extrapolation"}])
    ok(isinstance(e4, AnalysisBlocked), f"PV24 the point corrections guard the given plan ({e4!r})")
    # draws: the given plan's outcome and cost in each uncertainty draw (UQ.run's on_draw hook)
    A.outcomes = ["dalys"]
    D, e5 = attempt(g_("Draws"), A)
    out = {i: {"y": {k: {"dalys": 10.0 - ben[i][k]} for k in A.pk}} for i in A.ids}
    tabs = [{i: {k: m * cost[i][k] for k in A.pk} for i in A.ids} for m in (0.8, 1.2)]
    for t in tabs:
        attempt(lambda *a: D.add(*a), out, [], t)
    r6, e6 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [], D, {"level": 0.9})
    u6 = ({x["id"]: x for x in (r6 or {}).get("variants", [])}.get("given") or {}).get("uncertainty") or {}
    af = u6.get("affordability") or {}
    ok(e5 is None and e6 is None and u6.get("draws") == 2 and abs(u6["averted"]["dalys"]["median"] - 7.2) < 1e-9
       and af.get("draws_over_budget") == 1 and abs(af["fixed_plan_cost"]["median"] - 2.5) < 1e-9
       and u6.get("interval") == {"level": 0.9},
       f"PV25 the given plan's outcome and cost quantiles over the uncertainty draws, as the headline plan's ({e6!r}, "
       f"{u6})")
    _, e7 = attempt(lambda *a: D.add(*a), out, [{"unit": "u1", "package": "s", "kind": "clip"}], tabs[0])
    ok(isinstance(e7, AnalysisBlocked), f"PV26 a draw's corrections on the given plan stop publication ({e7!r})")
    # contract: a fixed plan is evaluated (never solved), and its record carries its eligibility report
    raw = {"analysis": {"plan_variants": {"variants": [ms, fx]}}}
    rec = {"id": "given", "type": "fixed_plan", "status": "evaluated", "ineligible_units": [], "units_ineligible": 0,
           "cost_minus_budget": 0.5}
    art = {"headline": "unconstrained", "variants": [{"id": "nets", "type": "minimum_service", "status": "solved"}, rec]}
    pt = {"plan": {}, "headline_variant": None}
    cf = lambda v: attempt(contract_findings, raw, pt, v)[0]  # noqa: E731
    ok(cf(art) == [] and cf(dict(art, variants=[dict(art["variants"][0], status="evaluated"), rec]))
       and cf(dict(art, variants=[art["variants"][0], dict(rec, status="solved")]))
       and cf(dict(art, variants=[art["variants"][0], {k: x for k, x in rec.items() if k != "ineligible_units"}])),
       "PV27 the contract: a fixed plan is evaluated, a solved variant is not, and the eligibility report is required")


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
    _self_test_round1(ok, attempt)
    _self_test_fixed(ok, attempt)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
