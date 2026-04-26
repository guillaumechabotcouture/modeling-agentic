---
name: allocation-cross-validation
description: Phase 6 Commit κ requirement. When a model produces an
  allocation, the allocation rule itself must be cross-validated
  under spatial holdout — not just the underlying calibration. A
  k-fold leave-one-archetype-out test answers "would my optimizer's
  recommended package for archetype A change if I had calibrated on
  the other 21 archetypes only?" Without this test, the optimization
  may have over-fit to specific in-sample EIRs. Trigger phrases
  include "allocation cross-validation", "leave-one-archetype-out",
  "allocation generalizes", "allocation robustness", "spatial holdout".
---

# Allocation Cross-Validation

## Why allocation CV is different from calibration CV

Calibration cross-validation (LOO via `compare_models.py`) answers:
"How well does my MODEL predict held-out PfPR / cases?" Allocation
cross-validation answers a different question: "How well does my
OPTIMIZER's recommended allocation rule generalize to held-out
spatial units?" These are not the same.

A model can have great calibration generalization (low LOO RMSE on
PfPR) but produce a brittle allocation rule that flips package
choices when individual archetypes are removed. This happens when:
- The optimizer is sensitive to small EIR differences near package-
  switching thresholds
- Greedy ordering depends on the exact set of available units
- Budget cliffs interact with archetype heterogeneity

A senior modeler always tests both. The Phase 6 Commit κ gate
enforces the allocation CV.

## The required artifact: `models/allocation_robustness.yaml`

```yaml
holdout_method: leave-one-archetype-out  # or leave-one-state-out, k-fold, etc.
n_folds: 22                              # or whatever the K is

metrics:
  # Rank correlation between full-sample LGA-level allocation budget
  # and the held-out fold's predicted allocation. Spearman or Pearson.
  rank_correlation_mean: 0.87
  rank_correlation_worst_fold: 0.78

  # Cases-averted at the held-out budget under the allocation predicted
  # from the n-k fold, vs. the optimal allocation for the held-out
  # archetype. Percentage gap, mean and worst-fold.
  cases_averted_gap_pct_mean: 4.3
  cases_averted_gap_pct_worst_fold: 8.5

  # Decision-rule classification concordance: does the n-k decision
  # tree assign the same package to the held-out unit as the optimal
  # full-sample tree? Percentage, mean and worst-fold.
  rule_classification_concordance_pct_mean: 92.1
  rule_classification_concordance_pct_worst_fold: 88

verdict: ROBUST | FRAGILE | UNSTABLE  # modeler's call; recomputed by validator

notes: |
  Per-fold details, edge cases, holdout-strategy rationale.
```

## Verdict thresholds (worst-fold metrics)

| Verdict | Rank corr (worst) | Cases gap (worst) | Rule concordance (worst) |
|---|---|---|---|
| **ROBUST** | ≥ 0.70 | ≤ 15% | ≥ 80% |
| **FRAGILE** | 0.40 – 0.70 | 15% – 30% | 60% – 80% |
| **UNSTABLE** | < 0.40 | > 30% | < 60% |

Any single metric in the UNSTABLE band sends the verdict to UNSTABLE.
ROBUST requires ALL applicable metrics in the ROBUST band.

## How to run the holdout

### Pseudocode

```python
def allocation_cross_validate(model, units, n_folds=None, k_holdout=1):
    """k_holdout per fold; n_folds per design.
    For leave-one-archetype-out: k_holdout=1, n_folds=22."""
    metrics = {
        "rank_corr": [], "cases_gap_pct": [], "rule_concordance_pct": []
    }
    for fold_units in iter_holdout_folds(units, n_folds, k_holdout):
        train_units = [u for u in units if u not in fold_units]

        # Re-calibrate on train (or hold calibrated EIRs fixed if the
        # design is "test allocation overfit to in-sample EIRs").
        train_calib = model.calibrate(train_units)

        # Run optimizer on train + held-out budget allocation.
        train_allocation = optimize(train_calib, train_units, budget)
        full_allocation = optimize(model.full_calib, units, budget)

        # Apply train's decision rule to fold_units.
        for u in fold_units:
            train_pkg = decision_rule(train_calib).predict(u)
            full_pkg = decision_rule(model.full_calib).predict(u)
            metrics["rule_concordance_pct"].append(
                100 * int(train_pkg == full_pkg))

        # Rank correlation of LGA-level budget under the two allocations.
        train_budget_per_lga = aggregate_budget(train_allocation, fold_units)
        full_budget_per_lga = aggregate_budget(full_allocation, fold_units)
        metrics["rank_corr"].append(
            spearmanr(train_budget_per_lga, full_budget_per_lga))

        # Cases averted gap: train's allocation applied to held-out
        # vs full-sample's allocation for held-out.
        cases_train = simulate_cases(train_allocation, fold_units)
        cases_full = simulate_cases(full_allocation, fold_units)
        gap = 100 * (cases_full - cases_train) / cases_full
        metrics["cases_gap_pct"].append(gap)

    return summarize_metrics(metrics)  # mean + worst-fold
```

### Choosing the holdout strategy

| Strategy | When to use | Folds |
|---|---|---|
| `leave-one-archetype-out` | Few large archetypes (≤30) | n_archetypes |
| `leave-one-state-out` | Want to test geographic generalization | n_states |
| `leave-one-zone-out` | Coarse spatial test (5-7 zones) | n_zones (small) |
| `k-fold-by-archetype` | Many archetypes (>50) — k=5 or 10 | k |
| `spatial-block-cv` | Test against contiguous blocks (geographic block CV) | k blocks |

For Nigeria 22-archetype malaria: `leave-one-archetype-out` with
n_folds=22 is the natural choice. Each fold trains on 21
archetypes, predicts the 22nd, and measures the rule's package
choice for that 22nd archetype.

## Worked example: 0912's expected behavior

For the 22-archetype Nigeria GC7 setup:
- Calibration uses 8 partial-pooling regression coefficients
- Decision rule has 3 features (SMC eligibility, PfPR, resistance)
- Optimization is greedy marginal CE

Leave-one-archetype-out CV would likely show:
- Rule classification concordance: ~92% (the 3-feature tree is robust;
  most archetypes' classification is determined by their coarse
  features, not specific calibration)
- Worst fold: probably an archetype near the SMC-eligibility threshold
  (seasonality_index ≈ 0.5) where small recalibrations flip the
  classification → concordance might drop to 80-85%
- Verdict: ROBUST or borderline-FRAGILE

A FRAGILE or UNSTABLE verdict would suggest: rebuild the model with
regularization (e.g., shrink rare-archetype EIRs toward zone mean),
or scope-declare the recommendation as applicable only to the
in-sample 22 archetypes (not extrapolatable to additional archetypes).

## Common pitfalls

1. **Confusing calibration CV with allocation CV.** `compare_models.py`
   does the former; this is the latter. Both are needed.

2. **Holding the calibration fixed across folds.** Some designs hold
   calibrated EIRs fixed and only re-run the optimizer on n-k units.
   That tests OPTIMIZER overfit but not CALIBRATION overfit. State
   explicitly which design you used in the YAML notes.

3. **Too few folds.** k=2 or k=3 is too small for stable estimates;
   prefer k=5 or leave-one-out for small sample sizes.

4. **Reporting only mean metrics.** WORST-fold metrics matter more —
   if 21 of 22 archetypes generalize but archetype #22 has 50%
   concordance, the rule has a sharp boundary somewhere.

5. **Wrong holdout level.** If your decision rule classifies LGAs but
   your CV holds out states, you're testing rule generalization to
   states (intermediate aggregation) — be explicit in the YAML.
