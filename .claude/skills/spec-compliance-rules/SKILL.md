---
name: spec-compliance-rules
description: Mechanical backstop that parses the research question for
  named frameworks (Starsim, LASER, stisim, EMOD), approaches (ABM,
  compartmental, stochastic), budget envelopes, and spatial/archetype
  counts, then verifies the delivered model code and allocation CSVs
  against those requirements. Invoked by the STAGE 7 validator via
  `--spec-compliance`. Framework/approach HIGH violations force
  `structural_mismatch: true` regardless of what critique agents said;
  budget/archetype HIGH violations add synthetic OBJ-NNN blockers to
  `unresolved_high`. Use when critique agents may be too generous about
  whether a rebuilt model actually satisfies an architectural requirement.
  Trigger phrases include "spec compliance", "framework check", "budget
  underutilized", "starsim not running", "silent downscope".
---

# Spec-Compliance Rules

## Why this exists

The STAGE 7 gate trusts critique agents to flag architectural issues via
the `structural_mismatch` field in `critique_*.yaml`. In practice,
critique agents can be generous: the malaria Nigeria run shipped a
compartmental ODE when the question explicitly asked for a Starsim ABM.
Round 1 critiques correctly flagged the mismatch (structural_mismatch
veto fired → RETHINK). But in round 2 both critique-methods and
critique-domain set `structural_mismatch: false`, and the lead accepted,
even though the rebuild was still a compartmental ODE with Starsim
imported only as decoration.

Spec-compliance rules are a **mechanical backstop** — pure-function
checks that run independently of critique judgment. They cannot be
argued with by a generous critique.

## What gets checked

The rules consume two inputs: the research question (from
`metadata.json['question']`) and the run directory. Four categories of
requirement are parsed from the question:

### 1. Named frameworks

Positive indicators required (conservative — avoids false positives):

| Framework | Trigger phrase in question                                     |
|-----------|----------------------------------------------------------------|
| `starsim` | "using [the] Starsim [framework]", "Starsim framework", "with Starsim", "built on Starsim", "implemented in Starsim" |
| `laser`   | "using [the] LASER [framework]", "LASER framework", "built on LASER" |
| `stisim`  | "using [the] stisim [framework]", "stisim framework"          |
| `emod`    | "using [the] EMOD [framework]", "implemented in EMOD"          |

Bare mentions like `"benchmark against EMOD"` or `"the Ozodiegwu paper
uses Starsim"` do NOT trigger a requirement — those are references, not
specifications.

When a framework IS required, the check verifies:

- `starsim`: `import starsim` or `from starsim import` MUST appear in
  at least one `.py` file under `{run_dir}/models/`, AND at least one
  of `ss.Sim(`, `ss.People(`, `starsim.Sim(`, `starsim.People(`, or
  `sim.run()` must appear. Subclassing `ss.SIS` / `ss.Disease` /
  `ss.Module` alone is NOT sufficient — that pattern can be done while
  the real dynamics run through `scipy.integrate.odeint`.
- `laser`: `import laser` or `from laser` must appear.
- `stisim`: `import stisim` or `from stisim` must appear.
- `emod`: `import emod` / `from emod` must appear.

If required but not met → HIGH violation of kind `framework_missing`.

### 2. Approach: ABM

If the question contains any of `"agent-based model"`, `"agent based
model"`, `"individual-based model"`, or `"ABM"`, the check looks for
per-agent state signals:

- `ss.People(...)` or `starsim.People(...)` construction
- Class definition with a per-agent name (`class FooAgent`,
  `class HostPerson`, `class Individual`)

AND counts competing compartmental-ODE signals:

- `scipy.integrate.odeint`, `scipy.integrate.solve_ivp`
- `odeint(...)`, `solve_ivp(...)` calls

Decision:

- ABM signals present, no ODE signals → compliant.
- ODE signals present, no ABM signals → HIGH violation
  (`approach_mismatch`).
- Both present, but ODE signals outnumber ABM signals by >2× → HIGH
  violation. The model is ODE-dominant with Starsim-flavored trimming.
