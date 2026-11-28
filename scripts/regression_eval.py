#!/usr/bin/env python3
"""
Deterministic regression eval for the v2 gates and built-engine checks (runs in CI).

Exercises:
  1. Built-engine planted-defect tripwires BE2, BE8, BE10 (`_run_built_engine_defects`);
  2. v2 analysis contract gate tripwires (`toolkit.analysis.contract.gate_findings`);
  3. v2 critique decision / done_when tripwires (`validate_critique_yaml`, `done_when`);
  4. Compare with `tests/regression_baseline.yaml`.

Usage:
    python3 scripts/regression_eval.py                  # compare to baseline
    python3 scripts/regression_eval.py --update-baseline
    python3 scripts/regression_eval.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
BASELINE = os.path.join(REPO, "tests", "regression_baseline.yaml")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _run_contract_defects() -> list[tuple[str, str, bool]]:
    """Deterministic v2 analysis contract tripwires via toolkit.analysis.contract.gate_findings."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    from toolkit.analysis import contract

    out: list[tuple[str, str, bool]] = []

    def _check(key: str, cls: str, want_kind: str, setup) -> None:
        with tempfile.TemporaryDirectory() as d:
            setup(d)
            kinds = {str(f.get("kind")) for f in contract.gate_findings(d)}
            out.append((key, cls, want_kind in kinds))

    def _write_spec_and_manifest(d: str, manifest_updates: dict | None = None,
                                 extra_artifacts: dict[str, bytes] | None = None) -> None:
        spec_raw = b"deliverable:\n  kind: estimates\n"
        with open(os.path.join(d, contract.SPEC), "wb") as f:
            f.write(spec_raw)
        os.makedirs(os.path.join(d, "analysis"), exist_ok=True)
        man = {
            "status": "complete",
            "complete": True,
            "spec_signed": True,
            "spec_sha256": _sha256_bytes(spec_raw),
            "settings_sha256": None,
            "engine_choice_sha256": None,
            "deliverable_kind": "estimates",
            "artifacts": {},
            "deliverables": {},
            "expected": {},
        }
        if extra_artifacts:
            for rel, raw in extra_artifacts.items():
                p = os.path.join(d, rel)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "wb") as f:
                    f.write(raw)
                man["artifacts"][rel] = {"kind": "point", "sha256": _sha256_bytes(raw)}
        if manifest_updates:
            man.update(manifest_updates)
        with open(os.path.join(d, contract.MANIFEST), "w", encoding="utf-8") as f:
            json.dump(man, f)

    def _s_missing(d: str) -> None:
        with open(os.path.join(d, contract.SPEC), "w", encoding="utf-8") as f:
            f.write("deliverable:\n  kind: estimates\n")

    def _s_incomplete(d: str) -> None:
        _write_spec_and_manifest(d, {"status": "running", "complete": False})

    def _s_blocked(d: str) -> None:
        _write_spec_and_manifest(d, {"status": "blocked", "complete": False, "reason": "refused"})

    def _s_unsigned(d: str) -> None:
        _write_spec_and_manifest(d, {"spec_signed": False})

    def _s_stale(d: str) -> None:
        _write_spec_and_manifest(d, {"spec_sha256": "0" * 64})

    def _s_tampered(d: str) -> None:
        _write_spec_and_manifest(d, extra_artifacts={"analysis/point.yaml": b"schema: analysis.point/1\n"})
        with open(os.path.join(d, "analysis", "point.yaml"), "wb") as f:
            f.write(b"tampered: true\n")

    _check("v2_contract:missing_manifest", "contract_missing_manifest", "analysis_contract_missing", _s_missing)
    _check("v2_contract:incomplete_manifest", "contract_incomplete_manifest", "analysis_contract_incomplete", _s_incomplete)
    _check("v2_contract:blocked_manifest", "contract_blocked_manifest", "analysis_contract_blocked", _s_blocked)
    _check("v2_contract:unsigned_spec", "contract_unsigned_spec", "analysis_contract_unsigned", _s_unsigned)
    _check("v2_contract:spec_stale", "contract_spec_stale", "analysis_contract_stale", _s_stale)
    _check("v2_contract:tampered_artifact", "contract_tampered_artifact", "analysis_contract_tampered", _s_tampered)
    return out


