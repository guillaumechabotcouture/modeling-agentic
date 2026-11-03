# v2: the figure set the analysis harness renders (design note)

**Status:** written before the code (2026-11-02), in branch `v2-figures`. It replaces §2 "figures" of
`v2-spec-and-analysis.md`. The steps after it: α (tests on a synthetic toy run dir, failing first), β (the
code). The malaria engine's side of §5 (engine v0.8: `schedule()` and `run_series()`, with its re-stamp proof)
is a separate PR built against the interface in §5.

## 1. Evidence (full v2 runs F2 and F3, 2026-11-01/02)

The harness wrote three figures per run. The owner, an expert modeller, judged them not fit for the report.

| Figure | Problem |
|---|---|
| `headline_interval.png` | A whole figure for one interval. Axis in `1e7` offset notation with the outcome's code as the unit. A ~30-word interval label as the title, overflowing the image. A red "point estimate" line next to an unlabelled blue dot (the median). |
| `plan_packages.png` | Bars of internal package codes. The decision is *where* each package goes; the figure has no place, no cost and no confidence. |
| `sensitivity_tornado.png` | Parameter code names with no tested range or baseline. `1e6` offset notation. Red and green meant "low end" and "high end", read as bad and good, and swapped sides between rows. Nothing says whether the plan changes. |

The owner then asked for the decision in dollars and outcomes (a budget frontier, dollars by component and by
region, cost per outcome averted by package), maps (allocation, stability, burden), a delivery timeline and
modelled trajectories.

## 2. Standards for every figure

- **Labels.** A component, parameter or outcome shows its display name from the spec's optional
  `presentation.labels` block (§3) when present. Otherwise a readable fallback: the id, and a legend line that
  defines it from the spec and the engine card (e.g. a component's engine mapping; a parameter's units, range
  and source). Never only a code.
- **Units.** Real units and no offset notation. One tick formatter (`figure_style.fmt`) for every axis:
  `12.3M`, `$1.2M`, `45%`, `1,234`. `axes.formatter.useoffset` is off.
- **Words.** A short title that states the takeaway (with the number where there is one). The caption, at most
  two lines, lives in `analysis/figures.yaml`, not in the image.
- **Colour.** The dataviz skill's reference palette, validated with its `validate_palette.js` in light and dark
  mode. Categorical colours in a fixed order (never cycled, at most 8; past 3 on a scatter the figure uses
  small multiples or a secondary encoding). Sequential: one blue ramp. Low and high ends of a sensitivity range:
  two fixed categorical slots, never status colours. Text in ink colours, never series colours.
- **Files.** PNG at 180 dpi, plus SVG. The provenance sidecar `<png>.provenance.json` is kept per figure
  (`scripts/figure_validator.py`, sources relative to the run dir).
- **Fail closed.** A figure whose inputs are missing gets a `not_produced: [{figure, reason}]` entry in
  `analysis/figures.yaml`, never a silent skip.

## 3. Inputs

Every figure is drawn from saved artifacts only, so the harness and the offline re-render share one code path.

| Source | Read for |
|---|---|
| `decision_spec.yaml`, units table | unit ids, regions, populations, coordinates (`decision_units.coordinates: {lat, lon}`, or columns named `lat`/`lon`), envelope and fixed costs, component and package structure, labels |
| `analysis/point.yaml` | plan, package counts, headline point values, cost allocated |
| `analysis/uncertainty.yaml`, `uncertainty_report.yaml` | headline interval; per unit, the share of draws in which the re-optimised plan keeps the unit's package (`categorical_outputs.package:<unit>.counts`) |
| `analysis/sensitivity.yaml`, `analysis/engine.yaml`, `analysis/decision_tests.yaml` | rows, tested values, parameter units; a row changes the plan when its prior-range test says `CHANGES_PLAN`, else when either end re-optimises more than the decision-test share threshold of units to another package |
| `analysis/calibration.yaml`, `analysis/emulator.yaml` | targets, fitted residuals, target uncertainty, the emulator's held-out calibration error |
| `analysis/figure_data.yaml` (new, §4) | frontier, dollars by component and region, cost per outcome by package with its interval, burden per unit, schedule |
| `analysis/trajectories.yaml` (new, §5) | modelled monthly series of a stratified unit sample |

