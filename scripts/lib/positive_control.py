#!/usr/bin/env python3
"""
Phase 21 β — positive-control run fixture for the STAGE 7 gate.

Every rigor gate added since Phase 2 was tested against NEGATIVE fixtures
(an artifact with a defect must fire). Nothing showed that an honest,
complete run can still reach ACCEPT — so a gate that blocks everything
would pass its own self-tests. This module builds a small but complete
run directory that does the work properly, and the validator's self-test
(E8) asserts the real CLI returns ACCEPT for it, then flips each single
defect (zero-width CI, renamed verdict, ledger value off by 10%).

The fixture is deliberately NOT malaria and not tied to any country:
a generic childhood-vaccination campaign allocated across six districts
under a fixed budget, choosing campaign vs no campaign per district.

  - Calibration: two fitted parameters (incidence scale, access slope) fit
    to six observed district case counts by multi-restart Nelder-Mead,
    with a held-out district.
  - Script-produced artifacts are produced by the real producers, run as
    subprocesses exactly as the pipeline runs them:
      scripts/compare_models.py        → model_comparison_formal.yaml
      scripts/identifiability.py       → identifiability.yaml
      scripts/propagate_uncertainty.py → uncertainty_report.yaml (200 draws)
      scripts/render_claims.py         → report.md
  - Benefit is computed on the eligible population (children), the same
    population the campaign is costed on, so scripts/eligibility_probe.py
    passes.
  - Sensitivity, allocation cross-validation (leave-one-district-out) and
    the optimizer benchmark (brute force over 2^6 subsets) are computed,
    not asserted.
  - Every number in report.md comes from a claims-ledger entry whose
    source_field resolves to the artifact holding it.

Usage:
    python3 scripts/lib/positive_control.py <empty_dir> [--round N]
    python3 scripts/lib/positive_control.py --self-test
"""

from __future__ import annotations

import itertools
import json
import math
import os
import subprocess
import sys
import textwrap

import yaml

_SCRIPTS = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

QUESTION = ("Allocate a $1.1 million childhood vaccination campaign budget "
            "across six districts to maximize DALYs averted, with "
            "uncertainty intervals and a decision rule a program officer "
            "can apply.")

# --------------------------------------------------------------------------- #
# Model code written into the fixture (models/model.py, models/outcome_fn.py)
# --------------------------------------------------------------------------- #

_MODEL_PY = '''\
"""District vaccination-campaign allocation model (positive-control fixture).

Benefit and cost are both computed on the eligible population (children
under five); the total population is carried for reporting only.
"""
import csv
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data", "districts.csv")

COVERAGE = 0.9
VACCINE_EFFICACY = 0.85          # registry: vaccine_efficacy
COST_PER_CHILD = 6.0             # registry: cost_per_child (USD)
FIXED_COST_PER_DISTRICT = 20000.0
DALYS_PER_CASE = 0.08
BUDGET = 1100000.0

# Calibrated (models/calibration_result.yaml).
RATE_SCALE = {rate_scale!r}
SLOPE = {slope!r}


def load_districts():
    with open(DATA, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in ("total_population", "eligible_children", "baseline_rate",
                  "access_index", "observed_cases"):
            r[k] = float(r[k])
    return rows


def incidence_per_child(row, rate_scale=None, slope=None):
    rs = RATE_SCALE if rate_scale is None else rate_scale
    sl = SLOPE if slope is None else slope
    return row["baseline_rate"] * rs * math.exp(sl * row["access_index"])


def predicted_cases(row, rate_scale, slope):
    return row["eligible_children"] * incidence_per_child(row, rate_scale, slope)


def evaluate_unit(inputs, efficacy=VACCINE_EFFICACY,
                  cost_per_child=COST_PER_CHILD, coverage=COVERAGE,
                  fixed_cost=FIXED_COST_PER_DISTRICT,
                  dalys_per_case=DALYS_PER_CASE):
    eligible = float(inputs["eligible_children"])
    incidence = float(inputs["incidence_per_child"])
    if inputs.get("package") == "campaign":
        cases = eligible * incidence * coverage * efficacy
        cost = eligible * coverage * cost_per_child + fixed_cost
    else:
        cases = 0.0
        cost = 0.0
    burden = eligible * incidence * dalys_per_case
    return {{"cases_averted": cases, "dalys_averted": cases * dalys_per_case,
            "dalys": burden - cases * dalys_per_case, "cost": cost}}


def candidates(efficacy=VACCINE_EFFICACY, cost_per_child=COST_PER_CHILD,
               coverage=COVERAGE, fixed_cost=FIXED_COST_PER_DISTRICT,
               dalys_per_case=DALYS_PER_CASE, rows=None,
               rate_scale=None, slope=None):
    out = []
    for r in (rows if rows is not None else load_districts()):
        unit = {{"eligible_children": r["eligible_children"],
                "incidence_per_child": incidence_per_child(r, rate_scale, slope),
                "package": "campaign"}}
        res = evaluate_unit(unit, efficacy, cost_per_child, coverage,
                            fixed_cost, dalys_per_case)
        res["district"] = r["district"]
        out.append(res)
    return out


def allocate(budget=BUDGET, **kw):
    """Greedy by DALYs averted per dollar under the budget."""
    cands = sorted(candidates(**kw),
                   key=lambda c: c["dalys_averted"] / c["cost"], reverse=True)
    spent = 0.0
    funded = set()
    for c in cands:
        if spent + c["cost"] <= budget:
            funded.add(c["district"])
            spent += c["cost"]
    return cands, funded
'''

