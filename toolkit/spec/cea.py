#!/usr/bin/env python3
"""Cost-effectiveness spec section and standalone `cost_effectiveness` deliverable (design §4), fail closed.

    python3 toolkit/spec/cea.py --self-test

Supports both:
- Standalone `deliverable.kind: cost_effectiveness` (strategy comparison without an envelope or allocation file)
- Add-on top-level `cost_effectiveness:` section on a `package_per_unit` allocation spec

Every field rule from `docs/design/cost-effectiveness-deliverable.md` §4.2 is checked here so downstream
`toolkit/analysis/cea.py` operates strictly on signed, validated declarations. Nothing here names a disease.
"""
from __future__ import annotations

import csv
import math
import os
import sys
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from toolkit.alloc import costs as acosts  # noqa: E402

KIND = "cost_effectiveness"
HEALTH_WEIGHTS = "health_weights"
PERSPECTIVES = ("health_system", "societal")
PAYERS = ("health_system", "household", "other_sector", "productivity")
NON_HS_PAYERS = ("household", "other_sector", "productivity")
HEALTH_KINDS = ("dalys", "qalys")
TIMINGS = ("uniform_accrual", "annual_series")
DEATH_HORIZONS = ("lifetime", "window")
PRICE_ROLES = ("negotiated", "unknown")
MAX_DISCOUNT_RATE = 0.15


def _isnum(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x))


def is_cea(raw) -> bool:
    """True when the spec's deliverable kind is standalone `cost_effectiveness`."""
    return isinstance(raw, Mapping) and isinstance(raw.get("deliverable"), Mapping) and raw["deliverable"].get("kind") == KIND


def has_cea(raw) -> bool:
    """True when the spec is standalone `cost_effectiveness` or carries the `cost_effectiveness` add-on section."""
    return isinstance(raw, Mapping) and (is_cea(raw) or raw.get("cost_effectiveness") is not None)


def load_life_table(spec) -> tuple[list[tuple[float, float]], list[str]]:
    """Read `(age, remaining_years)` rows from `cost_effectiveness.health_measure.life_table` sorted by age."""
    ce = (spec.raw.get("cost_effectiveness") or {}) if hasattr(spec, "raw") else (spec.get("cost_effectiveness") or {})
    hm = ce.get("health_measure") if isinstance(ce, Mapping) else {}
    lt = hm.get("life_table") if isinstance(hm, Mapping) else None
    if not isinstance(lt, Mapping):
        return [], ["cost_effectiveness.health_measure.life_table: mapping {csv, age, remaining, source} required"]
    for k in ("csv", "age", "remaining", "source"):
        if not str(lt.get(k) or "").strip():
            return [], [f"cost_effectiveness.health_measure.life_table.{k}: required"]
    path = spec.resolve(str(lt["csv"])) if hasattr(spec, "resolve") else str(lt["csv"])
    if not os.path.isfile(path):
        return [], [f"cost_effectiveness.health_measure.life_table.csv: unreadable ({lt['csv']})"]
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except OSError as e:
        return [], [f"cost_effectiveness.health_measure.life_table.csv: unreadable ({e})"]
    if not rows:
        return [], ["cost_effectiveness.health_measure.life_table: table is empty"]
    ac, rc = str(lt["age"]), str(lt["remaining"])
    if ac not in rows[0] or rc not in rows[0]:
        return [], [f"cost_effectiveness.health_measure.life_table: missing column(s) {[c for c in (ac, rc) if c not in rows[0]]}"]
    pairs: list[tuple[float, float]] = []
    for i, r in enumerate(rows):
        try:
            a, ex = float(r[ac]), float(r[rc])
        except (TypeError, ValueError):
            return [], [f"cost_effectiveness.health_measure.life_table row {i + 1}: non-numeric age or remaining"]
        if not (math.isfinite(a) and math.isfinite(ex) and a >= 0.0 and ex >= 0.0):
            return [], [f"cost_effectiveness.health_measure.life_table row {i + 1}: age and remaining must be finite >= 0"]
        pairs.append((a, ex))
    pairs.sort(key=lambda t: t[0])
    if len({a for a, _ in pairs}) != len(pairs):
        return [], ["cost_effectiveness.health_measure.life_table: duplicate age entries"]
    return pairs, []


