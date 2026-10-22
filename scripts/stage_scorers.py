#!/usr/bin/env python3
"""Deterministic scorers for stage-replay outputs (architecture v2, step 1).

No LLM judges: every score comes from an existing validator, so a replay
comparison measures the pipeline's own contracts.

    python3 scripts/stage_scorers.py model <run_dir> [--question Q]   # JSON to stdout
    python3 scripts/stage_scorers.py --self-test

score_model(run_dir) returns:
  artifacts.present / applicable / rate  MODEL-stage artifacts from rigor_artifacts.yaml
  effort_floors.{n_high, n_medium, evaluated, skipped}
  compare_models.verdict, identifiability.verdict   (CLI run on the outputs, with timeout)
  eligibility.{n_high, kinds}                        (only if an allocation question)
  framework.{required, violations}                   (spec_compliance framework check)
Each scorer is isolated: a crash is recorded as {"error": ...}, never raised.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

SCORER_TIMEOUT_S = 900


def _guard(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001 - recorded, not raised
        return {"error": f"{type(e).__name__}: {e}"[:500]}


def _question(run_dir):
    try:
        return json.load(open(os.path.join(run_dir, "metadata.json"))).get("question", "")
    except (OSError, ValueError):
        return ""


def score_artifacts(run_dir, allocation: bool) -> dict:
    from lib.rigor_artifacts import load_artifacts
    present, applicable = {}, []
    for a in load_artifacts():
        if a.stage != "model" or a.id in ("benchmark_match", "effort_floors_report"):
            continue
        if a.triggered_by == "allocation" and not allocation:
            continue
        if a.triggered_by not in ("always", "allocation"):
            continue                      # data-dependent triggers are not scored here
        applicable.append(a.id)
        present[a.id] = os.path.exists(os.path.join(run_dir, a.path))
    n = len(applicable)
    return {"present": present, "applicable": applicable,
            "rate": round(sum(present.values()) / n, 3) if n else None}


def score_effort(run_dir) -> dict:
    import effort_floors
    rep = effort_floors.evaluate(run_dir)
    probed = rep.get("probed", [])
    return {"n_high": rep.get("n_high"), "n_medium": rep.get("n_medium"),
            "evaluated": sum(1 for p in probed if p.get("status") in ("passed", "violated")),
            "skipped": sum(1 for p in probed if str(p.get("status", "")).startswith("skipped")),
            "violated": [v["floor_id"] for v in rep.get("violations", [])]}


def _cli_verdict(script, run_dir, out_name) -> dict:
    manifest = {"compare_models.py": "models/model_comparison.yaml",
                "identifiability.py": "models/identifiability.yaml"}[script]
    if not os.path.exists(os.path.join(run_dir, manifest)):
        return {"verdict": "ABSENT"}
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, out_name)
        try:
            p = subprocess.run([sys.executable, os.path.join(HERE, script), run_dir, "--output", out],
                               capture_output=True, text=True, timeout=SCORER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return {"verdict": "TIMEOUT"}
        if not os.path.exists(out):
            return {"verdict": "ERROR", "rc": p.returncode, "stderr": p.stderr[-300:]}
        doc = yaml.safe_load(open(out)) or {}
        return {"verdict": doc.get("verdict"), "rc": p.returncode}


def score_eligibility(run_dir) -> dict:
    import eligibility_probe
    f = eligibility_probe.check(run_dir, require=True)
    return {"n_high": sum(1 for x in f if x.get("severity") == "HIGH"),
            "kinds": sorted({x.get("kind") for x in f})}


def score_framework(run_dir, question) -> dict:
    import spec_compliance as sc
    req = sc.detect_required_spec(question)
    sub = {"frameworks": req.get("frameworks", []), "approaches": req.get("approaches", [])}
    v = sc.check_spec_compliance(sub, run_dir).get("violations", [])
    v = [x for x in v if x.get("kind") in ("framework_missing", "abm_missing", "approach_missing")]
    return {"required": sub["frameworks"], "violations": [x.get("kind") for x in v]}


def score_model(run_dir, question: str | None = None) -> dict:
    q = question if question is not None else _question(run_dir)
    allocation = any(w in q.lower() for w in ("allocat", "budget"))
    out = {
        "artifacts": _guard(score_artifacts, run_dir, allocation),
        "effort_floors": _guard(score_effort, run_dir),
        "compare_models": _guard(_cli_verdict, "compare_models.py", run_dir, "mcf.yaml"),
        "identifiability": _guard(_cli_verdict, "identifiability.py", run_dir, "id.yaml"),
        "framework": _guard(score_framework, run_dir, q),
    }
    if allocation:
        out["eligibility"] = _guard(score_eligibility, run_dir)
    return out


def flatten(scores: dict) -> dict:
    """Numeric view used by replay_compare (higher is better unless noted)."""
    a = scores.get("artifacts") or {}
    e = scores.get("effort_floors") or {}
    f = scores.get("framework") or {}
    el = scores.get("eligibility") or {}
    return {
        "artifact_rate": a.get("rate"),
        "effort_high_violations": e.get("n_high"),            # lower is better
        "compare_clean": None if "verdict" not in (scores.get("compare_models") or {})
                         else float(scores["compare_models"]["verdict"] == "CLEAN"),
        "identifiability_clean": None if "verdict" not in (scores.get("identifiability") or {})
                         else float(scores["identifiability"]["verdict"] == "CLEAN"),
        "framework_ok": None if "violations" not in f else float(not f["violations"]),
        "eligibility_high": el.get("n_high") if el else None,  # lower is better
    }


LOWER_IS_BETTER = {"effort_high_violations", "eligibility_high", "cost_usd", "wall_s",
                   "turns", "input_tokens"}


def _self_test() -> int:
    failures = []
    def ok(c, m):
        if not c: failures.append(m)
    with tempfile.TemporaryDirectory() as td:
        os.makedirs(os.path.join(td, "models"))
        json.dump({"question": "Using Starsim, allocate a fixed budget across districts"},
                  open(os.path.join(td, "metadata.json"), "w"))
        open(os.path.join(td, "models", "outcome_fn.py"), "w").write("def outcome_fn(p):\n    return {}\n")
        s = score_model(td)
        a = s["artifacts"]
        ok(a.get("present", {}).get("outcome_fn") is True and a["rate"] < 1, f"S1 artifacts: {a}")
        ok("eligibility_manifest" in a["applicable"], "S2 allocation makes eligibility applicable")
        ok(s["compare_models"] == {"verdict": "ABSENT"} and s["identifiability"] == {"verdict": "ABSENT"},
           "S3 absent manifests")
        ok(s["framework"].get("required") == ["starsim"] and s["framework"]["violations"],
           f"S4 framework missing flagged: {s['framework']}")
        ok("eligibility" in s and s["eligibility"].get("n_high", 0) >= 1, f"S5 eligibility manifest missing: {s.get('eligibility')}")
        flat = flatten(s)
        ok(flat["framework_ok"] == 0.0 and flat["artifact_rate"] == a["rate"], f"S6 flatten: {flat}")
        bad = _guard(lambda: 1 / 0)
        ok("error" in bad, "S7 guard records errors")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - "); return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", nargs="?", choices=["model"])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--question")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    print(json.dumps(score_model(a.run_dir, a.question), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
