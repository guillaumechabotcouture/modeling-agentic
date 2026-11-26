#!/usr/bin/env python3
"""Deterministic cost-effectiveness analysis core (docs/design/cost-effectiveness-deliverable.md §5).

    python3 toolkit/analysis/cea.py --self-test

Computes discounted costs and composed health measures (DALYs or QALYs) by perspective, strong and extended
dominance frontiers, net monetary benefit (NMB) with Stinnett & Mullahy (1998) frontier consistency check,
the five-case degenerate-ratio guard (§5.4), evaluated price grids, and Brent-bracketed threshold prices
cross-checked against closed-form linear solutions (§5.6). Nothing here names a disease.
"""
from __future__ import annotations

import math
import os
import sys
from collections.abc import Callable, Mapping

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from toolkit.alloc import costs as acosts  # noqa: E402
from toolkit.spec import cea as CEASPEC  # noqa: E402

PRODUCER = "toolkit/analysis/cea.py"
THRESHOLDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")


def load_thresholds() -> dict:
    """Read cost_effectiveness thresholds from `thresholds.yaml` (never restated in code)."""
    doc = yaml.safe_load(open(THRESHOLDS_FILE, encoding="utf-8")) or {}
    ce = doc.get("cost_effectiveness")
    if not isinstance(ce, Mapping):
        raise ValueError(f"cost_effectiveness section missing from {THRESHOLDS_FILE}")
    return dict(ce)


# --------------------------------------------------------------------------- Discounting & health measures (§5.1)
def discount_uniform(start: float, stop: float, rate: float, base_year: float) -> float:
    """Mean continuous discount factor of (1 + r)^-(t - base_year) over uniform accrual on [start, stop].
    Matches `core.discount_factor` and CE12 (300 over [0, 3] at 3% -> 287.083)."""
    t0, t1, r, b = float(start), float(stop), float(rate), float(base_year)
    if t1 <= t0:
        raise ValueError(f"discount_uniform: stop ({t1}) must exceed start ({t0})")
    if r < 0.0:
        raise ValueError(f"discount_uniform: negative rate {r}")
    if r == 0.0:
        return 1.0
    k = math.log(1.0 + r)
    return (math.exp(-k * (t0 - b)) - math.exp(-k * (t1 - b))) / (k * (t1 - t0))


def discount_start_of_year(start: float, stop: float, rate: float, base_year: float) -> float:
    """Sum of start-of-year discount factors (1 + r)^-(y - base_year) for annual charges over [start, stop).
    Matches CE12 (100/yr for 3 years at 3%, start of year -> 291.347)."""
    t0, t1, r, b = float(start), float(stop), float(rate), float(base_year)
    if t1 <= t0:
        raise ValueError(f"discount_start_of_year: stop ({t1}) must exceed start ({t0})")
    if r < 0.0:
        raise ValueError(f"discount_start_of_year: negative rate {r}")
    n_full = int(math.floor(t1 - t0 + 1e-9))
    rem = (t1 - t0) - n_full
    tot = sum((1.0 + r) ** (-(t0 + j - b)) for j in range(n_full))
    if rem > 1e-9:
        tot += rem * ((1.0 + r) ** (-(t0 + n_full - b)))
    return float(tot)


def yll_discounted(remaining_years: float, rate: float) -> float:
    """Continuous discounted remaining life years (Murray 1994, no age weighting; CE11):
    `(1 - exp(-r * L)) / r` for `r > 0`, else `L`."""
    L, r = float(remaining_years), float(rate)
    if L < 0.0 or r < 0.0:
        raise ValueError(f"yll_discounted: remaining_years ({L}) and rate ({r}) must be >= 0")
    if r == 0.0:
        return L
    return (1.0 - math.exp(-r * L)) / r


