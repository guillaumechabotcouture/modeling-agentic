---
name: optimizer-method-selection
description: When a model produces a budget-constrained allocation
  (e.g., distributing $320M across 774 LGAs and 8 intervention
  packages), the modeler must justify the optimizer choice by
  comparing the primary method against at least one alternative —
  ILP via PuLP, simulated annealing, random-restart greedy, or brute
  force when feasible. The Phase 6 Commit θ gate enforces this via
  models/optimization_quality.yaml. Trigger phrases include
  "optimization quality", "greedy vs ILP", "optimizer benchmark",
  "optimality gap", "is greedy good enough".
---

# Optimizer Method Selection

## Why this skill exists

A common failure mode in policy-allocation modeling: ship a greedy
optimizer, report "X% improvement over uniform distribution", and
declare success. This claim is **untrustworthy without a benchmark**.
Greedy can produce 60% of the optimal solution on problems with
package interactions or budget cliffs, while looking fine in
isolation. An experienced senior modeler would always run a second
method and report the gap.

The Phase 6 Commit θ gate enforces this via a required artifact
`models/optimization_quality.yaml` that documents the primary
optimizer's gap_pct against ≥1 benchmark method.

## Decision tree: which optimizer for which problem

```
Is the objective separable per spatial unit (per-LGA cases averted)?
├── YES, with simple budget constraint
│   ├── Greedy marginal CE is fine
│   │   Benchmark against: random-restart greedy (k=20 starts)
│   │   Expected gap: <2%
│   └── Use this when: ≥500 spatial units, monotone marginal returns
│
├── YES, with cardinality / mutual-exclusion constraints
│   ├── ILP via PuLP+CBC is the right tool
│   │   Benchmark against: greedy (to show the gap)
│   │   Expected gap: 2-15% in greedy's favor
│   └── Use this when: "fund exactly K LGAs", "package P excludes Q"
│
└── NO (cross-LGA spillovers, package interactions)
    ├── Simulated annealing or genetic algorithm
    │   Benchmark against: random-restart greedy
    │   Expected gap: variable; report best-of-N runs
    └── Use this when: spatial coupling, herd effects across LGAs
```

## Code templates

### Greedy marginal CE (the typical primary method)

```python
def greedy_allocation(units, packages, budget, score_fn):
    """Allocate budget across (unit, package) pairs by descending
    marginal score-per-dollar until budget exhausted."""
    pairs = []
    for u in units:
        for p in packages:
            cost = p.cost(u)
            score = score_fn(u, p)
            if cost > 0:
                pairs.append((score / cost, u, p, cost, score))
    pairs.sort(reverse=True)
    chosen = {}
    spent = 0
    for ratio, u, p, cost, score in pairs:
        if u in chosen:
            continue
        if spent + cost <= budget:
            chosen[u] = p
            spent += cost
    return chosen, spent
```

### ILP via PuLP (the typical benchmark)

```python
from pulp import LpProblem, LpVariable, LpMaximize, lpSum, PULP_CBC_CMD

def ilp_allocation(units, packages, budget, score_fn):
    """Maximize sum(score) subject to: each unit gets at most one
    package, total cost ≤ budget."""
    prob = LpProblem("allocation", LpMaximize)
    x = {(u, p): LpVariable(f"x_{u}_{p}", cat="Binary")
         for u in units for p in packages}

    prob += lpSum(score_fn(u, p) * x[u, p]
                   for u in units for p in packages)
    for u in units:
        prob += lpSum(x[u, p] for p in packages) <= 1
    prob += lpSum(p.cost(u) * x[u, p]
                   for u in units for p in packages) <= budget

    prob.solve(PULP_CBC_CMD(msg=False, timeLimit=600))

    chosen = {}
    for (u, p), var in x.items():
        if var.value() > 0.5:
            chosen[u] = p
    return chosen, prob.objective.value()
```

For Nigeria 774 LGAs × 8 packages = 6192 binary variables. CBC solves
to within 1% of optimal in <5 minutes on typical hardware.

### Simulated annealing (when the objective is non-convex)

```python
import random, math

def simulated_annealing(units, packages, budget, score_fn,
                        T0=100, alpha=0.95, n_iter=5000):
    """Random local search with cooling schedule."""
    # Initialize with greedy
    current, _ = greedy_allocation(units, packages, budget, score_fn)
    current_score = sum(score_fn(u, p) for u, p in current.items())
    best, best_score = dict(current), current_score
    T = T0
    for _ in range(n_iter):
        # Propose: swap one unit's package
        u = random.choice(units)
        p_old = current.get(u)
        p_new = random.choice(packages)
        proposed = dict(current)
        proposed[u] = p_new
        if total_cost(proposed) > budget:
            continue
        delta = score_fn(u, p_new) - (score_fn(u, p_old) if p_old else 0)
        if delta > 0 or random.random() < math.exp(delta / T):
            current = proposed
            current_score += delta
            if current_score > best_score:
                best, best_score = dict(current), current_score
        T *= alpha
    return best, best_score
```

## What goes in `optimization_quality.yaml`

Required fields:
- `primary_method`: one of greedy / ilp_pulp / simulated_annealing /
  random_restart_K (where K is the # of restarts) / brute_force
- `primary_objective`: numeric (cases averted, DALYs averted, etc.)
- `objective_name`: human-readable label
- `benchmark_methods`: list of dicts, each with method/objective/runtime_sec
- `gap_pct`: optional; recomputed by the validator

The gate emits MEDIUM if gap_pct > 10%. If the gap is genuinely large,
either:
- Use the better method as primary, or
- Scope-declare why the primary's choice is justified despite the gap
  (e.g., "ILP feasible but takes 30 min vs greedy's 1 min, and the
  policy decision doesn't depend on the last 5% of optimization").

## Common pitfalls

1. **Reporting only the primary method's "X% improvement vs uniform"**
   without benchmarking against ILP: the X% might be 60% of the
   achievable improvement.

2. **Single-restart greedy** when the problem has score plateaus.
   Random-restart with k≥20 catches the local-optimum issue cheaply.

3. **Time-limit exhaustion on ILP** without checking the gap. Use
   `solver.gapRel` or report `MIP gap` from CBC alongside the
   objective.

4. **Brute force when intractable** (8^774 = infeasible). State
   explicitly that brute force is infeasible and use ILP as the gold
   standard instead.

## Phase 6 Commit θ artifact schema

```yaml
# {run_dir}/models/optimization_quality.yaml
primary_method: greedy
primary_objective: 5407832
objective_name: cases_averted_per_year

benchmark_methods:
  - method: ilp_pulp
    objective: 5489102
    runtime_sec: 312
    notes: "PuLP w/ CBC solver, default settings, 1.5% MIP gap"
  - method: random_restart_50
    objective: 5398754
    runtime_sec: 45

gap_pct: 1.49
notes: |
  Brute force not feasible (8 packages × 22 archetypes = 8^22 combos).
  ILP via PuLP+CBC near-optimal in 5 min. Greedy is within 1.5% of ILP
  and 50× faster, so greedy is the primary method.
```
