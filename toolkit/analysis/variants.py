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
      - {id: as_funded, type: fixed_plan, column: <unit column>, text: "..."}
                                   # F6 S2-6: a plan given in full, each unit's package id in a units-table column
                                   # (every cell a known package; the comparator's id for a unit given none);
                                   # evaluated, never solved, and never the headline
    headline: service_everywhere   # optional: the headline plan is this variant's (default: the unconstrained plan)
Each variant is solved on the point benefits and costs within the headline plan's budget (the reserved budget when
uncertainty.affordability.enforce is set) and reported with its value lost against the unconstrained plan (absolute
and as a share of its objective), the dollars moved between components, the population and units whose package
changes (toolkit/analysis/moves.py), and the units left with no package. A variant the rule or the budget makes
infeasible is reported as not solved, with why, never dropped. With `headline`, the analysis's feasibility carries
the variant's rule, so every re-optimisation (draws, stability, sensitivity, decision tests) does too.
A fixed plan (status `evaluated`) reports the same fields on the same point tables, plus its cost against the
allocatable budget (over or under, never refused for it), the units given a package the signed constraints rule out
(with why; kept in the plan, evaluated as given), the caps and group rules it breaks, and, through UQ.run's on_draw
hook (`Draws`), its outcome and cost quantiles over the uncertainty draws as uncertainty.yaml reports the headline
plan's. Model corrections bearing on it, at the point or in a draw, stop publication as for every other plan.
Its value lost mixes a different allocation with a different spend, so it is also compared with the optimum re-solved
at its own point cost under the unconstrained feasibility (`same_spend`, review A2; not solved, with why, when none).
A solved variant is followed over the same draws (`solved_plans`, `Draws`): its record carries the same uncertainty
block, and its share of draws over the allocatable budget is judged against `uncertainty.affordability` when the spec
declares it (rule_met). The reserve is set for the headline plan, so a variant can fail the rule; it is then reported
as failing it, not re-solved.
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
TYPES = ("minimum_service", "group_rule", "fixed_plan")
KEYS = {"minimum_service": {"id", "type", "components", "text", "provide"},     # provide: refused below
        "group_rule": {"id", "type", "components", "by", "text", "provide"},
        "fixed_plan": {"id", "type", "column", "text"}}
MOVES = ("units_changed", "share_units_changed", "population_changed", "population_changed_share",
         "dollars_by_component", "dollars_moved", "dollars_moved_share", "dollars_reallocated",
         "dollars_reallocated_share", "units_changed_by_group")


def config(raw: dict) -> dict:
    return (raw.get("analysis") or {}).get("plan_variants") or {}


def headline_id(raw: dict):
    return config(raw).get("headline")


def fixed_variants(raw: dict) -> list[dict]:
    return [v for v in config(raw).get("variants") or [] if isinstance(v, dict) and v.get("type") == "fixed_plan"]


def _package_problems(cells: dict, pk, options=None) -> list[str]:
    """A fixed plan's column ({unit id: cell}): every cell a known, non-empty package id. A generated package that
    `within_envelope` pruning dropped (it costs more than the envelope in every unit) has no outcomes in the analysis:
    it is refused with why (Codex S2-6 r1 P1-1)."""
    out = []
    empty = [str(u) for u, c in cells.items() if not str(c or "").strip()]
    unknown = sorted({str(c).strip() for c in cells.values() if str(c or "").strip() and str(c).strip() not in pk})
    p = (options or {}).get("packages")
    pruned = []
    if unknown and isinstance(p, dict) and p.get("generate") is True and p.get("within_envelope") is True:
        from toolkit.alloc import costs as acosts
        pruned = [x for x in unknown if x in acosts.packages(options)]
        unknown = [x for x in unknown if x not in pruned]
    if empty:
        out.append(f"{len(empty)} units have no package id (e.g. {empty[:5]}); give the comparator's id for none")
    if pruned:
        out.append(f"packages {pruned[:5]} were dropped by options.packages.within_envelope (each costs more than the "
                   "envelope in every unit), so the analysis has no outcomes for them; set within_envelope: false to "
                   "evaluate this plan")
    if unknown:
        out.append(f"package ids {unknown[:5]} are not packages of options.packages {sorted(pk)[:8]}")
    return out