_OUTCOME_PY = '''\
"""UQ entry point and calibration loss for the fixture model."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import model  # noqa: E402


def outcome_fn(params):
    efficacy = params.get("vaccine_efficacy", model.VACCINE_EFFICACY)
    cost_per_child = params.get("cost_per_child", model.COST_PER_CHILD)
    cands, funded = model.allocate(efficacy=efficacy,
                                   cost_per_child=cost_per_child)
    chosen = [c for c in cands if c["district"] in funded]
    return {
        "total_cases_averted": sum(c["cases_averted"] for c in chosen),
        "total_dalys_averted": sum(c["dalys_averted"] for c in chosen),
    }


def loss(params):
    rows = model.load_districts()
    return sum((r["observed_cases"]
                - model.predicted_cases(r, params["rate_scale"],
                                        params["slope"])) ** 2
               for r in rows)
'''

_DISTRICTS = [
    # district, total_population, eligible_children, baseline_rate, access_index
    ("North", 400000, 64000, 0.050, 0.2),
    ("East", 250000, 42000, 0.080, 0.8),
    ("Central", 600000, 90000, 0.030, -0.3),
    ("Highland", 150000, 27000, 0.110, 1.0),
    ("River", 320000, 51000, 0.060, 0.4),
    ("Coast", 500000, 78000, 0.040, -0.1),
]
_TRUE_RATE_SCALE, _TRUE_SLOPE = 1.2, 0.4
_NOISE = (1.03, 0.97, 1.02, 0.99, 1.01, 0.98)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _w(run_dir: str, rel: str, content) -> str:
    path = os.path.join(run_dir, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(content, str):
            f.write(content)
        else:
            yaml.safe_dump(content, f, sort_keys=False)
    return path


def _run_script(name: str, *args: str) -> None:
    r = subprocess.run([sys.executable, os.path.join(_SCRIPTS, name), *args],
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"{name} {' '.join(args)} exited {r.returncode}:\n"
                           f"{r.stdout[-1500:]}\n{r.stderr[-1500:]}")


def _import_model(run_dir: str):
    import importlib.util
    path = os.path.join(run_dir, "models", "model.py")
    spec = importlib.util.spec_from_file_location("pc_fixture_model", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fit(rows, mod, starts):
    """Multi-restart Nelder-Mead on SSE. Returns (best_x, best_loss, nits)."""
    from scipy.optimize import minimize

    def sse(x):
        return sum((r["observed_cases"] - mod.predicted_cases(r, x[0], x[1])) ** 2
                   for r in rows)
    best, nits = None, []
    for x0 in starts:
        res = minimize(sse, x0, method="Nelder-Mead",
                       options={"maxiter": 2000, "xatol": 1e-8, "fatol": 1e-8})
        nits.append(int(res.nit))
        if best is None or res.fun < best.fun:
            best = res
    return [float(best.x[0]), float(best.x[1])], float(best.fun), nits


def _spearman(a, b):
    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        for rank, i in enumerate(order):
            r[i] = float(rank)
        return r
    ra, rb = ranks(a), ranks(b)
    n = len(a)
    d2 = sum((x - y) ** 2 for x, y in zip(ra, rb))
    return 1 - 6 * d2 / (n * (n * n - 1))


def _critique(reviewer: str, prefix: str, round_n: int) -> dict:
    """A PASS critique whose one blocker (raised in round 1) is resolved."""
    doc = {"reviewer": reviewer, "round": round_n, "verdict": "PASS",
           "structural_mismatch": {"detected": False},
           "blockers": [], "carried_forward": []}
    if round_n >= 2:
        bid = f"{prefix}001"
        doc["blockers"] = [{
            "id": bid, "severity": "HIGH", "category": "METHODS",
            "target_stage": "MODEL", "first_seen_round": 1,
            "claim": "Calibration lacked a held-out district.",
            "evidence": "round-1 calibration_result.yaml had no held-out fold",
            "fix_requires": "PATCH", "resolved": True,
            "resolved_in_round": 2,
            "resolved_evidence": "models/calibration_result.yaml held_out_fold",
        }]
        doc["carried_forward"] = [{"id": bid, "prior_round": round_n - 1,
                                   "still_present": False,
                                   "notes": "held-out fold added"}]
    return doc


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #

def _builder():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "build_claims_ledger",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "build_claims_ledger.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(run_dir: str, round_n: int = 2) -> dict:
    """Populate `run_dir` with an honest, complete run. Returns a small
    summary dict (funded districts, totals) for assertions."""
    os.makedirs(run_dir, exist_ok=True)
    # --- data --------------------------------------------------------------
    lines = ["district,total_population,eligible_children,baseline_rate,"
             "access_index,observed_cases"]
    for (name, pop, kids, rate, acc), noise in zip(_DISTRICTS, _NOISE):
        obs = kids * rate * _TRUE_RATE_SCALE * math.exp(_TRUE_SLOPE * acc) * noise
        lines.append(f"{name},{pop},{kids},{rate},{acc},{round(obs)}")
    _w(run_dir, "data/districts.csv", "\n".join(lines) + "\n")
    _w(run_dir, "data/data_schema.yaml", {"files": {"data/districts.csv": {"columns": {
        "district": {"kind": "id"},
        "total_population": {"kind": "count", "unit": "persons", "range": [50000, 2000000]},
        "eligible_children": {"kind": "count", "unit": "children under 5", "range": [5000, 300000]},
        "baseline_rate": {"kind": "proportion", "unit": "annual cases per eligible child",
                          "range": [0.0, 0.3]},
        "access_index": {"kind": "continuous", "unit": "standardized index", "range": [-3, 3]},
        "observed_cases": {"kind": "count", "unit": "cases per year", "range": [0, 100000]},
    }}}})

    # --- calibrate (fit with placeholder constants, then write final code) ---
    _w(run_dir, "models/model.py", _MODEL_PY.format(rate_scale=1.0, slope=0.0))
    mod = _import_model(run_dir)
    rows = mod.load_districts()
    starts = [(0.5, -0.5), (1.0, 0.0), (2.0, 0.5), (1.5, 1.0), (0.8, 0.8),
              (2.5, -0.2), (1.2, 0.3), (0.6, 1.2)]
    held_out = [5]
    train = [r for i, r in enumerate(rows) if i not in held_out]
    (rs, sl), _train_loss, nits = _fit(train, mod, starts)
    held_rmse = math.sqrt(sum(
        (rows[i]["observed_cases"] - mod.predicted_cases(rows[i], rs, sl)) ** 2
        for i in held_out) / len(held_out))
    (rs, sl), best_loss, nits_full = _fit(rows, mod, starts)
    _w(run_dir, "models/model.py", _MODEL_PY.format(rate_scale=rs, slope=sl))
    _w(run_dir, "models/outcome_fn.py", _OUTCOME_PY)
    mod = _import_model(run_dir)
    _w(run_dir, "models/calibration_result.yaml", {
        "method": "nelder_mead_multistart",
        "objective": "sse_observed_district_cases",
        "n_restarts": len(starts),
        "n_iterations": int(sum(nits_full)),
        "iterations_per_restart": nits_full,
        "best_loss": best_loss,
        "parameters": {"rate_scale": rs, "slope": sl},
        "held_out_fold": {"indices": held_out,
                          "units": [rows[i]["district"] for i in held_out],
                          "rmse": held_rmse,
                          "train_iterations": int(sum(nits))},
    })

    # --- multi-structural comparison (null / scale-only / full) -------------
    obs = [r["observed_cases"] for r in rows]
    n = len(rows)

    def scale_only_fit(subset):
        p0 = [r["eligible_children"] * r["baseline_rate"] for r in subset]
        o = [r["observed_cases"] for r in subset]
        return sum(a * b for a, b in zip(o, p0)) / sum(b * b for b in p0)

    k_scale = scale_only_fit(rows)
    preds_null = [sum(obs) / n] * n
    loo_null = [(sum(obs) - obs[i]) / (n - 1) for i in range(n)]
    preds_scale = [r["eligible_children"] * r["baseline_rate"] * k_scale for r in rows]
    loo_scale, loo_full = [], []
    for i in range(n):
        sub = [r for j, r in enumerate(rows) if j != i]
        loo_scale.append(rows[i]["eligible_children"] * rows[i]["baseline_rate"]
                         * scale_only_fit(sub))
        (a, b), _, _ = _fit(sub, mod, [(rs, sl)])
        loo_full.append(mod.predicted_cases(rows[i], a, b))
    preds_full = [mod.predicted_cases(r, rs, sl) for r in rows]
    _w(run_dir, "models/model_comparison.yaml", {
        "n_targets": n, "targets": obs,
        "models": [
            {"name": "null", "k_params": 1, "predictions": preds_null,
             "loo_predictions": loo_null, "description": "Mean predictor"},
            {"name": "simple", "k_params": 1, "predictions": preds_scale,
             "loo_predictions": loo_scale,
             "description": "Baseline rate × fitted scale"},
            {"name": "full", "k_params": 2, "predictions": preds_full,
             "loo_predictions": loo_full,
             "description": "Scale × exp(slope × access index)"},
        ]})
    _run_script("compare_models.py", run_dir)

    # --- identifiability (post-hoc, real tool) + a-priori arithmetic --------
    _w(run_dir, "models/identifiability.yaml", {
        "loss_fn": "models/outcome_fn.py::loss",
        "loss_type": "sse", "n_obs": n,
        "parameters": [
            {"name": "rate_scale", "point_estimate": rs,
             "lower_bound": 0.3, "upper_bound": 4.0},
            {"name": "slope", "point_estimate": sl,
             "lower_bound": -1.5, "upper_bound": 2.0}]})
    _run_script("identifiability.py", run_dir)
    _w(run_dir, "models/identifiability_a_priori.yaml", {
        "stage": "pre_model", "round_drafted": 1,
        "calibration_targets": [{
            "source": "district surveillance case counts",
            "n_independent": n,
            "derivation": f"{n} districts × 1 annual count"}],
        "total_independent_targets": n,
        "proposed_parameters": [
            {"name": "rate_scale", "count": 1, "fitted": True,
             "prior_constraint": "weak"},
            {"name": "slope", "count": 1, "fitted": True,
             "prior_constraint": "weak"}],
        "total_fitted_parameters": 2,
        "total_fixed_parameters": 5,
        "ratio": round(2 / n, 4),
        "verdict": "IDENTIFIABLE",
        "architecture_implication": "Two fitted parameters against six "
                                    "independent targets.",
    })

    # --- parameter registry + UQ (real propagation, 200 draws) -------------
    _w(run_dir, "citations.md", textwrap.dedent("""\
        # Citations

        ## Parameter Registry

        ```yaml
        parameters:
          - name: vaccine_efficacy
            value: 0.85
            kind: efficacy
            ci_low: 0.75
            ci_high: 0.92
            source: "Fixture trial meta-analysis (synthetic)"
          - name: cost_per_child
            value: 6.0
            kind: cost_usd
            ci_low: 4.8
            ci_high: 7.6
            source: "Fixture costing study (synthetic)"
        ```
        """))
    _run_script("propagate_uncertainty.py", run_dir, "--n-draws", "200",
                "--seed", "7")
    with open(os.path.join(run_dir, "uncertainty_report.yaml")) as f:
        uq = yaml.safe_load(f)

    # --- allocation, optimizer benchmark, universal coverage ---------------
    cands, funded = mod.allocate()
    by_name = {c["district"]: c for c in cands}
    alloc_lines = ["district,package,allocated,cost,cases_averted,dalys_averted"]
    for r in rows:
        c = by_name[r["district"]]
        on = r["district"] in funded
        alloc_lines.append(
            f"{r['district']},{'campaign' if on else 'none'},{on},"
            f"{c['cost'] if on else 0.0},{c['cases_averted'] if on else 0.0},"
            f"{c['dalys_averted'] if on else 0.0}")
    _w(run_dir, "models/allocation.csv", "\n".join(alloc_lines) + "\n")
    # Phase 38 β: the campaign's effect against no campaign, computed by the harness
    # from evaluate_unit on the funded districts and held to the trial efficacy.
    _w(run_dir, "models/units.csv", "district,eligible_children,incidence_per_child\n" + "".join(
        f"{r['district']},{r['eligible_children']},{mod.incidence_per_child(r)}\n" for r in rows))
    _w(run_dir, "models/effect_concordance.yaml", {
        "unit_fn": "models/model.py::evaluate_unit", "call_style": "dict",
        "allocation_csv": "models/allocation.csv", "unit_column": "district", "package_column": "package",
        "benefit_column": "dalys_averted", "units_csv": "models/units.csv", "package_input": "package",
        "benefit_output": "dalys_averted", "burden_output": "dalys",
        "effects": [{"id": "E1", "intervention": "vaccination campaign", "registry": "vaccine_efficacy",
                     "trial_comparator": "unvaccinated (placebo arm)", "trial_use": 1.0, "comparator_id": "CF1",
                     "comparator_delivers": "no campaign: no vaccination in the district",
                     "on_package": "campaign", "off_package": "none", "model_use": mod.COVERAGE}]})
    spent = sum(by_name[d]["cost"] for d in funded)
    dalys = sum(by_name[d]["dalys_averted"] for d in funded)
    cases = sum(by_name[d]["cases_averted"] for d in funded)

    best_obj = 0.0
    for k in range(len(cands) + 1):
        for combo in itertools.combinations(cands, k):
            if sum(c["cost"] for c in combo) <= mod.BUDGET:
                best_obj = max(best_obj, sum(c["dalys_averted"] for c in combo))
    _w(run_dir, "models/optimization_quality.yaml", {
        "primary_method": "greedy",
        "primary_objective": dalys,
        "objective_name": "dalys_averted_per_year",
        "benchmark_methods": [{"method": "brute_force", "objective": best_obj,
                               "runtime_sec": 0.01,
                               "notes": "All 2^6 district subsets enumerated."}],
        "gap_pct": 100 * (best_obj - dalys) / best_obj if best_obj else 0.0,
    })
    universal_cost = sum(c["cost"] for c in cands)
    universal_dalys = sum(c["dalys_averted"] for c in cands)
    _w(run_dir, "models/universal_coverage.yaml", {
        "total_units": len(rows),
        "actual_budget": mod.BUDGET,
        "budget_for_universal_campaign": universal_cost,
        "dalys_averted": dalys,
        "universal_coverage_dalys_averted_estimate": universal_dalys,
        "efficiency_pct": 100 * dalys / universal_dalys,
    })

    # --- sensitivity (5 load-bearing parameters × 2 perturbations) ---------
    # The primary recommendation is the decision rule itself: the
    # priority ranking of districts by DALYs averted per dollar. Which
    # district sits exactly at the budget margin is reported separately
    # (`funded_set_changes`), not counted as a recommendation flip.
    def objective_and_rank(**kw):
        cs, fs = mod.allocate(**kw)
        order = [c["district"] for c in cs]  # allocate() sorts by CE
        return (sum(c["dalys_averted"] for c in cs if c["district"] in fs),
                order, fs)

    base_obj, base_rank, base_set = objective_and_rank()
    params = [("vaccine_efficacy", "efficacy", 0.85, (0.75, 0.92)),
              ("cost_per_child", "cost_per_child", 6.0, (4.8, 7.6)),
              ("coverage", "coverage", 0.9, (0.8, 0.95)),
              ("fixed_cost_per_district", "fixed_cost", 20000.0, (15000.0, 25000.0)),
              ("dalys_per_case", "dalys_per_case", 0.08, (0.06, 0.10))]
    lbp = []
    for i, (pname, kw, val, alts) in enumerate(params, start=1):
        perts = []
        for alt in alts:
            obj, rank, fs = objective_and_rank(**{kw: alt})
            perts.append({"value": alt, "objective": obj,
                          "rank_change_top_n": sum(a != b for a, b in
                                                   zip(rank, base_rank)),
                          "primary_recommendation_changes": rank != base_rank,
                          "funded_set_changes": sorted(fs ^ base_set)})
        lbp.append({"id": f"SA-{i:03d}", "parameter": pname,
                    "description": f"{pname} at its 95% CI endpoints",
                    "primary_value": val, "primary_objective": base_obj,
                    "perturbations": perts})
    sys.path.insert(0, _SCRIPTS)
    import sensitivity_analysis as _sa  # noqa: E402
    sens_doc = {"load_bearing_parameters": lbp, "verdict": "ROBUST",
                "primary_recommendation": "district priority ranking by "
                                          "DALYs averted per dollar",
                "notes": "Optimizer re-run at each perturbed value."}
    _w(run_dir, "models/sensitivity_analysis.yaml", sens_doc)
    computed = _sa.validate_sensitivity_analysis(
        os.path.join(run_dir, "models", "sensitivity_analysis.yaml"))
    sens_doc["verdict"] = computed.get("computed_verdict") or computed["verdict"]
    _w(run_dir, "models/sensitivity_analysis.yaml", sens_doc)
    sens_verdict = sens_doc["verdict"]

    # --- allocation cross-validation (leave-one-district-out) --------------
    # Same (district) order on both sides; allocate() returns CE order.
    full_ce = [c["dalys_averted"] / c["cost"] for c in mod.candidates()]
    rank_corrs, gaps, concord = [], [], []
    for i in range(n):
        sub = [r for j, r in enumerate(rows) if j != i]
        (a, b), _, _ = _fit(sub, mod, [(rs, sl)])
        fold_c = mod.candidates(rate_scale=a, slope=b)
        fold_ce = [c["dalys_averted"] / c["cost"] for c in fold_c]
        rank_corrs.append(_spearman(full_ce, fold_ce))
        _, fold_set = mod.allocate(rate_scale=a, slope=b)
        fold_d = sum(c["dalys_averted"] for c in cands if c["district"] in fold_set)
        gaps.append(100 * abs(dalys - fold_d) / dalys)
        concord.append(100 * sum((d in fold_set) == (d in funded)
                                 for d in by_name) / n)
    _w(run_dir, "models/allocation_robustness.yaml", {
        "holdout_method": "leave-one-district-out", "n_folds": n,
        "metrics": {
            "rank_correlation_mean": sum(rank_corrs) / n,
            "rank_correlation_worst_fold": min(rank_corrs),
            "cases_averted_gap_pct_mean": sum(gaps) / n,
            "cases_averted_gap_pct_worst_fold": max(gaps),
            "rule_classification_concordance_pct_mean": sum(concord) / n,
            "rule_classification_concordance_pct_worst_fold": min(concord)},
        "verdict": "ROBUST",
        "notes": "Calibration refit without each district; optimizer re-run.",
    })

    # --- eligibility manifest (behavioral probe target) --------------------
    r0 = rows[0]
    _w(run_dir, "models/eligibility_manifest.yaml", {
        "outcome_fn": "models/model.py::evaluate_unit",
        "call_style": "dict",
        "baseline_inputs": {"total_population": r0["total_population"],
                            "eligible_children": r0["eligible_children"],
                            "incidence_per_child": mod.incidence_per_child(r0),
                            "package": "none"},
        "interventions": [{
            "name": "vaccination_campaign",
            "on_inputs": {"package": "campaign"},
            "off_inputs": {"package": "none"},
            "eligible_population_field": "eligible_children",
            "total_population_field": "total_population",
            "benefit_output": "cases_averted",
            "spillover": False}]})

    # --- model outputs used by benchmarks / ledger -------------------------
    mean_incidence = sum(mod.incidence_per_child(r) for r in rows) / n
    _w(run_dir, "models/model_outputs.yaml", {
        "budget": mod.BUDGET,
        "n_units": n,
        "n_funded": len(funded),
        "mean_incidence_per_1000_children": 1000 * mean_incidence,
    })
    _w(run_dir, "models/benchmark_targets.yaml", {"benchmarks": [{
        "id": "child_incidence_baseline",
        "metric": "incidence_per_1000_children",
        "target_value": 70.0, "units": "per_1000_child_years",
        "tolerance_factor": 1.5,
        "source": "Fixture surveillance review (synthetic)",
        "computed_field":
            "models/model_outputs.yaml::mean_incidence_per_1000_children"}]})

    # --- sanity schema ------------------------------------------------------
    baseline_cases = sum(mod.predicted_cases(r, rs, sl) for r in rows)
    _w(run_dir, "models/sanity_schema.yaml", {
        "outcome": {"name": "cases averted", "unit": "cases",
                    "baseline_total": baseline_cases, "averted_point": cases},
        "allocation": {"units_total": n, "units_allocated": len(funded),
                       "budget": mod.BUDGET},
        "outlier_sniff": {"metric": "dalys_averted_per_dollar",
                          "rule": "max_over_median", "threshold": 10.0,
                          "csv_path": "models/allocation.csv",
                          "numerator_col": "dalys_averted",
                          "denominator_col": "cost"}})

    # --- plan, success criteria, pre-mortem, strategy ----------------------
    _w(run_dir, "plan.md", "# Plan\n\nCalibrate district incidence, allocate "
                           "the campaign budget, quantify uncertainty.\n")
    with open(os.path.join(run_dir, "model_comparison_formal.yaml")) as f:
        formal = yaml.safe_load(f)
    _w(run_dir, "success_criteria.yaml", {"hard_blockers": [{
        "id": "HB-001",
        "criterion": "Full model generalizes at least as well as the null",
        "metric": "full_rmse_loo", "operator": "<=",
        "threshold": formal["models"]["null"]["rmse_loo"],
        "artifact": "model_comparison_formal.yaml",
        "artifact_field": "models.full.rmse_loo"}],
        "minimum_bar": [], "targets": []})
    _w(run_dir, "pre_mortem.yaml", {
        "agent": "critique-premortem",
        "concerns": [{
            "id": "PM-001", "severity": "HIGH", "category": "DATA",
            "concern": "Reported case counts may under-ascertain incidence.",
            "addressed_in": "modeling_strategy.md § Pre-mortem Responses"}]})
    _w(run_dir, "modeling_strategy.md",
       "# Modeling strategy\n\n## Pre-mortem Responses\n\n"
       "PM-001: incidence is calibrated to counts with a fitted scale "
       "factor, and the benchmark check compares the fitted incidence "
       "with a published range.\n")

    # --- decision rule ------------------------------------------------------
    _w(run_dir, "decision_rule.md", textwrap.dedent("""\
        ---
        rule_type: tabular
        ---
        # Decision rule

        ## Features
        - DALYs averted per dollar of campaign cost in each district,
          computed from eligible children and calibrated incidence.

        ## Rule
        Fund districts in descending order of DALYs averted per dollar
        until the next district's campaign cost exceeds the remaining
        envelope.

        ## Validation
        - accuracy_vs_optimizer: 1.0
        - exceptions_count: 0
        """))

    # --- claims ledger + report (rendered by render_claims) ----------------
    so = uq["scalar_outputs"]
    # Phase 22 γ: the analyst writes the spec (no values); the ledger is built.
    _w(run_dir, "models/claims_spec.yaml", {"claims": [
        {"id": "dalys_total", "claim_kind": "scalar", "units": "DALYs_per_year",
         "source_artifact": "uncertainty_report.yaml",
         "source_field": "scalar_outputs.total_dalys_averted.mean",
         "causal_label": "MODEL_ESTIMATE", "confidence": "MEDIUM"},
        {"id": "spend", "claim_kind": "currency", "units": "USD",
         "source_artifact": "models/allocation.csv",
         "source_field": "cost.sum() where allocated=True",
         "causal_label": "BY_CONSTRUCTION", "confidence": "HIGH"},
        {"id": "budget", "claim_kind": "currency",
         "units": "USD", "source_artifact": "models/model_outputs.yaml",
         "source_field": "budget",
         "causal_label": "BY_CONSTRUCTION", "confidence": "HIGH"},
        {"id": "n_funded", "claim_kind": "count",
         "units": "districts", "source_artifact": "models/model_outputs.yaml",
         "source_field": "n_funded",
         "causal_label": "BY_CONSTRUCTION", "confidence": "HIGH"},
        {"id": "n_units", "claim_kind": "count",
         "units": "districts", "source_artifact": "models/model_outputs.yaml",
         "source_field": "n_units",
         "causal_label": "BY_CONSTRUCTION", "confidence": "HIGH"},
        {"id": "sens_verdict", "claim_kind": "label",
         "source_artifact": "models/sensitivity_analysis.yaml",
         "source_field": "verdict",
         "causal_label": "MODEL_ESTIMATE", "confidence": "MEDIUM"},
    ]})
    _, spec_errors = _builder().write(run_dir)
    if spec_errors:
        raise RuntimeError(f"positive-control claims spec invalid: {spec_errors}")
    _w(run_dir, "report.md", textwrap.dedent("""\
        # District vaccination campaign allocation

        ## Summary

        Funding [CLAIM:n_funded] of [CLAIM:n_units] districts uses
        [CLAIM:spend] of the [CLAIM:budget] envelope and averts
        [CLAIM:dalys_total] DALYs per year (95% uncertainty interval from
        parameter-uncertainty propagation).

        ## Methods

        District incidence was calibrated to observed case counts with
        multiple optimizer restarts and a held-out district. Campaign
        benefit and cost are both computed on eligible children.

        ## Sensitivity

        The recommendation is [CLAIM:sens_verdict] to perturbing its
        load-bearing parameters to their interval endpoints.

        ## Limitations

        Incidence depends on the reporting completeness of surveillance
        counts.
        """))
    _run_script("render_claims.py", run_dir)

    # --- critiques (all four; one resolved blocker each from round 2) -----
    for rev, prefix, fname in (
            ("critique-methods", "M-", "critique_methods.yaml"),
            ("critique-domain", "D-", "critique_domain.yaml"),
            ("critique-presentation", "P-", "critique_presentation.yaml"),
            ("critique-redteam", "R-", "critique_redteam.yaml")):
        _w(run_dir, fname, _critique(rev, prefix, round_n))
    _w(run_dir, "critique_sufficiency.yaml", {
        "claim_verdicts": [
            {"claim_id": cid, "verdict": "ADEQUATE",
             "quoted_phrase": cid, "reason": "Supported by cited artifact."}
            for cid in ("dalys_total", "spend", "n_funded", "sens_verdict")],
        "verdict": "ADEQUATE"})
    with open(os.path.join(run_dir, "metadata.json"), "w") as f:
        json.dump({"question": QUESTION}, f)
    return {"funded": sorted(funded), "spent": spent, "dalys": dalys,
            "sens_verdict": sens_verdict}


def set_round(run_dir: str, round_n: int) -> None:
    """Rewrite the critique YAMLs for a different current round."""
    for rev, prefix, fname in (
            ("critique-methods", "M-", "critique_methods.yaml"),
            ("critique-domain", "D-", "critique_domain.yaml"),
            ("critique-presentation", "P-", "critique_presentation.yaml"),
            ("critique-redteam", "R-", "critique_redteam.yaml")):
        _w(run_dir, fname, _critique(rev, prefix, round_n))


def _self_test() -> int:
    import tempfile
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as d:
        try:
            summary = build(d, round_n=2)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL: build raised {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        for rel in ("model_comparison_formal.yaml", "identifiability.yaml",
                    "uncertainty_report.yaml", "report.md",
                    "models/claims_ledger.yaml"):
            if not os.path.exists(os.path.join(d, rel)):
                failures.append(f"missing {rel}")
        with open(os.path.join(d, "report.md")) as f:
            if "[CLAIM:" in f.read():
                failures.append("report.md has unrendered claim refs")
        if not summary["funded"]:
            failures.append("no district funded")
    if failures:
        print("FAIL:", *failures, sep="\n  - ", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--self-test" in args:
        sys.exit(_self_test())
    if not args:
        print(__doc__)
        sys.exit(2)
    rnd = int(args[args.index("--round") + 1]) if "--round" in args else 2
    print(json.dumps(build(args[0], round_n=rnd), indent=2))
