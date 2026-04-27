---
name: ecological-fallacy-quantification
description: Phase 12 γ — required when a model calibrates to k aggregated units (zones, regions, archetypes) but allocates resources to n>>k disaggregated units (LGAs, districts, individual sites). Within-unit value heterogeneity is invisible to the optimizer; the impact loss must be bounded with `models/within_zone_heterogeneity.yaml`. The 104914 malaria run had 6 zones → 774 LGAs (ratio 0.0078) with within-zone PfPR varying 5.9%-77.3%; results.md acknowledged this in one line but didn't quantify the magnitude. Trigger phrases include "ecological fallacy", "within-zone", "aggregation", "zone vs LGA", "calibration units vs allocation units", "ratio < 0.1".
type: rigor-remediation
---

# Ecological-Fallacy Quantification

## What this skill is for

You're building a spatial allocation model where the **calibration
units** (zones, regions, archetypes) are coarser than the
**allocation units** (LGAs, districts, individual sites). The
optimizer sees only zone-mean values; LGAs above the mean are
under-served, below the mean are over-served. The impact loss is
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

In the 104914 run: 6 calibration zones / 774 LGAs = 0.0078. **Way
below 0.1.** Yours probably is too — the whole point of zonal
calibration is to keep n_targets manageable while allocating
geographically.

## The artifact schema

```yaml
calibration_units: 6                    # n_targets in model_comparison_formal.yaml
allocation_units: 774                   # rows in lga_allocation.csv
within_unit_value_range:
  min: 0.059                            # observed within-zone PfPR low
  max: 0.773                            # observed within-zone PfPR high
  metric: PfPR                          # or whatever the calibration target is
modeled_uniform_per_unit: true          # the optimizer assumes per-LGA = zone-mean
sensitivity:
  - perturbation: lga_pfpr_uniform_within_zone (baseline)
    cases_averted: 54700000             # the optimizer's headline number
  - perturbation: lga_pfpr_normal_within_zone (sd from observed data)
    cases_averted: 49000000
    impact_loss_pct: 10.4
verdict: BOUNDED                        # <10%, INCONCLUSIVE 10-25%, UNBOUNDED >25%
notes: |
  Per-LGA PfPR distributed Normal(zone_mean, sd_observed); optimizer
  re-run; cases-averted reduction is the impact loss bound.
```

Verdict thresholds:
- **BOUNDED** (worst impact loss < 10%): ACCEPT-defensible. Surface the bound in §Sensitivity.
- **INCONCLUSIVE** (10-25%): MEDIUM blocker. Publishable but must be flagged in §Sensitivity, not buried in §Limitations.
- **UNBOUNDED** (> 25%): HIGH blocker. The headline cases-averted is NOT defensible at the LGA level. See escalation paths below.

## How to perturb

Three options, in order of fidelity:

### Option A — Per-LGA Normal perturbation from observed data (recommended)

If you have within-zone sub-survey data (e.g., MAP raster at LGA
level, NDHS sub-zone clusters), use the observed sd:

```python
# In a sensitivity run script:
for lga in alloc_units:
    zone_mean = zone_pfpr[lga.zone]
    zone_sd = observed_sd_within_zone[lga.zone]
    lga_pfpr = np.random.normal(zone_mean, zone_sd)
    lga_pfpr = np.clip(lga_pfpr, 0, 1)
    lga.pfpr = lga_pfpr

# Re-run the optimizer at this perturbation
perturbed_cases = optimizer(allocation_units_with_perturbed_pfpr)
impact_loss = (baseline_cases - perturbed_cases) / baseline_cases * 100
```

This is the gold standard. The validator's verdict thresholds were
calibrated against this kind of perturbation.

### Option B — Worst-case within-zone bracketing

If you don't have sub-survey data, use the published min-max range
within each zone (e.g., HBHI archetypes show 5.9%-77.3% within-zone
spread). Set half the LGAs to the within-zone min and half to the
max:

```yaml
sensitivity:
  - perturbation: lga_min_max_split (worst case)
    cases_averted: <re-optimized output>
    impact_loss_pct: <bound>
```

This produces a conservative (high) impact loss estimate, so a
BOUNDED verdict here is genuinely BOUNDED. An UNBOUNDED verdict
might be over-conservative — note the methodology in `notes:` so
reviewers can interpret.

### Option C — Scope-declare with explicit homogeneity citation

If you have a published paper showing within-zone PfPR variation is
empirically small (sd < 5pp) for your specific geography, you can
scope-declare:

```yaml
calibration_units: 6
allocation_units: 774
modeled_uniform_per_unit: true
sensitivity:
  - perturbation: published_within_zone_homogeneity (Smith 2023)
    cases_averted: 54700000
    impact_loss_pct: 0.0  # citation supports near-uniform within zone
verdict: BOUNDED
notes: |
  Smith 2023 reports zone-internal PfPR variation < 3pp for Nigerian
  zones using 2018-2021 sub-survey clustering (DOI:...). Within-zone
  homogeneity is empirically supported.
```

The citation MUST be a real paper. The validator does not check the
citation — but the redteam and critique-domain agents will, and a
reviewer absolutely will.

## When verdict is UNBOUNDED — escalation paths

If your sensitivity computes UNBOUNDED (>25% impact loss), three
options:

1. **Refit at the lower aggregation level.** If you have LGA-level
   PfPR data (even if noisier than the zone-level survey), refit
   the model at LGA-level. n_targets goes from 6 to 774, but the
   optimizer no longer aggregates. Check: does the noisier
   calibration RMSE produce a LARGER impact loss than the
   ecological-fallacy bound? If yes, switch.
2. **Scope-declare the recommendation as zone-level only.** Write
   the report at the zone level: "NW gets X allocation, NE gets Y."
   Don't publish the LGA-level CSV. The decision_rule.md becomes
   "apply within zone using local guidance."
3. **Two-tier optimization.** Optimize at the zone level for budget
   shares, then have a downstream rule (e.g., "highest-burden LGA
   in zone gets PBO; lowest gets baseline ACT") for within-zone
   distribution. The within-zone rule is justified separately.

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
- [ ] `allocation_units` matches row count in lga_allocation.csv (or your equivalent)
- [ ] `within_unit_value_range` cites observed data, not a guess
- [ ] At least 1 perturbation has `impact_loss_pct` (the bound)
- [ ] `verdict` matches what the data computes (validator rejects mismatch as MALFORMED)
- [ ] Notes section explains the perturbation methodology clearly enough for a reviewer to replicate

## Related skills and artifacts

- `allocation-cross-validation` — Phase 6 κ; tests robustness under spatial holdout. Different from this skill (which tests within-spatial-unit heterogeneity). Both are required for an aggregation model.
- `sensitivity-analysis-remediation` — Phase 10 ω; for parameter-uncertainty sensitivity (vs spatial-aggregation sensitivity here).
- `scripts/within_zone_sensitivity.py` — gate validator with self-tests covering BOUNDED / INCONCLUSIVE / UNBOUNDED / MALFORMED / MISSING.
- `data_quality.md` — within-zone value range observations live here; the §Spatial heterogeneity subsection should give you the sd to use.