def validate(raw: dict, cols, units=None, pk=None, uid=None) -> list[str]:
    """Spec errors in analysis.plan_variants (called by toolkit/spec/schema.py validate; given the units table and
    the packages, a fixed plan's package ids are checked too)."""
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
        if v["type"] == "fixed_plan":
            if v.get("column") not in cols:
                bad.append(f"plan variant {i}: column {v.get('column')!r} is not a column of the units table")
            elif units is not None and pk is not None:
                bad += [f"plan variant {i}: {m}" for m in _package_problems(
                    {u.get(uid): u.get(v["column"]) for u in units}, pk, raw.get("options"))]
        cs = v.get("components")
        if v["type"] != "fixed_plan" and (cs is not None or v["type"] == "group_rule") and not (
                isinstance(cs, list) and cs and all(c in comps for c in cs)):
            bad.append(f"plan variant {i}: components must be a non-empty list of known components")
        if v["type"] == "group_rule" and v.get("by") not in cols:
            bad.append(f"plan variant {i}: by {v.get('by')!r} is not a column of the units table")
        txt = str(v.get("text") or "")
        if not txt.strip() or any(ch.isdigit() for ch in txt):
            bad.append(f"plan variant {i}: text (what the rule means, in words, no numbers)")
        if "provide" in v and (v["type"] != "group_rule" or not isinstance(v["provide"], bool)):
            bad.append(f"plan variant {i}: provide (true or false) applies to a group_rule only")
        for k in sorted(set(v) - KEYS[v["type"]]):
            bad.append(f"plan variant {i}: unknown key {k}")
    for k in sorted(set(pv) - {"variants", "headline"}):
        bad.append(f"analysis.plan_variants: unknown key {k}")
    if pv.get("headline") is not None and pv["headline"] not in ids:
        bad.append(f"analysis.plan_variants.headline: {pv['headline']!r} is not a variant id")
    elif any(v.get("id") == pv.get("headline") for v in fixed_variants(raw)):
        bad.append(f"analysis.plan_variants.headline: {pv['headline']!r} is a fixed_plan, which is evaluated, not "
                   "solved: it cannot be the headline")
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
    types = {v["id"]: v.get("type") for v in cfg.get("variants") or []}
    bad = [x.get("id") for x in pv.get("variants") or [] if not isinstance(x, dict) or x.get("status") not in (
        ("evaluated",) if types.get(x.get("id")) == "fixed_plan" else ("solved", "not_solved"))]
    if bad:
        out.append(f"plan variants {bad}: status is not evaluated (a fixed plan) or solved / not_solved (a rule)")
    for x in pv.get("variants") or []:      # F6 S2-6: a fixed plan's eligibility report and budget difference
        if isinstance(x, dict) and types.get(x.get("id")) == "fixed_plan" and x.get("status") == "evaluated" and not (
                isinstance(x.get("ineligible_units"), list) and x.get("units_ineligible") == len(x["ineligible_units"])
                and isinstance(x.get("cost_minus_budget"), (int, float))):
            out.append(f"fixed plan {x.get('id')}: no eligibility report or cost against the budget")
        if isinstance(x, dict) and types.get(x.get("id")) == "fixed_plan" and x.get("status") == "evaluated":
            out += _same_spend_findings(x)      # review A2: the comparison at the plan's own cost
    if hv is not None:
        h = next((x for x in pv.get("variants") or [] if isinstance(x, dict) and x.get("id") == hv), {})
        if h.get("status") != "solved" or h.get("plan") != (point or {}).get("plan"):
            out.append(f"the headline plan is not the solved plan of variant {hv}")
    return out + _affordability_findings(raw, pv, types)


def _affordability_findings(raw: dict, pv: dict, types: dict) -> list[str]:
    """Codex r1 on A6: each variant's affordability record agrees with its own per-draw costs, the spec's rule (a
    solved variant) or no rule (a fixed plan), one budget and one draw count across variants; when any variant has
    draws, every solved variant has them. A consistent `rule_met: false` is a result, not a finding."""
    rule = ((raw.get("uncertainty") or {}).get("affordability") or None)
    recs = [x for x in pv.get("variants") or [] if isinstance(x, dict)]
    drawn = [x for x in recs if x.get("uncertainty")]
    if not drawn:
        return []
    out, budgets, counts = [], set(), set()
    for x in recs:
        vid, u = x.get("id"), x.get("uncertainty")
        if not u:
            if x.get("status") == "solved":
                out.append(f"plan variant {vid}: solved, but has no uncertainty draws while other variants do")
            continue
        a = u.get("affordability") if isinstance(u, dict) else None
        c = a.get("per_draw_cost") if isinstance(a, dict) else None
        if not isinstance(c, list) or not c or not all(isinstance(v, (int, float)) for v in c):
            out.append(f"plan variant {vid}: no per-draw cost in its affordability record")
            continue
        b = a.get("budget_allocatable")
        if not isinstance(b, (int, float)):
            out.append(f"plan variant {vid}: no allocatable budget in its affordability record")
            continue
        over = sum(v > float(b) * (1 + 1e-9) for v in c)
        want_rule = None if types.get(vid) == "fixed_plan" else rule
        met = None if not want_rule else over / len(c) <= float(want_rule["max_share_over_budget"]) + 1e-12
        if not (u.get("draws") == a.get("draws") == len(c)):
            out.append(f"plan variant {vid}: draw counts disagree ({u.get('draws')}, {a.get('draws')}, {len(c)} costs)")
        if a.get("draws_over_budget") != over or abs(float(a.get("share_over_budget") or 0) - over / len(c)) > 1e-12:
            out.append(f"plan variant {vid}: draws over budget are not its per-draw costs' ({over} of {len(c)})")
        if (a.get("rule") or None) != (dict(want_rule) if want_rule else None) or a.get("rule_met") is not met:
            out.append(f"plan variant {vid}: rule or rule_met is not the spec's rule applied to its draws "
                       f"({a.get('rule')}, {a.get('rule_met')!r}; expected {want_rule}, {met!r})")
        budgets.add(float(b))
        counts.add(len(c))
    if len(budgets) > 1 or len(counts) > 1:
        out.append(f"plan variants: affordability records use different budgets {sorted(budgets)} or draw counts "
                   f"{sorted(counts)}")
    return out


