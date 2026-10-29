#!/usr/bin/env python3
"""Phase 42 γ (D5): transfer check for effects tuned to benchmarks.

    python3 scripts/benchmark_transfer.py <run_dir>
    python3 scripts/benchmark_transfer.py --self-test

E7 attempt 8 tuned its effect-strength parameters so in-silico trials landed
inside the trial confidence intervals, at the trial sites' conditions. Every
benchmark passed; nothing reported what the tuned effect becomes under the
target setting's covariates, where the mechanism behind it looks different.

How a run states tuning: on each entry of `models/benchmark_targets.yaml`
whose match was obtained by adjusting parameters, list them, and report the
transfer:

    benchmarks:
      - id: B7
        ...
        tuned_parameters: [effect_strength, decay_half_life]
        transfer:
          target_covariates: "<the target setting's covariate values used, and
                               how they differ from the benchmark site's>"
          effect_at_benchmark: 0.68     # optional; defaults to computed_value
          effect_at_target: 0.74        # the same effect, re-computed under the
                                        # target setting's covariates
          difference_stated: "<the difference and what it means for the decision>"
          result_file: models/transfer_B7.yaml   # optional; must exist when given

Findings (all MEDIUM, scope-declarable):
  benchmark_transfer_missing      a tuned benchmark with no transfer block, or
                                  one without target_covariates, a numeric
                                  effect_at_target, or difference_stated
  benchmark_transfer_invalid      unreadable targets file / malformed entry /
                                  result_file that does not exist
  benchmark_tuning_undeclared     model code says parameters were tuned to
                                  trials or benchmarks, and no benchmark entry
                                  declares tuned_parameters
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join("models", "benchmark_targets.yaml")
# A model-code line that says parameters were tuned/fitted to trial or
# benchmark results. Deliberately narrow: "tuned"/"tuning" on the same line
# as "trial" or "benchmark".
_TUNING_RE = re.compile(r"\b(tuned|tuning|hand-tuned|retuned)\b.{0,80}\b(trials?|benchmarks?)\b"
                        r"|\b(trials?|benchmarks?)\b.{0,40}\b(tuned|tuning)\b", re.I)


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _f(kind, claim):
    return {"kind": kind, "severity": "MEDIUM", "stage": "MODEL", "target_stage": "MODEL",
            "scope_declarable": True, "claim": claim}


def _code_tuning_mentions(run_dir: str) -> list[str]:
    hits = []
    root = os.path.join(run_dir, "models")
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__" and not x.startswith("."))
        for fn in sorted(files):
            if not fn.endswith(".py"):
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    for i, line in enumerate(f, 1):
                        if _TUNING_RE.search(line):
                            hits.append(f"{os.path.relpath(os.path.join(d, fn), run_dir)}:{i}")
                            break
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def gate_findings(run_dir: str) -> list[dict]:
    path = os.path.join(run_dir, PATH)
    mentions = _code_tuning_mentions(run_dir)
    if not os.path.exists(path):
        if mentions:
            return [_f("benchmark_tuning_undeclared",
                       f"Model code says parameters were tuned to trials or benchmarks ({', '.join(mentions[:5])}), "
                       f"and there is no `{PATH}` declaring which benchmarks were tuned (`tuned_parameters:`) "
                       "with a transfer to the target setting.")]
        return []
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return [_f("benchmark_transfer_invalid", f"`{PATH}` cannot be parsed ({e}); tuning cannot be checked.")]
    bms = doc.get("benchmarks") if isinstance(doc, dict) else None
    if not isinstance(bms, list):
        return [_f("benchmark_transfer_invalid", f"`{PATH}` has no `benchmarks:` list; tuning cannot be checked.")]
    out, n_tuned = [], 0
    root = os.path.realpath(run_dir)
    for i, b in enumerate(bms):
        if not isinstance(b, dict):
            continue          # benchmark_match reports malformed entries
        bid = b.get("id") or f"benchmarks[{i}]"
        tp = b.get("tuned_parameters")
        if tp is None or tp == [] or tp is False:
            continue
        if not isinstance(tp, list) or not all(isinstance(x, str) and x.strip() for x in tp):
            out.append(_f("benchmark_transfer_invalid",
                          f"`{PATH}` {bid}: `tuned_parameters` must be a list of parameter names."))
            continue
        n_tuned += 1
        t = b.get("transfer")
        missing = []
        if not isinstance(t, dict):
            missing = ["the `transfer:` block"]
        else:
            if not (isinstance(t.get("target_covariates"), str) and t["target_covariates"].strip()):
                missing.append("`target_covariates`")
            if _num(t.get("effect_at_target")) is None:
                missing.append("a numeric `effect_at_target`")
            if not (isinstance(t.get("difference_stated"), str) and t["difference_stated"].strip()):
                missing.append("`difference_stated`")
            rf = t.get("result_file")
            if rf is not None:
                p = os.path.realpath(os.path.join(root, str(rf)))
                if not (p.startswith(root + os.sep) and os.path.isfile(p)):
                    out.append(_f("benchmark_transfer_invalid",
                                  f"`{PATH}` {bid}: transfer.result_file `{rf}` does not exist in the run."))
        if missing:
            out.append(_f("benchmark_transfer_missing",
                          f"`{PATH}` {bid} was matched by tuning {', '.join(tp)}, but the run does not report the "
                          f"effect under the target setting's covariates: missing {', '.join(missing)}. Re-compute "
                          "the tuned effect with the target setting's covariates and state the difference from the "
                          "benchmark site."))
    if not n_tuned and mentions:
        out.append(_f("benchmark_tuning_undeclared",
                      f"Model code says parameters were tuned to trials or benchmarks ({', '.join(mentions[:5])}), "
                      f"but no `{PATH}` entry declares `tuned_parameters:`. Declare the tuned benchmarks and report "
                      "each tuned effect under the target setting's covariates (`transfer:`)."))
    return out


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def w(d, rel, obj):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(obj if isinstance(obj, str) else yaml.safe_dump(obj))

    base = {"id": "B1", "metric": "effect_ratio", "target_value": 0.6, "tolerance_abs": 0.1, "source": "trial",
            "computed_value": 0.62}
    kinds = lambda v: sorted(x["kind"] for x in v)
    with tempfile.TemporaryDirectory() as d:
        ok(gate_findings(d) == [], "BT0 nothing declared, nothing to check")
        w(d, PATH, {"benchmarks": [dict(base)]})
        ok(gate_findings(d) == [], "BT1 untuned benchmarks need no transfer")
        w(d, PATH, {"benchmarks": [dict(base, tuned_parameters=["strength"])]})
        v = gate_findings(d)
        ok(kinds(v) == ["benchmark_transfer_missing"] and v[0]["severity"] == "MEDIUM",
           f"BT2 tuned without transfer → MEDIUM, got {v}")
        w(d, PATH, {"benchmarks": [dict(base, tuned_parameters=["strength"],
                                        transfer={"target_covariates": "target mix", "effect_at_target": "n/a",
                                                  "difference_stated": "x"})]})
        ok(kinds(gate_findings(d)) == ["benchmark_transfer_missing"], "BT3 a non-numeric effect_at_target")
        w(d, PATH, {"benchmarks": [dict(base, tuned_parameters=["strength"],
                                        transfer={"target_covariates": "target mix", "effect_at_target": 0.7,
                                                  "difference_stated": "0.08 weaker at the target"})]})
        ok(gate_findings(d) == [], f"BT4 complete transfer passes, got {gate_findings(d)}")
        w(d, PATH, {"benchmarks": [dict(base, tuned_parameters=["strength"],
                                        transfer={"target_covariates": "t", "effect_at_target": 0.7,
                                                  "difference_stated": "d", "result_file": "models/none.yaml"})]})
        ok(kinds(gate_findings(d)) == ["benchmark_transfer_invalid"], "BT5 a missing result_file")
        w(d, PATH, {"benchmarks": [dict(base)]})
        w(d, "models/model.py", "# Strength parameters (tuned so in-silico trials fall inside the trial CIs)\nK = 0.3\n")
        ok(kinds(gate_findings(d)) == ["benchmark_tuning_undeclared"], "BT6 tuning stated in code but undeclared")
        os.remove(os.path.join(d, PATH))
        ok(kinds(gate_findings(d)) == ["benchmark_tuning_undeclared"], "BT6b ... also without a targets file")
        w(d, "models/model.py", "# the fit uses hyperparameter tuning on the grid\nK = 0.3\n")
        ok(gate_findings(d) == [], "BT7 unrelated tuning wording does not fire")
        w(d, PATH, "benchmarks: [")
        ok(kinds(gate_findings(d)) == ["benchmark_transfer_invalid"], "BT8 unparseable targets fail closed")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    v = gate_findings(a.run_dir)
    for x in v:
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 1 if v else 0


if __name__ == "__main__":
    sys.exit(main())