def interpolate_life_table(pairs: list[tuple[float, float]], age: float) -> float:
    """Linearly interpolate remaining life expectancy at `age` inside the table bounds (fails closed outside)."""
    if not pairs:
        raise ValueError("empty life table")
    a = float(age)
    if a < pairs[0][0] or a > pairs[-1][0]:
        raise ValueError(f"age {a:g} lies outside life table range [{pairs[0][0]:g}, {pairs[-1][0]:g}]")
    for i, (ai, exi) in enumerate(pairs):
        if a == ai:
            return float(exi)
        if ai > a:
            a0, ex0 = pairs[i - 1]
            w = (a - a0) / (ai - a0)
            return float(ex0 + w * (exi - ex0))
    return float(pairs[-1][1])


def _check_price_item(it: Mapping, prices: Mapping, label: str) -> list[str]:
    bad: list[str] = []
    if not _isnum(it.get("value")) or float(it["value"]) < 0.0:
        bad.append(f"{label}.value: non-negative finite number required")
    rule = it.get("price_rule")
    if rule not in acosts.RULES:
        bad.append(f"{label}.price_rule: must be one of {', '.join(acosts.RULES)}")
        return bad
    base = prices.get("base_year")
    idx = {str(k): v for k, v in (prices.get("index") or {}).items()}
    py = str(it.get("price_year"))
    if rule == "index":
        if py not in idx or str(base) not in idx:
            bad.append(f"{label}: price_rule index needs options.prices.index entries for {py} and base_year {base}")
    elif rule == "local_index":
        loc = prices.get("local") or {}
        xr = {str(k): v for k, v in (loc.get("exchange_rate") or {}).items()}
        li = {str(k): v for k, v in (loc.get("index") or {}).items()}
        miss = [f"{n} {y}" for n, t in (("exchange_rate", xr), ("index", li)) for y in (py, str(base)) if y not in t]
        if miss:
            bad.append(f"{label}: price_rule local_index needs options.prices.local entries: {', '.join(miss)}")
    elif it.get("price_year") is None:
        bad.append(f"{label}.price_year: required")
    return bad