def group_choices(A, v: dict, plan: dict) -> dict:
    """{group: the class it selected for the rule's components}; "none" (a shared absence) is a valid choice."""
    cs = set(v["components"])
    out = {}
    for i in A.ids:
        out.setdefault(str(A.by_id[i][v["by"]]), set()).add("+".join(sorted(A.pk[plan[i]] & cs)) or "none")
    return {g: "/".join(sorted(c)) for g, c in sorted(out.items())}


def given_plan(A, v: dict) -> dict:
    """{unit: package} of fixed plan `v` from its units-table column; an unknown or empty id, or a missing column,
    fails closed (ValueError)."""
    if any(v["column"] not in A.by_id[i] for i in A.ids):
        raise ValueError(f"plan variant {v['id']}: no column {v['column']!r} in the units table")
    bad = _package_problems({i: A.by_id[i][v["column"]] for i in A.ids}, A.pk, A.spec.raw.get("options"))
    if bad:
        raise ValueError(f"plan variant {v['id']}: " + "; ".join(bad))
    return {i: str(A.by_id[i][v["column"]]).strip() for i in A.ids}


def eligibility(A, fp: dict, feas: dict) -> dict:
    """Where a given plan breaks the signed constraints: each unit whose package they rule out (with why), and the
    caps and group rules it breaks (toolkit/analysis/moves.py infeasible_reasons, the budget apart)."""
    from toolkit.analysis import moves as MV
    bad = [{"unit": i, "package": fp[i], "reasons": list((feas.get("reasons") or {}).get(i, {}).get(fp[i]) or [])}
           for i in A.ids if not feas["feasible"][i].get(fp[i], False)]
    rest = dict(feas, feasible={i: {fp[i]: True} for i in A.ids})       # eligibility is reported unit by unit above
    return {"ineligible_units": bad, "units_ineligible": len(bad),
            "constraints_broken": MV.infeasible_reasons(A.ids, fp, {i: {fp[i]: 0.0} for i in A.ids}, rest, 0.0)}


def solved_plans(A, ben0, cost0) -> dict:
    """{variant id: plan} of each rule variant that solves on the point tables, as run() solves it (within the headline
    plan's budget), for Draws to follow over the uncertainty draws. A variant with no feasible plan is left out here
    and reported as not solved by run()."""
    out = {}
    for v in config(A.spec.raw).get("variants") or []:
        if v["type"] == "fixed_plan":
            continue
        try:
            out[v["id"]] = dict(A.solve(ben0, cost0, feas=feasibility(A, v)[1])["choice"])
        except (ValueError, RuntimeError):
            continue
    return out


def same_spend(A, vid, fp, fo, spend, ben0, cost0, feas, events) -> dict:
    """Review A2: the optimum re-solved on the same point tables and the unconstrained (signed) feasibility `feas`,
    with the budget set to fixed plan `fp`'s own cost `spend`, guarded as every other solve; what moves from the given
    plan to it. Not solved (with why) when no plan is feasible at that spend; any other solver error raises."""
    from toolkit.analysis import moves as MV
    try:
        s = A.solve(ben0, cost0, budget=spend, feas=feas)
    except RuntimeError as e:
        if "infeasible" not in str(e):
            raise
        return {"status": "not_solved", "budget": spend, "reason": str(e)}
    sp, so = dict(s["choice"]), float(s["objective"])
    A.guard(f"plan variant {vid}: the optimum at its cost", [sp], events=events)
    m = MV.summarise(A, fp, sp, ben0, cost0, so, budget=spend, feas=feas)
    return {"status": "solved", "budget": spend, "objective": so, "cost": float(sum(cost0[i][sp[i]] for i in A.ids)),
            "value_gain": so - fo, "value_gain_pct": 100.0 * (so - fo) / so if so > 0 else None,
            **{k: m[k] for k in MOVES}, "plan": sp}