- Neither present → HIGH violation (can't verify ABM).

### 3. Budget envelope

Matches patterns like `$320M`, `$1.5 billion`, `$320 million`. Only
dollar-prefixed numbers count (avoids false positives on unrelated
numeric references).

When a budget IS parsed, the check:

1. Globs `{run_dir}/*allocation*.csv`, `*budget*.csv`,
   `*optimization*.csv`, and the same under `{run_dir}/data/`.
2. Picks the largest CSV and finds its cost-like column (`cost`,
   `total_cost`, `budget`, `price`, etc. — excluding per-unit columns
   like `cost_per_daly`).
3. If an `allocated` column exists, restricts the sum to rows where
   `allocated=True`.
4. Computes `utilization = sum / budget_envelope`.
5. If `utilization < 0.80` → HIGH violation of kind
   `budget_underutilized`.

If no allocation CSV exists, no violation (optimization step may not
have run yet).

### 4. Archetype aggregation

If the question names N archetypes (`"22 archetypes"`, etc.), the check
greps models/ for archetype identifiers (`A1`, `A2`, …, `A22`) to
estimate how many the code actually uses (K). If K < N, the check looks
for an error-bound discussion in `model_comparison.md`, `results.md`, or
`modeling_strategy.md` (keywords: `within_archetype_error`,
`within-archetype error`, `aggregation error`, `archetype variance`).

- K absent/unknown → no check.
- K ≥ N → compliant.
- K < N with error bound documented → compliant.
- K < N with no error bound, N/K ≥ 5 → HIGH violation
  (`archetype_aggregation_unvalidated`).
- K < N with no error bound, N/K < 5 → MEDIUM violation (does not block
  ACCEPT but is surfaced).

## How violations map to gate action

The validator's `incorporate_spec_violations` function folds violations
into the existing decision:

| Violation kind                       | Severity | Effect on decision                              |
|--------------------------------------|----------|-------------------------------------------------|
| `framework_missing`                  | HIGH     | Force `structural_mismatch: true` → RETHINK_STRUCTURAL |
| `approach_mismatch`                  | HIGH     | Force `structural_mismatch: true` → RETHINK_STRUCTURAL |
| `budget_underutilized`               | HIGH     | Add synthetic `OBJ-NNN` blocker to `unresolved_high` → PATCH_OR_RETHINK |
| `archetype_aggregation_unvalidated`  | HIGH     | Add synthetic `OBJ-NNN` blocker to `unresolved_high` |
| `archetype_aggregation_unvalidated`  | MEDIUM   | Reported but does not change action             |

When `structural_mismatch` is forced true by spec-compliance, the
`structural_reviewers` list includes the literal string
`"spec-compliance"` so the lead can see in the validator output that
the gate — not a critique agent — flagged the issue.

## For critique agents: what to do with spec violations

If you are a critique-methods or critique-domain agent writing YAML and
you observe that the delivered model does not use a framework the
question named, OR that the primary dynamics are not the approach the
question asked for: set `structural_mismatch.detected: true` in your
YAML. Do NOT wait for the gate to catch it — your job is to catch it
first. The spec-compliance check is a mechanical backstop, not a
replacement for your judgment.

A common failure mode: the rebuilt model in round 2 "looks better" and
you set `structural_mismatch: false` because the improvements are real.
That is the wrong frame. The question is not "is the new model better
than the old one?" It is "does the new model satisfy the architectural
specification in the question?" If the question asked for X and the
model is not X, `structural_mismatch` is `true` regardless of how
improved the model became between rounds.

## For the lead: what spec violations mean operationally

When the validator reports `action: RETHINK_STRUCTURAL` with
`structural_reviewers: ['spec-compliance']` (rather than a critique
agent), the modeler must rebuild. The re-spawn prompt should quote the
violation's `evidence` field verbatim. For a framework_missing on
Starsim, that means: "Your previous attempt imported starsim but did
not call `ss.Sim(...).run()`. The question requires running the
simulation through Starsim's framework, not using Starsim primitives
cosmetically. Build an `ss.Sim` with an `ss.People` population and call
`.run()` on it."

For a `budget_underutilized` OBJ blocker, the re-spawn should target
the ANALYZE stage or MODEL stage (wherever the optimizer lives) with:
"The optimizer produced an allocation summing to only $X of the $Y
budget envelope. Reformulate as an MILP or iterative allocation that
uses ≥80% of the envelope, OR declare in scope that the budget floor
is the natural stopping point and the remainder is genuinely better
unallocated given the available interventions."

## Running the check manually

```bash
python3 scripts/spec_compliance.py {run_dir}          # human-readable
python3 scripts/spec_compliance.py {run_dir} --json   # machine-readable
python3 scripts/spec_compliance.py --self-test        # run inline tests
```

Or as part of the full gate:

```bash
python3 scripts/validate_critique_yaml.py {run_dir} \
  --max-rounds 5 --current-round 2 --spec-compliance --json
```
