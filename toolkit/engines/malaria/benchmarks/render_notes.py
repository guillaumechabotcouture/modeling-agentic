#!/usr/bin/env python3
"""Render the tables of ../ENGINE_NOTES.md from the pack artifacts (no number is typed by hand).

    python3 toolkit/engines/malaria/benchmarks/render_notes.py            # rewrite the tables block
    python3 toolkit/engines/malaria/benchmarks/render_notes.py --self-test

Reads results.yaml and fitted.yaml (this engine version), history/results_v0.1-spike.yaml
(the spike, "before"), and b15_mechanisms.yaml if present. Replaces the text between
<!-- tables:start --> and <!-- tables:end --> in ENGINE_NOTES.md.
"""
from __future__ import annotations

import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
NOTES = os.path.join(os.path.dirname(HERE), "ENGINE_NOTES.md")
START, END = "<!-- tables:start -->", "<!-- tables:end -->"
LABEL = {"in_band": "in band", "out_of_band": "**out of band**", "not_assessable": "not assessable",
         "band_mis_based": "band mis-based"}


def _y(path):
    return yaml.safe_load(open(path)) if os.path.exists(path) else None


def _fmt(v):
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:.3g}" if abs(v) < 1000 else f"{v:,.0f}"
    return str(v)


def _status_old(r):
    return "in band" if r.get("in_band") else "**out of band**"


def _value(r):
    if r.get("points") is not None:
        return f"{r['rules_met']}/{r['rules_checked']} rules"
    return _fmt(r.get("computed"))


def _spread(r):
    if r.get("per_seed_rules_met"):
        return "per seed: " + ", ".join(r["per_seed_rules_met"])
    if r.get("seed_min") is None:
        return ""
    return f"{_fmt(r['seed_min'])}–{_fmt(r['seed_max'])}"


def _band(r):
    b = r.get("band")
    return f"{_fmt(r.get('target'))} ({_fmt(b[0])}–{_fmt(b[1])})" if b else "rules"