def _same_spend_findings(x: dict) -> list[str]:
    """A fixed plan's same-spend block is present and agrees with its record. [] when it does."""
    s, vid = x.get("same_spend"), x.get("id")
    if not isinstance(s, dict) or s.get("status") not in ("solved", "not_solved"):
        return [f"fixed plan {vid}: no same-spend comparison (status solved or not_solved)"]
    if s["status"] == "not_solved":
        return [] if s.get("reason") else [f"fixed plan {vid}: a same-spend comparison not solved, without why"]
    if not all(_num(s, k) for k in ("budget", "objective", "cost", "value_gain")) or not isinstance(s.get("plan"), dict):
        return [f"fixed plan {vid}: the same-spend comparison has no budget, objective, cost, value gain or plan"]
    tol = lambda a, b: abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(a)), abs(float(b)))  # noqa: E731
    out = []
    if not _num(x, "cost") or not tol(s["budget"], x["cost"]) or s["cost"] > s["budget"] * (1 + 1e-9) + 1e-9:
        out.append(f"fixed plan {vid}: the same-spend budget {s['budget']} is not the plan's cost {x.get('cost')}, "
                   f"or the same-spend optimum costs more ({s['cost']})")
    if not _num(x, "objective") or not tol(s["value_gain"], s["objective"] - float(x["objective"])):
        out.append(f"fixed plan {vid}: the same-spend value gain {s['value_gain']} is not its objective minus the "
                   f"plan's ({s['objective']} - {x.get('objective')})")
    pct = 100.0 * s["value_gain"] / s["objective"] if s["objective"] > 0 else None
    if (pct is None) != (s.get("value_gain_pct") is None) or (pct is not None and not tol(pct, s["value_gain_pct"])):
        out.append(f"fixed plan {vid}: the same-spend value gain share {s.get('value_gain_pct')} is not {pct}")
    return out


def _num(x: dict, k: str) -> bool:
    return isinstance(x.get(k), (int, float)) and not isinstance(x.get(k), bool)


class Draws:
    """Each uncertainty draw's outcomes averted and cost of every fixed plan and, with `solved` ({variant id: plan},
    solved_plans()), of every solved variant (UQ.run's on_draw hook), guarded against the draw's model corrections. A
    solved variant's cost is judged against the spec's affordability rule (rule_met); a fixed plan's is reported,
    not judged."""

    def __init__(self, A, solved=None):
        self.A, self.n = A, 0
        self.plans = {v["id"]: given_plan(A, v) for v in fixed_variants(A.spec.raw)}
        self.solved = {k: dict(p) for k, p in (solved or {}).items()}
        self.follows_solved = solved is not None      # Codex r1: then every variant run() solves must be here
        self.plans.update(self.solved)
        self.y = {k: {o: [] for o in A.outcomes} for k in self.plans}
        self.cost = {k: [] for k in self.plans}

    def add(self, out, events=(), cost=None):
        A, d = self.A, self.n
        if cost is None:
            raise ValueError("fixed plan draws: the draw's cost table is required")
        self.n += 1
        for k, p in self.plans.items():
            A.guard(f"plan variant {k}, uncertainty draw {d}", [p], events=list(events))
            for o in A.outcomes:
                self.y[k][o].append(sum(out[i]["y"][A.comp][o] - out[i]["y"][p[i]][o] for i in A.ids))
            self.cost[k].append(sum(float(cost[i][p[i]]) for i in A.ids))

    def summary(self, k: str, interval: dict) -> dict:
        from toolkit.analysis import uncertainty as UQ
        from toolkit.analysis.core import q
        lvl = (interval or {}).get("level", 0.95)
        rule = ((self.A.spec.raw.get("uncertainty") or {}).get("affordability") or None) if k in self.solved else None
        return {"draws": self.n, "interval": interval, "averted": {o: q(v, lvl) for o, v in self.y[k].items()},
                "affordability": UQ.affordability(self.cost[k], self.A.budget, rule, lvl)}