New optional spec block (validated: strings only; unknown ids refused):

```yaml
presentation:
  labels:
    components: {<component id>: "display name"}
    parameters: {<engine parameter>: "display name"}
    outcomes:   {<outcome>: "display name"}
    regions_title: "display name of the region level"
    units_title: "display name of the unit level"
```

## 4. `analysis/figure_data.yaml` (new artifact kind `figure_data`)

Written by `toolkit/analysis/figure_data.py` from the `Analysis` object (the emulator and the toolkit/alloc
solver the plan used; no engine runs, no new optimiser):

- **frontier**: the plan re-solved (`Analysis.solve`) at envelopes of 25, 50, 75, 90, 100, 110, 125 and 150% of
  the declared envelope (fixed costs held), each with its objective, outcomes averted, cost, and package counts.
  The marginal cost per outcome averted at the envelope is the central difference over 90-110%.
- **dollars**: the plan's cost split by component (cost items map to components in the spec) and by region,
  with the fixed costs and the envelope.
- **by_package**: for each package in the plan, units, cost, outcome averted and cost per outcome averted at the
  point, and the interval over the uncertainty draws. The draws are replayed with `uncertainty.Drawer` and the
  spec's seed, so they are the draws of the uncertainty step; the replay checks itself by reproducing the stored
  fixed-plan interval, and fails closed when it does not.
- **component_increment**: per component, the outcome averted and cost added by the component in the units
  that receive it (each unit's package against the same package without the component, where that package
  exists).
- **burden**: per unit, the comparator's primary outcome per person.
- **schedule**: per region, the delivery events of the hook in §5 (absent when the engine does not offer it).

Offline (`render --recompute`), the emulator is refitted from the run's stored batch rows. That needs the
engine identity the rows were computed with; with another engine version the recompute fails closed and the
figures that need it are recorded as not produced. The recompute checks that it reproduces the stored plan.

## 5. Engine hooks (`toolkit/engines/hooks.py`) and the trajectory step

`toolkit/engines/contract.py` is in every engine's code fingerprint, so the hooks live in a new module and are
optional; an engine offers one by defining the method. The harness calls them only through `hooks.schedule()`
and `hooks.run_series()`, which check the answer and raise `HookUnsupported` (figure not produced, with that
text) or `HookInvalid`.

```python
engine.schedule(params: dict, unit: UnitSpec, package: Package, horizon: Horizon) -> list[dict]
#   [{component, label, kind: history|decision|routine, t0, t1|None, charged}]; pure, from the code that
#   configures the simulation
EngineCard.outputs["series"] = {metric: {"label": str, "units": str, "aggregate": "mean" | "rate"}}
engine.run_series(params, unit, packages: list[Package], horizon, seed: int, metrics: list[str],
                  step: str = "month") -> list[dict]
#   per package, in order: {"package", "t": [step starts, horizon.start .. window end), "values": {metric: [...]}}
```

`run` and `run_batch` are untouched, so an engine that adds the hooks keeps every existing run byte-identical
(for the malaria engine, proved by `benchmarks/restamp_proof.py` in the engine PR).

**Trajectory step** (`toolkit/analysis/trajectories.py`, `python -m toolkit.analysis.trajectories run <run_dir>
[--per-region 3] [--seeds 3]`): picks, per region, the units at the lowest, median and highest comparator
burden (`figure_data.burden`; fewer when a region has fewer units), and records them. For each sampled unit and
seed it runs the comparator and the unit's plan package with `run_series` at the point parameters and the
unit's calibrated attribute, as one compute task per (unit, seed) through `scripts/compute.py` (`run_map`, the
same path as the batch, so it runs on Coiled in a real run). It writes `analysis/trajectories.yaml`: the sample,
per-unit series (seed mean), and population-weighted series per region and in total. Cost at 3 units per region
and 3 seeds: about 39 units x 3 seeds x 2 arms; with the measured shared burn-in (`benchmarks/runtime.yaml`,
about 150-210 CPU s per unit and seed) about 5-7 core-hours. Without the hook the step writes nothing and the
figure is recorded as not produced: needs trajectory runs.