def _run_critique_defects() -> tuple[list[tuple[str, str, bool]], list[str], list[str]]:
    """Deterministic v2 critique decision / done_when tripwires + clean baseline check."""
    import done_when as dw
    import validate_critique_yaml as V

    out: list[tuple[str, str, bool]] = []

    # 1. Unresolved HIGH blocker blocks ACCEPT.
    crit_open = {
        "critique-methods": {
            "blockers": [{
                "id": "M-001", "severity": "HIGH", "category": "METHODS",
                "target_stage": "MODEL", "first_seen_round": 1,
                "claim": "parameter bound violated", "resolved": False,
            }],
            "carried_forward": [],
            "structural_mismatch": {"detected": False},
        },
    }
    dec1 = V.decide(crit_open, max_rounds=8, current_round=2)
    out.append((
        "v2_critique:unresolved_high_blocks",
        "critique_unresolved_high",
        dec1.get("action") == "PATCH_OR_RETHINK"
        and any(b.get("kind") == "critique_blocker" for b in dec1.get("unresolved_high") or []),
    ))

    # 2. A done_when test that passes in round 3 and regresses in round 4 reopens as a HIGH finding.
    with tempfile.TemporaryDirectory() as d:
        out_json = os.path.join(d, "out.json")
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump({"ci95": [10, 20]}, f)
        crit_dw = {
            "critique-redteam": {
                "blockers": [{
                    "id": "R-001", "severity": "HIGH", "category": "METHODS",
                    "target_stage": "MODEL", "first_seen_round": 2,
                    "claim": "degenerate CI", "resolved": False,
                    "done_when": {"file": "out.json", "key": "ci95", "op": "width_gt", "value": 0},
                }],
                "carried_forward": [],
                "structural_mismatch": {"detected": False},
            },
        }
        V._apply_done_when(V.decide(crit_dw, 8, 3), crit_dw, d, 8, 3)
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump({"ci95": [15, 15]}, f)
        crit_dw["critique-redteam"]["blockers"][0]["resolved"] = True
        dec_reg, res_reg = V._apply_done_when(V.decide(crit_dw, 8, 4), crit_dw, d, 8, 4)
        dec_reg = V._incorporate_rigor_violations(dec_reg, dw.gate_findings(res_reg), 8, 4)
        out.append((
            "v2_critique:done_when_regressed_reopens",
            "critique_done_when_regressed",
            dec_reg.get("action") != "ACCEPT"
            and any(b.get("kind") == "done_when_regressed" for b in dec_reg.get("unresolved_high") or []),
        ))

    # 3. A non-declarable HIGH finding at round exhaustion triggers RUN_FAILED.
    base_clean = V.decide({}, max_rounds=8, current_round=8)
    dec_nd = V._incorporate_rigor_violations(
        base_clean,
        [{"kind": "pre_model_over_saturated", "severity": "HIGH", "stage": "PRE_MODEL", "claim": "over-saturated"}],
        8, 8,
    )
    out.append((
        "v2_critique:non_declarable_at_exhaustion_fails",
        "critique_non_declarable_exhaustion",
        dec_nd.get("action") == "RUN_FAILED",
    ))

    # Clean run baseline check.
    clean_dec = V.decide({
        "critique-methods": {"blockers": [], "carried_forward": [], "structural_mismatch": {"detected": False}},
    }, max_rounds=8, current_round=2)
    clean_high = sorted({str(b.get("kind")) for b in clean_dec.get("unresolved_high") or []})
    return out, clean_high, []