def run(A, ben0, cost0, plan, events=None, draws=None, interval=None) -> dict | None:
    """The plan_variants artifact: the unconstrained plan and every variant, solved on the point tables within the
    headline plan's budget (A.solve: the reserve when enforced), each guarded against the point evaluation's model
    corrections `events` (Codex S1-6 r1 P1-1); every fixed plan evaluated as given, with its draws from `draws` (a
    Draws fed by the uncertainty step; None: the point estimate only). None when the spec asks for none."""
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
        if v["type"] == "fixed_plan":         # F6 S2-6: evaluated as given, never solved (fails closed on bad ids)
            fp = given_plan(A, v)
            A.guard(f"plan variant {v['id']}", [fp], events=ev)
            fo = float(sum(ben0[i][fp[i]] for i in A.ids))
            bf = getattr(A, "base_feas", None) or A.feas
            m = MV.summarise(A, up, fp, ben0, cost0, fo, feas=bf)
            r, drawn = row(fp, fo), draws is not None and draws.n > 0      # no draws: the point estimate only
            rec.update(r, status="evaluated", column=v["column"], plan=dict(fp), value_lost=uo - fo,
                       same_spend=same_spend(A, v["id"], fp, fo, r["cost"], ben0, cost0, bf, ev),
                       value_lost_pct=100.0 * (uo - fo) / uo if uo > 0 else None, **{k: m[k] for k in MOVES},
                       budget_allocatable=float(A.budget), cost_minus_budget=r["cost"] - float(A.budget),
                       within_budget=r["cost"] <= float(A.budget) * (1 + 1e-9), **eligibility(A, fp, bf),
                       uncertainty=draws.summary(v["id"], interval) if drawn else None,
                       uncertainty_note=("outcome and cost over the uncertainty draws" if drawn else
                                         "point estimate only: no uncertainty draws were collected"))
            out["variants"].append(rec)
            continue
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
                   **{k: m[k] for k in MOVES})
        if draws is not None and draws.n > 0 and draws.follows_solved and v["id"] not in draws.solved:
            # Codex r1 P1: its pre-draw solve failed and this one succeeded, so no draw followed (or guarded) it
            raise RuntimeError(f"plan variant {v['id']}: solved here but not followed over the uncertainty draws")
        if draws is not None and v["id"] in draws.solved:     # its cost and outcome over the uncertainty draws
            if draws.solved[v["id"]] != dict(vp):
                raise RuntimeError(f"plan variant {v['id']}: the uncertainty draws followed another plan than its "
                                   "solved plan")
            if draws.n > 0:
                rec["uncertainty"] = draws.summary(v["id"], interval)
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
    A.outcomes = ["dalys"]
    r0, e0b = attempt(lambda: run(A, ben, cost, plan, [], Draws(A), {"level": 0.9}))
    f0 = {x["id"]: x for x in (r0 or {}).get("variants", [])}.get("given") or {}
    ok(e0b is None and f0.get("uncertainty") is None and "point" in str(f0.get("uncertainty_note")),
       f"PV21b a draw collector that saw no draws (draws: 0) gives the point estimate only ({e0b!r})")
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
    # review A6: a solved variant's cost in each draw too, judged against the spec's affordability rule
    A.spec.raw["uncertainty"] = {"affordability": {"max_share_over_budget": 0.25}}
    sp, e8 = attempt(g_("solved_plans"), A, ben, cost)
    D2, e9 = attempt(g_("Draws"), A, sp)
    for t in tabs:
        attempt(lambda *a: D2.add(*a), out, [], t)
    r8, e10 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [], D2, {"level": 0.9})
    v8 = {x["id"]: x for x in (r8 or {}).get("variants", [])}
    n8 = v8.get("nets") or {}
    a8 = (n8.get("uncertainty") or {}).get("affordability") or {}
    over8 = sum(m * n8.get("cost", 0.0) > A.budget * (1 + 1e-9) for m in (0.8, 1.2))
    ok(e8 is None and e9 is None and e10 is None and n8.get("status") == "solved" and (sp or {}).get("nets") == n8.get("plan")
       and a8.get("draws") == 2 and a8.get("draws_over_budget") == over8 == 1 and a8.get("rule_met") is False
       and ((v8.get("given") or {}).get("uncertainty") or {}).get("affordability", {}).get("rule_met") is None,
       f"PV31 a solved variant's cost is counted against the budget in each draw and judged against the spec's rule; "
       f"a given plan is reported, not judged ({e8!r}, {e9!r}, {e10!r}, {a8.get('draws_over_budget')} of "
       f"{a8.get('draws')} over, rule_met {a8.get('rule_met')!r})")
    A.budget = 1.0
    sp9, e11 = attempt(g_("solved_plans"), A, ben, cost)
    A.budget = 2.0
    ok(e11 is None and sp9 == {},
       f"PV32 a variant the budget makes infeasible has no plan to follow over the draws ({e11!r}, {sp9})")
    D3, _ = attempt(g_("Draws"), A, {"nets": {i: "none" for i in A.ids}})
    for t in tabs:
        attempt(lambda *a: D3.add(*a), out, [], t)
    _, e12 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [], D3, {"level": 0.9})
    ok(isinstance(e12, RuntimeError) and "nets" in str(e12),
       f"PV33 draws collected for another plan than the variant's solved plan fail closed ({e12!r})")
    # Codex r1 on A6. P1: a variant left out of the draws (its pre-draw solve failed) and solved later is refused.
    D4, _ = attempt(g_("Draws"), A, {})
    for t in tabs:
        attempt(lambda *a: D4.add(*a), out, [], t)
    _, e13 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [], D4, {"level": 0.9})
    ok(isinstance(e13, RuntimeError) and "nets" in str(e13),
       f"PV34 a solved variant the draws did not follow fails closed, not published without its uncertainty ({e13!r})")
    # P2: package-specific draw costs and outcomes; the variant's quantiles are its own plan's, not the headline's
    from toolkit.analysis.core import q as _q
    mult = ({"none": 1, "n1": 0.5, "n2": 2.0, "s": 1.0}, {"none": 1, "n1": 2.0, "n2": 0.5, "s": 1.5})
    tabs2 = [{i: {k: m[k] * cost[i][k] for k in A.pk} for i in A.ids} for m in mult]
    outs2 = [{i: {"y": {k: {"dalys": 10.0 - f * ben[i][k]} for k in A.pk}} for i in A.ids} for f in (1.0, 3.0)]
    sp5 = solved_plans(A, ben, cost)
    D5 = Draws(A, sp5)
    for o_, t in zip(outs2, tabs2):
        D5.add(o_, [], t)
    hp5 = A.solve(ben, cost)["choice"]
    r14, e14 = attempt(run, A, ben, cost, hp5, [], D5, {"level": 0.9})
    n14 = {x["id"]: x for x in (r14 or {}).get("variants", [])}.get("nets") or {}
    vp = n14.get("plan") or {}
    want_c = [sum(t[i][vp[i]] for i in A.ids) for t in tabs2] if vp else []
    head_c = [sum(t[i][hp5[i]] for i in A.ids) for t in tabs2]
    want_y = [sum(o_[i]["y"]["none"]["dalys"] - o_[i]["y"][vp[i]]["dalys"] for i in A.ids) for o_ in outs2] if vp else []
    u14 = n14.get("uncertainty") or {}
    a14 = u14.get("affordability") or {}
    ok(e14 is None and vp != dict(hp5) and want_c != head_c
       and a14.get("per_draw_cost") == want_c and a14.get("fixed_plan_cost") == _q(want_c, 0.9)
       and u14.get("averted", {}).get("dalys") == _q(want_y, 0.9)
       and a14.get("draws_over_budget") == sum(c > A.budget * (1 + 1e-9) for c in want_c),
       f"PV35 a solved variant's draw costs and outcomes are its own plan's, package by package ({e14!r}, "
       f"{a14.get('per_draw_cost')} vs {want_c}, headline {head_c})")
    # P2: the contract checks the affordability record against its own draws and the spec's rule
    pt14 = {"headline_variant": None, "plan": dict(hp5)}
    c0 = contract_findings(A.spec.raw, pt14, r14) if r14 else ["no record"]
    bad_recs = []
    for mutate in (lambda a, u: a.update(rule_met=not a["rule_met"]),
                   lambda a, u: a.update(draws_over_budget=a["draws_over_budget"] + 1),
                   lambda a, u: a.update(rule={"max_share_over_budget": 0.99}),
                   lambda a, u: a.update(budget_allocatable=a["budget_allocatable"] * 2),
                   lambda a, u: u.update(draws=u["draws"] + 1),
                   lambda a, u: u.pop("affordability")):
        rr = copy.deepcopy(r14 or {})
        nr = next((x for x in rr.get("variants", []) if x.get("id") == "nets"), None)
        if nr and nr.get("uncertainty"):
            mutate(nr["uncertainty"].get("affordability", {}), nr["uncertainty"])
        bad_recs.append(contract_findings(A.spec.raw, pt14, rr))
    rr = copy.deepcopy(r14 or {})
    for x in rr.get("variants", []):
        if x.get("id") == "nets":
            x.pop("uncertainty", None)
    bad_recs.append(contract_findings(A.spec.raw, pt14, rr))
    ok(c0 == [] and all(bad_recs),
       f"PV36 a consistent record passes (rule not met included); a verdict, count, rule, budget or draw count that "
       f"contradicts the draws, or a solved variant missing its draws, is a contract finding ({c0}, "
       f"{[len(b) for b in bad_recs]})")
    del A.spec.raw["uncertainty"]
    # contract: a fixed plan is evaluated (never solved), and its record carries its eligibility report
    raw = {"analysis": {"plan_variants": {"variants": [ms, fx]}}}
    rec = {"id": "given", "type": "fixed_plan", "status": "evaluated", "units_ineligible": 1, "cost": 2.5,
           "ineligible_units": [{"unit": "u1", "package": "s", "reasons": ["toy"]}], "cost_minus_budget": 0.5,
           "same_spend": {"status": "not_solved", "budget": 2.5, "reason": "toy"}}
    art = {"headline": "unconstrained", "variants": [{"id": "nets", "type": "minimum_service", "status": "solved"}, rec]}
    pt = {"plan": {}, "headline_variant": None}
    cf = lambda v: attempt(contract_findings, raw, pt, v)[0]  # noqa: E731
    ok(cf(art) == [] and cf(dict(art, variants=[dict(art["variants"][0], status="evaluated"), rec]))
       and cf(dict(art, variants=[art["variants"][0], dict(rec, status="solved")]))
       and cf(dict(art, variants=[art["variants"][0], {k: x for k, x in rec.items() if k != "ineligible_units"}])),
       "PV27 the contract: a fixed plan is evaluated, a solved variant is not, and the eligibility report is required")
    # Codex S2-6 r1 P1-2: any column name holds the plan (no internal key shadows it)
    for u in A.spec.units():
        u["__id"] = "none"
    p8, e8 = attempt(g_("given_plan"), A, dict(fx, column="__id"))
    ok(e8 is None and set((p8 or {}).values()) == {"none"}, f"PV28 a plan column of any name is read as given ({e8!r})")
    # Codex S2-6 r1 P1-1: a package the envelope pruning dropped is named as pruned, not as unknown
    raw9 = {"options": {"components": {"a": {}, "b": {}}, "packages": {"generate": True, "within_envelope": True}},
            "analysis": {"plan_variants": {"variants": [dict(fx, column="gc")]}}}
    e9, _ = attempt(validate, raw9, {"id", "gc"}, [{"id": "u1", "gc": "a+b"}],
                    {"none": frozenset(), "a": frozenset({"a"}), "b": frozenset({"b"})}, "id")
    ok(any("within_envelope" in x for x in e9 or []),
       f"PV29 a given package that envelope pruning dropped is refused with why, not as unknown ({e9})")


