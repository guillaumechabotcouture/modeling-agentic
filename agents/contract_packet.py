#!/usr/bin/env python3
"""Active contract packet for a pipeline stage (architecture v2 §3.3, experiment E1).

Instead of preloading ~28 skills (~90K tokens) into the modeler, give it the
exact contracts its outputs are checked against, generated from the two
manifests the validator reads:

  .claude/orchestration/rigor_artifacts.yaml  artifact ids, paths, rounds,
                                              triggers, declarability
  .claude/orchestration/effort_floors.yaml    quantitative minimums

plus compact schema cards (ARTIFACT_SCHEMAS / EXTRA_CONTRACTS below) whose
field names mirror what the validator and the producing scripts read
(scripts/compare_models.py, identifiability.py, sensitivity_analysis.py,
eligibility_probe.py, benchmark_match.py, sanity_checks.py,
within_zone_sensitivity.py, effort_floors.py, effect_size_registry.py,
plan_criteria.py, validate_critique_yaml.py). Disease-agnostic.

    python3 agents/contract_packet.py [--stage model] [--no-allocation] [--aggregation]
    python3 agents/contract_packet.py --self-test
"""
from __future__ import annotations

import os
import re
import sys

import yaml

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RIGOR = os.path.join(REPO, ".claude", "orchestration", "rigor_artifacts.yaml")
FLOORS = os.path.join(REPO, ".claude", "orchestration", "effort_floors.yaml")

# Stages whose artifacts the MODEL-stage agent produces.
STAGE_SCOPE = {"model": ("model", "allocation")}
# Gate/critic-produced artifacts never written by the modeler.
_NOT_MODELER = {"benchmark_match", "effort_floors_report", "coherence_audit",
                "critique_sufficiency", "writer_qa_report", "scope_declaration",
                "claims_ledger"}
_TRIGGER_FLAG = {"always": None, "allocation": "allocation",
                 "aggregation_ratio_lt_0.1": "aggregation"}