## 6. The figure set

Each figure is produced only when its inputs exist; otherwise a `not_produced` entry gives the reason.

| # | File | What it shows | Needs |
|---|---|---|---|
| 1 | `allocation_map` | each unit at its coordinates, coloured by package; small multiples per component when there are more than 3 packages (three categorical slots are validated on all pairs); without coordinates, a by-region dot strip (one row per region, one dot per unit) | plan, units |
| 2 | `stability_map` | the same layout, coloured by the share of draws in which the unit keeps its package | uncertainty report |
| 3 | `burden_map` | the same layout, coloured by the comparator outcome per person | figure_data |
| 4 | `calibration_fit` | target (with its interval) against model, a 1:1 line; a by-region residual strip | calibration |
| 5 | `frontier` | outcome averted against budget, the declared envelope and the plan marked, marginal cost per outcome at the envelope | figure_data |
| 6 | `dollars` | the plan's dollars by component and by region, against the envelope and fixed costs | figure_data |
| 7 | `cost_per_outcome` | cost per outcome averted by package, with its interval (dot plot) | figure_data |
| 8 | `sensitivity` | rows with human names and tested range with units; change in the objective at the low and high ends (two fixed colours); a marker on rows that change the plan; sorted by effect | sensitivity, engine, decision tests |
| 9 | `headline` | each reported outcome's point value, median and interval, compact (one small panel per outcome) | point, uncertainty |
| 10 | `timeline` | per region, what is delivered when: past campaigns, the decision's deliveries, routine deliveries, the delivery period against the outcome window, with unit counts | figure_data.schedule |
| 11 | `trajectories` | per region (population-weighted) and in total, the calibration metric and incidence over time, comparator and plan, delivery period shaded, targets with intervals | trajectories |

`python -m toolkit.analysis.figures render <run_dir> --out <dir> [--recompute] [--labels <yaml>]` re-renders a
run from its artifacts into `<dir>` (never into the run dir). `python -m toolkit.analysis.figures compare
<run_dir> <run_dir> ... --out <dir>` renders the headline with its interval and the component and package
counts side by side across runs of one project.

## 7. As built (β), and what differs from §1-§6

- **Calibration by inversion.** Inversion fits each target exactly, so a residual strip would show only zeros. The
  figure plots target against model with the emulator's held-out error as a band. Its right panel shows the
  targets by region with the region-level target interval, and the caption gives the largest residual. For
  `likelihood` and `posterior`, the right panel is the by-region Pearson residual strip.
- **Stability.** The figure shows the whole package and each component's yes/no as small multiples. In the
  re-rendered runs, few units keep their whole package across the draws, but most keep each net or spray
  decision. Package identity alone would hide that.
- **Colours.** Components get contiguous categorical slots in spec order, among the components the plan uses, so a
  stack shows only validated adjacent pairs. Dollars by region are a table of bars, one column per component,
  rather than stacked bars, because non-adjacent slot pairs fail the dark-mode CVD check.
- **Offline rebuild.** Runs computed on another platform can rebuild a job whose hash differs from the stored row
  in the last bit of a derived float. In both re-rendered runs this was one design point (32 rows). Those rows are
  taken by their index, but only when the run's own analysis verified every row (`batch.yaml` `rejected_rows: 0`,
  manifest complete). The rebuilt plan must still equal the stored plan, and the draw replay must still reproduce
  the stored interval. Both held for both runs.
- **Comparison.** `compare` names the spec differences that bear on the headline (outcome window, delivery period,
  envelope, comparator, uncertainty sources, engine attributes or version) in the title and caption.
- **trajectories.yaml** is written by its own step after the analysis. It uses the analysis envelope, and the
  manifest is not changed.

## 8. Open questions for the owner

1. The malaria engine v0.8 (`schedule`, `run_series`) and its proof: separate PR, run off the laptop.
2. Whether `presentation.labels` belongs in the signed spec (it changes the spec hash, not the analysis) or in a
   separate unsigned file. This note puts it in the spec, read only by the figures.
