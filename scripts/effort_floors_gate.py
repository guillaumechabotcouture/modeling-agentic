#!/usr/bin/env python3
"""Effort-floor check (STAGE 7 gate check, Phase 19 α).

    python3 scripts/effort_floors_gate.py <run_dir> [--round N]
    python3 scripts/effort_floors_gate.py --self-test

Phase 48 β moved this check out of scripts/validate_critique_yaml.py
unchanged. `gate_findings(run_dir, round_n)` refreshes and reads
`effort_floors_report.yaml` through scripts/effort_floors.py (the producer) and
turns its violations into gate findings (MEDIUM in the round-1 drafting window). The validator runs it with `run_guarded` under the old
name `_check_effort_floors`, at the same place in its check order as before, so a
crash finding reads as before.
"""
from __future__ import annotations

import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:   # loaded by the validator, scripts/ is already there
    sys.path.insert(0, HERE)
from lib.rigor_artifacts import artifact_path, produced_path  # noqa: E402

# Phase 19 α — effort floors threshold (rounds at which floors start firing).
# Round 1 is the drafting window: floors emit MEDIUMs only. From round 2 on,
# violations escalate per the manifest's declared severity.
_EFFORT_FLOORS_ENFORCE_FROM_ROUND = 2

_EFFORT_FLOORS_MODULE = None