def _self_test_same_spend(ok, attempt):
    """Review A2: a fixed plan is also compared with the optimum re-solved at its own cost (equal spend)."""
    import itertools
    fx = {"id": "given", "type": "fixed_plan", "column": "gc", "text": "the plan as funded"}

    def setup(given):
        A, ben, cost, _ms, _gr = _toy()
        for u in A.spec.units():
            u["gc"] = given[u["id"]]
        A.spec.raw["analysis"]["plan_variants"] = {"variants": [fx]}
        return A, ben, cost

    def brute(A, ben, cost, budget):        # every combination of the toy's packages, within budget and eligibility
        best = None
        for combo in itertools.product(sorted(A.pk), repeat=len(A.ids)):
            p = dict(zip(A.ids, combo))
            if any(not A.feas["feasible"][i].get(p[i], False) for i in A.ids):
                continue
            if sum(cost[i][p[i]] for i in A.ids) > budget * (1 + 1e-9):
                continue
            o = sum(ben[i][p[i]] for i in A.ids)
            best = o if best is None or o > best else best
        return best

    given = {"u1": "none", "u2": "none", "u3": "none", "u4": "s"}        # costs 1.0, below the budget 2.0
    A, ben, cost = setup(given)
    r, e = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [])
    f = {x["id"]: x for x in (r or {}).get("variants", [])}.get("given") or {}
    s = f.get("same_spend") or {}
    want = brute(A, ben, cost, 1.0)
    ok(e is None and s.get("status") == "solved" and abs(s.get("budget", -1) - 1.0) < 1e-9
       and s.get("cost", 9) <= f.get("cost", 0) + 1e-9 and s.get("objective", -1) >= f.get("objective", 9) - 1e-9
       and abs(s.get("objective", -1) - want) < 1e-9 and abs(s.get("value_gain", -1) - (want - 0.6)) < 1e-9
       and abs(s.get("value_gain_pct", -1) - 100 * (want - 0.6) / want) < 1e-9
       and set(MOVES) <= set(s) and isinstance(s.get("plan"), dict),
       f"PV37 a given plan cheaper than the budget is compared with the optimum at its own cost, as a brute force "
       f"over the toy's packages finds it ({e!r}, {s.get('objective')} vs {want}, gain {s.get('value_gain')})")
    up = (r or {}).get("unconstrained", {}).get("plan")
    ok(e is None and s.get("plan") != up and s.get("objective", 99) < r["unconstrained"]["objective"] - 1e-9
       and abs(f.get("value_lost", 0) - (14.0 - 0.6)) < 1e-9 and s.get("units_changed") == 3,
       f"PV38 at a spend below the budget the same-spend optimum is not the unconstrained plan; value_lost is kept "
       f"as it was ({s.get('plan')} vs {up})")
    A, ben, cost = setup({i: "none" for i in ("u1", "u2", "u3", "u4")})         # costs 0; u4 must take a package
    A.feas["feasible"]["u4"]["none"] = False
    r3, e3 = attempt(run, A, ben, cost, A.solve(ben, cost)["choice"], [])
    s3 = ({x["id"]: x for x in (r3 or {}).get("variants", [])}.get("given") or {}).get("same_spend") or {}
    ok(e3 is None and s3.get("status") == "not_solved" and "infeasible" in str(s3.get("reason")),
       f"PV39 no feasible plan at the given plan's cost: the same-spend block is not solved, with why, no crash "
       f"({e3!r}, {s3})")
    A, ben, cost = setup(given)
    r4 = run(A, ben, cost, A.solve(ben, cost)["choice"], []) if e is None else {"variants": []}
    pt, raw = {"plan": {}, "headline_variant": None}, A.spec.raw

    def mut(fn):
        rr = copy.deepcopy(r4)
        _, err = attempt(fn, next((y for y in rr["variants"] if y.get("id") == "given"), {}))
        return None if err else attempt(contract_findings, raw, pt, rr)[0]
    bad = [mut(lambda x: x.pop("same_spend", None)),
           mut(lambda x: x["same_spend"].update(budget=x["same_spend"]["budget"] + 1)),
           mut(lambda x: x["same_spend"].update(value_gain=x["same_spend"]["value_gain"] + 1)),
           mut(lambda x: x["same_spend"].update(status="maybe")),
           mut(lambda x: x.update(same_spend="solved"))]
    good = attempt(contract_findings, raw, pt, r4)[0]
    ok(good == [] and all(b and any("same" in m for m in b) for b in bad),
       f"PV40 the contract requires a consistent same-spend block for each evaluated fixed plan ({good}, {bad})")
    _self_test_same_spend_r1(ok, attempt, setup, mut)


