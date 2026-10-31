#!/usr/bin/env python3
"""A-priori identifiability check (STAGE 7 gate check, Phase 15 α / 21 β).

    python3 scripts/identifiability_gate.py <run_dir> [--round N]
    python3 scripts/identifiability_gate.py --self-test

Phase 48 β moved this check out of scripts/validate_critique_yaml.py
unchanged. `gate_findings(run_dir, round_n)` checks
`models/identifiability_a_priori.yaml` (required from round 2, not
scope-declarable at OVER_SATURATED); `count_findings(run_dir, round_n)` holds its
counts to the built model (identifiability_a_priori.cross_check_counts). The validator runs it with `run_guarded` under the old
name `_check_identifiability_a_priori` and
`_check_a_priori_counts`, at the same place in its check order as before, so a
crash finding reads as before.
"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:   # loaded by the validator, scripts/ is already there
    sys.path.insert(0, HERE)
from lib.rigor_artifacts import artifact_path  # noqa: E402
from lib.siblings import load_sibling  # noqa: E402

# Phase 15 Commit α: a-priori identifiability arithmetic.
#
# The 224202 run shipped a HYBRID model with 40 fitted parameters
# fitting 6 zone-level PfPR targets — over-saturated by 6.7×, yet
# the issue was only caught post-hoc at STAGE 5b RIGOR (round 3+).
# By that point the architecture was sunk cost; the modeler scope-
# declared 3 unidentified parameters and proceeded with a decorative
# ABM (predictions equivalent to PfPR × literature_OR).
#
# Phase 15 α requires a PRE-MODEL artifact with parameters/targets
# arithmetic, before the FIRST model build. Verdict OVER_SATURATED
# without a resolution decision blocks ACCEPT (HIGH). Critically,
# this kind is NOT scope-declarable — the modeler must fix the
# architecture, add data, or downgrade to the analytical model.
_IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND = 2


def gate_findings(run_dir: str,
                  round_n: int | None = None
                  ) -> list[dict]:
    """Phase 15 α: validate models/identifiability_a_priori.yaml.

    Emits:
      identifiability_a_priori_missing       MEDIUM @ r=2, HIGH @ r≥3
      identifiability_a_priori_invalid       HIGH (yaml MALFORMED)
      pre_model_over_saturated               HIGH (OVER_SATURATED
                                                   without resolution)
      pre_model_over_saturated_with_commitment
                                             MEDIUM (OVER_SATURATED
                                                     with documented
                                                     resolution.decision)
      pre_model_marginal_identifiability     MEDIUM (verdict MARGINAL)
      pre_model_decorative_undocumented      HIGH (resolution=
                                                   accept_decorative
                                                   without details)

    Round gating: returns [] when round_n is None (caller has not
    supplied a round; defer) or round_n < required_round (drafting
    window). Applies uniformly to missing-artifact AND present-artifact
    branches — the artifact is in flux during round 1 and shouldn't
    fire MEDIUM advisories until the modeler has had a full round to
    refine it.

    The pre_model_* kinds are NOT scope-declarable via
    sanity_check_acknowledged or scope_declaration.yaml. Architecture
    choice is inside pipeline reach; the modeler must redesign,
    not declare scope. Concretely, this function intentionally does
    NOT call `_load_sanity_check_acknowledged` (the helper used by
    the sanity-schema check, scripts/sanity_gate.py, for opt-out filtering). The non-scope-
    declarable contract is enforced by structural omission of that
    call, not by a runtime check.
    """
    # Round gating applies uniformly to all branches. Round 1 is the
    # drafting window; round_n=None means the caller hasn't supplied a
    # round (defer). Both produce silent passes regardless of artifact
    # state — without this, MEDIUM advisories fire spuriously when a
    # planner produces a draft artifact at round 1.
    if round_n is None:
        return []
    if round_n < _IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND:
        return []

    yaml_path = artifact_path("identifiability_a_priori", run_dir)

    if not os.path.exists(yaml_path):
        # At r≥2 the artifact is required. Severity escalates to HIGH
        # at r≥3 to align with Phase 12 β's persistence ledger.
        severity = "HIGH" if round_n >= 3 else "MEDIUM"
        return [{
            "kind": "identifiability_a_priori_missing",
            "severity": severity,
            "stage": "PRE_MODEL",
            "claim": (
                f"Required artifact `models/identifiability_a_priori.yaml` "
                f"is absent at round {round_n} (≥ "
                f"{_IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND}). "
                f"Before committing to a model architecture, count "
                f"free fitted parameters vs independent calibration "
                f"targets. Verdict IDENTIFIABLE if ratio < 1, MARGINAL "
                f"if 1-3, OVER_SATURATED if > 3. See the `pre-model-"
                f"identifiability-arithmetic` skill and "
                f"`scripts/identifiability_a_priori.py --self-test` "
                f"for the schema. This artifact is NOT scope-"
                f"declarable — architecture choice is inside pipeline "
                f"reach."
            ),
        }]

    try:
        spec = importlib.util.spec_from_file_location(
            "identifiability_a_priori",
            os.path.join(os.path.dirname(__file__),
                         "identifiability_a_priori.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    # Bare `except Exception` matches the 8 other importlib loaders in
    # this file. A narrower (ImportError, OSError, AttributeError) tuple
    # would let SyntaxError escape and crash the validator if the loaded
    # script ever has invalid Python. KeyboardInterrupt/SystemExit are
    # not Exception subclasses, so they still propagate.
    except Exception as e:
        return [{
            "kind": "identifiability_a_priori_invalid",
            "severity": "HIGH",
            "stage": "PRE_MODEL",
            "claim": (f"Could not load "
                      f"scripts/identifiability_a_priori.py: {e}"),
        }]

    result = mod.validate_identifiability_a_priori(yaml_path)
    if result["verdict"] == "MALFORMED":
        errors = result.get("errors") or []
        # Distinguish the decorative-undocumented sub-case: the
        # validator's error message for accept_decorative-without-
        # details starts with "resolution.decision is 'accept_decorative'".
        # The over-saturated-without-any-resolution error is generic
        # ("verdict X requires a resolution field..."). Match the
        # specific decorative-sub-case prefix only.
        if any(
            e.lstrip().startswith(
                "resolution.decision is 'accept_decorative'")
            for e in errors
        ):
            return [{
                "kind": "pre_model_decorative_undocumented",
                "severity": "HIGH",
                "stage": "PRE_MODEL",
                "claim": (f"identifiability_a_priori.yaml declares "
                          f"resolution.decision='accept_decorative' "
                          f"without resolution.details text. Decorative "
                          f"architectures must be justified (100-300 "
                          f"words). Errors: {'; '.join(errors)}"),
            }]
        # Generic missing-resolution case: dispatch on computed_verdict
        # so the user-facing kind matches the actual problem (over-
        # saturation or marginality), not "malformed YAML". MARGINAL
        # missing-resolution is MEDIUM advisory; OVER_SATURATED is HIGH.
        missing_resolution = any(
            "requires a `resolution`" in e for e in errors)
        if (missing_resolution
                and result.get("computed_verdict") == "OVER_SATURATED"):
            s = result.get("summary", {})
            return [{
                "kind": "pre_model_over_saturated",
                "severity": "HIGH",
                "stage": "PRE_MODEL",
                "claim": (
                    f"Pre-model identifiability arithmetic flags "
                    f"OVER_SATURATED: {s.get('n_fitted', 0)} fitted "
                    f"parameters / {s.get('n_targets', 0)} independent "
                    f"calibration targets = "
                    f"{s.get('ratio', 0):.2f}× ratio (threshold > 3.0). "
                    f"resolution.decision is required. Pick one: "
                    f"(a) reduce params (tie across groups), "
                    f"(b) add independent calibration targets, "
                    f"(c) downgrade to analytical model, or "
                    f"(d) accept_decorative with 100-300 word justification. "
                    f"NOT scope-declarable."
                ),
            }]
        if (missing_resolution
                and result.get("computed_verdict") == "MARGINAL"):
            s = result.get("summary", {})
            return [{
                "kind": "pre_model_marginal_identifiability",
                "severity": "MEDIUM",
                "stage": "PRE_MODEL",
                "claim": (
                    f"Pre-model identifiability arithmetic flags "
                    f"MARGINAL: {s.get('n_fitted', 0)} fitted / "
                    f"{s.get('n_targets', 0)} targets = "
                    f"{s.get('ratio', 0):.2f}× ratio (1.0-3.0 band). "
                    f"`resolution` field is missing. The model is at "
                    f"risk for ridge-trapped parameters; document the "
                    f"mitigation (or acknowledge the risk) in "
                    f"resolution.decision."
                ),
            }]
        return [{
            "kind": "identifiability_a_priori_invalid",
            "severity": "HIGH",
            "stage": "PRE_MODEL",
            "claim": (f"models/identifiability_a_priori.yaml is "
                      f"malformed: {'; '.join(errors)}. NOT scope-"
                      f"declarable — fix the artifact."),
        }]

    if result["verdict"] == "OVER_SATURATED":
        # When validate_identifiability_a_priori returns verdict
        # OVER_SATURATED (not MALFORMED), the artifact has a documented
        # resolution decision — has_resolution is True by construction.
        # The over-saturated-without-resolution case returns MALFORMED
        # above and is dispatched there as HIGH pre_model_over_saturated.
        # So this branch always fires the MEDIUM commitment advisory.
        s = result.get("summary", {})
        return [{
            "kind": "pre_model_over_saturated_with_commitment",
            "severity": "MEDIUM",
            "stage": "PRE_MODEL",
            "claim": (
                f"Pre-model identifiability arithmetic flags "
                f"OVER_SATURATED: {s.get('n_fitted', 0)} fitted "
                f"/ {s.get('n_targets', 0)} targets = "
                f"{s.get('ratio', 0):.2f}×. Resolution committed "
                f"in artifact. Once redesign is implemented, "
                f"update identifiability_a_priori.yaml to show the "
                f"new fitted count and verdict IDENTIFIABLE."
            ),
        }]

    if result["verdict"] == "MARGINAL":
        s = result.get("summary", {})
        return [{
            "kind": "pre_model_marginal_identifiability",
            "severity": "MEDIUM",
            "stage": "PRE_MODEL",
            "claim": (
                f"Pre-model identifiability arithmetic flags "
                f"MARGINAL: {s.get('n_fitted', 0)} fitted / "
                f"{s.get('n_targets', 0)} targets = "
                f"{s.get('ratio', 0):.2f}× ratio (1.0-3.0 band). "
                f"The model is at risk for ridge-trapped parameters. "
                f"Post-hoc identifiability check at STAGE 5b will "
                f"confirm or refute. Surface as a caveat in "
                f"§Limitations."
            ),
        }]

    return []  # IDENTIFIABLE — silent pass


def count_findings(run_dir: str,
                   round_n: int | None = None) -> list[dict]:
    """Phase 21 β/δ: the a-priori identifiability counts must match the
    built model (identifiability_a_priori.cross_check_counts). 235631
    declared 17 params / 37 targets (IDENTIFIABLE) while the comparison
    manifest showed k=22 vs n=22. Gated like the a-priori artifact."""
    if round_n is None or round_n < _IDENTIFIABILITY_A_PRIORI_REQUIRED_FROM_ROUND:
        return []
    try:
        mod = load_sibling("identifiability_a_priori")
    except (FileNotFoundError, ImportError):
        return []
    fn = getattr(mod, "cross_check_counts", None)
    if fn is None:
        return []
    out = []
    for f in fn(run_dir) or []:
        if isinstance(f, dict):
            v = dict(f)
            v.setdefault("stage", "PRE_MODEL")
            out.append(v)
    return out


def _self_test() -> int:
    import tempfile as _tempfile

    import yaml as _yaml

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # === Phase 15 β: STAGE 3 a-priori identifiability self-tests ===
    # I1-I5 verify the pre-model gate fires correctly across the five
    # scenarios documented in the plan: missing artifact, OVER_SAT
    # without resolution, OVER_SAT with resolution, IDENTIFIABLE,
    # scope-declared (which must NOT silence the HIGH). The I-prefix
    # is for "identifiability" — Phase 5 ζ already used F1-F4 for
    # stuck-blocker tests and Phase 9 ρ used F1-F6 for figure-validator
    # tests, so the F-prefix is taken in this self-test block.

    with _tempfile.TemporaryDirectory() as _d:
        # I1: missing artifact at r=2 → MEDIUM identifiability_a_priori_missing
        v = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "identifiability_a_priori_missing"
               and x["severity"] == "MEDIUM" for x in v),
           f"I1: missing artifact at r=2 should fire MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I1b: missing artifact at r≥3 escalates to HIGH
        v = gate_findings(_d, round_n=3)
        ok(any(x["kind"] == "identifiability_a_priori_missing"
               and x["severity"] == "HIGH" for x in v),
           f"I1b: missing artifact at r=3 should fire HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I2: OVER_SATURATED without resolution → HIGH pre_model_over_saturated
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED"}, f)
        v = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated"
               and x["severity"] == "HIGH" for x in v),
           f"I2: OVER_SAT no-res should fire HIGH "
           f"pre_model_over_saturated, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I3: OVER_SATURATED with resolution.decision → MEDIUM advisory
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED",
                             "resolution": {"decision": "tie_params_by_ecotype",
                                            "details": "5 ecotype values"}}, f)
        v = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated_with_commitment"
               and x["severity"] == "MEDIUM" for x in v),
           f"I3: OVER_SAT with resolution should fire MEDIUM "
           f"with_commitment, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I3b: MARGINAL with resolution → MEDIUM pre_model_marginal_identifiability
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 12,
                             "verdict": "MARGINAL",
                             "resolution": {
                                 "decision": "tie_params_partial",
                                 "details": "ridge-trapping acknowledged"}},
                            f)
        v = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_marginal_identifiability"
               and x["severity"] == "MEDIUM" for x in v),
           f"I3b: MARGINAL verdict should fire MEDIUM "
           f"pre_model_marginal_identifiability, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I3c: MARGINAL without resolution → MEDIUM (not HIGH-invalid).
        # Severity must match the documented contract; missing resolution
        # is the same class of issue as having one — the difference is
        # only in the claim text.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 12,
                             "verdict": "MARGINAL"}, f)
        v = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_marginal_identifiability"
               and x["severity"] == "MEDIUM" for x in v),
           f"I3c: MARGINAL no-res should fire MEDIUM (not HIGH "
           f"identifiability_a_priori_invalid), got {v}")
        ok(not any(x["kind"] == "identifiability_a_priori_invalid"
                   for x in v),
           f"I3c: MARGINAL no-res must NOT fire HIGH invalid, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I4: IDENTIFIABLE → silent (no violations)
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 5,
                             "verdict": "IDENTIFIABLE"}, f)
        v = gate_findings(_d, round_n=2)
        ok(not any(x["kind"].startswith("pre_model_")
                   or x["kind"].startswith("identifiability_a_priori_")
                   for x in v),
           f"I4: IDENTIFIABLE should be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I5: structural enforcement check — verify the function does
        # NOT call _load_sanity_check_acknowledged. The inversion of
        # the scope-declare-anything semantics for pre_model_* kinds
        # is enforced by structural omission of that call (no runtime
        # check). A behavioral test can't catch the regression directly
        # because writing scope_declaration.yaml has zero effect on a
        # function that doesn't read it. So we AST-inspect the
        # function: a Call node referencing _load_sanity_check_
        # acknowledged would fail this assertion. Mere docstring
        # mentions of the helper (e.g., to explain WHY it's omitted)
        # are tolerated because the AST sees them as Constant strings,
        # not Calls.
        import ast as _ast
        import inspect as _inspect
        import textwrap as _textwrap
        _src = _textwrap.dedent(
            _inspect.getsource(gate_findings))
        _tree = _ast.parse(_src)
        _calls_loader = any(
            isinstance(node, _ast.Call)
            and isinstance(node.func, _ast.Name)
            and node.func.id == "_load_sanity_check_acknowledged"
            for node in _ast.walk(_tree)
        )
        ok(not _calls_loader,
           "I5: gate_findings must NOT call "
           "_load_sanity_check_acknowledged. Pre-model arithmetic is "
           "non-scope-declarable; adding the loader call would silently "
           "allow scope_declaration.yaml to silence "
           "pre_model_over_saturated.")
        # Companion behavioral check: even with scope_declaration.yaml
        # listing the kind, the gate fires HIGH. Pairs with the source
        # check above — together they catch both static (loader added)
        # and dynamic (loader added but gated) regressions.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED"}, f)
        with open(os.path.join(_d, "scope_declaration.yaml"), "w") as f:
            _yaml.safe_dump({"sanity_check_acknowledged": [
                "pre_model_over_saturated",
                "identifiability_a_priori_missing",
            ]}, f)
        v = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "pre_model_over_saturated"
               and x["severity"] == "HIGH" for x in v),
           f"I5b: scope_declared on kind should NOT silence HIGH "
           f"pre_model_over_saturated (architecture is inside pipeline "
           f"reach), got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # I6: round gating — present artifact with non-IDENTIFIABLE
        # verdict at round_n=1 (drafting window) or round_n=None (caller
        # didn't supply round) must be silent. Without this, the
        # planner's round-1 draft fires spurious MEDIUM advisories.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models",
                               "identifiability_a_priori.yaml"), "w") as f:
            _yaml.safe_dump({"total_independent_targets": 6,
                             "total_fitted_parameters": 40,
                             "verdict": "OVER_SATURATED",
                             "resolution": {"decision": "tie_params_by_ecotype",
                                            "details": "5 ecotypes"}}, f)
        ok(gate_findings(_d, round_n=1) == [],
           "I6: round_n=1 must be silent even for OVER_SATURATED artifact")
        ok(gate_findings(_d, round_n=None) == [],
           "I6: round_n=None must be silent even for OVER_SATURATED artifact")

    # IW1: the validator wires both checks under their old names.
    with open(os.path.join(HERE, "validate_critique_yaml.py")) as f:
        _vsrc = f.read()
    ok('_load_sibling("identifiability_gate").gate_findings(rd, round_n)' in _vsrc
       and 'check_name="_check_identifiability_a_priori"' in _vsrc
       and '_load_sibling("identifiability_gate").count_findings(rd, round_n)' in _vsrc
       and 'check_name="_check_a_priori_counts"' in _vsrc,
       "IW1: validator wiring under the old check names")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        print(f"FAILED: {len(failures)} self-test case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    from lib import gate_cli
    return gate_cli.run(argv, __doc__, gate_findings, _self_test, fails=gate_cli.any_high, round_default=None)


if __name__ == "__main__":
    sys.exit(main())
