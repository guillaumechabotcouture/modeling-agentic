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
import re
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


# --------------------------------------------------------------------------- Probabilistic CEA (§5.7)
def _interval_stats(vals: np.ndarray, level: float) -> dict:
    a = (1.0 - float(level)) / 2.0
    v = np.asarray(vals, dtype=float)
    return {
        "mean": float(np.mean(v)),
        "median": float(np.median(v)),
        "sd": float(np.std(v, ddof=1)) if v.size > 1 else 0.0,
        "ci_low": float(np.quantile(v, a)),
        "ci_high": float(np.quantile(v, 1.0 - a)),
    }


def probabilistic_summary(
    draws_by_strategy: Mapping[str, tuple[np.ndarray, np.ndarray]],
    reference: str,
    thresholds: list[dict],
    *,
    ceac_grid: list[float] | None = None,
    level: float = 0.95,
    population: float = 1.0,
    ref_health_loss_draws: np.ndarray | None = None,
) -> dict:
    """Probabilistic CEA over the uncertainty draws (§5.7): ΔC/ΔE intervals, quadrant shares,
    CEAC (summing to 1), CEAF (argmax of expected NMB per Fenwick et al. 2001), EVPI with SE,
    and ICER at the means under §5.4 guard (never an ICER interval)."""
    strat_names = list(draws_by_strategy.keys())
    if len(strat_names) < 2 or reference not in draws_by_strategy:
        raise ValueError("probabilistic_summary requires >=2 strategies including reference")
    c_mat = np.vstack([np.asarray(draws_by_strategy[s][0], dtype=float) for s in strat_names])
    e_mat = np.vstack([np.asarray(draws_by_strategy[s][1], dtype=float) for s in strat_names])
    if c_mat.shape != e_mat.shape or c_mat.ndim != 2 or c_mat.shape[1] == 0:
        raise ValueError("probabilistic_summary: mismatched or empty draw arrays")
    n_strat, n_draws = c_mat.shape
    ref_idx = strat_names.index(reference)
    ref_c, ref_e = c_mat[ref_idx], e_mat[ref_idx]
    ref_hl_mean = float(np.mean(ref_health_loss_draws)) if ref_health_loss_draws is not None else 0.0

    dc_int: dict[str, dict] = {}
    de_int: dict[str, dict] = {}
    quad: dict[str, dict] = {}
    icer_means: dict[str, dict] = {}
    for idx, s in enumerate(strat_names):
        dc = c_mat[idx] - ref_c
        de = e_mat[idx] - ref_e
        dc_int[s] = _interval_stats(dc, level)
        de_int[s] = _interval_stats(de, level)
        quad[s] = {
            "ne": float(np.mean((de > 0.0) & (dc > 0.0))),
            "se": float(np.mean((de > 0.0) & (dc <= 0.0))),
            "sw": float(np.mean((de <= 0.0) & (dc <= 0.0))),
            "nw": float(np.mean((de <= 0.0) & (dc > 0.0))),
        }
        st_m, iv_m = evaluate_ratio_guard(float(np.mean(dc)), float(np.mean(de)), ref_health=ref_hl_mean)
        icer_means[s] = {"status": st_m, "icer": iv_m}

    th_lams = [float(t["lambda"] if "lambda" in t else t["usd_per_unit"]) for t in thresholds]
    if ceac_grid is None:
        lam_max = max(th_lams) * 2.0 if th_lams else 1000.0
        lam_max = max(lam_max, 1.0)
        base_grid = np.linspace(0.0, lam_max, 25).tolist()
        lam_grid = sorted({float(x) for x in (*base_grid, *th_lams)})
    else:
        lam_grid = sorted({float(x) for x in (*ceac_grid, *th_lams)})

    shares: dict[str, list[float]] = {s: [] for s in strat_names}
    ceaf: list[str] = []
    lam_to_idx: dict[float, int] = {}
    for j, lam in enumerate(lam_grid):
        lam_to_idx[lam] = j
        nmb = lam * e_mat - c_mat
        best_d = np.max(nmb, axis=0)
        tol = 1e-12 * np.maximum(1.0, np.abs(best_d))
        wins = nmb >= (best_d[None, :] - tol[None, :])
        n_win = np.maximum(1, np.sum(wins, axis=0))
        frac = wins / n_win[None, :]
        for idx, s in enumerate(strat_names):
            shares[s].append(float(np.mean(frac[idx])))
        mean_nmb = np.mean(nmb, axis=1)
        ceaf.append(strat_names[int(np.argmax(mean_nmb))])

    pop = max(float(population), 1.0)
    th_out: dict[str, dict] = {}
    for t in thresholds:
        tid = str(t["id"])
        lam = float(t["lambda"] if "lambda" in t else t["usd_per_unit"])
        nmb = lam * e_mat - c_mat
        mean_nmb = np.mean(nmb, axis=1)
        ceaf_idx = int(np.argmax(mean_nmb))
        ceaf_s = strat_names[ceaf_idx]
        best_d = np.max(nmb, axis=0)
        gains = np.maximum(0.0, best_d - nmb[ceaf_idx])
        evpi = float(np.mean(gains))
        evpi_se = float(np.std(gains, ddof=1) / math.sqrt(n_draws)) if n_draws > 1 else 0.0
        p_ce = shares[ceaf_s][lam_to_idx[lam]]
        th_out[tid] = {
            "lambda": lam,
            "expected_nmb": {s: float(mean_nmb[idx]) for idx, s in enumerate(strat_names)},
            "ceaf_chosen": ceaf_s,
            "p_cost_effective": float(p_ce),
            "p_cost_effective_pct": float(100.0 * p_ce),
            "evpi": evpi,
            "evpi_se": evpi_se,
            "evpi_per_person": evpi / pop,
        }

    min_draws = int(load_thresholds()["min_draws"])
    return {
        "draws": int(n_draws),
        "status": "computed" if n_draws >= min_draws else "indicative",
        "quadrant_share": quad,
        "delta_cost_interval": dc_int,
        "delta_health_interval": de_int,
        "icer_at_means": icer_means,
        "ceac": {"lambda": lam_grid, "share": shares, "ceaf": ceaf},
        "thresholds": th_out,
    }