def render(new: dict, old: dict | None, fitted: dict | None, mech: dict | None) -> str:
    o = {r["id"]: r for r in (old or {}).get("benchmarks", [])}
    out = ["### Benchmark pack, before (spike v0.1) and after (v0.2)", "",
           "| ID | Benchmark | Target (band) | v0.1 spike | v0.1 status | v0.2 | v0.2 seed range | v0.2 status | Role |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in new["benchmarks"]:
        p = o.get(r["id"])
        out.append(f"| {r['id']} | {r['metric']} | {_band(r)} | {_value(p) if p else 'n/a'} | "
                   f"{_status_old(p) if p else 'n/a'} | {_value(r)} | {_spread(r)} | {LABEL[r['status']]} | {r['role']} |")
    s = new["summary"]
    bs = s["by_status"]
    out += ["", f"v0.2: {bs['in_band']} in band, {bs['out_of_band']} out of band, {bs['not_assessable']} not assessable, "
            f"{bs['band_mis_based']} band mis-based, of {s['total']} ({s['held_out_in_band']} of {s['held_out_total']} "
            f"held-out in band). {new['n_sims']} engine runs, {len(new['seeds'])} seeds per arm (common random numbers). "
            f"v0.1: {(old or {}).get('summary', {}).get('in_band', 'n/a')} of {(old or {}).get('summary', {}).get('total', 'n/a')} in band."]
    notes = [r for r in new["benchmarks"] if r["status"] in ("not_assessable", "band_mis_based") or r.get("requires")
             or (r.get("price_floor") is not None and (r["price_floor"] > r["target"] or r.get("reach_in_fitted_range")))]
    for r in notes:
        if r.get("reach_in_fitted_range"):
            lo_, hi_ = r["reach_in_fitted_range"]
            out.append(f"- {r['id']}: over the fitted parameters' range (values consistent with the effect benchmarks) "
                       f"it spans {_fmt(lo_)}–{_fmt(hi_)} against the band {_fmt(r['band'][0])}–{_fmt(r['band'][1])}"
                       + ("; unreachable at the pack's prices." if r.get("mis_based_reason") == "unreachable_in_fitted_range" else "."))
        if r.get("price_floor") is not None and r["price_floor"] > r["target"]:
            out.append(f"- {r['id']}: the target {_fmt(r['target'])} is below the price floor {_fmt(r['price_floor'])} per DALY "
                       f"(cost per person divided by ALL the comparator's DALYs per person): no intervention can reach the "
                       f"target value at the pack's price; only the band's upper part is reachable. The band's basis "
                       f"(price or setting) differs from the decision's.")
        if r.get("price_floor") is not None and (r["status"] == "band_mis_based" or r.get("reach_in_fitted_range")
                                                 or r["price_floor"] > r["target"]):
            rel = "above" if r["price_floor"] > r["band"][1] else "below"
            out.append(f"- {r['id']}: price floor {_fmt(r['price_floor'])} per DALY (cost {_fmt(r['cost_per_capita'])} per person "
                       f"over the comparator's {_fmt(r['comparator_outcome_per_capita'])} DALYs per person), {rel} the band's "
                       f"upper edge {_fmt(r['band'][1])}; meeting the band would need {_fmt(100 * r['share_averted_needed_for_band'])}% "
                       f"of all DALYs averted; the engine averts {_fmt(100 * r['share_averted'])}%.")
        if r.get("requires"):
            vals = ", ".join(f"{k} {v}" for k, v in r["requires_values"].items())
            why = "" if r["status"] != "not_assessable" else " (not assessable: " + ", ".join(
                f"{k} {v['computed']} outside {v['required']}" for k, v in r["not_assessable_because"].items()) + ")"
            out.append(f"- {r['id']} requires {r['requires']}; computed {vals}{why}.")
        if r.get("arm_or_geomean"):
            a = r["arm_or_geomean"]
            out.append(f"- {r['id']} arms (OR vs standard LLIN): PBO alone {a['a10']}, IRS alone {a['a01']}, both {a['a11']}; "
                       f"prevalence {r['arm_prevalence']}.")
    if fitted:
        out += ["", f"Fitted engine-level parameters (fit rule `{fitted['fit_rule']['version']}`, seeds "
                f"{fitted['fit_rule'].get('seeds')}, {fitted['fit_rule'].get('design_points')} design points):", "",
                "| Parameter | Value | Range (all feasible targets in band) | Box | Fitted to |", "|---|---|---|---|---|"]
        for k, v in fitted["parameters"].items():
            out.append(f"| {k} | {v['value']} | {v['lo']}–{v['hi']} | {v.get('box')} | {', '.join(v['benchmarks'])} |")
        fz = fitted.get("feasibility") or {}
        out += ["", f"Feasibility before fitting: infeasible in the box: {fz.get('infeasible_in_box') or 'none'}; "
                f"conflicting pairs: {fz.get('conflicting_pairs') or 'none'}; all feasible targets in band at the value: "
                f"{fz.get('all_feasible_in_band_at_value')}. Surrogate leave-one-out error (log scale): "
                + ", ".join(f"{k} {v['loo_rmse_log']}" for k, v in (fitted.get("surrogate") or {}).items()) + "."]
        ver = fitted.get("verification") or {}
        if ver:
            out.append("Verification at the value (re-run, re-calibrated): " + ", ".join(
                f"{k} {v['computed']} (surrogate {v['predicted']}, {'in' if v['in_band'] else 'out'})" for k, v in ver.items()) + ".")
    if mech:
        t = mech["trial"]
        out += ["", "### B15 by mechanism (Muleba 2×2, 9 months; re-calibrated per row; fitted parameters fixed)", "",
                "| Configuration | PBO alone OR | IRS alone OR | Both OR | Interaction OR | Seed range | Single arms in trial CI |",
                "|---|---|---|---|---|---|---|",
                f"| Trial (Protopopoff 2018) | {t['or_pbo_alone']} | {t['or_irs_alone']} | {t['or_both']} | {t['interaction_or']} "
                f"| CI {t['interaction_ci'][0]}–{t['interaction_ci'][1]} | |"]
        for c in mech["configs"]:
            out.append(f"| {c['config']} | {c['or_pbo_alone']} | {c['or_irs_alone']} | {c['or_both']} | {c['interaction_or']} | "
                       f"{c['seed_min']}–{c['seed_max']} | {'yes' if c['single_arms_in_trial_ci'] else 'no'} |")
    return "\n".join(out)


def write(path=NOTES) -> None:
    new = _y(os.path.join(HERE, "results.yaml"))
    if new is None:
        raise SystemExit("results.yaml missing: run the pack first")
    block = render(new, _y(os.path.join(HERE, "history", "results_v0.1-spike.yaml")),
                   _y(os.path.join(HERE, "fitted.yaml")), _y(os.path.join(HERE, "b15_mechanisms.yaml")))
    text = open(path).read()
    i, j = text.index(START), text.index(END)
    open(path, "w").write(text[:i + len(START)] + "\n" + block + "\n" + text[j:])
    print(f"wrote tables into {path}")


def _self_test() -> int:
    new = {"benchmarks": [
        {"id": "X1", "metric": "m", "target": 1.0, "band": [0.5, 2.0], "computed": 1.1, "seed_min": 0.9, "seed_max": 1.3,
         "status": "in_band", "role": "fit", "in_band": True},
        {"id": "X2", "metric": "c", "target": 1.0, "band": [0.5, 2.0], "computed": 9.0, "status": "band_mis_based",
         "role": "held_out", "in_band": False, "price_floor": 4.0, "cost_per_capita": 4.0,
         "comparator_outcome_per_capita": 1.0, "share_averted_needed_for_band": 2.0, "share_averted": 0.4}],
        "summary": {"total": 2, "held_out_in_band": 0, "held_out_total": 1,
                    "by_status": {"in_band": 1, "out_of_band": 0, "not_assessable": 0, "band_mis_based": 1}},
        "n_sims": 10, "seeds": [1, 2]}
    txt = render(new, {"benchmarks": [{"id": "X1", "computed": 3.0, "in_band": False}], "summary": {"in_band": 0, "total": 1}},
                 None, None)
    ok = ("| X1 |" in txt and "**out of band**" in txt and "band mis-based" in txt and "200%" in txt
          and "0.9–1.3" in txt)
    print(("PASS" if ok else "FAIL") + " R1 the notes table renders statuses, seed ranges and the price-floor note")
    if not ok:
        print(txt)
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    write()