def problems(spec, card=None, pk: Mapping | None = None) -> list[str]:
    """Fail-closed validation of the `cost_effectiveness` section and standalone deliverable rules (§4.2)."""
    raw = spec.raw if hasattr(spec, "raw") else spec
    bad: list[str] = []
    standalone = is_cea(raw)
    if standalone:
        cons = raw.get("constraints")
        if isinstance(cons, Mapping) and "envelope_usd" in cons:
            bad.append("constraints.envelope_usd: not applicable to deliverable.kind cost_effectiveness "
                       "(budget impact is reported, not constrained)")
        deliv = raw.get("deliverable") or {}
        if "allocation_file" in deliv:
            bad.append("deliverable.allocation_file: not applicable to deliverable.kind cost_effectiveness")
    ce = raw.get("cost_effectiveness")
    if not isinstance(ce, Mapping):
        return bad + ["cost_effectiveness: required mapping when deliverable.kind is cost_effectiveness"]

    opts = raw.get("options") or {}
    pkgs = dict(pk) if pk is not None else (spec.packages() if hasattr(spec, "packages") else acosts.packages(opts))
    prices_cfg = opts.get("prices") or {}
    cost_items = {str(it.get("id")): it for it in (opts.get("cost_items") or []) if isinstance(it, Mapping) and it.get("id")}

    # 1. strategies, reference, context
    strats = ce.get("strategies")
    if not isinstance(strats, list) or len(strats) < 2 or not all(isinstance(x, str) and x for x in strats):
        bad.append("cost_effectiveness.strategies: list of at least two package ids required")
        strat_set: set[str] = set()
    else:
        if len(set(strats)) != len(strats):
            bad.append("cost_effectiveness.strategies: duplicate strategy ids")
        strat_set = set(strats)
        unknown_s = [x for x in strats if x not in pkgs]
        if unknown_s:
            bad.append(f"cost_effectiveness.strategies: unknown package(s) {unknown_s}")
    ref = ce.get("reference")
    if not isinstance(ref, str) or ref not in strat_set:
        bad.append(f"cost_effectiveness.reference: {ref!r} must be listed in cost_effectiveness.strategies")
    ctx = ce.get("context") or []
    if not isinstance(ctx, list) or any(x not in strat_set for x in ctx):
        bad.append("cost_effectiveness.context: every context entry must be listed in cost_effectiveness.strategies")

    # 2. perspectives, payers, consequence_costs
    persp = ce.get("perspectives")
    if not isinstance(persp, list) or not persp or any(p not in PERSPECTIVES for p in persp):
        bad.append(f"cost_effectiveness.perspectives: non-empty list of {PERSPECTIVES}")
        persp_list: list[str] = []
    else:
        if len(set(persp)) != len(persp):
            bad.append("cost_effectiveness.perspectives: duplicate perspective entries")
        persp_list = list(persp)

    payers = ce.get("payers") or {}
    if not isinstance(payers, Mapping):
        bad.append("cost_effectiveness.payers: mapping {cost_item_id: payer}")
        payers = {}
    else:
        for cid, py in payers.items():
            if str(cid) not in cost_items:
                bad.append(f"cost_effectiveness.payers.{cid}: unknown options.cost_items id")
            if py not in PAYERS:
                bad.append(f"cost_effectiveness.payers.{cid}: payer {py!r} not in {PAYERS}")

    cc_list = ce.get("consequence_costs") or []
    cc_payers: set[str] = set()
    if not isinstance(cc_list, list):
        bad.append("cost_effectiveness.consequence_costs: a list")
        cc_list = []
    else:
        seen_cc: set[str] = set()
        per_out_items = {
            str(it.get("outcome")): str(it.get("id"))
            for it in cost_items.values()
            if it.get("basis") == "per_outcome" and it.get("outcome")
        }
        cost_outputs = (raw.get("analysis") or {}).get("cost_outputs") or {}
        per_cap = set((card.outputs or {}).get("per_capita") or []) if card is not None else set()
        for i, cc in enumerate(cc_list):
            if not isinstance(cc, Mapping):
                bad.append(f"cost_effectiveness.consequence_costs[{i}]: must be a mapping")
                continue
            cid = str(cc.get("id") or f"[{i}]")
            lbl = f"cost_effectiveness.consequence_costs.{cid}"
            if cid in seen_cc:
                bad.append(f"{lbl}: duplicate id")
            seen_cc.add(cid)
            oc = str(cc.get("outcome") or "").strip()
            if not oc:
                bad.append(f"{lbl}.outcome: required")
            elif per_cap and oc not in per_cap:
                bad.append(f"{lbl}.outcome: {oc!r} is not an engine per-capita output {sorted(per_cap)}")
            py = cc.get("payer")
            if py not in PAYERS:
                bad.append(f"{lbl}.payer: must be one of {PAYERS}")
            else:
                cc_payers.add(str(py))
            if not str(cc.get("source") or "").strip():
                bad.append(f"{lbl}.source: required")
            bad += _check_price_item(cc, prices_cfg, lbl)
            if "range" in cc:
                rng = cc["range"]
                if not (isinstance(rng, list) and len(rng) == 2 and _isnum(rng[0]) and _isnum(rng[1])
                        and 0.0 <= float(rng[0]) < float(rng[1])):
                    bad.append(f"{lbl}.range: [lo, hi] with 0 <= lo < hi required")
                elif _isnum(cc.get("value")) and not (float(rng[0]) <= float(cc["value"]) <= float(rng[1])):
                    bad.append(f"{lbl}.range: must contain value {cc['value']}")
            # Double-count guard against per_outcome component cost items referencing the same outcome
            mapped_out = next((cname for cname, cd in cost_outputs.items()
                               if isinstance(cd, Mapping) and cd.get("cost_driver") == oc), None)
            overlap_item = per_out_items.get(oc) or (per_out_items.get(mapped_out) if mapped_out else None)
            if overlap_item and not str(cc.get("not_double_counted") or "").strip():
                bad.append(f"{lbl}: outcome {oc!r} also has per_outcome cost item {overlap_item!r}; "
                           "provide `not_double_counted: <reason>`")

    all_payers = set(payers.values()) | cc_payers
    if "societal" in persp_list and not (all_payers & set(NON_HS_PAYERS)):
        bad.append("cost_effectiveness.perspectives: societal perspective requires at least one non-health_system "
                   f"cost item or consequence cost (payer in {NON_HS_PAYERS}); otherwise it repeats health_system")

    # 3. time_horizon
    th = ce.get("time_horizon")
    hor = raw.get("horizon") or {}
    if not isinstance(th, Mapping):
        bad.append("cost_effectiveness.time_horizon: mapping {start, stop, deaths} required")
    else:
        ts, te = th.get("start"), th.get("stop")
        if not (_isnum(ts) and _isnum(te) and float(ts) < float(te)):
            bad.append("cost_effectiveness.time_horizon: finite start < stop required")
        elif _isnum(hor.get("start")) and _isnum(hor.get("stop")):
            if float(ts) < float(hor["start"]) - 1e-9 or float(te) > float(hor["stop"]) + 1e-9:
                bad.append("cost_effectiveness.time_horizon: must lie inside spec horizon")
        if th.get("deaths") not in DEATH_HORIZONS:
            bad.append(f"cost_effectiveness.time_horizon.deaths: one of {DEATH_HORIZONS}")

    # 4. discounting
    disc = ce.get("discounting")
    if not isinstance(disc, Mapping):
        bad.append("cost_effectiveness.discounting: mapping {costs, health, base_year, timing} required")
    else:
        for k in ("costs", "health"):
            sub = disc.get(k)
            r_val = sub.get("rate") if isinstance(sub, Mapping) else None
            if not (_isnum(r_val) and 0.0 <= float(r_val) <= MAX_DISCOUNT_RATE):
                bad.append(f"cost_effectiveness.discounting.{k}.rate: number in [0, {MAX_DISCOUNT_RATE}] required")
        if not _isnum(disc.get("base_year")):
            bad.append("cost_effectiveness.discounting.base_year: numeric year required")
        timing = disc.get("timing")
        if timing not in TIMINGS:
            bad.append(f"cost_effectiveness.discounting.timing: one of {TIMINGS}")
        elif timing == "annual_series" and card is not None and not (card.outputs or {}).get("supports_annual_series"):
            bad.append("cost_effectiveness.discounting.timing: annual_series requires engine card supports_annual_series")
        obj_disc = (raw.get("objective") or {}).get("discounting")
        if isinstance(obj_disc, Mapping) and _isnum(obj_disc.get("rate")):
            hr = (disc.get("health") or {}).get("rate") if isinstance(disc.get("health"), Mapping) else None
            if _isnum(hr) and abs(float(obj_disc["rate"]) - float(hr)) > 1e-12:
                bad.append("cost_effectiveness.discounting.health.rate: must equal objective.discounting.rate")

    # 5. health_measure
    hm = ce.get("health_measure")
    has_ranged_weight = False
    if not isinstance(hm, Mapping):
        bad.append("cost_effectiveness.health_measure: mapping {kind, events, ...} required")
    else:
        hkind = hm.get("kind")
        if hkind not in HEALTH_KINDS:
            bad.append(f"cost_effectiveness.health_measure.kind: one of {HEALTH_KINDS}")
        events = hm.get("events")
        if not isinstance(events, Mapping) or not events:
            bad.append("cost_effectiveness.health_measure.events: non-empty mapping required")
        else:
            per_cap = set((card.outputs or {}).get("per_capita") or []) if card is not None else set()
            has_death_event = False
            for ev_name, ev_cfg in events.items():
                lbl = f"cost_effectiveness.health_measure.events.{ev_name}"
                if per_cap and ev_name not in per_cap:
                    bad.append(f"{lbl}: {ev_name!r} is not an engine per-capita output {sorted(per_cap)}")
                if not isinstance(ev_cfg, Mapping):
                    bad.append(f"{lbl}: must be a mapping")
                    continue
                if ev_cfg.get("death") is True:
                    has_death_event = True
                    continue
                wkey = "yld" if hkind == "dalys" else "qaly"
                w_sub = ev_cfg.get(wkey)
                if not isinstance(w_sub, Mapping):
                    bad.append(f"{lbl}.{wkey}: mapping required for non-death {hkind} event")
                    continue
                w_field = "weight" if hkind == "dalys" else "decrement"
                wv = w_sub.get(w_field)
                if not (_isnum(wv) and 0.0 <= float(wv) <= 1.0):
                    bad.append(f"{lbl}.{wkey}.{w_field}: number in [0, 1] required")
                if not (_isnum(w_sub.get("duration_years")) and float(w_sub["duration_years"]) > 0.0):
                    bad.append(f"{lbl}.{wkey}.duration_years: positive number required")
                if not str(w_sub.get("source") or "").strip():
                    bad.append(f"{lbl}.{wkey}.source: required")
                if "range" in w_sub:
                    rng = w_sub["range"]
                    if not (isinstance(rng, list) and len(rng) == 2 and _isnum(rng[0]) and _isnum(rng[1])
                            and 0.0 <= float(rng[0]) < float(rng[1]) <= 1.0):
                        bad.append(f"{lbl}.{wkey}.range: [lo, hi] in [0, 1] with lo < hi required")
                    elif _isnum(wv) and not (float(rng[0]) <= float(wv) <= float(rng[1])):
                        bad.append(f"{lbl}.{wkey}.range: must contain {w_field} {wv}")
                    else:
                        has_ranged_weight = True
                elif not str(w_sub.get("excluded") or "").strip():
                    bad.append(f"{lbl}.{wkey}: declare either `range: [lo, hi]` or `excluded: <reason>`")
            if has_death_event and isinstance(th, Mapping) and th.get("deaths") == "lifetime":
                pairs, lt_errs = load_life_table(spec)
                bad += lt_errs
                aae = hm.get("age_at_event")
                if not isinstance(aae, Mapping) or not aae:
                    bad.append("cost_effectiveness.health_measure.age_at_event: required when deaths is lifetime")
                else:
                    if not str(hm.get("age_at_event_source") or "").strip():
                        bad.append("cost_effectiveness.health_measure.age_at_event_source: required")
                    if card is not None:
                        miss_g = [g for g in (card.age_groups or []) if g not in aae]
                        if miss_g:
                            bad.append(f"cost_effectiveness.health_measure.age_at_event: missing age group(s) {miss_g}")
                    if pairs:
                        lo_a, hi_a = pairs[0][0], pairs[-1][0]
                        for g, ag in aae.items():
                            if not _isnum(ag) or float(ag) < lo_a or float(ag) > hi_a:
                                bad.append(f"cost_effectiveness.health_measure.age_at_event.{g}: "
                                           f"{ag!r} outside life_table age range [{lo_a:g}, {hi_a:g}]")

    # 6. thresholds
    thresh = ce.get("thresholds")
    if not isinstance(thresh, list) or not thresh:
        bad.append("cost_effectiveness.thresholds: non-empty list required (no default threshold in code)")
    else:
        seen_t: set[str] = set()
        base_yr = str(prices_cfg.get("base_year") or "")
        base_cur = str(prices_cfg.get("currency") or "USD")
        for i, t in enumerate(thresh):
            if not isinstance(t, Mapping):
                bad.append(f"cost_effectiveness.thresholds[{i}]: must be a mapping")
                continue
            tid = str(t.get("id") or "")
            if not tid:
                bad.append(f"cost_effectiveness.thresholds[{i}].id: required")
            elif tid in seen_t:
                bad.append(f"cost_effectiveness.thresholds.{tid}: duplicate id")
            seen_t.add(tid)
            if not (_isnum(t.get("usd_per_unit")) and float(t["usd_per_unit"]) > 0.0):
                bad.append(f"cost_effectiveness.thresholds.{tid or i}.usd_per_unit: positive number required")
            if not str(t.get("source") or "").strip():
                bad.append(f"cost_effectiveness.thresholds.{tid or i}.source: required")
            if "price_year" in t and base_yr and str(t["price_year"]) != base_yr:
                bad.append(f"cost_effectiveness.thresholds.{tid or i}.price_year: {t['price_year']} != options.prices.base_year {base_yr}")
            if "currency" in t and str(t["currency"]).upper() != base_cur.upper():
                bad.append(f"cost_effectiveness.thresholds.{tid or i}.currency: {t['currency']} != {base_cur}")

    # 7. prices (negotiated / unknown price grids)
    pr_list = ce.get("prices") or []
    uq = raw.get("uncertainty") or {}
    uq_sources = [x for x in (uq.get("sources") or []) if isinstance(x, Mapping)]
    cost_drawn_items: set[str] = set()
    cost_draws_all = False
    for s_u in uq_sources:
        if s_u.get("kind") == "costs":
            if isinstance(s_u.get("items"), Mapping) and s_u["items"]:
                cost_drawn_items.update(str(k) for k in s_u["items"])
            else:
                cost_draws_all = True
    if not isinstance(pr_list, list):
        bad.append("cost_effectiveness.prices: a list")
    else:
        seen_p: set[str] = set()
        for i, pr in enumerate(pr_list):
            if not isinstance(pr, Mapping):
                bad.append(f"cost_effectiveness.prices[{i}]: must be a mapping")
                continue
            pid = str(pr.get("id") or f"[{i}]")
            lbl = f"cost_effectiveness.prices.{pid}"
            if pid in seen_p:
                bad.append(f"{lbl}: duplicate id")
            seen_p.add(pid)
            ci_id = str(pr.get("cost_item") or "")
            if ci_id not in cost_items:
                bad.append(f"{lbl}.cost_item: {ci_id!r} not in options.cost_items")
                continue
            role = pr.get("role")
            if role not in PRICE_ROLES:
                bad.append(f"{lbl}.role: must be one of {PRICE_ROLES}")
            if role == "negotiated" and (cost_draws_all or ci_id in cost_drawn_items):
                bad.append(f"{lbl}: negotiated price for {ci_id!r} cannot also be drawn in uncertainty.sources (costs)")
            rng = pr.get("range")
            if not (isinstance(rng, list) and len(rng) == 2 and _isnum(rng[0]) and _isnum(rng[1])
                    and 0.0 <= float(rng[0]) < float(rng[1])):
                bad.append(f"{lbl}.range: [lo, hi] with 0 <= lo < hi required")
                continue
            lo, hi = float(rng[0]), float(rng[1])
            iv = cost_items[ci_id].get("value")
            if _isnum(iv) and not (lo <= float(iv) <= hi):
                bad.append(f"{lbl}.range: [{lo:g}, {hi:g}] must contain cost_item {ci_id!r} value {iv}")
            grid = pr.get("grid")
            if not (isinstance(grid, list) and len(grid) >= 2 and all(_isnum(g) for g in grid)):
                bad.append(f"{lbl}.grid: list of at least 2 numeric prices required")
            elif any(float(g) < lo or float(g) > hi for g in grid):
                bad.append(f"{lbl}.grid: every grid price must lie inside range [{lo:g}, {hi:g}]")

    # 8. health_weights conditional uncertainty kind
    inc_kinds = [x.get("kind") for x in uq_sources]
    exc_kinds = {x.get("kind"): x.get("reason") for x in (uq.get("excluded") or []) if isinstance(x, Mapping) and "kind" in x}
    if HEALTH_WEIGHTS in inc_kinds and HEALTH_WEIGHTS in exc_kinds:
        bad.append(f"uncertainty: {HEALTH_WEIGHTS} is both a source and excluded")
    if HEALTH_WEIGHTS in inc_kinds and not has_ranged_weight:
        bad.append(f"uncertainty: source {HEALTH_WEIGHTS} declared but no health_measure event has a weight range")
    if has_ranged_weight and HEALTH_WEIGHTS not in inc_kinds and not str(exc_kinds.get(HEALTH_WEIGHTS) or "").strip():
        bad.append(f"uncertainty: {HEALTH_WEIGHTS} (ranged health-measure weights) must be declared in sources or excluded with a reason")

    return bad


