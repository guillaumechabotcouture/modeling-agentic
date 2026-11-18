#!/usr/bin/env python3
"""Golden records of decision tests (simplification pass S1): digests that pin what each decision test decided, so a
refactor of the decision-test code (one generic `perturb` type, design note S2-S3) can show it changed nothing.

    python3 toolkit/analysis/dt_golden.py --self-test
    python3 toolkit/analysis/dt_golden.py digest <run_dir> [--out F]
    python3 toolkit/analysis/dt_golden.py retro <run_dir> --work <empty dir> (--write F | --expect F) [--unsigned]

A record's digest has three parts, each a sha256 prefix of a canonical JSON form:
  decision  what the test decided: status, plan changes, shares, regret, dollars moved, recommendations, benefit
            changes, and each tested case's results (without its label). It excludes `type` and `perturb`, so a test
            restated in another form keeps its decision digest.
  detail    what was tested: each case's label and the type's own block (cost_multipliers, unit_attribute,
            calibration_target, outcome_ratio, burden_scale, boundary_engine, spec_value, relevance, the arm ratios,
            rejected values, identities, unpaired packages, automatic).
  words     the sentences (statement, varied, question, criterion, reason).
Numbers are rounded to 6 significant digits (platform noise in the emulator fits stays below that), integers and
floats compare equal, and an absolute path keeps only its file name (self-tests run in temporary directories).

`digest` reads a finished run's analysis/decision_tests.yaml and each test's models/decision_tests/<id>.yaml (a test
that was not run has no file; its entry is digested from the summary). It fails closed: a run with no decision-test
artifact, or a listed result file that is missing, raises.

`retro` copies the run dir into --work, re-runs the analysis there on the current code (toolkit/analysis/run.py),
and digests the result. Zero engine jobs: scripts/compute.py's map_fn and the engine's own run / run_batch /
run_series raise EngineJobRefused, so every engine row must come from the stored rows (row_reuse, #241/#244). A run
whose stored rows do not cover the analysis, or a direct-path run (it evaluates the engine in-process), is refused,
never computed. Legacy rows may need MA_JC_LEGACY_ADAPTER_SHA256 (toolkit/analysis/row_reuse.py). The collected run
dir is never written. --write records the digests to F; --expect compares with F (exit 1 on any difference). The
report also compares the re-run with the run's own stored results. Expected digests of real runs are kept outside
the repo.

The toy golden records are toolkit/analysis/dt_golden.json, checked by toolkit/analysis/run.py's self-test: every
record decision_tests.run_one returns there is captured (start_capture / check_capture), keyed by the self-test
function that asked for it, the test id and the call's order. A PR that changes a digest says why in its description;
MA_DT_GOLDEN_UPDATE=1 rewrites the file from the observed records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
GOLDEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dt_golden.json")
UPDATE_ENV = "MA_DT_GOLDEN_UPDATE"
SIG = 6
WORD_KEYS = ("statement", "varied", "question", "criterion", "reason")
DETAIL_KEYS = ("cost_multipliers", "unit_attribute", "calibration_target", "outcome_ratio", "burden_scale",
               "boundary_engine", "spec_value", "relevance", "engine_arm_or", "engine_interaction", "arm_source",
               "arm_inputs", "rejected_values", "perturbation_identity", "missing_subtraction", "automatic",
               "parameter", "info")
IGNORED = ("type", "perturb", "path", "producer", "produced_by")


class EngineJobRefused(RuntimeError):
    """An engine job was requested during a retro (stored rows do not cover the analysis)."""


def canon(x):
    raise NotImplementedError


def _h(x) -> str:
    raise NotImplementedError


def parts(rec: dict) -> dict:
    raise NotImplementedError


def digest(rec: dict) -> dict:
    raise NotImplementedError


def digest_dir(run_dir: str) -> dict:
    raise NotImplementedError


def compare(expected: dict, got: dict) -> list[str]:
    raise NotImplementedError


class no_engine_jobs:
    def __init__(self, engine_cls=None):
        raise NotImplementedError


def retro(run_dir: str, work: str, spec_rel: str = "decision_spec.yaml", unsigned: bool = False, log=print) -> dict:
    raise NotImplementedError


def start_capture() -> None:
    raise NotImplementedError


def check_capture(ok) -> None:
    raise NotImplementedError


# --------------------------------------------------------------------------- self-test
def _rec(**kw) -> dict:
    r = {"status": "run", "test_id": "DT1", "type": "parameter_value", "finding": "decision_test:DT1",
         "can_move_plan": True, "decision_changes": False, "triggered_by": None, "share_units_changed": 0.125,
         "share_threshold": 0.1, "statement": "This test can move the plan.", "varied": "p set to each value",
         "question": "Does p move it?", "criterion": "more than 10%", "recommendation": {"with": "a", "without": "b"},
         "results": [{"value": 0.5, "share_units_changed": 0.125, "objective": 1234.5678912, "recommendation": "b"}],
         "benefit_change": {"a": 0.1}, "producer": "toolkit/analysis/decision_tests.py", "produced_by": "harness"}
    r.update(kw)
    return r


def _self_test() -> int:
    import tempfile
    import numpy as np
    import yaml
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- a case that raises is a failure, named
            ok(False, f"{name} raised {type(e).__name__}: {e}")

    def dg1():
        a = digest(_rec())
        b = digest(_rec(type="perturb", perturb={"input": "engine_parameters.p"}, path="models/decision_tests/x.yaml",
                        statement="Another sentence."))
        ok(a["decision"] == b["decision"] and a["detail"] == b["detail"] and a["words"] != b["words"],
           "DG1 type, perturb and path leave the decision and detail digests; a sentence changes only the words")
        c = digest(_rec(share_units_changed=0.25))
        ok(c["decision"] != a["decision"], "DG1b a changed share changes the decision digest")
    case("DG1", dg1)

    def dg2():
        a = digest(_rec())
        r = _rec()
        r["results"][0]["objective"] *= 1 + 1e-12
        r["share_units_changed"] = np.float64(0.125)
        r["benefit_change"] = {"a": np.float32(0.1)}
        ok(digest(r)["decision"] == a["decision"], "DG2 noise below 6 significant digits and numpy scalars: same digest")
        r2 = _rec()
        r2["results"][0]["objective"] *= 1 + 1e-4
        ok(digest(r2)["decision"] != a["decision"], "DG2b a change at the 4th significant digit changes the digest")
        ok(canon(1) == canon(1.0) and canon(True) is True and canon(float("nan")) == "nan"
           and canon({"b": 1, "a": {3, 1}}) == {"a": [1, 3], "b": 1.0}, "DG2c ints equal floats; bools, NaN, sets")
    case("DG2", dg2)

    def dg3():
        a = digest(_rec())
        r = _rec()
        r["results"][0]["value"] = "case"
        ok(digest(r)["decision"] == a["decision"] and digest(r)["detail"] != a["detail"],
           "DG3 a case's label is detail, not decision (a test restated with another label keeps its decision)")
        r = _rec(calibration_target={"scale": 0.8, "units": {"u1": {"z": 0.3}}})
        ok(digest(r)["decision"] == a["decision"] and digest(r)["detail"] != a["detail"],
           "DG3b the type's own block is detail")
        r = _rec(new_field=1)
        ok(digest(r)["decision"] != a["decision"], "DG3c an unknown field is decision (pinned, never ignored)")
    case("DG3", dg3)

    def dg4():
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            a = digest(_rec(arm_source=os.path.join(d1, "results.yaml")))
            b = digest(_rec(arm_source=os.path.join(d2, "results.yaml")))
            ok(a == b, "DG4 an absolute path keeps only its file name")
    case("DG4", dg4)

    def dg5():
        e = {"A": {"decision": "1", "detail": "2", "words": "3"}, "B": {"decision": "1", "detail": "2", "words": "3"}}
        g = {"A": {"decision": "1", "detail": "9", "words": "3"}, "C": {"decision": "1", "detail": "2", "words": "3"}}
        diff = compare(e, g)
        ok(sorted(diff) == ["A: detail differs", "B: missing", "C: not expected"] and compare(e, e) == [],
           f"DG5 compare lists each differing part, missing and unexpected test ({diff})")
    case("DG5", dg5)

    def dg6():
        with tempfile.TemporaryDirectory() as d:
            ok(_raises(lambda: digest_dir(d)), "DG6 a run without analysis/decision_tests.yaml is refused (fail closed)")
            os.makedirs(os.path.join(d, "analysis"))
            os.makedirs(os.path.join(d, "models", "decision_tests"))
            yaml.safe_dump(_rec(), open(os.path.join(d, "models", "decision_tests", "DT1.yaml"), "w"))
            tests = [{"test_id": "DT1", "path": "models/decision_tests/DT1.yaml", "status": "run"},
                     {"test_id": "DT2", "path": None, "status": "not_run", "statement": "This test was not run: x."}]
            yaml.safe_dump({"data": {"tests": tests}}, open(os.path.join(d, "analysis", "decision_tests.yaml"), "w"))
            got = digest_dir(d)
            ok(set(got) == {"DT1", "DT2"} and got["DT1"] == digest(_rec()) and got["DT2"]["decision"]
               != got["DT1"]["decision"], f"DG6b each listed test digested, a not-run test from the summary ({got})")
            os.remove(os.path.join(d, "models", "decision_tests", "DT1.yaml"))
            ok(_raises(lambda: digest_dir(d)), "DG6c a listed result file that is missing is refused")
    case("DG6", dg6)

    def dg7():
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import compute
        from toolkit.engines.contract import ToyEngine
        orig_map, orig_run = compute.map_fn, ToyEngine.run
        with tempfile.TemporaryDirectory() as d:
            g = no_engine_jobs(ToyEngine)
            with g:
                r1 = _raises(lambda: compute.map_fn(d, "x.py::f", [1, 2]), EngineJobRefused)
                r2 = _raises(lambda: ToyEngine().run({}, None, None, None, 0), EngineJobRefused)
            ok(r1 and r2 and g.refused == 2, f"DG7 under the guard compute.map_fn and the engine's run raise "
                                              f"EngineJobRefused ({r1}, {r2}, {g.refused})")
        ok(compute.map_fn is orig_map and ToyEngine.run is orig_run, "DG7b the guard restores both on exit")
    case("DG7", dg7)

    def dg8():
        with tempfile.TemporaryDirectory() as d:
            w = os.path.join(d, "work")
            os.makedirs(os.path.join(w, "x"))
            ok(_raises(lambda: retro(d, w, log=lambda *a: None)), "DG8 retro refuses a work dir that is not empty")
            ok(_raises(lambda: retro(d, d, log=lambda *a: None)), "DG8b retro refuses the run dir as its work dir")
    case("DG8", dg8)

    print(f"\n{'OK: all self-test cases passed.' if not fails else f'FAILED: {len(fails)} case(s)'}")
    return 1 if fails else 0


def _raises(fn, kind=Exception) -> bool:
    try:
        fn()
    except kind:
        return True
    except Exception:  # noqa: BLE001 -- another exception is not the one asked for
        return False
    return False


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main())
