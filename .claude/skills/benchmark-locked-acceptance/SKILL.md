---
name: benchmark-locked-acceptance
description: Translate published benchmarks into structured acceptance bands the validator can enforce. Use when the planner has written a "Published benchmarks" table in plan.md OR when the modeler needs to check fitted outputs against literature ranges before declaring calibration complete.
---

# Benchmark-locked acceptance (Phase 19 γ)

## What this is for

Most planner outputs include a "Published benchmarks" prose table —
WMR-2023 incidence per 1,000 PYO, DHS ITN-use percentages, NMIS PfPR
ranges, etc. Without a mechanical gate, those numbers stay prose and
the modeler's fitted outputs are never compared against them. A model
can ship with a calibrated incidence 4× the literature range and the
report will still ACCEPT because nothing computed the comparison.

This skill turns the prose table into a structured YAML artifact
(`models/benchmark_targets.yaml`) that `scripts/benchmark_match.py`
diffs against the model's computed outputs and writes
`benchmark_match.yaml`. The validator (`_check_benchmark_match`)
escalates DRIFT to HIGH after the model has had a round to settle.

## When to invoke

- **Planner**, round 1: extract the published-benchmarks table from
  plan.md into `models/benchmark_targets.yaml` with `target_value`,
  `tolerance_factor` (or `tolerance_abs`), and `source` for each row.
  Leave `computed_value` and `computed_field` blank — the modeler
  fills them.
- **Modeler**, round 1+: after each model run, set `computed_value`
  on each benchmark target (or set `computed_field` to point at
  another YAML the run produced).
- **Lead**, round 2+: run `python3 scripts/benchmark_match.py {run_dir}`
  after the modeler completes. Reads `benchmark_match.yaml` via
  `_check_benchmark_match` in the validator.

## Schema

```yaml
# models/benchmark_targets.yaml
benchmarks:
  - id: incidence_under5_baseline
    metric: cases_per_1000_pyo_under5
    target_value: 412
    units: per_1000_pyo
    tolerance_factor: 2.0           # acceptance band [target/factor, target*factor]
    # OR an absolute tolerance:
    # tolerance_abs: 80
    source: "WMR 2023, Table A.5, Nigeria row"

    # Modeler fills ONE of these after the model run:
    computed_value: 387             # inline numeric
    # OR
    computed_field: "uncertainty_report.yaml::scalar_outputs.incidence.mean"

  - id: itn_use_under5_pct
    metric: itn_use_under5
    target_value: 56
    units: pct
    tolerance_abs: 8                # band [48, 64]
    source: "DHS 2018 Nigeria, Table 11.4"
    computed_value: 51
```

## Tolerance defaults

- For **stocks** (prevalence, mortality, incidence): `tolerance_factor: 2.0`
  is the looseness most epi calibrations need; if the literature
  itself disagrees by 3×, raise to 3.0 and document why.
- For **percentages** (coverage, use, access): `tolerance_abs` is
  often clearer than factor. ±5pp for high-coverage targets, ±10pp
  for low.
- For **rates that should be near zero** (e.g., resistance prevalence
  pre-2020): use `tolerance_abs` only — factor bands collapse around
  zero.

## When to scope-declare

- Question implies a novel population where no direct benchmark exists
  (e.g., a hypothetical intervention package, a country with sparse
  data). Document `target_value: null` with a citation to the closest
  proxy and a `scope_declared_reason` field.
- The literature numbers themselves are known stale or contested
  (e.g., DHS pre-2015 in regions with rapid transmission change).
  Use `tolerance_factor: 3.0` and note the staleness.

## What HIGH violations look like

`scripts/benchmark_match.py` emits DRIFT when at least one benchmark's
observed value is outside its band. The validator escalates DRIFT to
HIGH at round ≥ 2; round 1 is the drafting window (MEDIUM advisory).
`missing_computed` (modeler never filled `computed_value`) is HIGH
from round 2 onward — it's the most common failure mode.

## Why this exists

Phases 2-18 hardened the harness against paraphrase drift, structural
punt, and cross-file inconsistency. None of those gates check whether
the *model's numbers* match published reality. A modeler can ship a
fitted PfPR of 0.5% when WMR says 24% and the report will still pass
identifiability_a_priori (the model is identifiable), pass
multi-structural-comparison (AIC says simpler model wins), pass UQ
(CIs are wide), pass coherence (numbers are self-consistent). The
only thing missing is the comparison against published reality.
Phase 19 γ closes that gap mechanically.