def toy_cea_spec(d: str, *, standalone: bool = True) -> dict:
    """Minimal disease-agnostic valid CEA spec dictionary (with files written inside temp dir `d`)."""
    with open(os.path.join(d, "units.csv"), "w", encoding="utf-8") as f:
        f.write("id,pop,x\nu1,10000,0.2\n")
    with open(os.path.join(d, "life_table.csv"), "w", encoding="utf-8") as f:
        f.write("age,ex\n0,80.0\n10,70.5\n50,32.0\n80,9.0\n")
    raw = {
        "schema_version": 1,
        "question": "compare two preventive packages on cost-effectiveness",
        "decision_maker": "programme director",
        "decision_date": "2026-01-01",
        "engine": {"ref": "toolkit.engines.contract:ToyEngine"},
        "horizon": {"start": 2026.0, "stop": 2029.0},
        "decision_units": {
            "csv": "units.csv", "id": "id",
            "populations": {"all": {"column": "pop", "age_groups": ["all"]}},
            "engine_attributes": {"x": {"column": "x"}},
        },
        "options": {
            "populations": {"total": "pop"},
            "components": {"a": {"group": "g"}, "b": {"group": "g"}},
            "packages": {"none": [], "pkg_a": ["a"], "pkg_b": ["b"]},
            "engine_map": {"a": {"a": 1}, "b": {"b": 1}},
            "prices": {"base_year": "2026", "index": {"2026": 1.0}},
            "cost_items": [
                {"id": "cost_a", "component": "a", "value": 10.0, "price_year": "2026",
                 "price_rule": "as_published", "basis": "per_person", "includes": ["commodity"]},
                {"id": "cost_b", "component": "b", "value": 25.0, "price_year": "2026",
                 "price_rule": "as_published", "basis": "per_person", "includes": ["commodity"]},
            ],
        },
        "objective": {"outcome": "cases", "horizon": [2026.0, 2029.0], "discounting": {"rate": 0.03, "base_year": 2026}},
        "comparator": {"package": "none", "text": "no programme"},
        "outcomes_reported": ["cases"],
        "calibration": {"method": "none"},
        "uncertainty": {
            "draws": 4, "seed": 1,
            "sources": [{"kind": "health_weights"}],
            "excluded": [
                {"kind": "engine_parameters", "reason": "toy test"},
                {"parameter": "synergy", "reason": "toy test"},
                {"kind": "calibration", "reason": "toy test"},
                {"kind": "costs", "reason": "negotiated price grid"},
                {"kind": "emulator", "reason": "direct evaluation"},
            ],
        },
        "analysis": {"evaluation": "direct"},
        "deliverable": (
            {"kind": KIND}
            if standalone else {
                "kind": "package_per_unit", "allocation_file": "allocation.csv",
                "columns": {"unit": "unit", "package": "package", "value": "averted"},
                "headline": {"name": "total_averted", "outcome": "cases"},
            }
        ),
        "cost_effectiveness": {
            "strategies": ["none", "pkg_a", "pkg_b"],
            "reference": "none",
            "perspectives": ["health_system", "societal"],
            "time_horizon": {"start": 2026.0, "stop": 2029.0, "deaths": "window"},
            "discounting": {"costs": {"rate": 0.03}, "health": {"rate": 0.03}, "base_year": 2026,
                            "timing": "uniform_accrual"},
            "health_measure": {
                "kind": "dalys",
                "events": {
                    "cases": {"yld": {"weight": 0.10, "duration_years": 0.05, "range": [0.05, 0.15],
                                      "source": "test disability weight"}},
                },
            },
            "thresholds": [{"id": "t1", "usd_per_unit": 500.0, "source": "test opportunity cost"}],
            "consequence_costs": [
                {"id": "case_hs", "outcome": "cases", "value": 80.0, "price_year": "2026",
                 "price_rule": "as_published", "payer": "health_system", "source": "test hs cost"},
                {"id": "case_hh", "outcome": "cases", "value": 20.0, "price_year": "2026",
                 "price_rule": "as_published", "payer": "household", "source": "test hh cost"},
            ],
            "payers": {"cost_a": "health_system", "cost_b": "health_system"},
            "prices": [
                {"id": "p_b", "cost_item": "cost_b", "range": [5.0, 50.0],
                 "grid": [5.0, 15.0, 25.0, 40.0, 50.0], "role": "negotiated"},
            ],
        },
    }
    if not standalone:
        raw["constraints"] = {"envelope_usd": 500000.0}
    return raw