# Compact schema cards: exact field names the checks read. Keep terse.
ARTIFACT_SCHEMAS = {
    "outcome_fn": """\
`def outcome_fn(params: dict) -> dict` — deterministic; params = {registered_param_name: sampled_value};
returns {output_name: scalar | str}. MUST read every registered decision-critical parameter via
`params.get('NAME', default)` (else HIGH `param_frozen_in_uq`). Zero-width 95% CI while inputs vary =
HIGH non-declarable `ci_degenerate`; deterministic-by-design outputs go in
`models/uq_declarations.yaml: deterministic_outputs: {output: "reason"}`, constraint-fixed ones in
`pinned_by_constraint: {output_or_family: {constraint: NAME, value: NUMBER}}` (draws must sit at value).
Surrogate/emulator path ⇒
`models/outcome_fn_calibration.md` with: architecture, ≥10 held-out full-model validation runs,
per-output "RMSE", cross-validation error, extrapolation bounds (else HIGH `surrogate_uq_undocumented`).
Lead runs `scripts/propagate_uncertainty.py` → `uncertainty_report.yaml` (do not write it).""",
    "model_comparison": """\
```yaml
n_targets: 22                 # data points fit
targets: [..]                 # observed values, len == n_targets
models:                       # ≥3: null, simple, full
  - {name: null,   k_params: 1,  predictions: [..], loo_predictions: [..], description: ".."}
  - {name: simple, k_params: 4,  predictions: [..], loo_predictions: [..]}
  - {name: full,   k_params: 23, predictions: [..], loo_predictions: [..]}
```
Provide loo_predictions (main degenerate-fit check). `scripts/compare_models.py` → model_comparison_formal.yaml
(CLEAN | DEGENERATE_FIT_DETECTED | INSUFFICIENT_STRUCTURES; never hand-edit). DEGENERATE (k ≥ n/2, LOO ≫ train)
needs resolution: pooling, tied parameters, or scope declaration. ΔAIC > 10 to justify added parameters.""",
    "identifiability": """\
```yaml
loss_fn: "models/outcome_fn.py::loss"   # loss(params) -> float
loss_type: sse        # REQUIRED: nll | deviance | chi2 | sse | mse (else UNKNOWN_LOSS_SCALE = fail)
n_obs: 22             # REQUIRED for sse/mse
profile: full         # full (default) | conditional
parameters:
  - {name: beta, point_estimate: 0.5, lower_bound: 0.1, upper_bound: 2.0, profile_n_points: 20}
```
Loss MUST be pointwise: evaluate residuals at exactly the given params; never optimize/fit inside
(re-optimizing loss raises LossFunctionReoptimizesError). Unidentified parameters used in policy outputs = HIGH:
fix via priors, tying, or fixing at defaults. Lead runs `scripts/identifiability.py` → identifiability.yaml.""",
    "calibration_result": """\
Write this exact YAML file whenever ANY parameter is fitted, even if your own code also writes JSON/logs
(missing = HIGH non-declarable):
```yaml
method: nelder-mead            # optimizer
loss: negative log-likelihood
n_restarts: 3                  # independent starts actually run, same convergence criterion
n_iterations: 3000             # per-restart iteration BUDGET you configured (maxiter), not iterations-to-converge
iterations_used: [412, 388, 455]   # optional: actual iterations per restart
held_out_fold: {indices: [3, 11], loss: 0.042}   # targets excluded from fit, then scored; non-empty
best_loss: 0.031
restart_losses: [0.031, 0.033, 0.031]
```""",
    "eligibility_manifest": """\
```yaml
outcome_fn: models/outcome_fn.py::evaluate_unit   # per-unit evaluator whose inputs include population fields
call_style: dict                                   # dict | kwargs
baseline_inputs: {total_population: 120000, eligible_pop: 21000, baseline_rate: 0.3, package: none}
interventions:
  - name: targeted_intervention
    on_inputs: {package: x}
    off_inputs: {package: none}
    eligible_population_field: eligible_pop
    total_population_field: total_population
    benefit_output: cases_averted
    spillover: false             # true requires spillover_justification
```
Benefit must be computed on the population the cost is charged for. `scripts/eligibility_probe.py` checks:
eligible→0 ⇒ benefit→0; eligible×k ⇒ benefit responds; total×m (eligible fixed) ⇒ benefit unchanged. HIGH on failure.""",
    "benchmark_targets": """\
Planner creates `benchmarks: [{id, metric, target_value, units, tolerance_factor (≤10) | tolerance_abs, source}]`;
the gate locks it on first sight. After every model run the modeler writes the current values to
`models/benchmark_computed.yaml` (`benchmarks: [{id, computed_value | computed_field: "file.yaml::dot.path"}]`,
or `computed: {id: value}`); NaN/null = not computed. These win over any copy in the targets file.
`scripts/benchmark_match.py` → PASS | DRIFT | MALFORMED. Drift needs a literature-grounded scope declaration.""",
    "sanity_schema": """\
Optional sections (each enables one generic check): `outcome: {name, unit, baseline_total, averted_point}`;
`exposure: {unit, total_in_allocated, rate_per_unit_per_year, years}`; `allocation: {units_total, units_allocated,
budget, canonical_csv?}`; `shares: [{name, sums_to, tol, values: {..}}]`; `counterfactual: {name, averted,
optimized_vs_null_acceptable: [lo, hi]}`; `derived_consistency: [{primary, primary_value, derived, derived_actual,
formula, constants, tol}]`; `composite_dimensions: [{name, value, window}]`; `structural_uncertainty: [{source,
verdict, lower_bound, must_appear_in}]`; `outlier_sniff: {metric, rule: max_over_median, threshold, csv_path,
numerator_col, denominator_col}`. `units_total` must equal allocation CSV rows.""",
    "within_zone_heterogeneity": """\
```yaml
calibration_units: 6
allocation_units: 300
within_unit_value_range: {min: 0.06, max: 0.77, metric: prevalence}
modeled_uniform_per_unit: true
sensitivity:
  - {perturbation: uniform_within_zone (baseline), outcome_averted: 5.47e7}
  - {perturbation: normal_within_zone (sd from data), outcome_averted: 4.90e7, impact_loss_pct: 10.4}
verdict: BOUNDED   # max impact_loss_pct <10 BOUNDED | 10–25 INCONCLUSIVE | >25 UNBOUNDED (optimizer re-run)
```""",
    "sensitivity_analysis": """\
Draft after the first optimizer pass, then expand. EVERY parameter entry needs ≥2 perturbations from the
first draft (the floor takes the minimum across entries). Re-run the FULL optimizer per value.
```yaml
load_bearing_parameters:          # ≥5 for full coverage
  - id: SA-001
    parameter: effect_a           # a parameter whose alternative value could change the recommendation
    primary_value: 0.55
    primary_objective: 4565827
    perturbations:                # ≥2 per parameter (CI endpoints / competing estimates)
      - {value: 0.37, objective: 4480000, rank_change_top_n: 0, primary_recommendation_changes: false}
      - {value: 0.81, objective: 4710000, rank_change_top_n: 3, primary_recommendation_changes: false}
verdict: ROBUST    # must equal computed verdict
```
ROBUST: no flip & worst rank_change_top_n ≤ 10; SENSITIVE: ≤1 flip or ≤ 30; UNSTABLE: ≥2 flips or > 30 (HIGH).
"Primary recommendation" = the decision rule / priority ranking, not the funded set.""",
    "allocation_robustness": """\
```yaml
holdout_method: leave-one-group-out
n_folds: 22
metrics: {rank_correlation_worst_fold: 0.78, cases_averted_gap_pct_worst_fold: 8.5,
          rule_classification_concordance_pct_worst_fold: 88}
verdict: ROBUST   # ROBUST: rank≥0.70 & gap≤15 & concordance≥80; UNSTABLE: rank<0.40 | gap>30 | conc<60 (HIGH)
```
Holdout = re-calibrate without each group, re-optimize, compare its recommended package (not calibration CV).""",
    "optimization_quality": """\
```yaml
primary_method: greedy            # greedy | ilp_pulp | simulated_annealing | random_restart_K
primary_objective: 4.56e6
objective_name: dalys_averted
benchmark_methods: [{method: ilp_pulp, objective: 4.60e6, runtime_sec: 12, notes: ".."}]   # non-empty (else HIGH)
gap_pct: 0.9                      # 100*(best-primary)/best; > 10 = MEDIUM
```""",
    "universal_coverage": """\
```yaml
total_units: 300
actual_budget: 5.0e7
budget_for_universal_<dominant_package>: 1.6e8
dalys_averted: 1.2e6
universal_coverage_dalys_averted_estimate: 3.6e6
efficiency_pct: 33.3          # constrained / universal * 100
notes: ".."
```""",
    "decision_rule": """\
Markdown with a `rule_type` (tabular | tree | prose-with-exceptions | non-compressible), the rule itself,
`accuracy_vs_optimizer` (share of units whose package the rule reproduces) and `exceptions_count`/list.
tabular/tree: ≥95% with ≤7 nodes/cells; prose-with-exceptions: ≥90%; non-compressible needs a Justification
section. rule_type ≠ non-compressible with accuracy < 0.90 and no exceptions = HIGH. Never fabricate a rule.""",
}

