---
name: allocation-toolkit
description: >-
  Required when a model produces an allocation across units: declare costs and
  packages as a spec and use the repo's allocation toolkit (toolkit/alloc:
  costs, packages, optimize) instead of writing a cost model and optimizer for
  the run. The toolkit's benchmark writes models/optimization_quality.yaml.
  Extend it in models/ext/ only for what it lacks, and record why in
  models/toolkit_use.yaml. Trigger phrases include "cost model", "cost spec",
  "packages", "feasibility", "budget allocation", "optimizer", "knapsack".
---

# Allocation toolkit

## Why this skill exists

The cost model and the optimizer were the most finding-dense code of earlier
allocation runs, rewritten each run: a commodity counted twice beside an
all-in cost, a targeted intervention costed on a population it did not reach,
prices left in mixed years, and an "optimal" solver beaten by greedy. The
toolkit makes these errors fail validation before any number is computed, and
its optimizer re-checks every allocation and stops when a method beats an
exact one. Each module's docstring is its reference; read it before writing
the spec.

| Module | What it does | Read |
|---|---|---|
| `toolkit/alloc/costs.py` | Cost spec (units and populations, components, packages, prices, cost items) → validated cost matrix per unit and package, in base-year prices | docstring, `cost_matrix()` |
| `toolkit/alloc/packages.py` | Eligibility rules, locks (already delivered), other funders' footprints, caps → `feasible[unit][package]` with the reason for every infeasible pair | docstring, `feasibility()` |
| `toolkit/alloc/optimize.py` | One package per unit within a budget and caps (ILP, greedy, brute force); `benchmark()` runs them against each other and writes `models/optimization_quality.yaml` | docstring, `solve()`, `benchmark()` |

A complete cost spec from an earlier run: `toolkit/alloc/examples/e7a7_costs.yaml`.

## Use it from the run

The toolkit is read-only during a run, like `scripts/`. Import it as a
package; find the repo root from the file's own location (the same line works
in a pipeline step, a scratch copy and on compute workers, which receive the
repo's `toolkit/`):

```python
import pathlib, sys
sys.path.insert(0, next(str(p) for p in pathlib.Path(__file__).resolve().parents
                        if (p / "toolkit" / "alloc").is_dir()))
from toolkit.alloc import costs, packages, optimize
```

Do not add `toolkit/alloc/` itself to `sys.path` or name a run file
`costs.py`, `packages.py` or `optimize.py` and import the toolkit by those
bare names: the package import keeps the run's modules and the toolkit's
apart.

## Workflow

1. **Write the spec** (`models/alloc_spec.yaml`): `units` (the units table and
   its population columns), `components` (each intervention component; a
   targeted one names its `eligible` population), `packages` (explicit, or
   `{generate: true}`), `prices`, `cost_items`, and, for feasibility,
   `eligibility`, `locks`, `footprints`, `caps`. Every cost value and price
   year comes from a cited source in the parameter registry.
2. **Validate early.** `python3 toolkit/alloc/costs.py models/alloc_spec.yaml`
   and `python3 toolkit/alloc/packages.py models/alloc_spec.yaml` print the
   errors (fail closed). Fix the spec, not the checks.
3. **Costs from model outputs.** A cost per treated case or other model
   output is a `per_outcome` item (`incremental: true` for the package minus
   the package without this component); pass
   `outcome=<fn(unit_row, package) -> {name: value}>` to `costs.cost_matrix`.
4. **Benefit** comes from the model: `benefit[unit][package]`, one number per
   feasible pair, in the objective's units.
5. **Optimize and benchmark in one call:**

   ```python
   spec = yaml.safe_load(open(run_dir / "models/alloc_spec.yaml"))
   units = costs.load_units(spec, str(run_dir))
   F = packages.feasibility(spec, units)
   cm = costs.cost_matrix(spec, units, outcome=outcome)
   rep = optimize.benchmark(benefit, cm, F, budget, run_dir=str(run_dir),
                            objective_name="<objective>", sweep_budgets=[...])
   choice = rep["results"]["ilp"]["choice"]      # unit -> package; write the allocation CSV from it
   ```

   `benchmark()` writes `models/optimization_quality.yaml` in the schema the
   gate reads (see the `optimizer-method-selection` skill for what the gate
   requires). An `OptimizerError` is a solver defect, not a result: report it.
6. **Declare it as a pipeline step** (see the modeler prompt, "Build
   pipeline"): inputs are the spec, the units table and the benefit table;
   outputs are the allocation CSV and `models/optimization_quality.yaml`.

## When the toolkit lacks something

Extend it in `models/ext/` (for example a cost basis it does not have, or a
coupling constraint beyond `max_units` / `max_quantity`), importing the
toolkit rather than copying it. If a module does not fit at all (for example,
an objective that couples units, which a one-package-per-unit knapsack cannot
represent), write the run's own code. Either way record why in
`models/toolkit_use.yaml`:

```yaml
not_used:                    # toolkit module -> why this run does not use it
  optimize: "<what the objective needs that the toolkit's optimizer does not do>"
extensions:                  # models/ext/ file -> what the toolkit lacks
  models/ext/cost_items.py: "<what it adds>"
```

`scripts/alloc_toolkit_use.py` checks this at the gate when the run produces
an allocation: a toolkit module neither used nor explained, or an extension
without a reason, is an advisory MEDIUM. The critics read the reasons.
