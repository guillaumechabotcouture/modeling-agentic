#!/usr/bin/env python3
"""Benchmark-locked acceptance (STAGE 7 gate check, Phase 19 γ).

    python3 scripts/benchmark_gate.py <run_dir> [--round N]
    python3 scripts/benchmark_gate.py --self-test

Phase 46 β moved this check out of scripts/validate_critique_yaml.py
unchanged. `gate_findings(run_dir, round_n)` refreshes benchmark_match.yaml
through scripts/benchmark_match.py (the producer) and turns its result into
gate findings; the validator runs it with `run_guarded` under the name
`_check_benchmark_match`, at the same place in its check order as before.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:   # loaded by the validator, scripts/ is already there
    sys.path.insert(0, HERE)
from lib.rigor_artifacts import artifact_path  # noqa: E402

# Phase 19 γ — benchmark-locked acceptance. Round 1 is drafting; from round 2,
# DRIFT escalates to HIGH (scope-declarable). missing_computed is HIGH from
# round 2 — the modeler must populate computed_value/computed_field.
_BENCHMARK_MATCH_ENFORCE_FROM_ROUND = 2

_BENCHMARK_MATCH_MODULE = None

# benchmark_match.py's verdicts, as validate_critique_yaml._PRODUCER_VERDICTS lists them
# (self-test BG1 here and BW1 there hold the three equal).
KNOWN_VERDICTS = ("PASS", "DRIFT", "MALFORMED", "ABSENT", "PENDING")


def _load_benchmark_match_module():
    """Lazy import of scripts/benchmark_match.py. Cached."""
    global _BENCHMARK_MATCH_MODULE
    if _BENCHMARK_MATCH_MODULE is not None:
        return _BENCHMARK_MATCH_MODULE
    try:
        spec = importlib.util.spec_from_file_location(
            "benchmark_match",
            os.path.join(os.path.dirname(__file__), "benchmark_match.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["benchmark_match"] = mod
        spec.loader.exec_module(mod)
        _BENCHMARK_MATCH_MODULE = mod
        return mod
    except (FileNotFoundError, ImportError):
        return None


def gate_findings(run_dir: str,
                  round_n: int | None = None) -> list[dict]:
    """Phase 19 γ: enforce literature-anchored acceptance band.

    Reads `{run_dir}/benchmark_match.yaml` (produced by
    `scripts/benchmark_match.py`). If the report is absent but
    `models/benchmark_targets.yaml` exists, the script is invoked in-
    process so the gate is not silently skippable.

    Emits:
      benchmark_targets_missing      MEDIUM @ r=1, HIGH @ r≥2 (planner
                                      forgot to extract the table)
      benchmark_match_invalid        HIGH (MALFORMED targets)
      benchmark_drift                HIGH per drifted target (scope-
                                      declarable — domain heuristic)
      benchmark_computed_missing     HIGH per target with no
                                      computed_value/computed_field
      benchmark_match_unavailable    MEDIUM (script unimportable)

    Round gating:
      round_n=None → silent
      round 1      → demote all violations to MEDIUM (drafting)
      round ≥ 2    → enforce per-kind severities
    """
    if round_n is None:
        return []

    targets_path = artifact_path("benchmark_targets", run_dir)
    drafting = round_n < _BENCHMARK_MATCH_ENFORCE_FROM_ROUND
    out: list[dict] = []

    # Case 1: no targets file at all. Modeler hasn't (yet) been asked to
    # build against benchmarks. At round 1 silent. At round ≥ 2,
    # emit MEDIUM benchmark_targets_missing so the planner notices.
    if not os.path.exists(targets_path):
        if drafting:
            return []
        return [{
            "kind": "benchmark_targets_missing",
            "severity": "MEDIUM",
            "stage": "PLAN",
            "claim": (
                "models/benchmark_targets.yaml is absent at round "
                f"{round_n}. Phase 19 γ — the planner must extract "
                "the `Published benchmarks` table from plan.md into "
                "structured YAML so the modeler's computed outputs "
                "can be compared against literature ranges. See the "
                "`benchmark-locked-acceptance` skill. Without this "
                "file the harness has no mechanical way to check that "
                "fitted incidences/prevalences/coverages match "
                "published reality."
            ),
        }]

    # Case 2: targets exist. Always resolve a fresh report; the YAML is a
    # derived cache and can go stale when computed_value or computed_field
    # targets change between critique rounds.
    mod = _load_benchmark_match_module()
    if mod is None:
        return [{
            "kind": "benchmark_match_unavailable",
            "severity": "HIGH",  # Phase 21 β: fail closed
            "stage": "GATE",
            "claim": (
                "benchmark_match.yaml cannot be refreshed because "
                "scripts/benchmark_match.py could not be imported. Run "
                f"`python3 scripts/benchmark_match.py {run_dir}` "
                "manually. Phase 19 γ."
            ),
        }]
    try:
        rep = mod.evaluate(run_dir)
        mod.write_report(run_dir, rep)
    except Exception as e:  # noqa: BLE001 — Phase 21 β: fail closed
        return [{
            "kind": "benchmark_match_unavailable",
            "severity": "HIGH",
            "stage": "GATE",
            "claim": f"benchmark_match evaluation failed: {e}",
        }]

    verdict = str(rep.get("verdict") or "PENDING").upper()
    if verdict not in KNOWN_VERDICTS:
        return [{
            "kind": "benchmark_match_unknown_verdict",
            "severity": "HIGH",
            "stage": "GATE",
            "scope_declarable": False,
            "claim": (f"benchmark_match returned verdict {verdict!r}, outside "
                      f"{list(KNOWN_VERDICTS)}."),
        }]
    if verdict == "MALFORMED":
        sev = "MEDIUM" if drafting else "HIGH"
        return [{
            "kind": "benchmark_match_invalid",
            "severity": sev,
            "stage": "MODEL",
            "claim": (
                f"benchmark inputs are MALFORMED: "
                f"{rep.get('error', 'see benchmark_match.yaml')}. "
                f"Phase 19 γ — fix the schema (each benchmark needs "
                f"id, metric, target_value, tolerance_factor or "
                f"tolerance_abs, source)."
            ),
        }]

    results = rep.get("results")
    if results is None:
        results = []
    if not isinstance(results, list):
        return [{
            "kind": "benchmark_match_invalid",
            "severity": "HIGH",
            "stage": "MODEL",
            "claim": "benchmark_match result `results` must be a list.",
        }]

    # Per-target violations (one HIGH per drifted target; HIGH per
    # missing-computed target). At round 1 → demote to MEDIUM.
    drifted = []
    missing_computed = []
    for r in results:
        if not isinstance(r, dict):
            continue
        rid = r.get("id", "?")
        observed = r.get("observed")
        within = r.get("within_band")
        if observed is None:
            missing_computed.append(r)
            continue
        # Phase 21 β: anything but an explicit True is drift (a missing or
        # non-boolean within_band must not pass silently).
        if within is not True:
            drifted.append(r)

    # Phase 45 γ: a frozen copy in the locked targets file that would have
    # passed where the current computed value does not is named in the
    # drift / not-computed finding it caused. Other stale copies stay in
    # benchmark_match.yaml: the targets file is locked, so they cannot be
    # removed, and they change no verdict.
    stale = {str(x.get("id")): x for x in (rep.get("stale_copies") or [])
             if isinstance(x, dict) and x.get("severity") == "HIGH"}

    def _stale_note(rid) -> str:
        x = stale.get(str(rid))
        return (f" The locked models/benchmark_targets.yaml still holds {x.get('targets_value')}, which is in "
                f"band; that copy is stale and is not used." if x else "")

    # Emit. Cap each list at 5 to avoid swamping the gate (Phase 10 ψ
    # consolidation pattern).
    for r in drifted[:5]:
        rid = r.get("id", "?")
        sev = "MEDIUM" if drafting else "HIGH"
        out.append({
            "kind": "benchmark_drift",
            "severity": sev,
            "stage": "MODEL",
            "claim": (
                f"Benchmark `{rid}` ({r.get('metric', '?')}): observed "
                f"{r.get('observed')} outside band [{r.get('band_low')}, "
                f"{r.get('band_high')}] (target {r.get('target_value')}, "
                f"source {r.get('source', '?')}). Phase 19 γ — fitted "
                f"output does not match published reality. Refit, "
                f"widen the tolerance with cited justification, or "
                f"scope-declare with a literature-grounded reason."
                + _stale_note(rid)
            ),
        })
    if len(drifted) > 5:
        out.append({
            # Phase 21 β: overflow stays a blocker at the same severity.
            "kind": "benchmark_drift",
            "severity": "MEDIUM" if drafting else "HIGH",
            "stage": "MODEL",
            "claim": (
                f"{len(drifted) - 5} additional benchmark(s) drifted; "
                f"see benchmark_match.yaml for the full list."
            ),
        })

    for r in missing_computed[:5]:
        rid = r.get("id", "?")
        sev = "MEDIUM" if drafting else "HIGH"
        out.append({
            "kind": "benchmark_computed_missing",
            "severity": sev,
            "stage": "MODEL",
            "claim": (
                f"Benchmark `{rid}` ({r.get('metric', '?')}): no computed "
                f"value. Phase 19 γ — after the model run, record it in "
                f"models/benchmark_computed.yaml (`computed_value` or "
                f"`computed_field`) so the comparison against "
                f"{r.get('source', 'the source')} can fire. Diagnostic: "
                f"{r.get('diagnostic', '')}" + _stale_note(rid)
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

    mod = _load_benchmark_match_module()
    ok(mod is not None and tuple(mod.VERDICTS) == KNOWN_VERDICTS,
       "BG1: KNOWN_VERDICTS is benchmark_match.VERDICTS")

    # --- Phase 19 γ: benchmark_match tests (BM-prefix) ---
    with _tempfile.TemporaryDirectory() as _d:
        # BM1: round_n=None → silent
        ok(gate_findings(_d, round_n=None) == [],
           "BM1: round_n=None must be silent")

    with _tempfile.TemporaryDirectory() as _d:
        # BM2: round 1, no benchmark_targets.yaml → silent (drafting)
        v = gate_findings(_d, round_n=1)
        ok(v == [], f"BM2: round 1 without targets must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM3: round 2, no benchmark_targets.yaml → MEDIUM benchmark_targets_missing
        v = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "benchmark_targets_missing"
               and x["severity"] == "MEDIUM" for x in v),
           f"BM3: missing targets at r=2 should fire MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM4: round 2, drifted benchmark → HIGH benchmark_drift
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 400,
                     "tolerance_factor": 1.5, "source": "WMR",
                     "computed_value": 100},  # outside [267, 600]
                ]
            }, f)
        v = gate_findings(_d, round_n=2)
        drift = [x for x in v if x["kind"] == "benchmark_drift"]
        ok(len(drift) == 1 and drift[0]["severity"] == "HIGH",
           f"BM4: drift at r=2 should be HIGH, got {v}")
        # auto-writes report
        ok(os.path.exists(os.path.join(_d, "benchmark_match.yaml")),
           "BM4: validator must auto-write benchmark_match.yaml")

    with _tempfile.TemporaryDirectory() as _d:
        # BM5: round 1 drafting window demotes drift to MEDIUM
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 400,
                     "tolerance_factor": 1.5, "source": "WMR",
                     "computed_value": 100},
                ]
            }, f)
        v = gate_findings(_d, round_n=1)
        drift = [x for x in v if x["kind"] == "benchmark_drift"]
        ok(len(drift) == 1 and drift[0]["severity"] == "MEDIUM",
           f"BM5: drift at r=1 must be MEDIUM, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM6: round 2, all within band → silent
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 400,
                     "tolerance_factor": 2.0, "source": "WMR",
                     "computed_value": 380},
                    {"id": "b2", "metric": "y", "target_value": 50,
                     "tolerance_abs": 10, "source": "DHS",
                     "computed_value": 55},
                ]
            }, f)
        v = gate_findings(_d, round_n=2)
        ok(not any(x["kind"] in ("benchmark_drift", "benchmark_computed_missing")
                   for x in v),
           f"BM6: all in-band must be silent, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM6b: stale cached benchmark_match.yaml must be refreshed.
        os.makedirs(os.path.join(_d, "models"))
        targets_path = os.path.join(_d, "models", "benchmark_targets.yaml")
        with open(targets_path, "w") as f:
            _yaml.safe_dump({"benchmarks": [
                {"id": "b1", "metric": "x", "target_value": 400,
                 "tolerance_factor": 1.5, "source": "WMR",
                 "computed_value": 100},
            ]}, f)
        v1 = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "benchmark_drift" for x in v1),
           f"BM6b: first drifted target should fire, got {v1}")
        with open(targets_path, "w") as f:
            _yaml.safe_dump({"benchmarks": [
                {"id": "b1", "metric": "x", "target_value": 400,
                 "tolerance_factor": 1.5, "source": "WMR",
                 "computed_value": 400},
            ]}, f)
        v2 = gate_findings(_d, round_n=2)
        ok(not any(x["kind"] == "benchmark_drift" for x in v2),
           f"BM6b: refreshed report should clear fixed benchmark, got {v2}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM7: missing computed → HIGH benchmark_computed_missing at r≥2
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": "b1", "metric": "x", "target_value": 400,
                     "tolerance_factor": 2.0, "source": "WMR"},
                ]
            }, f)
        v = gate_findings(_d, round_n=2)
        miss = [x for x in v if x["kind"] == "benchmark_computed_missing"]
        ok(len(miss) == 1 and miss[0]["severity"] == "HIGH",
           f"BM7: missing computed_value at r=2 should be HIGH, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM8: MALFORMED targets → HIGH benchmark_match_invalid at r≥2
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            f.write("benchmarks: this_is_not_a_list\n")
        v = gate_findings(_d, round_n=2)
        ok(any(x["kind"] == "benchmark_match_invalid"
               and x["severity"] == "HIGH" for x in v),
           f"BM8: malformed targets at r=2 should fire HIGH invalid, got {v}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM9: many drifted benchmarks → cap at 5 + 1 advisory
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({
                "benchmarks": [
                    {"id": f"b{i}", "metric": f"m{i}",
                     "target_value": 400, "tolerance_factor": 1.2,
                     "source": "WMR", "computed_value": 100}
                    for i in range(8)
                ]
            }, f)
        v = gate_findings(_d, round_n=2)
        drift = [x for x in v if x["kind"] == "benchmark_drift"]
        # Phase 21 β: 5 itemized + 1 overflow summary, all HIGH.
        ok(len(drift) == 6 and all(x["severity"] == "HIGH" for x in drift),
           f"BM9: must cap at 5 + 1 HIGH overflow, got drift={len(drift)}")

    with _tempfile.TemporaryDirectory() as _d:
        # BM10 (Phase 45 γ): the current value in models/benchmark_computed.yaml
        # wins over the locked targets file's copy. Not computed there (NaN)
        # is HIGH benchmark_computed_missing naming the stale in-band copy; a
        # stale copy that changes no verdict adds no finding.
        os.makedirs(os.path.join(_d, "models"))
        with open(os.path.join(_d, "models", "benchmark_targets.yaml"), "w") as f:
            _yaml.safe_dump({"benchmarks": [
                {"id": "b1", "metric": "x", "target_value": 400, "tolerance_factor": 2.0,
                 "source": "WMR", "computed_value": 380},
                {"id": "b2", "metric": "y", "target_value": 400, "tolerance_factor": 2.0,
                 "source": "WMR", "computed_value": 390}]}, f)
        with open(os.path.join(_d, "models", "benchmark_computed.yaml"), "w") as f:
            f.write("benchmarks:\n- id: b1\n  computed_value: .nan\n- id: b2\n  computed_value: 410\n")
        v = gate_findings(_d, round_n=2)
        miss = [x for x in v if x["kind"] == "benchmark_computed_missing"]
        ok(len(v) == 1 and len(miss) == 1 and miss[0]["severity"] == "HIGH" and "`b1`" in miss[0]["claim"]
           and "still holds 380" in miss[0]["claim"],
           f"BM10: NaN computed value must be HIGH not-computed naming the stale copy, got {v}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--round", type=int, default=2)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    v = gate_findings(a.run_dir, a.round)
    for x in v:
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 1 if any(x["severity"] == "HIGH" for x in v) else 0


if __name__ == "__main__":
    sys.exit(main())