EXTRA_CONTRACTS = """\
### Other checked contracts
- **Parameter registry** (`citations.md` § `## Parameter Registry`): every literature-sourced constant gets
  `name, value, ci_low, ci_high, kind, source ([CNN] in citations.md), subgroup, applies_to, code_refs [file:line]`,
  and a `# @registry:<name>` comment at each use site. `kind: odds_ratio` ≠ `relative_risk`: convert with
  `or_to_rr(OR, baseline_p)` before using as RR (HIGH `or_rr_conflation`). Costs: list every CSV holding the
  value in code_refs (>10% disagreement = HIGH). Every registered parameter must appear under `models/`
  (HIGH `param_not_in_code`); decision-critical kinds (odds_ratio, relative_risk, hazard_ratio,
  incidence_rate_ratio, efficacy, cost_usd, proportion) must be read in outcome_fn (HIGH `param_frozen_in_uq`).
- **Pre-mortem**: answer every HIGH concern of `pre_mortem.yaml` in `modeling_strategy.md` under the heading
  `## Pre-mortem Responses` (one subsection each: concern, ADDRESSED / SCOPE_DECLARED / DEFERRED, the resolving
  change) and set that concern's `addressed_in:` to the section; unaddressed HIGH at round ≥ 2 blocks ACCEPT.
- **Aggregation**: if you aggregate units into fewer groups than the plan specifies, add the exact line
  `**Within-archetype error**: <value>pp` (or `%`, or fraction) to modeling_strategy.md (else HIGH).
- **Plan criteria**: for each criterion in `success_criteria.yaml`, write its `artifact_field` into the named
  artifact (use `models/plan_metrics.yaml` when it names a script output); `*_done` flags are 1/0.
- **DALYs** (allocation runs): compute DALYs averted alongside cases averted (report needs $/DALY), or justify
  absence in Methods.
- **Figures**: after every `plt.savefig`, call `validate_figure(png, source_data_paths=[..],
  expected_annotations=[..])` from `scripts/figure_validator.py` within 10 lines; regenerate on staleness.
- **Not applicable**: an artifact that genuinely does not apply → `scope_declaration.yaml` entry
  `not_applicable: <artifact_id>` + justification (counts against the declaration budget, default cap 3).
- **Write-protected script outputs** (never write/edit): uncertainty_report.yaml, model_comparison_formal.yaml,
  identifiability.yaml (run-root), benchmark_match.yaml, effort_floors_report.yaml, metadata.json.
"""