def _self_test_same_spend_r1(ok, attempt, setup, mut):
    """Codex round 1 on the same-spend comparison."""
    from toolkit.analysis import claims as CL
    from toolkit.analysis.core import AnalysisBlocked
    given = {"u1": "none", "u2": "none", "u3": "none", "u4": "s"}
    # P2-1: the gain in undiscounted outcomes, the objective difference apart
    A, ben, cost = setup(given)
    A.dfac = 0.5
    out = {i: {"y": {k: {"dalys": 100.0 - ben[i][k] / A.dfac} for k in A.pk}} for i in A.ids}
    r, e = attempt(lambda: run(A, ben, cost, A.solve(ben, cost)["choice"], [], None, None, out))
    s = ({x["id"]: x for x in (r or {}).get("variants", [])}.get("given") or {}).get("same_spend") or {}
    cl = {c["id"]: c for c in (CL.variant_claims(A, {"plan_variants": "pv"}, r) if r else [])}
    md = CL.variants_markdown(A, r) if r else ""
    ok(e is None and abs(s.get("objective_gain", 0) - 7.4) < 1e-9 and abs(s.get("value_gain", 0) - 14.8) < 1e-9
       and abs(s.get("value_gain_pct", 0) - 100 * 14.8 / 16) < 1e-9
       and (cl.get("variant.given.same_spend_gain") or {}).get("source_field", "").endswith("same_spend.value_gain")
       and "undiscounted" in md and "[CLAIM:variant.given.same_spend_gain]" in md,
       f"PV41 with discounting, the same-spend gain is in undiscounted outcomes and the claim uses it; the objective "
       f"difference is apart ({e!r}, {s.get('value_gain')}, {s.get('objective_gain')})")
    # P2-2: not solved is refused for a feasible given plan, and needs the plan's cost as its budget
    ns = {"status": "not_solved", "reason": "optimizer status infeasible"}
    feas_ns = mut(lambda x: x.update(same_spend=dict(ns, budget=x["cost"])))
    no_b = mut(lambda x: x.update(same_spend=dict(ns), units_ineligible=1, ineligible_units=[{"unit": "u1"}]))
    bad_b = mut(lambda x: x.update(same_spend=dict(ns, budget=x["cost"] + 1), units_ineligible=1,
                                   ineligible_units=[{"unit": "u1"}]))
    ok_ns = mut(lambda x: x.update(same_spend=dict(ns, budget=x["cost"]), units_ineligible=1,
                                   ineligible_units=[{"unit": "u1"}]))
    broken = mut(lambda x: x.update(same_spend=dict(ns, budget=x["cost"]), constraints_broken=["cap c"]))
    ok(all(b and any("same" in m for m in b) for b in (feas_ns, no_b, bad_b)) and ok_ns == [] and broken == [],
       f"PV42 not solved is a finding for a fixed plan feasible under the signed constraints, or without its cost as "
       f"budget ({feas_ns}, {no_b}, {bad_b}, {ok_ns}, {broken})")
    # P2-3: the plan covers the fixed plan's units with package ids; numbers finite; never an exception
    bad = [mut(lambda x: x["same_spend"]["plan"].pop("u1")),
           mut(lambda x: x["same_spend"]["plan"].update(u9="n1")),
           mut(lambda x: x["same_spend"]["plan"].update(u1="")),
           mut(lambda x: x["same_spend"]["plan"].update(u1=3)),
           mut(lambda x: x["same_spend"].update(objective=float("nan"))),
           mut(lambda x: x["same_spend"].update(cost="a lot")),
           mut(lambda x: x["same_spend"].update(value_gain=float("inf"))),
           mut(lambda x: x.update(cost=None)),
           mut(lambda x: x.update(objective="high"))]
    ok(all(b and any("same" in m for m in b) for b in bad),
       f"PV43 a same-spend plan off the fixed plan's units or without package ids, or a field that is not a finite "
       f"number, is a finding, not an exception ({bad})")
    # note: with a headline variant, the same-spend solve uses the unconstrained feasibility
    A, ben, cost = setup(given)
    ms = {"id": "nets", "type": "minimum_service", "components": ["n1", "n2"], "text": "a net in every unit"}
    A.spec.raw["analysis"]["plan_variants"] = {"variants": [ms, A.spec.raw["analysis"]["plan_variants"]["variants"][0]],
                                               "headline": "nets"}
    _, eh = attempt(apply_headline, A)
    r5, e5 = attempt(lambda: run(A, ben, cost, A.solve(ben, cost)["choice"], []))
    s5 = ({x["id"]: x for x in (r5 or {}).get("variants", [])}.get("given") or {}).get("same_spend") or {}
    ok(eh is None and e5 is None and s5.get("status") == "solved" and abs(s5.get("objective", 0) - 8.0) < 1e-9,
       f"PV44 with a variant headline the same-spend optimum is solved under the unconstrained feasibility "
       f"({eh!r}, {e5!r}, {s5.get('status')}, {s5.get('objective')})")
    # note: a correction on a package only the same-spend optimum uses stops the analysis
    A, ben, cost = setup({"u1": "none", "u2": "none", "u3": "none", "u4": "s"})
    ben["u3"]["n1"] = 5.5          # at cost 1 the optimum is n1 in u1 and u3; u3 takes s in the unconstrained plan
    r6, e6 = attempt(lambda: run(A, ben, cost, A.solve(ben, cost)["choice"], [
        {"unit": "u3", "package": "n1", "kind": "extrapolation"}]))
    ok(isinstance(e6, AnalysisBlocked) and "cost" in str(e6),
       f"PV45 a correction on a package only the same-spend optimum uses stops the analysis ({e6!r})")


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
    _self_test_same_spend(ok, attempt)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