# --------------------------------------------------------------------------- Degenerate-ratio guard (§5.4)
def evaluate_ratio_guard(
    delta_cost: float,
    delta_health: float,
    *,
    ref_health: float = 0.0,
    effect_floor_rel: float | None = None,
    effect_floor_abs: float | None = None,
) -> tuple[str, float | None]:
    """Classify (ΔC, ΔE) into the five §5.4 states; return `(status, icer_or_None)`.
    An ICER number is returned ONLY in the north-east quadrant (`delta_health > eps` and `delta_cost > 0`)."""
    if effect_floor_rel is None or effect_floor_abs is None:
        th = load_thresholds()
        effect_floor_rel = float(th["effect_floor_rel"]) if effect_floor_rel is None else float(effect_floor_rel)
        effect_floor_abs = float(th["effect_floor_abs"]) if effect_floor_abs is None else float(effect_floor_abs)
    dc, de = float(delta_cost), float(delta_health)
    if not (math.isfinite(dc) and math.isfinite(de)):
        raise ValueError(f"non-finite incremental values: dc={dc}, de={de}")
    eps = max(float(effect_floor_rel) * abs(float(ref_health)), 0.0) + float(effect_floor_abs)
    if de > eps:
        if dc > 0.0:
            return "icer", dc / de
        return "dominant", None
    if abs(de) <= eps:
        return "no_health_difference", None
    # de < -eps
    if dc >= 0.0:
        return "dominated", None
    return "less_health_lower_cost", None


# --------------------------------------------------------------------------- Incremental analysis & NMB (§5.2, §5.3)
def incremental_analysis(
    strategies: Mapping[str, tuple[float, float]],
    reference: str,
    *,
    ref_health_loss: float = 0.0,
    effect_floor_rel: float | None = None,
    effect_floor_abs: float | None = None,
) -> dict:
    """Run §5.2 dominance + extended dominance over `{strategy: (cost, health)}` (higher health is better).
    Returns `{"frontier": [ids], "strategies": {id: {...}}}` with §5.4 ratio guard enforced on every row."""
    if len(strategies) < 2:
        raise ValueError("incremental_analysis requires at least two strategies")
    if reference not in strategies:
        raise ValueError(f"reference {reference!r} not in strategies")
    th = load_thresholds()
    ef_rel = float(th["effect_floor_rel"]) if effect_floor_rel is None else float(effect_floor_rel)
    ef_abs = float(th["effect_floor_abs"]) if effect_floor_abs is None else float(effect_floor_abs)
    tie_tol = float(th["nmb_tie_rel"])
    order_idx = {name: i for i, name in enumerate(strategies)}

    def _same(a: float, b: float) -> bool:
        return abs(a - b) <= tie_tol * max(1.0, abs(a), abs(b))

    # 1. Sort by cost ascending, ties by health descending, preserving declaration order on exact ties
    ordered = sorted(strategies.keys(), key=lambda s: (float(strategies[s][0]), -float(strategies[s][1]), order_idx[s]))
    status: dict[str, str] = {}
    vs: dict[str, str | list[str] | None] = {}

    # 2. Strong dominance (and exact ties -> identical)
    surviving: list[str] = []
    for s in ordered:
        cs, es = float(strategies[s][0]), float(strategies[s][1])
        ident = next((t for t in surviving if _same(float(strategies[t][0]), cs) and _same(float(strategies[t][1]), es)), None)
        if ident is not None:
            status[s] = "identical"
            vs[s] = ident
            continue
        dom_by = next(
            (t for t in surviving
             if float(strategies[t][0]) <= cs and float(strategies[t][1]) >= es
             and (float(strategies[t][0]) < cs or float(strategies[t][1]) > es)),
            None,
        )
        if dom_by is not None:
            status[s] = "dominated"
            vs[s] = dom_by
            continue
        surviving.append(s)

    # 3. Extended dominance: repeat until ICERs along the remaining list are strictly increasing
    frontier = list(surviving)
    changed = True
    while changed and len(frontier) >= 3:
        changed = False
        for i in range(1, len(frontier) - 1):
            c_prev, e_prev = map(float, strategies[frontier[i - 1]])
            c_cur, e_cur = map(float, strategies[frontier[i]])
            c_next, e_next = map(float, strategies[frontier[i + 1]])
            de1 = e_cur - e_prev
            de2 = e_next - e_cur
            if de1 <= 0.0:
                status[frontier[i]] = "dominated"
                vs[frontier[i]] = frontier[i - 1]
                frontier.pop(i)
                changed = True
                break
            icer1 = (c_cur - c_prev) / de1
            icer2 = (c_next - c_cur) / de2 if de2 > 0.0 else float("inf")
            if icer1 >= icer2 - 1e-12 * max(1.0, abs(icer1), abs(icer2)):
                status[frontier[i]] = "extendedly_dominated"
                vs[frontier[i]] = [frontier[i - 1], frontier[i + 1]]
                frontier.pop(i)
                changed = True
                break

    for rank, s in enumerate(frontier):
        status[s] = "frontier"
        vs[s] = frontier[rank - 1] if rank > 0 else None

    c_ref, e_ref = map(float, strategies[reference])
    rows: dict[str, dict] = {}
    for s in strategies:
        cs, es = map(float, strategies[s])
        dc_ref, de_ref = cs - c_ref, es - e_ref
        ref_status, _ = evaluate_ratio_guard(
            dc_ref, de_ref, ref_health=ref_health_loss, effect_floor_rel=ef_rel, effect_floor_abs=ef_abs,
        )
        row: dict = {
            "cost": cs,
            "health": es,
            "delta_cost_vs_ref": dc_ref,
            "delta_health_vs_ref": de_ref,
            "ref_quadrant_status": ref_status,
            "status": status[s],
            "vs": vs[s],
            "frontier_rank": frontier.index(s) if s in frontier else None,
            "delta_cost": None,
            "delta_health": None,
            "icer": None,
            "icer_status": None,
        }
        if s in frontier and row["frontier_rank"] > 0:
            prev = frontier[row["frontier_rank"] - 1]
            cp, ep = map(float, strategies[prev])
            dc_f, de_f = cs - cp, es - ep
            ist, iv = evaluate_ratio_guard(
                dc_f, de_f, ref_health=ref_health_loss, effect_floor_rel=ef_rel, effect_floor_abs=ef_abs,
            )
            row["delta_cost"] = dc_f
            row["delta_health"] = de_f
            row["icer_status"] = ist
            row["icer"] = iv
        rows[s] = row
    return {"frontier": frontier, "strategies": rows}