def _load(path):
    with open(path) as f:
        return yaml.safe_load(f) or {}


def required_artifacts(stage="model", applicable=None):
    """Manifest entries the stage agent must produce, honoring trigger flags.

    applicable: set of flags in {"allocation", "aggregation"} that hold for the
    run; None = include conditional artifacts, marked as conditional.
    """
    scope = STAGE_SCOPE.get(stage, (stage,))
    out = []
    for a in _load(RIGOR).get("artifacts") or []:
        if a.get("stage") not in scope or a["id"] in _NOT_MODELER:
            continue
        if a.get("required_for_accept") == "never":
            continue
        flag = _TRIGGER_FLAG.get(a.get("triggered_by", "always"))
        if applicable is not None and flag and flag not in applicable:
            continue
        out.append({**a, "_flag": flag})
    return out


def floors():
    return _load(FLOORS).get("floors") or []


_TRIGGER_TEXT = {
    "always": "always",
    "uq_present": "when `uncertainty_report.yaml` exists",
    "cloud_uq_present": "when UQ ran on the cloud backend",
    "sensitivity_present": "when `models/sensitivity_analysis.yaml` exists",
    "calibration_present": "when `models/calibration_result.yaml` exists",
    "calibration_evidence": ("whenever ANY parameter is fitted (i.e. `models/identifiability.yaml`, a "
                             "`model_comparison.yaml` model with `k_params` > 0, or any `models/*calibrat*` file exists)"),
}


def _shortcut_rules() -> str:
    """Generated from scripts/effort_floors.py so the packet cannot drift from the scan."""
    import sys
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import effort_floors as ef  # noqa: E402
    lits = ", ".join(f"{k}<{v}" for k, v in ef._SHORTCUT_LITERAL_NAMES.items())
    benign = "/".join(sorted(ef._BENIGN_PASS_EXCEPTIONS))
    return (f"any `.py` under `models/` (incl. exploratory/robustness scripts) containing: a literal or "
            f"constant below its floor ({lits}); a `# TODO|FIXME|HACK|XXX|SHORTCUT` comment; or "
            f"`except ...: pass` (except {benign}). Each is one finding; ≥3 findings = HIGH. Put scratch "
            f"experiments outside `models/` or use the floor values.")


def floor_instruction(f: dict) -> str:
    """One actionable line per floor, generated from effort_floors.yaml."""
    fields = [x.strip() for x in str(f["field"]).split("|")]
    sev = f"{f['severity']}{'' if f.get('scope_declarable') else ', not declarable'}"
    when = _TRIGGER_TEXT.get(f.get("triggered_by"), f.get("triggered_by"))
    art, m = f["artifact"], f["minimum"]
    probe = f.get("probe")
    if probe == "artifact_present":
        what = f"write `{art}` (exact path, YAML)"
    elif probe == "yaml_field":
        what = f"`{art}` field {' (or) '.join(f'`{x}`' for x in fields)} ≥ {m}"
        if f.get("subtract_field"):
            what += f" after subtracting `{f['subtract_field']}`"
    elif probe == "yaml_field_list_length" and "[*]" in fields[0]:
        head, tail = fields[0].split("[*]", 1)
        what = (f"`{art}`: EVERY item of `{head.rstrip('.')}` needs `{tail.lstrip('.')}` with ≥ {m} entries "
                f"(minimum across items is checked)")
    elif probe == "yaml_field_list_length":
        what = f"`{art}` field `{fields[0]}` must be a list with ≥ {m} entries"
    elif probe == "ast_shortcut_scan":
        what = _shortcut_rules()
    else:
        what = f"`{art}` `{fields[0]}` ≥ {m}"
    return f"- `{f['id']}` ({sev}; {when}): {what.rstrip(".")}."


def _cond(flag):
    return {"allocation": "if an allocation is produced",
            "aggregation": "if calibration units / allocation units < 0.1"}.get(flag, "always")


