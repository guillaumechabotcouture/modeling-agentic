#!/usr/bin/env python3
"""
Planted-defect regression eval for the deterministic gates (runs in CI).

E5 measured which defects the gates catch by planting known defects in real
run fixtures. Those fixtures are 100-400 MB of gitignored run output, so
they can't run on every pull request. This eval uses the same planter
(scripts/plant_defects.py) and matcher (scripts/score_critiques.py) on the
synthetic positive-control run (scripts/lib/positive_control.py), which is
built from code in seconds:

  1. build the honest positive-control run;
  2. plant one defect per variant, every applicable class, for each seed;
  3. run the validator alone on every variant (the E5 deterministic arm,
     scripts/replay.py::deterministic_critic);
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
import asyncio
import os
import shutil
import sys
import tempfile

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


def run_eval(seeds=SEEDS) -> dict:
    from lib import positive_control as pc
    import plant_defects as plant
    import replay

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
            asyncio.run(replay.deterministic_critic(run, {"wall_timeout_s": 600}))
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
    return out


def compare(res: dict, base: dict) -> list[str]:
    probs = []
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
    ok(compare({"detected": {"a": True, "b": False}, "clean_high": [], "spurious": []}, base) == [],
       "R1 identical → pass")
    ok(compare({"detected": {"a": True, "b": True}, "clean_high": [], "spurious": []}, base) == [],
       "R2 new catch is not a failure")
    ok(any("REGRESSION: a" in p for p in compare({"detected": {"a": False}, "clean_high": [], "spurious": []}, base)),
       "R3 lost catch fails")
    ok(any("clean run gained" in p for p in compare({"detected": {"a": True}, "clean_high": ["x"], "spurious": []}, base)),
       "R4 new clean HIGH fails")
    ok(any("SPURIOUS" in p for p in compare({"detected": {"a": True}, "clean_high": [], "spurious": ["k"]}, base)),
       "R5 spurious fails")
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
    base = yaml.safe_load(open(BASELINE)) or {}
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
