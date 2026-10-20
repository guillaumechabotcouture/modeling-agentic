---
name: ecological-fallacy-quantification
description: >-
  Required when a model calibrates to k aggregated units (zones, regions,
  archetypes) but allocates to n >> k finer units (districts, counties,
  facilities). Within-unit heterogeneity is invisible to the optimizer, so the
  impact loss must be bounded in `models/within_zone_heterogeneity.yaml`
  rather than acknowledged in one line. Trigger phrases include "ecological
  fallacy", "within-zone", "aggregation", "calibration units vs allocation
  units", "ratio < 0.1".
type: rigor-remediation
---

# Ecological-Fallacy Quantification

## What this skill is for

You're building a spatial allocation model where the **calibration
units** (zones, regions, archetypes) are coarser than the
**allocation units** (districts, counties, facilities, individual
sites). The optimizer sees only zone-mean values; units above the mean
are under-served, units below the mean are over-served. The impact loss is
unbounded unless you quantify it.

Phase 12 γ added a required rigor artifact:
`models/within_zone_heterogeneity.yaml`. The validator
(`scripts/within_zone_sensitivity.py`) computes a verdict
(BOUNDED < 10%, INCONCLUSIVE 10-25%, UNBOUNDED > 25%) from your
perturbation outcomes.

## When the gate fires

`_check_within_zone_heterogeneity` in
`scripts/validate_critique_yaml.py` triggers when:

```
calibration_units / allocation_units < 0.1
```

For example, 6 calibration zones / 774 districts = 0.0078. **Way
below 0.1.** Yours probably is too — the whole point of zonal
calibration is to keep n_targets manageable while allocating
geographically.

## The artifact schema

```yaml
calibration_units: 8                    # n_targets in model_comparison_formal.yaml
allocation_units: 312                   # rows in the allocation CSV
within_unit_value_range:
  min: 0.42                             # observed within-zone low
  max: 0.97                             # observed within-zone high
  metric: mcv1_coverage                 # whatever the calibration target is
                                        # (prevalence, incidence rate, coverage, ...)
modeled_uniform_per_unit: true          # the optimizer assumes per-unit = zone-mean
sensitivity:
  - perturbation: unit_value_uniform_within_zone (baseline)
    outcome_averted: 18400              # the optimizer's headline number
  - perturbation: unit_value_normal_within_zone (sd from observed data)
    outcome_averted: 16500
    impact_loss_pct: 10.3
verdict: INCONCLUSIVE                   # BOUNDED <10%, INCONCLUSIVE 10-25%, UNBOUNDED >25%
notes: |
  Per-district coverage distributed Normal(zone_mean, sd_observed);
  optimizer re-run; reduction in outcomes averted is the impact loss
  bound.
```

Verdict thresholds:
- **BOUNDED** (worst impact loss < 10%): ACCEPT-defensible. Surface the bound in §Sensitivity.
- **INCONCLUSIVE** (10-25%): MEDIUM blocker. Publishable but must be flagged in §Sensitivity, not buried in §Limitations.
- **UNBOUNDED** (> 25%): HIGH blocker. The headline outcomes-averted figure is NOT defensible at the allocation-unit level. See escalation paths below.

## How to perturb

Three options, in order of fidelity:

### Option A — Per-unit Normal perturbation from observed data (recommended)

If you have within-zone sub-survey data (e.g., a modeled raster at
district level, DHS/MICS cluster data, routine HMIS data by facility),
use the observed sd:

```python
# In a sensitivity run script:
for unit in alloc_units:
    zone_mean = zone_value[unit.zone]
    zone_sd = observed_sd_within_zone[unit.zone]
    value = np.random.normal(zone_mean, zone_sd)
    unit.value = np.clip(value, lower_bound, upper_bound)  # e.g. 0-1 for a proportion

# Re-run the optimizer at this perturbation
perturbed = optimizer(alloc_units)
impact_loss = (baseline_outcome - perturbed) / baseline_outcome * 100
```

This is the gold standard. The validator's verdict thresholds were
calibrated against this kind of perturbation.

### Option B — Worst-case within-zone bracketing

If you don't have sub-survey data, use the published min-max range
within each zone (e.g., a published subnational stratification).
Set half the units to the within-zone min and half to the max:

```yaml
sensitivity:
  - perturbation: unit_min_max_split (worst case)
    outcome_averted: <re-optimized output>
    impact_loss_pct: <bound>
```

This produces a conservative (high) impact loss estimate, so a
BOUNDED verdict here is genuinely BOUNDED. An UNBOUNDED verdict
might be over-conservative — note the methodology in `notes:` so
reviewers can interpret.

### Option C — Scope-declare with explicit homogeneity citation

If you have a published paper showing within-zone variation of the
calibration metric is empirically small (e.g., sd < 5 percentage
points) for your specific geography, you can scope-declare:

```yaml
calibration_units: 8
allocation_units: 312
modeled_uniform_per_unit: true
sensitivity:
  - perturbation: published_within_zone_homogeneity ([Author YYYY])
    outcome_averted: 18400
    impact_loss_pct: 0.0  # citation supports near-uniform within zone
verdict: BOUNDED
notes: |
  [Author YYYY] reports within-zone variation < 3pp for these zones
  using recent sub-survey clustering (DOI:...). Within-zone
  homogeneity is empirically supported.
```

The citation MUST be a real paper. The validator does not check the
citation — but the redteam and critique-domain agents will, and a
reviewer absolutely will.

## When verdict is UNBOUNDED — escalation paths

If your sensitivity computes UNBOUNDED (>25% impact loss), three
options:

1. **Refit at the lower aggregation level.** If you have unit-level
   data (even if noisier than the zone-level survey), refit the model
   at the unit level. n_targets grows from k to n, but the optimizer
   no longer aggregates. Check: does the noisier
   calibration RMSE produce a LARGER impact loss than the
   ecological-fallacy bound? If yes, switch.
2. **Scope-declare the recommendation as zone-level only.** Write
   the report at the zone level: "Zone A gets X allocation, zone B
   gets Y." Don't publish the unit-level CSV. The decision_rule.md becomes
   "apply within zone using local guidance."
3. **Two-tier optimization.** Optimize at the zone level for budget
   shares, then have a downstream rule (e.g., "highest-burden
   district in a zone gets the enhanced package; the rest get the
   standard package") for within-zone distribution. The within-zone rule is justified separately.

## Worked example — 104914

The 104914 malaria run had:
- 6 zones, 774 LGAs (ratio 0.0078)
- Within-zone PfPR range 5.9% to 77.3% (HBHI archetype data)
- Optimizer assumed per-LGA PfPR = zone mean
- results.md line 223 acknowledged the issue in one sentence
- No sensitivity perturbation; no impact bound

The Phase 12 γ retro fires:
```
within_zone_heterogeneity_missing  MEDIUM
  Model calibrates to 6 units but allocates to 774 units
  (ratio 0.0078 < 0.1). Required artifact:
  models/within_zone_heterogeneity.yaml
```

Hypothesis: Option A perturbation with observed within-zone sd
~10pp would produce ~10-15% impact loss → INCONCLUSIVE verdict →
MEDIUM blocker → publishable with explicit §Sensitivity statement.

## Checklist before declaring verdict

- [ ] `calibration_units` matches `n_targets` in model_comparison_formal.yaml
- [ ] `allocation_units` matches row count in the allocation CSV
- [ ] `within_unit_value_range` cites observed data, not a guess
- [ ] At least 1 perturbation has `impact_loss_pct` (the bound)
- [ ] `verdict` matches what the data computes (validator rejects mismatch as MALFORMED)
- [ ] Notes section explains the perturbation methodology clearly enough for a reviewer to replicate

## Related skills and artifacts

- `allocation-cross-validation` — Phase 6 κ; tests robustness under spatial holdout. Different from this skill (which tests within-spatial-unit heterogeneity). Both are required for an aggregation model.
- `sensitivity-analysis-remediation` — Phase 10 ω; for parameter-uncertainty sensitivity (vs spatial-aggregation sensitivity here).
- `scripts/within_zone_sensitivity.py` — gate validator with self-tests covering BOUNDED / INCONCLUSIVE / UNBOUNDED / MALFORMED / MISSING.
- `data_quality.md` — within-zone value range observations live here; the §Spatial heterogeneity subsection should give you the sd to use.