def build_packet(stage="model", question="", applicable=None) -> str:
    arts = required_artifacts(stage, applicable)
    lines = [f"## Active contract packet — {stage.upper()} stage",
             "Your outputs are checked mechanically against these contracts. Draft early; iterate each round.",
             "", "### Required artifacts",
             "| id | path | when | draft→final | declarable |", "|---|---|---|---|---|"]
    for a in arts:
        lines.append(f"| `{a['id']}` | `{a['path']}` | {_cond(a['_flag'])} | r{a.get('first_draft_round')}→"
                     f"r{a.get('finalize_round')} | {'yes' if a.get('scope_declarable') else '**NO**'} |")
    lines += ["", "### Effort floors — do each of these (checked from round 2)"]
    lines += [floor_instruction(f) for f in floors()]
    lines += ["", "### Schemas"]
    for a in arts:
        card = ARTIFACT_SCHEMAS.get(a["id"])
        if card:
            lines += [f"**`{a['path']}`**", card, ""]
    lines.append(EXTRA_CONTRACTS)
    return "\n".join(lines).strip() + "\n"


def _self_test() -> int:
    failures = []
    def ok(c, m):
        if not c:
            failures.append(m)
    p = build_packet("model")
    ids = [a["id"] for a in required_artifacts("model")]
    ok(len(ids) >= 10, f"expected ≥10 model/allocation artifacts, got {ids}")
    for i in ids:
        ok(f"`{i}`" in p, f"packet missing artifact id {i}")
        ok(i in ARTIFACT_SCHEMAS or i in ("benchmark_targets",), f"no schema card for {i}")
    import sys as _sys
    _sys.path.insert(0, os.path.join(REPO, "scripts"))
    import effort_floors as _ef
    for f in floors():
        line = next((l for l in p.splitlines() if l.startswith(f"- `{f['id']}`")), None)
        ok(line is not None, f"packet missing instruction for floor {f['id']}")
        if not line:
            continue
        if f.get("probe") != "ast_shortcut_scan":
            ok(f"`{f['artifact']}" in line, f"floor {f['id']}: instruction does not name {f['artifact']}")
        for fld in str(f["field"]).split("|"):
            for part in [x.strip(".") for x in fld.strip().split("[*]") if x.strip(".") and fld.strip() not in ("-", "model_files")]:
                ok(part in line, f"floor {f['id']}: instruction does not name probed field {part!r}")
        if f.get("probe") in ("yaml_field", "yaml_field_list_length"):
            ok(f"≥ {f['minimum']}" in line, f"floor {f['id']}: minimum {f['minimum']} missing")
        ok(f["severity"] in line, f"floor {f['id']}: severity missing")
        ok(("not declarable" in line) == (not f.get("scope_declarable")), f"floor {f['id']}: declarability wrong")
    sc = next(l for l in p.splitlines() if l.startswith("- `shortcut_markers_in_model_code`"))
    for k, v in _ef._SHORTCUT_LITERAL_NAMES.items():
        ok(f"{k}<{v}" in sc, f"shortcut rule missing {k}<{v}")
    ok("1 value each" not in p, "sensitivity card still contradicts the perturbation floor")
    ok("iteration BUDGET" in p and "even if your own code also writes JSON" in p, "calibration_result card gaps")
    toks = len(p) / 4
    ok(toks <= 6000, f"packet ~{toks:.0f} tokens > 6000 budget")
    ok(not re.search(r"\b(malaria|nigeria|lgas?|smc|llin|pfpr|measles|kenya)\b", p, re.I),
       "packet is not disease-agnostic")
    no_alloc = build_packet("model", applicable=set())
    ok("`decision_rule`" not in no_alloc and "`outcome_fn`" in no_alloc, "applicable filter broken")
    for kw in ("loss_type", "n_obs", "held_out_fold", "load_bearing_parameters", "eligible_population_field",
               "param_frozen_in_uq", "Pre-mortem Responses", "validate_figure", "not_applicable"):
        ok(kw in p, f"packet missing key contract term {kw}")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - ")
        return 1
    print(f"OK: all self-test cases passed. (packet ~{toks:.0f} tokens, {len(ids)} artifacts, "
          f"{len(floors())} floors)")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    app = None
    if "--no-allocation" in sys.argv or "--aggregation" in sys.argv:
        app = set() if "--no-allocation" in sys.argv else {"allocation"}
        if "--aggregation" in sys.argv:
            app.add("aggregation")
    print(build_packet("model", applicable=app))
