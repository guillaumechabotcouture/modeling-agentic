#!/usr/bin/env python3
"""
Planted-defect regression eval for the deterministic gates (runs in CI).

E5 measured which defects the gates catch by planting known defects in real
run fixtures. Those fixtures are 100-400 MB of gitignored run output, so
they can't run on every pull request. This eval uses the same planter
(scripts/plant_defects.py; E5's lexical matcher, scripts/score_critiques.py, was removed with v1) on the
synthetic positive-control run (scripts/lib/positive_control.py), which is
built from code in seconds:

  1. build the honest positive-control run;
  2. plant one defect per variant, every applicable class, for each seed;
  3. run the validator alone on every variant (the E5 deterministic arm,
     `deterministic_critic` below);
  4. compare with `tests/regression_baseline.yaml`.

A defect counts as caught when the validator emits a finding of a kind
expected for its class (EXPECTED_KINDS; the E5 matcher is lexical and was
built for LLM prose, so it under-credits structured gate findings). It fails
when a defect the baseline caught is no longer caught, when the clean run
gains a HIGH finding, or when the clean run already emits a kind expected
for a planted class (spurious). Newly caught defects are reported; adopt them with
`--update-baseline` in the same pull request that improves the gate.

The real-fixture E5 suite remains the measurement of record; this is the
fast tripwire.

Usage:
    python3 scripts/regression_eval.py                  # compare to baseline
    python3 scripts/regression_eval.py --update-baseline
    python3 scripts/regression_eval.py --self-test
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
BASELINE = os.path.join(REPO, "tests", "regression_baseline.yaml")
SEEDS = (7, 11)

# Finding kinds that count as catching each planted class. The E5 matcher is
# lexical (it was built to score LLM prose); a gate's finding is structured,
# so match on what the gate emits. Kinds ending in "*" are prefixes.
EXPECTED_KINDS = {
    "zero_width_ci": ("ci_degenerate",),
    "ci_inverted": ("ci_degenerate", "ci_inverted"),
    "ledger_mismatch": ("claims_ledger_stale", "claim_source_mismatch", "claim_value_not_in_source"),
    "renamed_verdict": ("*_unknown_verdict",),
    "budget_overspend": ("budget_exceeded",),
    "unit_error": ("data_schema_out_of_range", "unit_*"),
    "data_vintage": ("data_vintage_conflict", "vintage_*"),
    "prose_stale_number": ("coherence_audit_*", "numeric_drift_*"),
    "citation_mismatch": ("citation_*",),
    "sensitivity_contradiction": ("coherence_audit_label_coherence",),
    # Phase 42 γ (D1-D5).
    "mechanism_untested": ("mechanism_sufficiency_missing",),
    "prior_severity_below": ("pre_mortem_severity_below_prior",),
    "prose_closure": ("pre_mortem_closed_without_evidence",),
    "unused_column": ("data_columns_unread_summary",),
    "tuned_without_transfer": ("benchmark_transfer_missing",),
    # Phase 43 α (B1).
    "scenario_fitted": ("calibration_scenario_assumption_fitted",),
}
# On the positive control the planter's edit leaves `population` undefined,
# so the gate sees a crash (eligibility_probe_error), not the defect.
SKIP_CLASSES = {"benefit_total_population"}


def _kind_hit(cls: str, kinds: set[str]) -> bool:
    for want in EXPECTED_KINDS.get(cls, ()):
        if want.startswith("*") and any(k.endswith(want[1:]) for k in kinds):
            return True
        if want.endswith("*") and any(k.startswith(want[:-1]) for k in kinds):
            return True
        if want in kinds:
            return True
    return False




_STUB_REVIEWERS = ("critique-methods", "critique-domain", "critique-presentation", "critique-redteam")


def deterministic_critic(run_dir: str, wall_timeout_s: float = 900) -> dict:
    """E5 arm: the mechanical gate alone (no LLM). Writes empty PASS stubs for
    the four critics so the validator runs, collects every finding it raises
    (unresolved HIGHs, accept blockers, rigor violations of any severity),
    removes the stubs, and records the findings as critique_deterministic.yaml. (Moved here from
    scripts/replay.py, the v1 stage-replay lane, when v1 was retired.)"""
    import subprocess
    stubs = []
    for r in _STUB_REVIEWERS:
        p = os.path.join(run_dir, f"critique_{r.split('-', 1)[1]}.yaml")
        yaml.safe_dump({"reviewer": r, "round": 1, "verdict": "PASS",
                        "structural_mismatch": {"detected": False}, "blockers": [], "carried_forward": []},
                       open(p, "w"), sort_keys=False)
        stubs.append(p)
    t0 = time.time()
    cmd = [sys.executable, os.path.join(HERE, "validate_critique_yaml.py"), run_dir, "--max-rounds", "8",
           "--current-round", "1", "--spec-compliance", "--parameter-registry", "--rigor-artifacts",
           "--json", "--no-history"]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=float(wall_timeout_s))
    for p in stubs:
        os.remove(p)
    out = proc.stdout.strip()
    try:
        dec = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        dec = {"error": (proc.stderr or out)[-500:]}
    # A schema error (exit 3), VALIDATOR_ERROR (exit 4) or a missing action
    # is a failed evaluation, not an empty review (Codex review of PR #14).
    failed = ("error" in dec or bool(dec.get("schema_errors"))
              or dec.get("action") in (None, "VALIDATOR_ERROR")
              or proc.returncode not in (0, 1))
    if failed and "error" not in dec:
        dec["error"] = (f"validator exit {proc.returncode}, action {dec.get('action')!r}, "
                        f"schema_errors {dec.get('schema_errors')!r}")[:500]
    findings = [("HIGH", b) for b in dec.get("unresolved_high") or []] + \
               [("HIGH", b) for b in dec.get("accept_blockers") or []] + \
               [(str(v.get("severity", "MEDIUM")).upper(), v) for v in dec.get("rigor_violations") or []]
    seen, blockers = set(), []
    for sev, f in findings:
        key = (f.get("kind"), f.get("claim"))
        if key in seen:
            continue
        seen.add(key)
        blockers.append({"id": f"DET-{len(blockers) + 1:03d}", "severity": sev if sev in ("HIGH", "MEDIUM", "LOW") else "MEDIUM",
                         "kind": f.get("kind"), "claim": str(f.get("claim") or f.get("kind") or ""),
                         "evidence": json.dumps({k: v for k, v in f.items() if k not in ("claim",)}, default=str)[:2000]})
    yaml.safe_dump({"reviewer": "deterministic", "round": 1, "action": dec.get("action"),
                    "validator_exit": proc.returncode, "blockers": blockers,
                    **({"error": dec["error"]} if "error" in dec else {})},
                   open(os.path.join(run_dir, "critique_deterministic.yaml"), "w"), sort_keys=False)
    return {"session_id": None, "subtype": "deterministic", "is_error": failed, "cost_usd": 0.0,
            "num_turns": 0, "duration_s": round(time.time() - t0, 1)}


def run_eval(seeds=SEEDS) -> dict:
    from lib import positive_control as pc
    import plant_defects as plant

    out = {"detected": {}, "planted": {}, "clean_high": [], "spurious": []}
    with tempfile.TemporaryDirectory() as root:
        src = os.path.join(root, "src", "PC")
        pc.build(os.path.join(src, "inputs"), round_n=2)
        inp = os.path.join(src, "inputs")
        files = {os.path.relpath(os.path.join(dp, fn), inp): {"sha256": plant._sha(os.path.join(dp, fn))}
                 for dp, _, fns in os.walk(inp) for fn in fns}
        with open(os.path.join(src, "fixture.yaml"), "w") as f:
            yaml.safe_dump({"id": "PC", "stage": "CRITIQUE", "source": "positive_control",
                            "files": files}, f)
        classes = plant.applicable(os.path.join(src, "inputs"), seeds[0])
        variants = []
        for seed in seeds:
            vroot = os.path.join(root, f"s{seed}")
            for v in plant.build_variants(src, vroot, k=len(classes), per_variant=1, seed=seed):
                variants.append((seed, vroot, v))
        truth_all = []
        for seed, vroot, v in variants:
            vid = v.get("variant") or v.get("id")
            truth = yaml.safe_load(open(os.path.join(vroot, "_truth", f"{vid}.yaml")))
            run = os.path.join(root, "runs", f"s{seed}_{vid}")
            shutil.copytree(os.path.join(vroot, vid, "inputs"), run)
            deterministic_critic(run, wall_timeout_s=600)
            doc = yaml.safe_load(open(os.path.join(run, "critique_deterministic.yaml"))) or {}
            blockers = doc.get("blockers") or []
            kinds = {str(b.get("kind")) for b in blockers}
            if doc.get("error"):
                raise RuntimeError(f"validator failed on {vid}: {doc['error']}")
            if truth["tag"] == "clean":
                out["clean_high"] = sorted({str(b.get("kind")) for b in blockers if b.get("severity") == "HIGH"})
                clean_kinds = kinds
                continue
            for d in truth["defects"]:
                if d["class"] in SKIP_CLASSES:
                    continue
                key = f"s{seed}:{d['class']}:{d['location']}"
                out["planted"][key] = d["class"]
                out["detected"][key] = _kind_hit(d["class"], kinds)
                truth_all.append((key, d))
        out["spurious"] = sorted(k for k, d in truth_all if _kind_hit(d["class"], clean_kinds))
    # PR epsilon of #363 (§6.4): deterministic built-engine planted-defect tripwires (BE2, BE8, BE10)
    for key, cls, caught in _run_built_engine_defects():
        out["planted"][key] = cls
        out["detected"][key] = caught
    return out


def _run_built_engine_defects() -> list[tuple[str, str, bool]]:
    """Deterministic planted-defect tripwires BE2 (dead parameter -> A6), BE8 (closed-conservation
    population growth -> A5), and BE10 (public lookup table -> S4 + hidden A4)."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import numpy as np
    from toolkit.engines.built import admission as BADM
    from toolkit.engines.built import candidate as BCAND
    from toolkit.engines.built import fixtures as BFIX
    from toolkit.engines.built import snapshot as BSNAP
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
    c2 = BCAND.CandidateEngine(BFIX.two_group_sir_files("dead_param"), b, unsandboxed_ok=dev_un)
    a6_2, _ = BADM.check_a6_a7(c2, b, sp, np.random.default_rng(11), n_bases=1, min_moving=1, th=th)
    be2_ok = a6_2.get("status") == "fail" and any("p_sev" in str(p) for p in a6_2.get("problems") or [])

    # BE8: 0.1%/step population growth under conservation=closed caught by A5
    c8 = BCAND.CandidateEngine(BFIX.two_group_sir_files("mass_growth"), b, unsandboxed_ok=dev_un)
    a5_8 = BADM.check_a5(c8, b, units, pkgs, hor, th=th)
    be8_ok = a5_8.get("status") == "fail" and any("conservation" in str(p) for p in a5_8.get("problems") or [])

    # BE10: public lookup table caught by S4 static audit and hidden A4 (n_free=3 matches admission.py)
    f10 = BFIX.two_group_sir_files("public_table")
    s4_probs = BSNAP.check_static(f10, request=b)
    c10 = BCAND.CandidateEngine(f10, b, unsandboxed_ok=dev_un)
    hid_a4 = BADM.check_a4(c10, b, sp, np.random.default_rng(19), n_free=3, th=th)
    be10_ok = any("S4:" in p for p in s4_probs) and hid_a4.get("status") == "fail"

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
       "R1 identical → pass")
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
    # R6 (v1 retirement): the deterministic arm lives here; the v1 stage-replay lane (scripts/replay.py) is gone.
    import inspect
    src = inspect.getsource(sys.modules[__name__])
    ok(callable(globals().get("deterministic_critic")) and "import " + "replay" not in src,
       "R6 the deterministic critic is this module's own; nothing imports scripts/replay.py")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Planted-defect regression eval (deterministic gates)")
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
