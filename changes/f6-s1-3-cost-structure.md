---
phase: "F6"
commit: "S1-3"
status_group: "F6 Stage 1"
---

## Changelog

**Component cost structure (F6 S1-3, C2).** A cost item can have basis `per_unit`: a fixed amount per unit whose package has the component (setup, supervision, capacity), whatever its population; `fixed` stays as its older name. A `per_unit` item charged on a population is refused. It goes through `toolkit/alloc/costs.py` `cost_matrix`, so it enters every cost path the same way: the point costs, drawn and sensitivity cost multipliers (by item id), the uncertainty draws and affordability, decision tests, the envelope pruning of generated packages, the figures' dollars by component, and the claims. Two items of one component whose `includes` overlap are still refused, now with the remedy named, unless one of them states `not_double_counted: {<other id>: <reason>}` (at least 10 characters; a name that is not an overlapping item of the same component is refused). Incremental `per_outcome` costs floored at zero (the package has less of the outcome than the package without the component) are recorded by `cost_matrix(..., floors=[])`; `analysis/point.yaml` carries `cost_floors` (the incremental items, the count of unit x package combinations, by item, and a statement that these are floors, not costs or savings), and with incremental items the results state the count as claim `cost.floored_unit_packages`. Tests: costs.py TC25-TC34, including a 3-unit toy whose plan moves from one component in every unit (300 averted) to the other component in one unit (150) when a $1,500 per-unit fixed part is added, as computed by hand; run.py R5n-R5r on the emulator-path analysis. The schema docstring, the allocation-toolkit skill and the run console's cost labels describe the basis.

## Status

- **S1-3:** costs have a fixed part per unit receiving a component, each price states what it includes and an overlap needs a stated reason, and floored per-outcome increments are reported. The F6 spec's case-management setup and capacity costs (S2-2) use it.
