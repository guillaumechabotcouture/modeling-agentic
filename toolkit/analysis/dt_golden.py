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
    """The canonical form digested: numbers to 6 significant digits (ints equal floats), sets sorted, an absolute
    path to its file name, mappings by string key."""
    import numpy as np
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if x is None:
        return None
    if isinstance(x, (int, float, np.integer, np.floating)):
        v = float(x)
        if not math.isfinite(v):
            return "nan" if math.isnan(v) else ("inf" if v > 0 else "-inf")
        v = float(f"{v:.{SIG}g}")
        return 0.0 if v == 0 else v
    if isinstance(x, str):
        return os.path.basename(x) if os.path.isabs(x) else x
    if isinstance(x, dict):
        return {str(k): canon(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, np.ndarray)):
        return [canon(v) for v in (x.tolist() if isinstance(x, np.ndarray) else x)]
    if isinstance(x, (set, frozenset)):
        return sorted((canon(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True))
    return str(x)


def _h(x) -> str:
    return hashlib.sha256(json.dumps(canon(x), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def parts(rec: dict) -> dict:
    """{decision, detail, words} of one decision-test record (module docstring). A field this module does not name
    is decision: pinned, never ignored."""
    if not isinstance(rec, dict):
        raise ValueError(f"a decision-test record is a mapping, not {type(rec).__name__}")
    words = {k: rec[k] for k in WORD_KEYS if k in rec}
    detail = {k: rec[k] for k in DETAIL_KEYS if k in rec}
    decision = {k: v for k, v in rec.items() if k not in WORD_KEYS and k not in DETAIL_KEYS and k not in IGNORED}
    res = rec.get("results")
    if isinstance(res, list):
        detail["cases"] = [{k: r.get(k) for k in ("value",) + DETAIL_KEYS if k in r} if isinstance(r, dict) else r
                           for r in res]
        decision["results"] = [{k: v for k, v in r.items() if k != "value" and k not in DETAIL_KEYS}
                               if isinstance(r, dict) else r for r in res]
    return {"decision": decision, "detail": detail, "words": words}


def digest(rec: dict) -> dict:
    return {k: _h(v) for k, v in parts(rec).items()}


def digest_dir(run_dir: str) -> dict:
    """{test id: digest} of a finished run's decision tests (fail closed: no artifact, a malformed one, a duplicated
    id or a listed result file that is missing raises)."""
    import yaml
    p = os.path.join(run_dir, "analysis", "decision_tests.yaml")
    if not os.path.isfile(p):
        raise FileNotFoundError(f"{p}: no decision-test artifact (the analysis did not reach its decision tests)")
    with open(p) as f:
        doc = yaml.safe_load(f)
    tests = ((doc or {}).get("data") or {}).get("tests") if isinstance(doc, dict) else None
    if not isinstance(tests, list):
        raise ValueError(f"{p}: data.tests is not a list")
    out = {}
    for t in tests:
        tid = str((t or {}).get("test_id"))
        if tid in out:
            raise ValueError(f"{p}: test id {tid} is listed twice")
        if t.get("path"):
            f_ = os.path.join(run_dir, t["path"])
            if not os.path.isfile(f_):
                raise FileNotFoundError(f"{f_}: listed for decision test {tid} but missing")
            with open(f_) as f:
                out[tid] = digest(yaml.safe_load(f))
        else:
            out[tid] = digest({"status": t.get("status"), "test_id": tid, "statement": t.get("statement")})
    return out


def compare(expected: dict, got: dict) -> list[str]:
    """One line per test missing, not expected, or with a part that differs."""
    out = []
    for k in sorted(set(expected) | set(got)):
        if k not in got:
            out.append(f"{k}: missing")
        elif k not in expected:
            out.append(f"{k}: not expected")
        else:
            out += [f"{k}: {p} differs" for p in ("decision", "detail", "words")
                    if (expected[k] or {}).get(p) != (got[k] or {}).get(p)]
    return out


class no_engine_jobs:
    """While active, scripts/compute.py's map_fn (every engine batch: batch.py, row_reuse.py, the joint calibration,
    trajectories) and the engine class's run / run_batch / run_series raise EngineJobRefused; `refused` counts the
    attempts, so one an analysis caught and survived is still reported."""
    METHODS = ("run", "run_batch", "run_series")

    def __init__(self, engine_cls=None):
        self.cls, self.refused, self._saved = engine_cls, 0, []

    def _refuse(self, what):
        self.refused += 1
        raise EngineJobRefused(f"an engine job was requested ({what}); a retro computes nothing: the stored rows "
                               "do not cover this analysis")

    def __enter__(self):
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import compute
        orig = compute.map_fn
        guard = self

        def map_fn(run_dir, fn_spec, inputs, *a, **k):
            if inputs:
                guard._refuse(f"{len(inputs)} task(s) of {fn_spec} through scripts/compute.py, purpose "
                              f"{k.get('purpose')!r}")
            return orig(run_dir, fn_spec, inputs, *a, **k)
        self._saved.append((compute, "map_fn", compute.__dict__["map_fn"]))
        compute.map_fn = map_fn
        for m in self.METHODS if self.cls is not None else ():
            if callable(getattr(self.cls, m, None)):
                self._saved.append((self.cls, m, self.cls.__dict__.get(m, _MISSING)))
                setattr(self.cls, m, lambda *a, _m=m, **k: guard._refuse(f"{self.cls.__name__}.{_m} in-process"))
        return self

    def __exit__(self, *exc):
        for obj, name, val in reversed(self._saved):
            if val is _MISSING:
                delattr(obj, name)
            else:
                setattr(obj, name, val)
        self._saved = []
        return False


_MISSING = object()


def retro(run_dir: str, work: str, spec_rel: str = "decision_spec.yaml", unsigned: bool = False, log=print) -> dict:
    """The analysis of a copy of `run_dir` (in `work`, empty or absent, outside the run dir) re-run on the current code
    with zero engine jobs; its decision tests digested and compared with the run's stored ones (module docstring).
    {status: complete | blocked | engine_job_refused | error, reason, engine_jobs, tests, digests, stored,
    stored_vs_rerun, work}."""
    run_dir, work = os.path.abspath(run_dir), os.path.abspath(work)
    if work == run_dir or work.startswith(run_dir + os.sep):
        raise ValueError(f"--work {work} is inside the run dir; the collected run is never written")
    if os.path.exists(work) and os.listdir(work):
        raise ValueError(f"--work {work} is not empty")
    if not os.path.isfile(os.path.join(run_dir, spec_rel)):
        raise FileNotFoundError(f"{os.path.join(run_dir, spec_rel)}: no decision spec")
    try:
        stored = digest_dir(run_dir)
    except FileNotFoundError:
        stored = None
    dst = os.path.join(work, os.path.basename(run_dir))
    shutil.copytree(run_dir, dst, symlinks=True)
    from toolkit.analysis import run as RUN
    from toolkit.spec import schema
    cls = type(schema.load(os.path.join(dst, spec_rel)).engine())
    status, reason = "complete", None
    paused, _CAP["paused"] = _CAP["paused"], True       # a retro inside a capturing self-test is not a golden record
    try:
        with no_engine_jobs(cls) as g:
            try:
                RUN.run(dst, spec_rel, unsigned=unsigned, log=log)
            except EngineJobRefused as e:
                status, reason = "engine_job_refused", str(e)
            except RUN.AnalysisBlocked as e:
                status, reason = "blocked", f"{e.where}: {e.reason or ''}"
            except Exception as e:  # noqa: BLE001 -- reported with the refused count (an engine refusal may be wrapped)
                status, reason = "error", f"{type(e).__name__}: {str(e)[:500]}"
    finally:
        _CAP["paused"] = paused
    if status == "complete" and g.refused:
        status, reason = "engine_job_refused", f"{g.refused} engine job request(s) refused and caught by the analysis"
    digests = digest_dir(dst) if status == "complete" else {}
    return {"status": status, "reason": reason, "engine_jobs": g.refused, "tests": len(digests), "digests": digests,
            "stored": "absent" if stored is None else "present",
            "stored_vs_rerun": compare(stored, digests) if stored is not None and status == "complete" else None,
            "work": dst}


_CAP = {"on": False, "paused": False, "orig": None, "recs": {}, "counts": {}}
_RUNNERS = {"_run", "run", "run_one", "run_all", "_self_test_retro"}


def _caller() -> str:
    """The run.py self-test function that asked for the record (the nearest frame of run.py outside the runners)."""
    f = sys._getframe(2)
    tail = os.path.join("toolkit", "analysis", "run.py")
    while f is not None:
        if f.f_code.co_filename.endswith(tail) and f.f_code.co_name not in _RUNNERS:
            return f.f_code.co_name
        f = f.f_back
    return "other"


def start_capture() -> None:
    """Every record decision_tests.run_one returns from here on is digested (check_capture compares them)."""
    from toolkit.analysis import decision_tests as DTm
    from toolkit.analysis.artifacts import plain
    if _CAP["on"]:
        raise RuntimeError("dt_golden: capture already started")
    orig = DTm.run_one
    _CAP.update(on=True, paused=False, orig=orig, recs={}, counts={})

    def run_one(*a, **k):
        rec = orig(*a, **k)
        if _CAP["on"] and not _CAP["paused"]:
            who, tid = _caller(), str(rec.get("test_id"))
            n = _CAP["counts"].get((who, tid), 0)
            _CAP["counts"][(who, tid)] = n + 1
            _CAP["recs"][f"{who}:{tid}#{n}"] = digest(plain(rec))
        return rec
    DTm.run_one = run_one


def check_capture(ok) -> None:
    """Stop capturing and compare the records with GOLDEN (MA_DT_GOLDEN_UPDATE=1 rewrites it instead)."""
    from toolkit.analysis import decision_tests as DTm
    if not _CAP["on"]:
        ok(False, "DG-R1 the golden capture was not started")
        return
    DTm.run_one = _CAP["orig"]
    obs = dict(sorted(_CAP["recs"].items()))
    _CAP.update(on=False, orig=None)
    if os.environ.get(UPDATE_ENV) == "1":
        with open(GOLDEN, "w") as f:
            f.write("{\n" + ",\n".join(f" {json.dumps(k)}: {json.dumps(v, sort_keys=True)}" for k, v in obs.items())
                    + "\n}\n")
        ok(bool(obs), f"DG-R1 {len(obs)} golden records written to {GOLDEN} ({UPDATE_ENV}=1)")
        return
    try:
        with open(GOLDEN) as f:
            exp = json.load(f)
    except (OSError, ValueError) as e:
        exp, err = {}, f"{type(e).__name__}: {e}"
    else:
        err = None
    diff = compare(exp, obs)
    ok(obs and not diff and err is None, f"DG-R1 every decision-test record of this self-test matches its golden "
                                         f"record ({len(obs)} records; {err or ''} {diff[:12]})")
    if diff or err:
        print("DT_GOLDEN_OBSERVED " + json.dumps(obs, sort_keys=True))


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
    ap = argparse.ArgumentParser(description="decision-test golden records (module docstring)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("digest", help="digest a finished run's decision tests")
    d.add_argument("run_dir")
    d.add_argument("--out")
    r = sub.add_parser("retro", help="re-run a copy's analysis on stored rows (zero engine jobs) and digest it")
    r.add_argument("run_dir")
    r.add_argument("--work", required=True, help="an empty directory outside the run dir for the copy")
    r.add_argument("--spec", default="decision_spec.yaml")
    r.add_argument("--unsigned", action="store_true", help="re-run a copy whose spec signature does not verify here")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", help="record the re-run's digests to this file (kept outside the repo)")
    g.add_argument("--expect", help="compare the re-run's digests with this file")
    a = ap.parse_args(argv)
    if a.cmd == "digest":
        got = digest_dir(a.run_dir)
        text = json.dumps(got, indent=1, sort_keys=True)
        if a.out:
            with open(a.out, "w") as f:
                f.write(text + "\n")
        print(text if not a.out else f"{len(got)} decision tests digested to {a.out}")
        return 0
    rep = retro(a.run_dir, a.work, a.spec, a.unsigned)
    rep["expected_vs_rerun"] = None
    code = 0 if rep["status"] == "complete" and rep["engine_jobs"] == 0 else 2 if rep["status"] == "blocked" else 1
    if code == 0 and a.write:
        with open(a.write, "w") as f:
            f.write(json.dumps(rep["digests"], indent=1, sort_keys=True) + "\n")
    if code == 0 and a.expect:
        with open(a.expect) as f:
            rep["expected_vs_rerun"] = compare(json.load(f), rep["digests"])
        code = 1 if rep["expected_vs_rerun"] else 0
    print(json.dumps({k: v for k, v in rep.items() if k != "digests"}, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