def _frontier_choice_at_lambda(strategies: Mapping[str, tuple[float, float]], frontier: list[str], lam: float) -> str:
    """Select the highest-effect frontier strategy whose incremental ICER vs predecessor is <= `lam`."""
    chosen = frontier[0]
    for i in range(1, len(frontier)):
        c0, e0 = map(float, strategies[frontier[i - 1]])
        c1, e1 = map(float, strategies[frontier[i]])
        de = e1 - e0
        if de > 0.0 and (c1 - c0) / de <= lam:
            chosen = frontier[i]
        else:
            break
    return chosen


def nmb_choice(
    strategies: Mapping[str, tuple[float, float]],
    lam: float,
    *,
    reference: str | None = None,
    frontier: list[str] | None = None,
    tie_rel: float | None = None,
) -> dict:
    """Compute `NMB(s, lam) = lam * E(s) - C(s)` and choose argmax; verify Stinnett & Mullahy (1998)
    equivalence with the incremental frontier (raises RuntimeError on non-tie disagreement)."""
    th = load_thresholds()
    tol = float(th["nmb_tie_rel"]) if tie_rel is None else float(tie_rel)
    lam_f = float(lam)
    if lam_f < 0.0 or not math.isfinite(lam_f):
        raise ValueError(f"nmb_choice: lambda must be finite >= 0 (got {lam!r})")
    nmb = {s: lam_f * float(e) - float(c) for s, (c, e) in strategies.items()}
    best_val = max(nmb.values())
    scale = max(1.0, abs(best_val), *(abs(v) for v in nmb.values()))
    tied = [s for s, v in nmb.items() if abs(best_val - v) <= tol * scale]
    is_tie = len(tied) > 1
    chosen = tied[0]
    if frontier is None:
        ref = reference if reference in strategies else next(iter(strategies))
        frontier = incremental_analysis(strategies, ref)["frontier"]
    f_chosen = _frontier_choice_at_lambda(strategies, frontier, lam_f)
    if not is_tie and chosen != f_chosen:
        # Check if frontier choice is within numerical tie tolerance of NMB best
        if abs(nmb[chosen] - nmb[f_chosen]) > tol * scale:
            raise RuntimeError(f"NMB argmax ({chosen}, NMB={nmb[chosen]:.6g}) disagrees with frontier ICER choice "
                               f"({f_chosen}, NMB={nmb[f_chosen]:.6g}) at lambda={lam_f:g}")
    return {"lambda": lam_f, "nmb": nmb, "chosen": chosen, "tie": is_tie, "tied": tied if is_tie else []}