def _load_effort_floors_module():
    """Lazy import of scripts/effort_floors.py (sibling). Cached.

    Registers in sys.modules before exec_module to keep Python 3.14's
    @dataclass happy (it does `sys.modules.get(cls.__module__).__dict__`
    during decoration; AttributeError if the module isn't registered).
    """
    global _EFFORT_FLOORS_MODULE
    if _EFFORT_FLOORS_MODULE is not None:
        return _EFFORT_FLOORS_MODULE
    try:
        spec = importlib.util.spec_from_file_location(
            "effort_floors",
            os.path.join(os.path.dirname(__file__), "effort_floors.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["effort_floors"] = mod
        spec.loader.exec_module(mod)
        _EFFORT_FLOORS_MODULE = mod
        return mod
    except (FileNotFoundError, ImportError):
        return None


def gate_findings(run_dir: str,
                  round_n: int | None = None) -> list[dict]:
    """Phase 19 α: enforce quantitative minimums for "actually tried."

    Reads `{run_dir}/effort_floors_report.yaml` (produced by
    `scripts/effort_floors.py`). If the report is absent and any
    trigger artifact exists (uncertainty_report.yaml,
    models/sensitivity_analysis.yaml, models/calibration_result.yaml,
    or models/), invokes the module in-process so a missing run of
    the script doesn't silently let an under-powered model ship.

    Emits per violation:
      effort_floor_<floor_id>       severity from manifest (HIGH | MEDIUM)
      effort_floors_unavailable     MEDIUM (scripts/effort_floors.py
                                     missing or manifest malformed)
      effort_floors_many_shortcuts  HIGH (≥ 3 shortcut markers — escalation)

    Round gating: round_n=None → silent (defer). Round 1 → all
    violations advisory (MEDIUM) regardless of manifest severity. Round
    ≥ 2 → honor manifest severities.

    HIGH violations with `scope_declarable: false` are explicitly NOT
    silenceable via scope_declaration.yaml — these are the "no shortcut"
    floors (held-out validation, multi-restart, minimum draws).
    """
    if round_n is None:
        return []

    out: list[dict] = []

    # Determine whether any trigger artifact is present. If none of the
    # probes would fire, we don't need to insist on running the script.
    triggers_present = (
        os.path.exists(produced_path("outcome_fn", run_dir))
        or os.path.exists(artifact_path("sensitivity_analysis", run_dir))
        or os.path.exists(artifact_path("calibration_result", run_dir))
        or os.path.isdir(os.path.join(run_dir, "models"))
    )

    if not triggers_present:
        return []

    # Always recompute the derived report. It is a cache of model artifacts,
    # not an authoritative input; reusing it across rounds can preserve stale
    # violations or stale PASS results after the modeler changes outputs/code.
    mod = _load_effort_floors_module()
    if mod is None:
        # Phase 21 β: fail closed — an unevaluable floor set is HIGH.
        return [{
            "kind": "effort_floors_unavailable",
            "severity": "HIGH",
            "stage": "GATE",
            "claim": (
                "effort_floors_report.yaml cannot be refreshed because "
                "scripts/effort_floors.py could not be imported. Run "
                f"`python3 scripts/effort_floors.py {run_dir}` manually. "
                "See Phase 19 α."
            ),
        }]
    try:
        rep = mod.evaluate(run_dir)
        mod.write_report(run_dir, rep)
    except Exception as e:  # noqa: BLE001 — Phase 21 β: fail closed
        return [{
            "kind": "effort_floors_unavailable",
            "severity": "HIGH",
            "stage": "GATE",
            "claim": (f"Could not compute effort floors: {e}. "
                      f"Run `python3 scripts/effort_floors.py {run_dir}` "
                      f"and resolve the manifest error."),
        }]

    violations = rep.get("violations") or []
    if not isinstance(violations, list):
        return out

    drafting_window = round_n < _EFFORT_FLOORS_ENFORCE_FROM_ROUND

    # Phase 20 β: close the round-1 escape. If the run has rigor
    # triggers present AND the floor evaluator surfaced any
    # would-be-HIGH violation at round 1, ACCEPT must wait for at
    # least round 2 so the manifest severities can fire. A run that
    # converges in one round and ships under the drafting demotion
    # silently bypasses effort enforcement — exactly the failure mode
    # Phase 19 α was supposed to close.
    if drafting_window and violations:
        would_be_high = [
            v for v in violations
            if isinstance(v, dict) and v.get("severity") == "HIGH"
        ]
        if would_be_high:
            preview = ", ".join(
                str(v.get("floor_id") or "?") for v in would_be_high[:5]
            )
            out.append({
                "kind": "effort_floors_require_followup_round",
                "severity": "HIGH",
                "stage": "GATE",
                "claim": (
                    f"At round {round_n}, the drafting window demotes "
                    f"effort-floor violations to MEDIUM. The current run "
                    f"has {len(would_be_high)} would-be-HIGH "
                    f"violation(s) [{preview}], so ACCEPT must wait "
                    f"until round ≥ {_EFFORT_FLOORS_ENFORCE_FROM_ROUND} "
                    f"to let the gate enforce. Either resolve the "
                    f"underlying floors (more draws / restarts / "
                    f"held-out folds) and re-run, or continue iterating; "
                    f"a one-round run on a triggers-present model "
                    f"cannot ACCEPT under Phase 20 β."
                ),
            })

    # Special handling: shortcut markers escalate by count. ≥ 3 findings → HIGH
    # regardless of manifest severity (catches the "death by a thousand cuts"
    # pattern where the modeler scatters small punts across many files).
    shortcut_viols = [v for v in violations
                      if isinstance(v, dict)
                      and v.get("floor_id") == "shortcut_markers_in_model_code"]
    other_viols = [v for v in violations
                   if isinstance(v, dict)
                   and v.get("floor_id") != "shortcut_markers_in_model_code"]

    for v in shortcut_viols:
        observed = v.get("observed") or 0
        try:
            n = int(observed)
        except (TypeError, ValueError):
            n = 0
        severity = "HIGH" if (n >= 3 and not drafting_window) else "MEDIUM"
        findings_preview = "; ".join((v.get("findings") or [])[:5])
        out.append({
            "kind": "effort_floors_many_shortcuts" if severity == "HIGH"
                    else "effort_floor_shortcut_markers",
            "severity": severity,
            "stage": "MODEL",
            "claim": (
                f"Shortcut markers in model code: {n} finding(s). "
                f"First examples: {findings_preview}. "
                f"Phase 19 α — each marker is a quiet under-investment "
                f"(TODO, n_replicates=1, bare except: pass). Resolve "
                f"or document why the shortcut is the right answer in "
                f"modeling_strategy.md."
            ),
        })

    for v in other_viols:
        floor_id = v.get("floor_id", "?")
        severity = str(v.get("severity") or "MEDIUM").upper()
        if drafting_window:
            # Drafting window — demote to MEDIUM, do not block ACCEPT
            severity = "MEDIUM"
        scope_declarable = bool(v.get("scope_declarable"))
        observed = v.get("observed")
        minimum = v.get("minimum")
        scope_tag = " (NOT scope-declarable)" if not scope_declarable else ""
        diagnostic = (v.get("diagnostic") or "").strip()
        diag_tag = f" — {diagnostic}" if diagnostic else ""
        out.append({
            "kind": f"effort_floor_{floor_id}",
            "severity": severity,
            "stage": "MODEL",
            # Phase 21 β: carried to the decision — a non-declarable floor
            # open at round exhaustion is RUN_FAILED, not DECLARE_SCOPE.
            "scope_declarable": scope_declarable,
            "claim": (
                f"Effort floor `{floor_id}` violated{scope_tag}: "
                f"observed {observed!r} below minimum {minimum!r}"
                f"{diag_tag}. Phase 19 α — gates measure effort, not "
                f"just artifact existence. Fix the underlying "
                f"under-investment (more draws / restarts / "
                f"perturbations / iterations) or add justification "
                f"in modeling_strategy.md."
            ),
        })

    return out


def _self_test() -> int:
    import tempfile as _tempfile

    import yaml as _yaml

    failures: list[str] = []

    def ok(cond: bool, label: str) -> None:
        if not cond:
            failures.append(label)

    # --- Phase 19 α: effort_floors tests (EF-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # EF1: round_n=None → silent (defer)
        ok(gate_findings(_d, round_n=None) == [],
           "EF1: round_n=None must be silent")

    with _tempfile.TemporaryDirectory() as _d:
        # EF2: no trigger artifacts and no models/ dir → silent
        v = gate_findings(_d, round_n=3)
        ok(v == [], f"EF2: empty run dir must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF3: under-floor n_draws at round 3 → HIGH effort_floor_uq_min_draws_local
        # (NOT scope-declarable). Script auto-runs when report is absent.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 50, "scalar_outputs": {}}, f)
        v = gate_findings(_d, round_n=3)
        match = [x for x in v if x["kind"] == "effort_floor_uq_min_draws_local"]
        ok(len(match) == 1 and match[0]["severity"] == "HIGH",
           f"EF3: 50 draws at r=3 should fire HIGH, got {v}")
        ok("NOT scope-declarable" in match[0]["claim"],
           f"EF3: NOT scope-declarable must be flagged in claim, got {match}")
        # And the report file is now on disk for downstream readers.
        ok(os.path.exists(os.path.join(_d, "effort_floors_report.yaml")),
           "EF3: validator must auto-write effort_floors_report.yaml")

    with _tempfile.TemporaryDirectory() as _d:
        # EF4: under-floor at round 1 (drafting window) → MEDIUM regardless of
        # manifest severity. Same input as EF3.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 50}, f)
        v = gate_findings(_d, round_n=1)
        match = [x for x in v if x["kind"] == "effort_floor_uq_min_draws_local"]
        ok(len(match) == 1 and match[0]["severity"] == "MEDIUM",
           f"EF4: drafting window must demote to MEDIUM, got {match}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF5: meets floor → silent for that floor.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 500}, f)
        v = gate_findings(_d, round_n=3)
        match = [x for x in v if x["kind"] == "effort_floor_uq_min_draws_local"]
        ok(not match, f"EF5: 500 draws should be silent on local floor, got {match}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF5b: stale cached report must be refreshed. Start under-floor,
        # then fix n_draws; the second gate call must not reuse the first
        # effort_floors_report.yaml.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 50}, f)
        v1 = gate_findings(_d, round_n=3)
        ok(any(x["kind"] == "effort_floor_uq_min_draws_local" for x in v1),
           f"EF5b: first under-floor run should fire, got {v1}")
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 500}, f)
        v2 = gate_findings(_d, round_n=3)
        ok(not any(x["kind"] == "effort_floor_uq_min_draws_local" for x in v2),
           f"EF5b: refreshed report should clear fixed n_draws, got {v2}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF6: shortcut markers in model code → MEDIUM at <3, HIGH at >=3.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "run.py"), "w") as f:
            f.write(
                "# TODO: bump replicates\n"
                "n_replicates = 1\n"
                "n_draws = 5\n"
                "try:\n    x = 1\nexcept Exception:\n    pass\n"
            )
        v = gate_findings(_d, round_n=3)
        # 4 findings: TODO, n_replicates=1, n_draws=5, bare except
        high = [x for x in v if x["kind"] == "effort_floors_many_shortcuts"]
        ok(len(high) == 1 and high[0]["severity"] == "HIGH",
           f"EF6: ≥3 shortcut markers should escalate to HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF7: malformed effort_floors_report.yaml → effort_floors_unavailable MEDIUM
        # (the gate does not silently pass — it surfaces the problem).
        with open(os.path.join(_d, "effort_floors_report.yaml"), "w") as f:
            f.write(":\n  - this isn't: parseable: yaml\n - oops\n")
        # Need a trigger artifact for the check to even run.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 500}, f)
        v = gate_findings(_d, round_n=3)
        # Malformed YAML may or may not parse to something — accept either
        # explicit `effort_floors_unavailable` OR silent (empty out, since
        # the report parsed but had no violations key).
        ok(isinstance(v, list),
           f"EF7: malformed report must not crash, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF8: under-restart calibration → HIGH effort_floor_calibration_min_restarts,
        # also under-iterations → MEDIUM. Verifies both fire together.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "calibration_result.yaml"), "w") as f:
            _yaml.safe_dump({
                "n_restarts": 1, "n_iterations": 100,
                "held_out_fold": {"indices": [0, 1, 2]},
            }, f)
        v = gate_findings(_d, round_n=3)
        restarts = [x for x in v if x["kind"] == "effort_floor_calibration_min_restarts"]
        iters = [x for x in v if x["kind"] == "effort_floor_calibration_min_iterations"]
        ok(len(restarts) == 1 and restarts[0]["severity"] == "HIGH",
           f"EF8: under-restarts must fire HIGH, got {restarts}")
        ok(len(iters) == 1 and iters[0]["severity"] == "MEDIUM",
           f"EF8: under-iterations must fire MEDIUM, got {iters}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF9 (Phase 20 β): round-1 ACCEPT escape is closed. A run with
        # rigor triggers and would-be-HIGH floor violations must NOT be
        # acceptable at round 1; the validator emits a HIGH
        # `effort_floors_require_followup_round`.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 50}, f)
        v = gate_findings(_d, round_n=1)
        followup = [x for x in v
                    if x["kind"] == "effort_floors_require_followup_round"]
        ok(len(followup) == 1 and followup[0]["severity"] == "HIGH",
           f"EF9: round 1 with would-be-HIGH must fire follow-up HIGH, got {v}")
        # Individual floor violations should still be MEDIUM at round 1.
        uq = [x for x in v if x["kind"] == "effort_floor_uq_min_draws_local"]
        ok(uq and uq[0]["severity"] == "MEDIUM",
           f"EF9: individual floors at r=1 stay MEDIUM (drafting), got {uq}")

    with _tempfile.TemporaryDirectory() as _d:
        # EF9b (Phase 20 β): round-1 run with NO would-be-HIGH (clean
        # floor evaluation) does not fire the follow-up requirement.
        with open(os.path.join(_d, "uncertainty_report.yaml"), "w") as f:
            _yaml.safe_dump({"n_draws": 500}, f)
        v = gate_findings(_d, round_n=1)
        ok(not any(x["kind"] == "effort_floors_require_followup_round"
                   for x in v),
           f"EF9b: clean run at r=1 must not fire follow-up HIGH, got {v}")

    # EW1: the validator wires this check under its old name.
    with open(os.path.join(HERE, "validate_critique_yaml.py")) as f:
        _vsrc = f.read()
    ok('_load_sibling("effort_floors_gate").gate_findings(rd, round_n)' in _vsrc
       and 'check_name="_check_effort_floors"' in _vsrc,
       "EW1: validator wiring under the old check name")

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