def run_eval() -> dict:
    out = {"detected": {}, "planted": {}, "clean_high": [], "spurious": []}
    for key, cls, caught in _run_built_engine_defects():
        out["planted"][key] = cls
        out["detected"][key] = caught
    for key, cls, caught in _run_contract_defects():
        out["planted"][key] = cls
        out["detected"][key] = caught
    crit_rows, clean_high, spurious = _run_critique_defects()
    for key, cls, caught in crit_rows:
        out["planted"][key] = cls
        out["detected"][key] = caught
    out["clean_high"] = clean_high
    out["spurious"] = spurious
    return out


def _run_built_engine_defects() -> list[tuple[str, str, bool]]:
    """Deterministic planted-defect tripwires BE2 (dead parameter -> A6), BE8 (closed-conservation
    population growth -> A5), and BE10 (public lookup table -> hidden A4)."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import numpy as np
    from toolkit.engines.built import adapter as BADAPT
    from toolkit.engines.built import admission as BADM
    from toolkit.engines.built import fixtures as BFIX
    from toolkit.engines.built import worker as BWORK
    from toolkit.engines.contract import Package

    dev_un = BWORK.engine_sandbox_kind() is None
    th = BADM.load_thresholds()
    b = BFIX.two_group_sir_build()
    sp = BFIX.two_group_sir_spec(b)
    hor = BADM._default_horizon(sp, b["request"])
    units = BADM.generate_domain_units(b["request"], 1, np.random.default_rng(7))
    pkgs = [Package("none", {"comp_a": 0}), Package("a", {"comp_a": 1})]

    # BE2: dead parameter p_sev caught by A6
    c2 = BADAPT.BuiltEngine.from_verified(BFIX.two_group_sir_files("dead_param"), b, unsandboxed_ok=dev_un)
    a6_2 = BADM.check_a6(c2, b, sp, np.random.default_rng(11), n_bases=1, min_moving=1, th=th)
    be2_ok = a6_2.get("status") == "fail" and any("p_sev" in str(p) for p in a6_2.get("problems") or [])

    # BE8: 0.1%/step population growth under conservation=closed caught by A5
    c8 = BADAPT.BuiltEngine.from_verified(BFIX.two_group_sir_files("mass_growth"), b, unsandboxed_ok=dev_un)
    a5_8 = BADM.check_a5(c8, b, units, pkgs, hor, th=th)
    be8_ok = a5_8.get("status") == "fail" and any("conservation" in str(p) for p in a5_8.get("problems") or [])

    # BE10: public lookup table caught by hidden A4 (n_free=3 matches admission.py)
    f10 = BFIX.two_group_sir_files("public_table")
    c10 = BADAPT.BuiltEngine.from_verified(f10, b, unsandboxed_ok=dev_un)
    hid_a4 = BADM.check_a4(c10, b, sp, np.random.default_rng(19), n_free=3, th=th)
    be10_ok = hid_a4.get("status") == "fail"

    return [
        ("built_engine:BE2", "built_dead_param", bool(be2_ok)),
        ("built_engine:BE8", "built_mass_growth", bool(be8_ok)),
        ("built_engine:BE10", "built_public_table", bool(be10_ok)),
    ]


def compare(res: dict, base: dict) -> list[str]:
    det = base.get("detected") if isinstance(base, dict) else None
    if not isinstance(det, dict) or not any(v is True for v in det.values()):
        # An empty baseline would compare nothing and report "no regressions".
        return ["INVALID BASELINE: no detected: true entries; regenerate with --update-baseline"]
    probs = []
    missing = sorted(k for k in det if k not in res["detected"])
    if missing:
        probs.append(f"REGRESSION: baseline defects no longer planted/scored {missing[:5]}")
    for k, was in (base.get("detected") or {}).items():
        if was and not res["detected"].get(k):
            probs.append(f"REGRESSION: {k} was caught, now missed")
    extra = sorted(set(res["clean_high"]) - set(base.get("clean_high") or []))
    if extra:
        probs.append(f"REGRESSION: clean run gained HIGH finding(s) {extra}")
    if res["spurious"]:
        probs.append(f"SPURIOUS: clean-run blockers match planted defects {res['spurious']}")
    return probs


def _self_test() -> int:
    fails = []
    base = {"detected": {"a": True, "b": False}, "clean_high": []}
    ok = lambda c, m: None if c else fails.append(m)
    ok(any("INVALID BASELINE" in p for p in compare({"detected": {}, "clean_high": [], "spurious": []}, {})),
       "R0 empty baseline is invalid, not a pass")
    ok(compare({"detected": {"a": True, "b": False}, "clean_high": [], "spurious": []}, base) == [],
       "R1 identical -> pass")
    ok(compare({"detected": {"a": True, "b": True}, "clean_high": [], "spurious": []}, base) == [],
       "R2 new catch is not a failure")
    ok(any("no longer planted" in p for p in compare({"detected": {"a": True}, "clean_high": [], "spurious": []}, base)),
       "R2b a baseline defect that is no longer scored fails")
    ok(any("REGRESSION: a" in p for p in compare({"detected": {"a": False}, "clean_high": [], "spurious": []}, base)),
       "R3 lost catch fails")
    ok(any("clean run gained" in p for p in compare({"detected": {"a": True}, "clean_high": ["x"], "spurious": []}, base)),
       "R4 new clean HIGH fails")
    ok(any("SPURIOUS" in p for p in compare({"detected": {"a": True}, "clean_high": [], "spurious": ["k"]}, base)),
       "R5 spurious fails")
    ok(callable(globals().get("_run_built_engine_defects"))
       and callable(globals().get("_run_contract_defects"))
       and callable(globals().get("_run_critique_defects"))
       and not hasattr(sys.modules[__name__], "deterministic_critic"),
       "R6 v2 built-engine, contract, and critique tripwires live here; no deterministic_critic")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic regression eval (v2 gates & built-engine checks)")
    ap.add_argument("--update-baseline", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    res = run_eval()
    n, k = len(res["planted"]), sum(res["detected"].values())
    by = {}
    for key, cls in res["planted"].items():
        by.setdefault(cls, [0, 0])
        by[cls][0] += res["detected"][key]
        by[cls][1] += 1
    print(f"planted-defect recall: {k}/{n}  " + ", ".join(f"{c} {a}/{b}" for c, (a, b) in sorted(by.items())))
    print(f"clean-run HIGH kinds: {res['clean_high'] or 'none'}")
    if a.update_baseline:
        os.makedirs(os.path.dirname(BASELINE), exist_ok=True)
        with open(BASELINE, "w") as f:
            f.write("# Written by `python3 scripts/regression_eval.py --update-baseline`.\n"
                    "# A detected: true entry may only flip to false with a stated reason in the PR.\n")
            yaml.safe_dump({"detected": dict(sorted(res["detected"].items())),
                            "clean_high": res["clean_high"]}, f, sort_keys=False)
        print(f"baseline written: {os.path.relpath(BASELINE, REPO)}")
        return 0
    if not os.path.exists(BASELINE):
        print(f"no baseline at {os.path.relpath(BASELINE, REPO)}; run with --update-baseline")
        return 2
    try:
        base = yaml.safe_load(open(BASELINE)) or {}
    except yaml.YAMLError as e:
        print(f"INVALID BASELINE: {e}")
        return 2
    new = sorted(k for k, v in res["detected"].items() if v and not (base.get("detected") or {}).get(k))
    if new:
        print(f"newly caught (adopt with --update-baseline): {new}")
    probs = compare(res, base)
    for p in probs:
        print(p)
    print("OK: no regressions." if not probs else f"FAILED: {len(probs)} problem(s).")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main())