# --------------------------------------------------------------------------- Price grid & threshold price (§5.5, §5.6)
def threshold_price(
    eval_strategies: Callable[[float], Mapping[str, tuple[float, float]]],
    target: str,
    lam: float,
    lo: float,
    hi: float,
    grid: list[float],
    *,
    closed_form: float | None = None,
) -> dict:
    """Evaluate `eval_strategies(p)` across `grid` and bracket any switching price `p*` in `[lo, hi]` where
    `g(p) = NMB(target, lam, p) - max_{t != target} NMB(t, lam, p)` crosses zero via `scipy.optimize.brentq`.
    Never extrapolates outside `[lo, hi]`; cross-checks against `closed_form` when supplied (§5.6)."""
    from scipy.optimize import brentq

    lo_f, hi_f, lam_f = float(lo), float(hi), float(lam)
    if not (hi_f > lo_f):
        raise ValueError(f"threshold_price: invalid range [{lo_f}, {hi_f}]")
    pts = sorted({float(g) for g in grid})
    if any(p < lo_f - 1e-12 or p > hi_f + 1e-12 for p in pts):
        raise ValueError("threshold_price: grid points must lie inside [lo, hi]")

    def g_val(p: float) -> tuple[float, dict]:
        st = eval_strategies(float(p))
        if target not in st or len(st) < 2:
            raise ValueError(f"threshold_price: target {target!r} missing or fewer than 2 strategies")
        nmbs = {s: lam_f * float(e) - float(c) for s, (c, e) in st.items()}
        best_other = max(v for s, v in nmbs.items() if s != target)
        return nmbs[target] - best_other, nmbs

    cells: list[dict] = []
    g_pts: list[tuple[float, float]] = []
    for p in pts:
        gp, nmbs = g_val(p)
        chosen = max(nmbs, key=lambda s: nmbs[s])
        cells.append({"price": p, "nmb": nmbs, "chosen": chosen, "margin": gp})
        g_pts.append((p, gp))

    xtol = 1e-6 * (hi_f - lo_f)
    roots: list[dict] = []
    for (p0, g0), (p1, g1) in zip(g_pts, g_pts[1:]):
        if g0 == 0.0:
            roots.append({"value": p0, "bracket": [p0, p0]})
        elif g0 * g1 < 0.0:
            eval_log: list[tuple[float, float]] = [(p0, g0), (p1, g1)]

            def _wrapped(x: float) -> float:
                gx, _ = g_val(x)
                eval_log.append((float(x), gx))
                return gx

            root = float(brentq(_wrapped, p0, p1, xtol=xtol))
            # Tightest evaluated bracket from real harness evaluations bracketing the sign change
            lo_br = max((x for x, gx in eval_log if (gx >= 0.0) == (g0 >= 0.0) and x <= root + xtol), default=p0)
            hi_br = min((x for x, gx in eval_log if (gx >= 0.0) == (g1 >= 0.0) and x >= root - xtol), default=p1)
            if lo_br > hi_br:
                lo_br, hi_br = hi_br, lo_br
            roots.append({"value": root, "bracket": [float(lo_br), float(hi_br)]})
    if g_pts[-1][1] == 0.0 and (not roots or abs(roots[-1]["value"] - g_pts[-1][0]) > xtol):
        roots.append({"value": g_pts[-1][0], "bracket": [g_pts[-1][0], g_pts[-1][0]]})

    if not roots:
        if all(g > 0.0 for _, g in g_pts):
            res = {"status": "above_range", "value": None, "bracket": None, "range": [lo_f, hi_f], "cells": cells}
        else:
            res = {"status": "below_range", "value": None, "bracket": None, "range": [lo_f, hi_f], "cells": cells}
        return res

    primary = roots[0]
    if closed_form is not None and lo_f <= float(closed_form) <= hi_f:
        if abs(primary["value"] - float(closed_form)) > max(10.0 * xtol, 1e-6 * max(1.0, abs(float(closed_form)))):
            raise RuntimeError(f"threshold_price: Brent root ({primary['value']:.6g}) disagrees with closed form "
                               f"({float(closed_form):.6g})")
    out: dict = {
        "status": "threshold" if len(roots) == 1 else "multiple_thresholds",
        "value": primary["value"],
        "bracket": primary["bracket"],
        "range": [lo_f, hi_f],
        "cells": cells,
    }
    if len(roots) > 1:
        out["all_thresholds"] = roots
    return out