def _self_test() -> int:
    import copy
    import tempfile
    from toolkit.spec import schema

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as d:
        raw_st = toy_cea_spec(d, standalone=True)
        e_st = schema.validate(schema.Spec(raw_st, d, os.path.join(d, "s1.yaml")))
        raw_add = toy_cea_spec(d, standalone=False)
        e_add = schema.validate(schema.Spec(raw_add, d, os.path.join(d, "s2.yaml")))
        ok(e_st == [] and e_add == [],
           f"CE_SPEC1 standalone and allocation add-on CEA specs validate cleanly (st={e_st}, add={e_add})")

        # CE13 refusal: societal perspective with no non-health-system cost item or consequence cost
        raw_soc = copy.deepcopy(raw_st)
        raw_soc["cost_effectiveness"]["consequence_costs"] = [raw_soc["cost_effectiveness"]["consequence_costs"][0]]
        e_soc = schema.validate(schema.Spec(raw_soc, d, os.path.join(d, "s3.yaml")))
        ok(any("societal" in e for e in e_soc),
           f"CE_SPEC2 societal perspective without non-health-system costs is refused ({e_soc})")

        # Negotiated price cannot also be drawn in uncertainty.sources (costs)
        raw_neg = copy.deepcopy(raw_st)
        raw_neg["uncertainty"]["sources"].append({"kind": "costs", "items": {"cost_b": [0.8, 1.2]}})
        raw_neg["uncertainty"]["excluded"] = [x for x in raw_neg["uncertainty"]["excluded"] if x.get("kind") != "costs"]
        e_neg = schema.validate(schema.Spec(raw_neg, d, os.path.join(d, "s4.yaml")))
        ok(any("negotiated" in e for e in e_neg),
           f"CE_SPEC3 negotiated price drawn in uncertainty costs is refused ({e_neg})")

        # Standalone refuses envelope_usd and allocation_file; missing health_weights source/exclusion refused
        raw_env = copy.deepcopy(raw_st)
        raw_env["constraints"] = {"envelope_usd": 1000.0}
        raw_env["deliverable"]["allocation_file"] = "alloc.csv"
        raw_env["uncertainty"]["sources"] = []
        e_env = schema.validate(schema.Spec(raw_env, d, os.path.join(d, "s5.yaml")))
        ok(any("envelope_usd" in e for e in e_env) and any("allocation_file" in e for e in e_env)
           and any("health_weights" in e for e in e_env),
           f"CE_SPEC4 standalone refuses envelope/allocation_file and requires health_weights accounting ({e_env})")

        # Discount rate consistency with objective.discounting & life table interpolation bounds
        raw_dr = copy.deepcopy(raw_st)
        raw_dr["cost_effectiveness"]["discounting"]["health"]["rate"] = 0.05
        e_dr = schema.validate(schema.Spec(raw_dr, d, os.path.join(d, "s6.yaml")))
        pairs, lt_e = load_life_table(schema.Spec(raw_st, d, os.path.join(d, "s1.yaml")))
        # toy_cea_spec omits life_table when deaths=window -> test lifetime + life_table
        raw_lt = copy.deepcopy(raw_st)
        raw_lt["cost_effectiveness"]["time_horizon"]["deaths"] = "lifetime"
        raw_lt["cost_effectiveness"]["health_measure"]["events"]["dalys"] = {"death": True}
        raw_lt["cost_effectiveness"]["health_measure"]["life_table"] = {
            "csv": "life_table.csv", "age": "age", "remaining": "ex", "source": "test life table",
        }
        raw_lt["cost_effectiveness"]["health_measure"]["age_at_event"] = {"all": 5.0}
        raw_lt["cost_effectiveness"]["health_measure"]["age_at_event_source"] = "midpoint"
        pairs_lt, lt_ok = load_life_table(schema.Spec(raw_lt, d, os.path.join(d, "s7.yaml")))
        e_lt = schema.validate(schema.Spec(raw_lt, d, os.path.join(d, "s7.yaml")))
        ex5 = interpolate_life_table(pairs_lt, 5.0)
        ok(any("objective.discounting.rate" in e for e in e_dr) and lt_ok == [] and e_lt == [] and abs(ex5 - 75.25) < 1e-12,
           f"CE_SPEC5 discount-rate consistency enforced and life table interpolates cleanly (e_dr={e_dr}, e_lt={e_lt}, ex5={ex5})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