# --------------------------------------------------------------------------- Run collector, artifact & claims (§7)
def _cid(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", str(name))


def _population_total(A) -> float:
    units = [A.by_id[i] for i in A.ids]
    tot = 0.0
    for u in units:
        for col in ("population", "pop", "n_people", "cohort_size"):
            if col in u and u[col] is not None:
                try:
                    v = float(u[col])
                    if v > 0.0:
                        tot += v
                        break
                except (TypeError, ValueError):
                    pass
    return tot if tot > 0.0 else float(max(len(units), 1))


def extract_events_and_prog_costs(A, out: Mapping, *, cost_overrides: Mapping[str, float] | None = None) -> tuple[dict, dict]:
    """Aggregate engine outcomes by age group and unfloored per-item programme costs (`floor=False`) per strategy."""
    ce = A.spec.raw["cost_effectiveness"]
    strategies = list(ce["strategies"])
    hm_events = list((ce["health_measure"].get("events") or {}).keys())
    cc_outcomes = [str(cc["outcome"]) for cc in (ce.get("consequence_costs") or [])]
    needed_outcomes = list(dict.fromkeys([*hm_events, *cc_outcomes]))

    units = [A.by_id[i] for i in A.ids]
    alloc = dict(A.alloc)
    if cost_overrides:
        items_copy = []
        for it in alloc.get("cost_items") or []:
            it2 = dict(it)
            if str(it2["id"]) in cost_overrides:
                it2["value"] = float(cost_overrides[str(it2["id"])])
            items_copy.append(it2)
        alloc["cost_items"] = items_copy
    fn = lambda u, p: out[str(u[A.spec.uid])]["cost"][p]  # noqa: E731
    cm = acosts.cost_matrix(alloc, units, outcome=fn, floor=False)

    events_by_s: dict[str, dict[str, dict[str, float]]] = {s: {oc: {} for oc in needed_outcomes} for s in strategies}
    prog_by_s: dict[str, dict[str, float]] = {s: {} for s in strategies}
    for i in A.ids:
        row_u = out[i]
        yg_u = row_u.get("y_group") or {}
        y_u = row_u.get("y") or {}
        for s in strategies:
            for oc in needed_outcomes:
                by_g = (yg_u.get(s) or {}).get(oc)
                if isinstance(by_g, Mapping) and by_g:
                    for g, val in by_g.items():
                        events_by_s[s][oc][str(g)] = events_by_s[s][oc].get(str(g), 0.0) + float(val)
                elif s in y_u and oc in y_u[s]:
                    events_by_s[s][oc]["all"] = events_by_s[s][oc].get("all", 0.0) + float(y_u[s][oc])
            for iid, val in cm[i][s]["items"].items():
                prog_by_s[s][iid] = prog_by_s[s].get(iid, 0.0) + float(val)
    return events_by_s, prog_by_s


class Collector:
    """Hooks into `uncertainty.run`'s `on_draw(out, events, cost, sampled)` so CEA reads the run's exact draws (§5.7)."""

    def __init__(self, A):
        self.A = A
        self.ce = A.spec.raw["cost_effectiveness"]
        self.perspectives = list(self.ce["perspectives"])
        self.strategies = list(self.ce["strategies"])
        self.life_table = None
        th = self.ce["time_horizon"]
        hm = self.ce["health_measure"]
        if th.get("deaths") == "lifetime" and any(
            isinstance(cfg, Mapping) and cfg.get("death") is True for cfg in (hm.get("events") or {}).values()
        ):
            self.life_table, errs = CEASPEC.load_life_table(A.spec)
            if errs:
                raise ValueError("; ".join(errs))
        self.costs: dict[str, dict[str, list[float]]] = {
            p: {s: [] for s in self.strategies} for p in self.perspectives
        }
        self.health: dict[str, dict[str, list[float]]] = {
            p: {s: [] for s in self.strategies} for p in self.perspectives
        }
        self.ref_hl: list[float] = []
        self.n_draws = 0

    def add(self, out, events, cost, sampled=None) -> None:
        smp = sampled or {}
        w_ov = {k[3:]: float(v) for k, v in smp.items() if isinstance(k, str) and k.startswith("hw:")}
        c_mult = {k[5:]: float(v) for k, v in smp.items() if isinstance(k, str) and k.startswith("cost:")}
        evs, prog = extract_events_and_prog_costs(self.A, out)
        units = [self.A.by_id[i] for i in self.A.ids]
        tot = compose_perspective_totals(
            self.A.spec, units, evs, prog, life_table=self.life_table, weight_overrides=w_ov, cost_mult=c_mult,
        )
        for p in self.perspectives:
            for s in self.strategies:
                c_val, e_val = tot[p]["strategies_ce"][s]
                self.costs[p][s].append(float(c_val))
                self.health[p][s].append(float(e_val))
        self.ref_hl.append(float(tot[self.perspectives[0]]["ref_health_loss"]))
        self.n_draws += 1


def _strategy_record_with_status(row: dict, is_ref: bool) -> dict:
    """Expose §7 strategy row where `status` is `"icer"` only in the valid north-east frontier case."""
    out = dict(row)
    if out["status"] == "frontier":
        if is_ref and out["frontier_rank"] == 0:
            out["status"] = "reference"
        elif out["icer_status"] is not None:
            out["status"] = out["icer_status"]
    return out


def run_cea(A, out0: Mapping, cost0: Mapping, collector: Collector | None = None, *, uq_level: float = 0.95) -> dict:
    """Compute the full `analysis/cost_effectiveness.yaml` payload (§7) from the point evaluation and UQ collector."""
    ce = A.spec.raw["cost_effectiveness"]
    ref = ce["reference"]
    perspectives = list(ce["perspectives"])
    thresholds = list(ce["thresholds"])
    hm = ce["health_measure"]
    th = ce["time_horizon"]
    units = [A.by_id[i] for i in A.ids]
    pop = _population_total(A)

    life_table = collector.life_table if collector is not None else None
    if life_table is None and th.get("deaths") == "lifetime" and any(
        isinstance(cfg, Mapping) and cfg.get("death") is True for cfg in (hm.get("events") or {}).values()
    ):
        life_table, errs = CEASPEC.load_life_table(A.spec)
        if errs:
            raise ValueError("; ".join(errs))

    evs0, prog0 = extract_events_and_prog_costs(A, out0)
    tot0 = compose_perspective_totals(A.spec, units, evs0, prog0, life_table=life_table)
    horizon_len = max(float(th["stop"]) - float(th["start"]), 1e-12)

    persp_out: dict[str, dict] = {}
    for p in perspectives:
        ce_map = tot0[p]["strategies_ce"]
        inc = incremental_analysis(ce_map, ref, ref_health_loss=tot0[p]["ref_health_loss"])
        strat_rows = {s: _strategy_record_with_status(r, s == ref) for s, r in inc["strategies"].items()}
        prob = None
        if collector is not None and collector.n_draws > 0:
            dbs = {s: (np.asarray(collector.costs[p][s]), np.asarray(collector.health[p][s])) for s in ce["strategies"]}
            prob = probabilistic_summary(
                dbs, ref, thresholds, level=uq_level, population=pop,
                ref_health_loss_draws=np.asarray(collector.ref_hl, dtype=float),
            )
        th_rec: dict[str, dict] = {}
        for t in thresholds:
            tid = str(t["id"])
            lam = float(t["lambda"] if "lambda" in t else t["usd_per_unit"])
            ch = nmb_choice(ce_map, lam, reference=ref, frontier=inc["frontier"])
            entry = {
                "lambda": lam,
                "chosen": ch["chosen"],
                "nmb": ch["nmb"],
                "tie": ch["tie"],
            }
            if prob is not None:
                pt = prob["thresholds"][tid]
                entry.update(
                    ceaf_chosen=pt["ceaf_chosen"],
                    p_cost_effective=pt["p_cost_effective"],
                    p_cost_effective_pct=pt["p_cost_effective_pct"],
                    evpi=pt["evpi"],
                    evpi_se=pt["evpi_se"],
                    evpi_per_person=pt["evpi_per_person"],
                )
            th_rec[tid] = entry

        # Evaluated price grids & threshold prices (§5.5, §5.6)
        item_comp = {str(it["id"]): str(it["component"]) for it in (A.spec.raw["options"].get("cost_items") or [])}
        prices_out: dict[str, dict] = {}
        for pr in (ce.get("prices") or []):
            pid = str(pr["id"])
            iid = str(pr["cost_item"])
            target = str(pr["strategy"]) if pr.get("strategy") else next(
                (s for s in ce["strategies"] if item_comp.get(iid) in (A.pk.get(s) or ())),
                next(s for s in ce["strategies"] if s != ref),
            )
            lo, hi = float(pr["range"][0]), float(pr["range"][1])
            grid = [float(x) for x in pr["grid"]]

            def _eval_at_price(pval: float, _iid: str = iid, _p: str = p) -> dict[str, tuple[float, float]]:
                _, prog_p = extract_events_and_prog_costs(A, out0, cost_overrides={_iid: float(pval)})
                t_p = compose_perspective_totals(A.spec, units, evs0, prog_p, life_table=life_table)
                return t_p[_p]["strategies_ce"]

            t_by_lam: dict[str, dict] = {}
            last_cells = []
            for t in thresholds:
                tid = str(t["id"])
                t_lam = float(t["lambda"] if "lambda" in t else t["usd_per_unit"])
                tp = threshold_price(_eval_at_price, target, t_lam, lo, hi, grid)
                last_cells = tp["cells"]
                t_by_lam[tid] = {
                    "status": tp["status"],
                    "value": tp["value"],
                    "bracket_lo": tp["bracket"][0] if tp.get("bracket") else None,
                    "bracket_hi": tp["bracket"][1] if tp.get("bracket") else None,
                    "bracket": tp.get("bracket"),
                }
            prices_out[pid] = {"cost_item": iid, "strategy": target, "grid": grid, "cells": last_cells, "threshold": t_by_lam}

        # Undiscounted budget impact per year by payer (§5.9)
        first_chosen = th_rec[str(thresholds[0]["id"])]["chosen"]
        payers_map = {str(k): str(v) for k, v in (ce.get("payers") or {}).items()}
        prices_cfg = A.spec.raw["options"].get("prices") or {}
        bi_by_payer: dict[str, float] = {py: 0.0 for py in CEASPEC.PAYERS}
        for s_tag, sign in ((first_chosen, 1.0), (ref, -1.0)):
            for iid, raw_c in (prog0.get(s_tag) or {}).items():
                bi_by_payer[payers_map.get(iid, "health_system")] += sign * float(raw_c) / horizon_len
            for cc in (ce.get("consequence_costs") or []):
                n_ev = sum(float(v) for v in (evs0[s_tag].get(str(cc["outcome"])) or {}).values())
                u_val = float(cc["value"]) * acosts._factor(cc, prices_cfg)
                bi_by_payer[str(cc["payer"])] += sign * n_ev * u_val / horizon_len

        p_block: dict = {
            "strategies": strat_rows,
            "frontier": inc["frontier"],
            "thresholds": th_rec,
            "prices": prices_out,
            "budget_impact": {"chosen": first_chosen, "reference": ref, "annual_by_payer": bi_by_payer},
        }
        if prob is not None:
            p_block["uncertainty"] = {
                "draws": prob["draws"],
                "status": prob["status"],
                "quadrant_share": prob["quadrant_share"],
                "delta_cost_interval": prob["delta_cost_interval"],
                "delta_health_interval": prob["delta_health_interval"],
                "icer_at_means": prob["icer_at_means"],
            }
            p_block["ceac"] = prob["ceac"]
        persp_out[p] = p_block

    agree: dict[str, str] = {}
    if len(perspectives) >= 2:
        p0, p1 = perspectives[0], perspectives[1]
        for t in thresholds:
            tid = str(t["id"])
            c0 = persp_out[p0]["thresholds"][tid]["chosen"]
            c1 = persp_out[p1]["thresholds"][tid]["chosen"]
            agree[tid] = "agree" if c0 == c1 else "differ"

    first_p = perspectives[0]
    first_t = str(thresholds[0]["id"])
    hl_chosen = persp_out[first_p]["thresholds"][first_t]["chosen"]
    hl_row = persp_out[first_p]["strategies"][hl_chosen]
    headline = {
        "perspective": first_p,
        "threshold_id": first_t,
        "lambda": float(thresholds[0]["lambda"] if "lambda" in thresholds[0] else thresholds[0]["usd_per_unit"]),
        "chosen": hl_chosen,
        "reference": ref,
        "status": hl_row["status"],
        "delta_cost": hl_row["delta_cost_vs_ref"],
        "delta_health": hl_row["delta_health_vs_ref"],
        "icer": hl_row["icer"] if hl_row["status"] == "icer" else None,
    }
    return {
        "perspectives": persp_out,
        "perspectives_agree": agree,
        "headline": headline,
        "health_measure": {"kind": hm["kind"], "events": hm["events"]},
        "timing": (ce.get("discounting") or {}).get("timing", "uniform_accrual"),
        "population": pop,
        "switching": [],
    }


def claims(_c, art_rel: str, cea_data: dict, spec, uq_interval: dict | None = None) -> list[dict]:
    """Emit §7 cost-effectiveness claims; `.icer` claims are emitted ONLY when strategy status is `"icer"`."""
    raw = spec.raw if hasattr(spec, "raw") else spec
    ce = raw["cost_effectiveness"]
    prices_cfg = raw["options"].get("prices") or {}
    cur = str(prices_cfg.get("currency") or "USD")
    byr = int(prices_cfg.get("base_year") or ce["discounting"]["base_year"])
    cur_unit = f"{cur} ({byr})"
    hkind = ce["health_measure"]["kind"]
    h_unit = "DALYs averted" if hkind == "dalys" else "QALYs gained"
    icer_unit = f"{cur_unit} per {'DALY averted' if hkind == 'dalys' else 'QALY gained'}"

    out: list[dict] = []
    for p, pdoc in cea_data["perspectives"].items():
        pc = _cid(p)
        uq_p = pdoc.get("uncertainty")
        for s, row in pdoc["strategies"].items():
            sc = _cid(s)
            base_f = f"data.perspectives.{p}.strategies.{s}"
            out.append(_c(f"cea.{pc}.{sc}.cost", "currency", art_rel, f"{base_f}.cost", cur_unit))
            out.append(_c(f"cea.{pc}.{sc}.health", "scalar", art_rel, f"{base_f}.health", h_unit))
            if uq_p is not None and uq_interval is not None:
                out.append(_c(
                    f"cea.{pc}.{sc}.delta_cost", "currency", art_rel,
                    f"data.perspectives.{p}.uncertainty.delta_cost_interval.{s}.median", cur_unit, interval=uq_interval,
                ))
                out.append(_c(
                    f"cea.{pc}.{sc}.delta_health", "scalar", art_rel,
                    f"data.perspectives.{p}.uncertainty.delta_health_interval.{s}.median", h_unit, interval=uq_interval,
                ))
            else:
                out.append(_c(f"cea.{pc}.{sc}.delta_cost", "currency", art_rel, f"{base_f}.delta_cost_vs_ref", cur_unit))
                out.append(_c(f"cea.{pc}.{sc}.delta_health", "scalar", art_rel, f"{base_f}.delta_health_vs_ref", h_unit))
            out.append(_c(f"cea.{pc}.{sc}.status", "label", art_rel, f"{base_f}.status"))
            if row.get("status") == "icer" and row.get("icer") is not None:
                out.append(_c(f"cea.{pc}.{sc}.icer", "currency", art_rel, f"{base_f}.icer", icer_unit))

        for tid, tdoc in pdoc["thresholds"].items():
            tc = _cid(tid)
            tf = f"data.perspectives.{p}.thresholds.{tid}"
            out.append(_c(f"cea.{pc}.{tc}.chosen", "label", art_rel, f"{tf}.chosen"))
            for s in tdoc["nmb"]:
                out.append(_c(f"cea.{pc}.{tc}.{_cid(s)}.nmb", "currency", art_rel, f"{tf}.nmb.{s}", cur_unit))
            if "p_cost_effective_pct" in tdoc:
                out.append(_c(f"cea.{pc}.{tc}.p_ce", "percentage", art_rel, f"{tf}.p_cost_effective_pct", "%"))
                out.append(_c(f"cea.{pc}.{tc}.evpi", "currency", art_rel, f"{tf}.evpi", cur_unit))
                out.append(_c(f"cea.{pc}.{tc}.evpi_per_person", "currency", art_rel, f"{tf}.evpi_per_person", cur_unit))

        for pid, prdoc in (pdoc.get("prices") or {}).items():
            prc = _cid(pid)
            for tid, tr in (prdoc.get("threshold") or {}).items():
                tc = _cid(tid)
                pf = f"data.perspectives.{p}.prices.{pid}.threshold.{tid}"
                out.append(_c(f"cea.{pc}.{prc}.{tc}.status", "label", art_rel, f"{pf}.status"))
                if tr.get("value") is not None:
                    out.append(_c(f"cea.{pc}.{prc}.{tc}.threshold", "currency", art_rel, f"{pf}.value", cur_unit))
                    out.append(_c(f"cea.{pc}.{prc}.{tc}.bracket_lo", "currency", art_rel, f"{pf}.bracket_lo", cur_unit))
                    out.append(_c(f"cea.{pc}.{prc}.{tc}.bracket_hi", "currency", art_rel, f"{pf}.bracket_hi", cur_unit))

    for tid in cea_data.get("perspectives_agree") or {}:
        tc = _cid(tid)
        out.append(_c(f"cea.perspectives_agree.{tc}", "label", art_rel, f"data.perspectives_agree.{tid}"))

    hl = cea_data["headline"]
    out.append(_c("cea.headline.chosen", "label", art_rel, "data.headline.chosen"))
    out.append(_c("cea.headline.status", "label", art_rel, "data.headline.status"))
    out.append(_c("cea.headline.delta_cost", "currency", art_rel, "data.headline.delta_cost", cur_unit))
    out.append(_c("cea.headline.delta_health", "scalar", art_rel, "data.headline.delta_health", h_unit))
    if hl.get("status") == "icer" and hl.get("icer") is not None:
        out.append(_c("cea.headline.icer", "currency", art_rel, "data.headline.icer", icer_unit))
    return out


def results_lines(cea_data: dict, spec) -> list[str]:
    """Render the §7 cost-effectiveness section for `results.md`, with every number and verdict as a claim."""
    L = ["", "## Cost-effectiveness", ""]
    L.append(
        "Headline choice (first perspective, first threshold): **[CLAIM:cea.headline.chosen]** "
        "(status: [CLAIM:cea.headline.status]; incremental cost vs reference: [CLAIM:cea.headline.delta_cost]; "
        "incremental health vs reference: [CLAIM:cea.headline.delta_health]"
        + ("; ICER: [CLAIM:cea.headline.icer]" if cea_data["headline"].get("status") == "icer" else "")
        + ")."
    )
    for p, pdoc in cea_data["perspectives"].items():
        pc = _cid(p)
        L += ["", f"### Perspective: {p}", "",
              "| Strategy | Cost | Health | ΔCost vs ref | ΔHealth vs ref | Status | ICER |",
              "|---|---:|---:|---:|---:|---|---:|"]
        for s, row in pdoc["strategies"].items():
            sc = _cid(s)
            icer_cell = f"[CLAIM:cea.{pc}.{sc}.icer]" if row.get("status") == "icer" and row.get("icer") is not None else "—"
            L.append(
                f"| {s} | [CLAIM:cea.{pc}.{sc}.cost] | [CLAIM:cea.{pc}.{sc}.health] | "
                f"[CLAIM:cea.{pc}.{sc}.delta_cost] | [CLAIM:cea.{pc}.{sc}.delta_health] | "
                f"[CLAIM:cea.{pc}.{sc}.status] | {icer_cell} |"
            )
        L += ["", "| Threshold | Chosen | P(cost-effective) | EVPI | EVPI / person |",
              "|---|---|---:|---:|---:|"]
        for tid, tdoc in pdoc["thresholds"].items():
            tc = _cid(tid)
            if "p_cost_effective_pct" in tdoc:
                L.append(
                    f"| {tid} | [CLAIM:cea.{pc}.{tc}.chosen] | [CLAIM:cea.{pc}.{tc}.p_ce] | "
                    f"[CLAIM:cea.{pc}.{tc}.evpi] | [CLAIM:cea.{pc}.{tc}.evpi_per_person] |"
                )
            else:
                L.append(f"| {tid} | [CLAIM:cea.{pc}.{tc}.chosen] | — | — | — |")
        if pdoc.get("prices"):
            L += ["", "| Price input | Threshold | Status | Threshold price | Bracket |",
                  "|---|---|---|---:|---|"]
            for pid, prdoc in pdoc["prices"].items():
                prc = _cid(pid)
                for tid, tr in (prdoc.get("threshold") or {}).items():
                    tc = _cid(tid)
                    if tr.get("value") is not None:
                        L.append(
                            f"| {pid} | {tid} | [CLAIM:cea.{pc}.{prc}.{tc}.status] | "
                            f"[CLAIM:cea.{pc}.{prc}.{tc}.threshold] | "
                            f"[[CLAIM:cea.{pc}.{prc}.{tc}.bracket_lo], [CLAIM:cea.{pc}.{prc}.{tc}.bracket_hi]] |"
                        )
                    else:
                        L.append(f"| {pid} | {tid} | [CLAIM:cea.{pc}.{prc}.{tc}.status] | — | — |")
    if cea_data.get("perspectives_agree"):
        L += ["", "Perspective agreement by threshold: " + ", ".join(
            f"{tid}: [CLAIM:cea.perspectives_agree.{_cid(tid)}]" for tid in cea_data["perspectives_agree"]
        ) + "."]
    L.append("")
    return L


# --------------------------------------------------------------------------- Self-test (CE1-CE4, CE6-CE13, CE15)
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

    # CE4: Drummond et al. (4th ed., 2015, Ch. 4 Table 4.5) mutually exclusive programmes with SD & ED
    drum_st = {"A": (4147.0, 0.593), "B": (8363.0, 0.658), "C": (8907.0, 0.787), "D": (9078.0, 0.758)}
    inc4 = incremental_analysis(drum_st, "A")
    st4 = inc4["strategies"]
    ok(inc4["frontier"] == ["A", "C"]
       and st4["D"]["status"] == "dominated" and st4["D"]["vs"] == "C"
       and st4["B"]["status"] == "extendedly_dominated" and st4["B"]["vs"] == ["A", "C"]
       and abs(st4["C"]["icer"] - (8907.0 - 4147.0) / (0.787 - 0.593)) < 1e-6,
       f"CE4 Drummond et al. (2015, 4th ed., Table 4.5) frontier={inc4['frontier']}, C ICER={st4['C']['icer']:.2f}")

    # CE6: Gray et al. (2011, Ch. 2 Table 2.1) incremental table + probabilistic CEAC exercise
    gray_st = {"A": (0.0, 0.0), "B": (10000.0, 0.40), "C": (22000.0, 0.55), "D": (25000.0, 0.50), "E": (40000.0, 1.00)}
    inc6 = incremental_analysis(gray_st, "A")
    st6 = inc6["strategies"]
    rng6 = np.random.default_rng(2011)
    n6 = 4000
    gray_draws = {
        "A": (np.zeros(n6), np.zeros(n6)),
        "B": (rng6.normal(10000.0, 1000.0, n6), rng6.normal(0.40, 0.04, n6)),
        "E": (rng6.normal(40000.0, 3000.0, n6), rng6.normal(1.00, 0.08, n6)),
    }
    p6 = probabilistic_summary(gray_draws, "A", [{"id": "t35", "lambda": 35000.0}, {"id": "t80", "lambda": 80000.0}])
    ceac_sums6 = [sum(p6["ceac"]["share"][s][j] for s in gray_draws) for j in range(len(p6["ceac"]["lambda"]))]
    ok(inc6["frontier"] == ["A", "B", "E"]
       and st6["D"]["status"] == "dominated"
       and st6["C"]["status"] == "extendedly_dominated" and st6["C"]["vs"] == ["B", "E"]
       and abs(st6["B"]["icer"] - 25000.0) < 1e-6 and abs(st6["E"]["icer"] - 50000.0) < 1e-6
       and all(abs(x - 1.0) < 1e-9 for x in ceac_sums6)
       and p6["thresholds"]["t35"]["ceaf_chosen"] == "B" and p6["thresholds"]["t80"]["ceaf_chosen"] == "E",
       f"CE6 Gray et al. (2011, Table 2.1) frontier={inc6['frontier']}, CEAC sums to 1, CEAF switches B->E")

    # CE8: Fenwick et al. (2001) — CEAF picks highest expected NMB even when another strategy has higher CEAC
    n8 = 1000
    b_nmb_c = np.where(np.arange(n8) < 700, 90.0, 140.0)   # at lam=100, E=1 -> NMB(B)=+10 (70%) or -40 (30%), mean=-5
    dbs8 = {"A": (np.zeros(n8), np.zeros(n8)), "B": (b_nmb_c, np.ones(n8))}
    p8 = probabilistic_summary(dbs8, "A", [{"id": "t100", "lambda": 100.0}], ceac_grid=[100.0])
    ok(p8["ceac"]["share"]["B"][0] > 0.69 and p8["thresholds"]["t100"]["ceaf_chosen"] == "A"
       and abs(p8["thresholds"]["t100"]["p_cost_effective"] - 0.30) < 1e-9,
       f"CE8 Fenwick (2001): B has CEAC {p8['ceac']['share']['B'][0]:.2f} but CEAF picks A (p_ce={p8['thresholds']['t100']['p_cost_effective']:.2f})")

    # CE9: Analytic normal CEAC — ΔE ~ N(1, 0.5^2), ΔC ~ N(300, 200^2), lam=500 -> Φ(200 / sqrt(102500)) ≈ 0.7339
    rng9 = np.random.default_rng(9001)
    n9 = 20000
    de9 = rng9.normal(1.0, 0.5, n9)
    dc9 = rng9.normal(300.0, 200.0, n9)
    p9 = probabilistic_summary({"A": (np.zeros(n9), np.zeros(n9)), "B": (dc9, de9)}, "A",
                               [{"id": "t500", "lambda": 500.0}], ceac_grid=[500.0])
    z9 = (500.0 * 1.0 - 300.0) / math.sqrt(500.0 ** 2 * 0.25 + 200.0 ** 2)
    phi9 = 0.5 * (1.0 + math.erf(z9 / math.sqrt(2.0)))
    mc_se9 = math.sqrt(phi9 * (1.0 - phi9) / n9)
    got9 = p9["ceac"]["share"]["B"][0]
    ok(abs(got9 - phi9) < 4.0 * mc_se9,
       f"CE9 analytic normal CEAC at lam=500: got {got9:.4f}, exact {phi9:.4f} (4*SE={4*mc_se9:.4f})")

    # CE10: Unit normal loss EVPI — ΔNMB ~ N(10, 20^2) -> EVPI = σ φ(μ/σ) + μ Φ(μ/σ) - max(0, μ) ≈ 3.9559
    rng10 = np.random.default_rng(10001)
    n10 = 20000
    dnmb10 = rng10.normal(10.0, 20.0, n10)
    # Set lam=1, E=dnmb10, C=0 so NMB(B) - NMB(A) = dnmb10
    p10 = probabilistic_summary({"A": (np.zeros(n10), np.zeros(n10)), "B": (np.zeros(n10), dnmb10)}, "A",
                                [{"id": "t1", "lambda": 1.0}], ceac_grid=[1.0])
    mu10, sig10 = 10.0, 20.0
    r10 = mu10 / sig10
    exact_evpi10 = sig10 * (math.exp(-0.5 * r10 * r10) / math.sqrt(2.0 * math.pi)) + mu10 * (
        0.5 * (1.0 + math.erf(r10 / math.sqrt(2.0)))) - max(0.0, mu10)
    got10 = p10["thresholds"]["t1"]["evpi"]
    se10 = p10["thresholds"]["t1"]["evpi_se"]
    ok(abs(got10 - exact_evpi10) < 4.0 * max(se10, 0.05),
       f"CE10 unit normal loss EVPI: got {got10:.3f} (SE={se10:.3f}), exact {exact_evpi10:.3f}")

    # CE15: Collector reads uncertainty.run's exact draws without extra evaluations; uncertainty.yaml byte-identical
    import copy
    from toolkit.analysis import artifacts as ART, claims as CL, core as CORE, model as MOD, uncertainty as UQ
    with tempfile.TemporaryDirectory() as d15:
        raw_base = CEASPEC.toy_cea_spec(d15, standalone=False)
        raw_base["uncertainty"] = {
            "draws": 15, "seed": 77,
            "sources": [{"kind": "engine_parameters"}],
            "excluded": [
                {"kind": "calibration", "reason": "toy test"},
                {"kind": "costs", "reason": "negotiated price grid"},
                {"kind": "emulator", "reason": "direct evaluation"},
                {"kind": "health_weights", "reason": "held fixed in CE15 parity check"},
            ],
        }
        raw_no_cea = copy.deepcopy(raw_base)
        raw_no_cea.pop("cost_effectiveness", None)
        raw_no_cea["uncertainty"]["excluded"] = [
            x for x in raw_no_cea["uncertainty"]["excluded"] if x.get("kind") != "health_weights"
        ]
        sp_no = schema.Spec(raw_no_cea, d15, os.path.join(d15, "spec_no.yaml"))
        sp_ce = schema.Spec(raw_base, d15, os.path.join(d15, "spec_ce.yaml"))
        m_no = MOD.DirectModel(sp_no)
        m_ce = MOD.DirectModel(sp_ce)
        A_no = CORE.Analysis(sp_no, m_no)
        A_ce = CORE.Analysis(sp_ce, m_ce)
        z_no = {i: None for i in A_no.ids}
        out_no, ben_no, cost_no = A_no.evaluate(A_no.theta0, z_no)
        plan_no = A_no.solve(ben_no, cost_no)["choice"]
        u_no = UQ.run(A_no, plan_no, "total_averted", draws=15, seed=77)
        z_ce = {i: None for i in A_ce.ids}
        out_ce, ben_ce, cost_ce = A_ce.evaluate(A_ce.theta0, z_ce)
        plan_ce = A_ce.solve(ben_ce, cost_ce)["choice"]
        ccol15 = Collector(A_ce)
        u_ce = UQ.run(A_ce, plan_ce, "total_averted", draws=15, seed=77, on_draw=ccol15.add)
        # Note: draw_plan/excludes lists `health_weights` only when CEA section is present; compare draws & quantiles
        u_no_cmp = {k: v for k, v in u_no["data"].items() if k not in ("excludes", "label", "interval", "draw_plan")}
        u_ce_cmp = {k: v for k, v in u_ce["data"].items() if k not in ("excludes", "label", "interval", "draw_plan")}
        y_no = yaml.safe_dump(ART.plain(u_no_cmp), sort_keys=False)
        y_ce = yaml.safe_dump(ART.plain(u_ce_cmp), sort_keys=False)
        cea_doc15 = run_cea(A_ce, out_ce, cost_ce, collector=ccol15, uq_level=0.95)
        st15 = ART.Store(d15, "sha", True, {"name": "toy"})
        rel15 = st15.write("cost_effectiveness", cea_doc15, PRODUCER, [])
        cl15 = claims(CL._c, rel15, cea_doc15, sp_ce, uq_interval=u_ce["data"]["interval"])
        rl15 = results_lines(cea_doc15, sp_ce)
        os.makedirs(os.path.join(d15, "models"), exist_ok=True)
        with open(os.path.join(d15, "models", "claims_spec.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump({"producer": PRODUCER, "produced_by": "harness", "claims": cl15}, f, sort_keys=False)
        B15, R15 = CL._scripts()
        bdoc15, berrs15 = B15.write(d15)
        loaded15 = R15.load_claims(os.path.join(d15, "models", "claims_ledger.yaml"))
        _, unres15 = R15.render_text("\n".join(rl15), loaded15)
        # Also verify health_weights stream draws hw:cases without altering engine_parameters draws
        raw_hw = copy.deepcopy(raw_base)
        raw_hw["uncertainty"]["sources"].append({"kind": "health_weights"})
        raw_hw["uncertainty"]["excluded"] = [
            x for x in raw_hw["uncertainty"]["excluded"] if x.get("kind") != "health_weights"
        ]
        sp_hw = schema.Spec(raw_hw, d15, os.path.join(d15, "spec_hw.yaml"))
        A_hw = CORE.Analysis(sp_hw, MOD.DirectModel(sp_hw))
        out_hw, ben_hw, cost_hw = A_hw.evaluate(A_hw.theta0, {i: None for i in A_hw.ids})
        plan_hw = A_hw.solve(ben_hw, cost_hw)["choice"]
        ccol_hw = Collector(A_hw)
        u_hw = UQ.run(A_hw, plan_hw, "total_averted", draws=15, seed=77, on_draw=ccol_hw.add)
        ok(y_no == y_ce and ccol15.n_draws == 15 and m_ce.runs == m_no.runs
           and u_hw["data"]["fixed_plan"] == u_no["data"]["fixed_plan"]
           and "hw:cases" in (u_hw["report"].get("parameter_samples") or {})
           and cea_doc15["perspectives"]["health_system"]["uncertainty"]["draws"] == 15
           and cea_doc15["perspectives"]["health_system"]["uncertainty"]["status"] == "indicative"
           and not berrs15 and not (bdoc15 or {}).get("unresolved") and not unres15,
           f"CE15 uncertainty draws identical with/without CEA & with health_weights stream (runs={m_ce.runs}), collector reads exact 15 draws, claims+results render ({len(loaded15)} claims, unres={unres15})")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
