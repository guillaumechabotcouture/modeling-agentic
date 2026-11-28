"""Allocation stability under leave-one-group-out (spike lesson 5: value lost beside package identity).

For each value of `analysis.stability.holdout_group`, the held-out units' calibration is
replaced by what the other units imply, the plan is re-solved, and the fold plan is judged
with the full calibration:
  emulator_inversion  the held-out units' calibrated attribute comes from a ridge regression on
                      `stability.covariates` fitted to the other units (the spike's method);
  likelihood          the calibration is refitted without the held-out units' targets, their
                      own attribute kept at the full fit (it is not identified without them);
  posterior           the same fold, at the fold posterior's mode (the estimator of the full fit's point).
Metrics: the share of the objective lost over all units (the headline, lesson 5) and in the
held-out units; rank correlation of the held-out units' spend; package concordance. The
identity verdict is scripts/allocation_robustness.compute_verdict; `value_verdict` applies the
value-lost thresholds in toolkit/analysis/thresholds.yaml. Each fold also reports what moves
(toolkit/analysis/moves.py: regret, population and dollars moved, units by group), and `components` counts,
per component, the held-out units where it is provided in both plans (stable provision) apart from those where
it is absent from both (stable absence).
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import yaml

from toolkit.analysis import calibration as C, mode_start as MS, moves as MV
from toolkit.analysis.core import SolveFailed, auxiliary, best_known, merge_costs

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PRODUCER = "toolkit/analysis/stability.py"
MIN_FOLD_UNITS = 5


def thresholds() -> dict:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return yaml.safe_load(f)["stability"]


VALID_HOLDOUT_METHODS = {
    "leave-one-archetype-out",
    "leave-one-state-out",
    "leave-one-zone-out",
    "5-fold-by-archetype",
    "10-fold-by-archetype",
    "spatial-block-cv",
    "leave-one-out",
    "k-fold",
}

ROBUST_RANK_CORR_MIN = 0.70
ROBUST_CASES_GAP_MAX = 15.0
ROBUST_RULE_CONCORDANCE_MIN = 80.0

FRAGILE_RANK_CORR_MIN = 0.40
FRAGILE_CASES_GAP_MAX = 30.0
FRAGILE_RULE_CONCORDANCE_MIN = 60.0


def compute_verdict(metrics: dict) -> str:
    """Recompute verdict from worst-fold metrics. Returns ROBUST, FRAGILE, or UNSTABLE."""
    rank_worst = metrics.get("rank_correlation_worst_fold")
    gap_worst = metrics.get("cases_averted_gap_pct_worst_fold")
    rule_worst = metrics.get("rule_classification_concordance_pct_worst_fold")

    if rank_worst is not None and rank_worst < FRAGILE_RANK_CORR_MIN:
        return "UNSTABLE"
    if gap_worst is not None and gap_worst > FRAGILE_CASES_GAP_MAX:
        return "UNSTABLE"
    if rule_worst is not None and rule_worst < FRAGILE_RULE_CONCORDANCE_MIN:
        return "UNSTABLE"

    rank_ok = rank_worst is None or rank_worst >= ROBUST_RANK_CORR_MIN
    gap_ok = gap_worst is None or gap_worst <= ROBUST_CASES_GAP_MAX
    rule_ok = rule_worst is None or rule_worst >= ROBUST_RULE_CONCORDANCE_MIN
    if rank_ok and gap_ok and rule_ok:
        return "ROBUST"

    return "FRAGILE"


def _spearman(a, b):
    from scipy.stats import spearmanr
    if np.std(a) == 0 or np.std(b) == 0:
        return 1.0 if list(a) == list(b) else float("nan")
    return float(spearmanr(a, b).correlation)


def _mean(v):
    v = [x for x in v if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return float(np.mean(v)) if v else None


def run(A, z0, out0, ben0, cost0, plan) -> dict:
    st = A.spec["analysis"].get("stability") or {}
    g = st.get("holdout_group")
    if not g:
        return {"method": "skipped", "reason": "no analysis.stability.holdout_group in the spec", "folds": [],
                "metrics": {}, "verdict": "NOT_RUN", "value_verdict": "NOT_RUN"}
    groups = sorted({A.by_id[i][g] for i in A.ids}, key=str)
    if A.method == "emulator_inversion":
        cov = st.get("covariates") or []
        if not cov:
            raise ValueError("analysis.stability.covariates: emulator inversion needs covariates to predict held-out units")
        Xall = np.array([[float(A.by_id[i][c]) for c in cov] for i in A.ids])
        zlo, zhi = A.spec["calibration"]["range"]
        y = np.array([z0[i] for i in A.ids])
    folds, pairs = [], []
    for gv in groups:
        m = np.array([A.by_id[i][g] == gv for i in A.ids])
        if m.all():
            continue
        hid = [i for i, k in zip(A.ids, m) if k]
        theta, nclamp = A.theta0, 0
        if A.method == "emulator_inversion":
            # units flagged by the joint calibration (calibration.joint.unmatched: flag) carry no calibrated value:
            # they never inform the prediction of a held-out unit, nor the covariate scaling (Codex r6 P1-3)
            fl = np.array([i in (getattr(A, "flagged", None) or {}) for i in A.ids])
            tr = ~m & ~fl
            lam = 1e-3 * np.eye(Xall.shape[1] + 1)
            if tr.sum() < Xall.shape[1] + 1:   # Codex r5 P2-7: no regression without enough unflagged training units
                folds.append({"group": gv, "status": "not_run", "n_units": len(hid),
                              "reason": "too few unflagged units outside the fold to predict the held-out values"})
                continue
            mu, sdv = Xall[tr].mean(0), Xall[tr].std(0) + 1e-12
            Xs = np.column_stack([np.ones(len(A.ids)), (Xall - mu) / sdv])
            beta = np.linalg.solve(Xs[tr].T @ Xs[tr] + lam, Xs[tr].T @ y[tr])
            zh = dict(z0)
            for i, k in zip(A.ids, m):
                if k:      # 1C-b 2c-i: kept inside calibration.range (the emulator's design box) and counted
                    raw = float(Xs[A.ids.index(i)] @ beta)
                    zh[i] = min(max(raw, float(zlo)), float(zhi))
                    nclamp += zh[i] != raw
            oh, bh, ch = A.evaluate(theta, zh, hid)
            ben_f = dict(ben0)
            ben_f.update(bh)
            cost_f = merge_costs(cost0, ch)
        elif A.method in ("likelihood", "posterior"):
            rows = [r for r in C.read_targets(A.spec) if r["unit"] not in set(hid)]
            try:
                fit = (C.fit_likelihood(A.spec, A.model, rows=rows, fixed_z={i: z0[i] for i in hid},
                                        start={**{p: A.lik["x"][p] for p in A.lik["params"]},
                                               **{f"z:{u}": A.lik["z"][u] for u in A.lik["z"] if u not in hid}},
                                        identifiability=False) if A.method == "likelihood" else
                       MS.fit_posterior_panel(A.spec, A.model, rows=rows, fixed_z={i: z0[i] for i in hid},
                                              sample_it=False))
            except C.CalibrationRefused as e:     # 1C-b 2c-ii: one refused refit is that fold's result, not a crash
                folds.append({"group": gv, "status": "not_run", "n_units": len(hid),
                              "reason": f"the calibration without the held-out units was refused ({e})"})
                continue
            theta = dict(A.theta0, **fit["theta"])
            zh = dict(fit["z"], **{i: z0[i] for i in hid})
            _, ben_f, cost_f = A.evaluate(theta, zh)
        else:
            return {"method": "skipped", "reason": "calibration.method none: nothing is calibrated, nothing to hold out",
                    "folds": [], "metrics": {}, "verdict": "NOT_RUN", "value_verdict": "NOT_RUN"}
        try:
            with auxiliary(A, "stability_folds"):
                sol = A.solve(ben_f, cost_f)
        except SolveFailed as e:      # 1C-c: the fold has no plan to compare; the other folds run
            if e.status == "infeasible":      # a proven infeasibility stops the step, as before (Codex r1 P1-2)
                raise
            folds.append({"group": gv, "status": "not_run", "n_units": len(hid),
                          "reason": f"the fold's plan was not solved ({e})"})
            continue
        sol = best_known(A, sol, plan, ben_f, cost_f)      # an unproven incumbent never loses to the point plan
        A.guard(f"stability fold {gv}", [plan, sol["choice"]])
        pf = sol["choice"]
        c_full = [cost0[i][plan[i]] for i in hid]
        c_fold = [cost0[i][pf[i]] for i in hid]
        rho = _spearman(c_full, c_fold) if len(hid) >= 3 else None
        tot_full = sum(ben0[i][plan[i]] for i in A.ids)
        tot_fold = sum(ben0[i][pf[i]] for i in A.ids)
        b_full = sum(ben0[i][plan[i]] for i in hid)
        b_fold = sum(ben0[i][pf[i]] for i in hid)
        pairs += [(plan[i], pf[i]) for i in hid]
        # F6 S1-5: what moves under the fold (regret: the fold's re-optimised objective minus the point plan's there)
        folds.append({**MV.summarise(A, plan, pf, ben_f, cost_f, sol["objective"], gap=sol.get("solver_gap")),
                      "group": gv, "n_units": len(hid), "rank_corr": rho, "predictions_clamped": int(nclamp),
                      "package_concordance_pct": 100.0 * np.mean([plan[i] == pf[i] for i in hid]),
                      "benefit_gap_pct": 100.0 * (b_full - b_fold) / b_full if b_full > 0 else 0.0,
                      "total_benefit_loss_pct": 100.0 * (tot_full - tot_fold) / tot_full if tot_full > 0 else 0.0,
                      "z_shift_mean": (float(np.mean([zh[i] - z0[i] for i in hid]))
                                       if all(zh[i] is not None and z0[i] is not None for i in hid) else None)})
    skipped = [f for f in folds if f.get("status") == "not_run"]
    folds = [f for f in folds if f.get("status") != "not_run"]        # Codex r6 P2: metrics over run folds only
    big = [f for f in folds if f["n_units"] >= MIN_FOLD_UNITS and f["rank_corr"] is not None
           and not math.isnan(f["rank_corr"])]
    met = {"total_benefit_loss_pct_mean": _mean([f["total_benefit_loss_pct"] for f in folds]),
           "total_benefit_loss_pct_worst_fold": max((f["total_benefit_loss_pct"] for f in folds), default=None),
           "rank_correlation_mean": _mean([f["rank_corr"] for f in big]),
           "rank_correlation_worst_fold": min((f["rank_corr"] for f in big), default=None),
           "folds_with_rank_corr": len(big),
           "package_concordance_pct_mean": _mean([f["package_concordance_pct"] for f in folds]),
           "package_concordance_pct_worst_fold": min((f["package_concordance_pct"] for f in folds), default=None),
           "package_concordance_pct_worst_fold_ge5": min((f["package_concordance_pct"] for f in big), default=None),
           "benefit_gap_pct_mean": _mean([f["benefit_gap_pct"] for f in folds]),
           "benefit_gap_pct_worst_fold": max((f["benefit_gap_pct"] for f in folds), default=None),
           "regret_pct_worst_fold": max((100.0 * f["regret_share"] for f in folds if f["regret_share"] is not None),
                                        default=None),
           "dollars_moved_worst_fold": max((f["dollars_moved"] for f in folds), default=None),
           "population_changed_worst_fold": max((f["population_changed"] for f in folds), default=None)}
    verdict = compute_verdict({"rank_correlation_worst_fold": met["rank_correlation_worst_fold"],
                               "cases_averted_gap_pct_worst_fold": met["benefit_gap_pct_worst_fold"],
                               "rule_classification_concordance_pct_worst_fold":
                                   met["package_concordance_pct_worst_fold"]})
    if not folds:       # Codex r7 P2: no fold ran; no verdict from all-None metrics
        verdict = "NOT_RUN"
    th = thresholds()
    w = met["total_benefit_loss_pct_worst_fold"]
    vv = ("NOT_RUN" if w is None else "ROBUST" if w <= th["value_loss_pct_robust"] else
          "FRAGILE" if w <= th["value_loss_pct_fragile"] else "UNSTABLE")
    return {"method": f"leave-one-{g}-out", "holdout_group": g, "calibration_method": A.method,
            "n_folds": len(folds), "folds": folds, "folds_not_run": skipped, "metrics": met, "verdict": verdict,
            "value_verdict": vv,
            # 1C-b 2c-i: held-out predictions moved to the nearer end of calibration.range, over the folds that ran
            "predictions_clamped": sum(f["predictions_clamped"] for f in folds),
            # each held-out unit's package in its own fold against the point plan, per component (F6 S1-5)
            "components": MV.component_stability(A.pk, pairs, list(A.spec["options"]["components"])) if pairs else None,
            "value_thresholds_pct": th, "headline_metric": "total_benefit_loss_pct_worst_fold"}


def robustness_yaml(stab: dict) -> dict:
    """models/allocation_robustness.yaml in the schema scripts/allocation_robustness.py validates."""
    m = stab["metrics"]
    meth = stab["method"]
    return {"holdout_method": meth if meth in VALID_HOLDOUT_METHODS else "leave-one-out",
            "n_folds": stab.get("n_folds", 0),
            "metrics": {"rank_correlation_mean": m.get("rank_correlation_mean"),
                        "rank_correlation_worst_fold": m.get("rank_correlation_worst_fold"),
                        "cases_averted_gap_pct_mean": m.get("benefit_gap_pct_mean"),
                        "cases_averted_gap_pct_worst_fold": m.get("benefit_gap_pct_worst_fold"),
                        "rule_classification_concordance_pct_mean": m.get("package_concordance_pct_mean"),
                        "rule_classification_concordance_pct_worst_fold": m.get("package_concordance_pct_worst_fold"),
                        "total_objective_lost_pct_worst_fold": m.get("total_benefit_loss_pct_worst_fold")},
            "verdict": stab["verdict"], "value_verdict": stab["value_verdict"], "produced_by": "harness",
            "notes": f"Written by {PRODUCER} ({meth}); value lost = fold plan judged with the full calibration."}