# --------------------------------------------------------------------------- Totals composition (§5.1)
def compose_perspective_totals(
    spec,
    units: list[dict],
    events_by_strategy: Mapping[str, Mapping[str, Mapping[str, float]]],
    prog_costs_by_strategy: Mapping[str, Mapping[str, float]],
    *,
    life_table: list[tuple[float, float]] | None = None,
    weight_overrides: Mapping[str, float] | None = None,
    cost_mult: Mapping[str, float] | None = None,
) -> dict:
    """Compose discounted total costs and health (relative to reference) per perspective and strategy (§5.1).

    `events_by_strategy`: `{strategy: {outcome: {age_group: total_event_count_over_window}}}`
    `prog_costs_by_strategy`: `{strategy: {cost_item_id: undiscounted_base_year_cost_from_floor_False}}`
    """
    raw = spec.raw if hasattr(spec, "raw") else spec
    ce = raw["cost_effectiveness"]
    th = ce["time_horizon"]
    disc = ce["discounting"]
    t0, t1 = float(th["start"]), float(th["stop"])
    b_yr = float(disc["base_year"])
    r_c, r_h = float(disc["costs"]["rate"]), float(disc["health"]["rate"])
    df_c = discount_uniform(t0, t1, r_c, b_yr)
    df_h = discount_uniform(t0, t1, r_h, b_yr)
    df_start = (1.0 + r_c) ** (-(t0 - b_yr)) if r_c > 0.0 else 1.0
    df_annual = discount_start_of_year(t0, t1, r_c, b_yr) / max(t1 - t0, 1e-12)

    opts = raw["options"]
    prices_cfg = opts.get("prices") or {}
    item_meta = {str(it["id"]): it for it in (opts.get("cost_items") or [])}
    payers_map = {str(k): str(v) for k, v in (ce.get("payers") or {}).items()}
    cc_list = list(ce.get("consequence_costs") or [])
    hm = ce["health_measure"]
    hkind = hm["kind"]
    w_ov = dict(weight_overrides or {})
    c_mult = dict(cost_mult or {})

    if life_table is None and th.get("deaths") == "lifetime" and any(
        isinstance(cfg, Mapping) and cfg.get("death") is True for cfg in (hm.get("events") or {}).values()
    ):
        life_table, lt_errs = CEASPEC.load_life_table(spec)
        if lt_errs:
            raise ValueError("; ".join(lt_errs))

    health_loss: dict[str, float] = {}
    costs_by_payer: dict[str, dict[str, float]] = {}

    for s in ce["strategies"]:
        ev_s = events_by_strategy[s]
        # Programme costs by payer
        p_tot: dict[str, float] = {p: 0.0 for p in CEASPEC.PAYERS}
        for iid, raw_c in (prog_costs_by_strategy.get(s) or {}).items():
            it = item_meta[iid]
            py = payers_map.get(iid, "health_system")
            mult = float(c_mult.get(iid, 1.0))
            if it.get("years") is not None:
                disc_c = float(raw_c) * mult * df_annual
            elif it.get("basis") == "per_outcome":
                disc_c = float(raw_c) * mult * df_c
            else:
                disc_c = float(raw_c) * mult * df_start
            p_tot[py] += disc_c
        # Consequence costs by payer (never floored)
        for cc in cc_list:
            oc = str(cc["outcome"])
            n_ev = sum(float(v) for v in (ev_s.get(oc) or {}).values())
            unit_val = float(cc["value"]) * acosts._factor(cc, prices_cfg) * float(c_mult.get(str(cc["id"]), 1.0))
            p_tot[str(cc["payer"])] += n_ev * unit_val * df_c
        costs_by_payer[s] = p_tot

        # Health loss (DALYs lost or QALYs lost)
        hl = 0.0
        aae = hm.get("age_at_event") or {}
        for oc, cfg in (hm.get("events") or {}).items():
            by_g = ev_s.get(oc) or {}
            if cfg.get("death") is True:
                for g, cnt in by_g.items():
                    if th.get("deaths") == "lifetime":
                        rem_L = CEASPEC.interpolate_life_table(life_table or [], float(aae[g]))
                        hl += float(cnt) * yll_discounted(rem_L, r_h) * df_h
                    else:
                        hl += float(cnt) * 0.5 * (t1 - t0) * df_h
            else:
                wkey = "yld" if hkind == "dalys" else "qaly"
                w_field = "weight" if hkind == "dalys" else "decrement"
                w_sub = cfg[wkey]
                wv = float(w_ov.get(oc, w_sub[w_field]))
                dur = float(w_sub["duration_years"])
                hl += sum(float(v) for v in by_g.values()) * wv * dur * df_h
        health_loss[s] = hl

    ref = ce["reference"]
    ref_hl = health_loss[ref]
    # E(s) = health gained relative to reference (DALYs averted or QALYs gained -> higher is better)
    health_rel = {s: ref_hl - health_loss[s] for s in ce["strategies"]}

    out: dict[str, dict] = {}
    for p in ce["perspectives"]:
        if p == "health_system":
            c_map = {s: costs_by_payer[s]["health_system"] for s in ce["strategies"]}
        else:
            c_map = {s: sum(costs_by_payer[s].values()) for s in ce["strategies"]}
        out[p] = {
            "strategies_ce": {s: (c_map[s], health_rel[s]) for s in ce["strategies"]},
            "costs_by_payer": costs_by_payer,
            "health_loss": health_loss,
            "ref_health_loss": ref_hl,
        }
    return out


# --------------------------------------------------------------------------- Self-test (CE1-CE3, CE7, CE11-CE13)
def _self_test() -> int:
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    # CE1: six strategies — dominance, extended dominance, frontier, ICERs, NMB choices
    ce1_st = {"A": (0.0, 0.0), "B": (100.0, 1.0), "C": (300.0, 1.5),
              "D": (250.0, 3.0), "E": (600.0, 4.0), "F": (1000.0, 4.2)}
    inc1 = incremental_analysis(ce1_st, "A")
    st1 = inc1["strategies"]
    c100 = nmb_choice(ce1_st, 100.0, reference="A", frontier=inc1["frontier"])
    c500 = nmb_choice(ce1_st, 500.0, reference="A", frontier=inc1["frontier"])
    ok(inc1["frontier"] == ["A", "D", "E", "F"]
       and st1["C"]["status"] == "dominated" and st1["C"]["vs"] == "D"
       and st1["B"]["status"] == "extendedly_dominated" and st1["B"]["vs"] == ["A", "D"]
       and abs(st1["D"]["icer"] - 250.0 / 3.0) < 1e-9
       and abs(st1["E"]["icer"] - 350.0) < 1e-9
       and abs(st1["F"]["icer"] - 2000.0) < 1e-9
       and c100["chosen"] == "D" and abs(c100["nmb"]["D"] - 50.0) < 1e-9
       and c500["chosen"] == "E" and abs(c500["nmb"]["E"] - 1400.0) < 1e-9,
       f"CE1 dominance, extended dominance, frontier ICERs, and NMB choices match hand calculation ({inc1['frontier']})")

    # CE2: threshold price by Brent cross-checked against closed form + above_range guard
    def _eval_ce2(p: float) -> dict[str, tuple[float, float]]:
        return dict(ce1_st, E=(200.0 + 10.0 * p, 4.0))

    tp500 = threshold_price(_eval_ce2, "E", 500.0, 0.0, 100.0, [0.0, 25.0, 50.0, 75.0, 100.0], closed_form=55.0)
    tp200 = threshold_price(_eval_ce2, "E", 200.0, 0.0, 100.0, [0.0, 20.0, 40.0, 60.0, 100.0], closed_form=25.0)
    tp_abv = threshold_price(_eval_ce2, "E", 200.0, 0.0, 20.0, [0.0, 10.0, 20.0])
    ok(tp500["status"] == "threshold" and abs(tp500["value"] - 55.0) < 1e-4
       and tp500["bracket"][0] <= 55.0 <= tp500["bracket"][1]
       and tp200["status"] == "threshold" and abs(tp200["value"] - 25.0) < 1e-4
       and tp_abv["status"] == "above_range" and tp_abv["value"] is None,
       f"CE2 Brent threshold price matches closed form (55, 25) and never extrapolates outside range ({tp500['value']}, {tp200['value']}, {tp_abv['status']})")

    # CE3: five-case degenerate-ratio guard
    g_zero = evaluate_ratio_guard(10.0, 0.0)
    g_dom = evaluate_ratio_guard(10.0, -1.0)
    g_sw = evaluate_ratio_guard(-10.0, -1.0)
    g_eps = evaluate_ratio_guard(10.0, 1e-14)
    g_best = evaluate_ratio_guard(-10.0, 1.0)
    g_ne = evaluate_ratio_guard(10.0, 2.0)
    ok(g_zero == ("no_health_difference", None)
       and g_dom == ("dominated", None)
       and g_sw == ("less_health_lower_cost", None)
       and g_eps == ("no_health_difference", None)
       and g_best == ("dominant", None)
       and g_ne == ("icer", 5.0),
       f"CE3 degenerate-ratio guard emits numeric ICER only in north-east quadrant ({g_zero}, {g_dom}, {g_sw}, {g_eps}, {g_best}, {g_ne})")

    # CE7: Stinnett & Mullahy (1998) property test over 500 random strategy sets and lambda values
    rng = np.random.default_rng(1998)
    ce7_ok = True
    for _ in range(500):
        k = int(rng.integers(3, 9))
        st = {f"s{i}": (float(rng.uniform(0.0, 2000.0)), float(rng.uniform(0.0, 10.0))) for i in range(k)}
        lam = float(rng.uniform(1.0, 1000.0))
        try:
            nmb_choice(st, lam, reference="s0")
        except RuntimeError:
            ce7_ok = False
            break
    ok(ce7_ok, "CE7 NMB argmax equals frontier ICER choice across 500 random strategy sets and lambda values")

    # CE11: Murray (1994) continuous discounted YLL
    y30 = yll_discounted(30.0, 0.03)
    y0 = yll_discounted(30.0, 0.0)
    ok(abs(y30 - 19.78101) < 1e-3 and abs(y0 - 30.0) < 1e-12,
       f"CE11 Murray (1994) continuous discounted YLL: L=30 r=3% -> {y30:.3f}, r=0 -> {y0:.1f}")

    # CE12: discounting timing conventions (100/yr for 3 years at 3%)
    c_soy = 100.0 * discount_start_of_year(0.0, 3.0, 0.03, 0.0)
    c_unif = 300.0 * discount_uniform(0.0, 3.0, 0.03, 0.0)
    ok(abs(c_soy - 291.347) < 1e-3 and abs(c_unif - 287.083) < 1e-3,
       f"CE12 start-of-year ({c_soy:.3f}) and uniform-accrual ({c_unif:.3f}) discounting match hand values")

    # CE13: perspectives composition — societal equals health_system + household
    import tempfile
    from toolkit.spec import schema
    with tempfile.TemporaryDirectory() as d:
        raw = CEASPEC.toy_cea_spec(d, standalone=True)
        sp = schema.Spec(raw, d, os.path.join(d, "spec.yaml"))
        evs = {
            "none": {"cases": {"all": 1000.0}},
            "pkg_a": {"cases": {"all": 600.0}},
            "pkg_b": {"cases": {"all": 400.0}},
        }
        prog = {"none": {}, "pkg_a": {"cost_a": 100000.0}, "pkg_b": {"cost_b": 250000.0}}
        tot = compose_perspective_totals(sp, sp.units(), evs, prog)
        hs_a = tot["health_system"]["strategies_ce"]["pkg_a"][0]
        soc_a = tot["societal"]["strategies_ce"]["pkg_a"][0]
        hh_a = tot["societal"]["costs_by_payer"]["pkg_a"]["household"]
        ok(hh_a > 0.0 and abs(soc_a - (hs_a + hh_a)) < 1e-9,
           f"CE13 societal total ({soc_a:.2f}) equals health_system ({hs_a:.2f}) + household ({hh_a:.2f})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
