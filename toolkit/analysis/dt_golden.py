#!/usr/bin/env python3
"""Golden records of decision tests (simplification pass S1): digests that pin what each decision test decided, so a
refactor of the decision-test code (one generic `perturb` type, design note S2-S3) can show it changed nothing.

    python3 toolkit/analysis/dt_golden.py --self-test
    python3 toolkit/analysis/dt_golden.py digest <run_dir> [--out F]
    python3 toolkit/analysis/dt_golden.py retro <run_dir> --work <empty dir> (--write F | --expect F) [--unsigned]

Parts of a record's digest (sha256 prefixes of canonical JSON; parts()): `decision` (status, plan changes, shares,
regret, dollars moved, recommendations, benefit changes, identities, each case's results, the run's summary entry;
not `type` or `perturb`, and any field not named here), `detail` (case labels and the type's own block), `words`
(the sentences) and `plans` (each case's allocation digest, decision_tests.plan_sha256). Numbers: 6 significant
digits, below 1e-9 in size zero; an absolute path keeps its file name.

`digest` reads analysis/decision_tests.yaml and models/decision_tests/<id>.yaml, and fails closed (digest_dir).
`retro` copies the run into --work (outside the run dir by real path), removes the copy's decision-test outputs,
re-runs the analysis there on the current code with the run's own number of draws, and digests it. Zero engine jobs:
compute.map_fn and the engine's evaluating methods raise EngineJobRefused, so a run whose stored rows (row_reuse,
#241/#244) do not cover the analysis, or a direct-path run, is refused, never computed (legacy rows may need
MA_JC_LEGACY_ADAPTER_SHA256). The collected run is never written. --write refuses a re-run that differs from the
stored results unless --accept-stored-diff; expected digests of real runs are kept outside the repo.

Toy golden records: toolkit/analysis/dt_golden.json, checked by toolkit/analysis/run.py's self-test, which captures
every record decision_tests.run_one returns (start_capture / check_capture), keyed by the calling self-test function,
test id and call order. A PR that changes a digest says why; MA_DT_GOLDEN_UPDATE=1 rewrites the file.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
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
               "arm_inputs", "rejected_values", "missing_subtraction", "automatic",
               "parameter", "info")
IGNORED = ("type", "perturb", "path", "producer", "produced_by")
PARTS = ("decision", "detail", "words", "plans")
STORED_PARTS = ("decision", "detail", "words")      # stored records from before this module carry no plans part
TINY = 1e-9
_PATH = re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\s\\/'\"]+[\\/])+([^\s\\/'\"]+)")


class EngineJobRefused(RuntimeError):
    """An engine job was requested during a retro (stored rows do not cover the analysis)."""


class StoredIncomplete(ValueError):
    """The run's decision-test artifact lists a result it does not hold (or holds it in a form refused)."""


def canon(x):
    """The canonical form digested: numbers to 6 significant digits with values below 1e-9 in size as zero (ints equal
    floats), sets sorted, every absolute path (POSIX or Windows, alone or inside a sentence) to its file name, mappings
    by string key."""
    import numpy as np
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if x is None:
        return None
    if isinstance(x, (int, float, np.integer, np.floating)):
        v = float(x)
        if not math.isfinite(v):
            return "nan" if math.isnan(v) else ("inf" if v > 0 else "-inf")
        return 0.0 if abs(v) < TINY else float(f"{v:.{SIG}g}")
    if isinstance(x, str):
        return _PATH.sub(lambda m: m.group(1), x)
    if isinstance(x, dict):
        return {str(k): canon(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, np.ndarray)):
        return [canon(v) for v in (x.tolist() if isinstance(x, np.ndarray) else x)]
    if isinstance(x, (set, frozenset)):
        return sorted((canon(v) for v in x), key=lambda v: json.dumps(v, sort_keys=True))
    return str(x)


def _h(x) -> str:
    return hashlib.sha256(json.dumps(canon(x), sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:16]


def parts(rec: dict, summary: dict | None = None) -> dict:
    """{decision, detail, words, plans} of one decision-test record and, from a finished run, its entry in
    analysis/decision_tests.yaml (module docstring). A field this module does not name is decision: pinned, never
    ignored. A case's label is detail, also where moves.py repeats it (worst_by_criterion.*.value); each case's
    allocation digest (plan_sha256) is the plans part."""
    if not isinstance(rec, dict):
        raise ValueError(f"a decision-test record is a mapping, not {type(rec).__name__}")
    words = {k: rec[k] for k in WORD_KEYS if k in rec}
    detail = {k: rec[k] for k in DETAIL_KEYS if k in rec}
    decision = {k: v for k, v in rec.items() if k not in WORD_KEYS and k not in DETAIL_KEYS and k not in IGNORED}
    plans = None
    res = rec.get("results")
    if isinstance(res, list):
        detail["cases"] = [{k: r.get(k) for k in ("value",) + DETAIL_KEYS if k in r} if isinstance(r, dict) else r
                           for r in res]
        decision["results"] = [{k: v for k, v in r.items() if k not in ("value", "plan_sha256") and k not in DETAIL_KEYS}
                               if isinstance(r, dict) else r for r in res]
        if any(isinstance(r, dict) and "plan_sha256" in r for r in res):
            plans = [r.get("plan_sha256") if isinstance(r, dict) else None for r in res]
    wbc = decision.get("worst_by_criterion")
    if isinstance(wbc, dict):
        decision["worst_by_criterion"] = {k: ({kk: vv for kk, vv in v.items() if kk != "value"}
                                              if isinstance(v, dict) else v) for k, v in wbc.items()}
    if summary is not None:
        detail["summary_detail"] = summary.get("detail")
        if "statement" in summary:
            words["summary_statement"] = summary["statement"]
        decision["summary"] = {k: v for k, v in summary.items() if k not in ("path", "statement", "detail")}
    out = {"decision": decision, "detail": detail, "words": words}
    if plans is not None:
        out["plans"] = plans
    return out


def digest(rec: dict, summary: dict | None = None) -> dict:
    return {k: _h(v) for k, v in parts(rec, summary).items()}


def _inside(path: str, root: str) -> bool:
    p, r = os.path.realpath(path), os.path.realpath(root)
    return p == r or p.startswith(r + os.sep)


def _regular_in(run_dir: str, rel: str) -> str:
    """The absolute path of `rel` under run_dir when no component is a link and it is a regular file there."""
    cur = os.path.realpath(run_dir)
    for c in rel.split("/"):
        cur = os.path.join(cur, c)
        if os.path.islink(cur):
            raise StoredIncomplete(f"{rel}: {cur} is a link (a result is read from the run dir only)")
    if not os.path.isfile(cur):
        raise StoredIncomplete(f"{rel}: listed but missing")
    return cur


def digest_dir(run_dir: str) -> dict:
    """{test id: digest} of a finished run's decision tests, each with its summary entry. Fail closed: no artifact
    raises FileNotFoundError; a malformed artifact, an entry without an id or with an unknown status, a repeated id,
    a run test without its result file, a result file elsewhere than models/decision_tests/<id>.yaml or through a link,
    or a result whose test id or status differs from its entry raises StoredIncomplete."""
    import yaml
    p = os.path.join(run_dir, "analysis", "decision_tests.yaml")
    if not os.path.isfile(p) or os.path.islink(p):
        raise FileNotFoundError(f"{p}: no decision-test artifact (the analysis did not reach its decision tests)")
    with open(p) as f:
        doc = yaml.safe_load(f)
    tests = ((doc or {}).get("data") or {}).get("tests") if isinstance(doc, dict) else None
    if not isinstance(tests, list):
        raise StoredIncomplete(f"{p}: data.tests is not a list")
    out = {}
    for t in tests:
        tid = t.get("test_id") if isinstance(t, dict) else None
        if not isinstance(tid, str) or not tid:
            raise StoredIncomplete(f"{p}: an entry without a test id ({t!r:.120})")
        if tid in out:
            raise StoredIncomplete(f"{p}: test id {tid} is listed twice")
        st = t.get("status")
        if st not in ("run", "not_run"):
            raise StoredIncomplete(f"{p}: test {tid}: status {st!r} is neither run nor not_run")
        if st == "not_run":
            if t.get("path"):
                raise StoredIncomplete(f"{p}: test {tid} was not run but lists a result file")
            out[tid] = digest({"status": st, "test_id": tid}, t)
            continue
        want = f"models/decision_tests/{tid}.yaml"
        if t.get("path") != want:
            raise StoredIncomplete(f"{p}: test {tid}: result file {t.get('path')!r}, not {want}")
        with open(_regular_in(run_dir, want)) as f:
            rec = yaml.safe_load(f)
        if not isinstance(rec, dict) or rec.get("test_id") != tid or rec.get("status") != st:
            raise StoredIncomplete(f"{want}: holds test {(rec or {}).get('test_id')!r} with status "
                                   f"{(rec or {}).get('status')!r}, not {tid} {st}")
        out[tid] = digest(rec, t)
    return out


def compare(expected: dict, got: dict, parts: tuple | None = None) -> list[str]:
    """One line per test missing, not expected, or with a part that differs (every part, or the named ones)."""
    out = []
    for k in sorted(set(expected) | set(got)):
        if k not in got:
            out.append(f"{k}: missing")
        elif k not in expected:
            out.append(f"{k}: not expected")
        else:
            e, g = expected[k] or {}, got[k] or {}
            out += [f"{k}: {p} differs" for p in (parts or sorted(set(e) | set(g))) if e.get(p) != g.get(p)]
    return out


class no_engine_jobs:
    """While active, scripts/compute.py's map_fn (every engine batch: batch.py, row_reuse.py, the joint calibration,
    trajectories) and the engine class's evaluating methods (run, run_batch, run_series, initial_state, build_sim)
    raise EngineJobRefused; `refused` counts the attempts, so one an analysis caught and survived is still reported.
    One guard at a time per process: a second is refused (overlapping guards would restore each other's methods)."""
    METHODS = ("run", "run_batch", "run_series", "initial_state", "build_sim")
    _active = None

    def __init__(self, engine_cls=None):
        self.cls, self.refused, self._saved = engine_cls, 0, []

    def _refuse(self, what):
        self.refused += 1
        raise EngineJobRefused(f"an engine job was requested ({what}); a retro computes nothing: the stored rows "
                               "do not cover this analysis")

    def __enter__(self):
        if no_engine_jobs._active is not None:
            raise RuntimeError("dt_golden: an engine-job guard is already active in this process (one retro at a time)")
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
        no_engine_jobs._active = self
        return self

    def __exit__(self, *exc):
        for obj, name, val in reversed(self._saved):
            if val is _MISSING:
                delattr(obj, name)
            else:
                setattr(obj, name, val)
        self._saved = []
        no_engine_jobs._active = None
        return False


_MISSING = object()


def write_out(path: str, text: str, run_dir: str) -> None:
    """An operator's output file (--out, --write): never inside the run dir, never through a link."""
    path = os.path.abspath(path)
    if _inside(path, run_dir) or _inside(os.path.dirname(path), run_dir):
        raise ValueError(f"{path} is inside the run dir {run_dir}; the collected run is never written")
    if os.path.islink(path):
        raise ValueError(f"{path} is a link; give a plain file path")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def write_allowed(rep: dict, accept_stored_diff: bool) -> tuple[bool, str]:
    """Whether --write may record a retro's digests as the expectation."""
    if rep.get("status") != "complete" or rep.get("engine_jobs"):
        return False, f"the retro did not complete with zero engine jobs ({rep.get('status')}: {rep.get('reason')})"
    if rep.get("stored_vs_rerun") and not accept_stored_diff:
        return False, ("the re-run differs from the run's stored results " + "; ".join(rep["stored_vs_rerun"][:8])
                       + " (pass --accept-stored-diff to record it anyway, and say why)")
    return True, ""


def stored_draws(run_dir: str):
    """The number of draws the run's own uncertainty step used (analysis/uncertainty.yaml), or None."""
    import yaml
    try:
        with open(os.path.join(run_dir, "analysis", "uncertainty.yaml")) as f:
            n = ((yaml.safe_load(f) or {}).get("data") or {}).get("draws")
    except (OSError, ValueError, AttributeError):
        return None
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None


def retro(run_dir: str, work: str, spec_rel: str = "decision_spec.yaml", unsigned: bool = False, log=print,
          runner=None) -> dict:
    """The analysis of a copy of `run_dir` (in `work`, empty or absent, outside the run dir by real path) re-run on the
    current code with zero engine jobs and the run's own number of draws, after the copy's decision-test outputs are
    removed (so only freshly written results are read); its decision tests digested and compared with the run's stored
    ones (module docstring). {status: complete | blocked | engine_job_refused | error, reason, engine_jobs, draws,
    tests, digests, stored, stored_vs_rerun, links_outside, work}."""
    run_dir, work = os.path.abspath(run_dir), os.path.abspath(work)
    if _inside(work, run_dir):
        raise ValueError(f"--work {work} is inside the run dir; the collected run is never written")
    if os.path.exists(work) and os.listdir(work):
        raise ValueError(f"--work {work} is not empty")
    if not os.path.isfile(os.path.join(run_dir, spec_rel)):
        raise FileNotFoundError(f"{os.path.join(run_dir, spec_rel)}: no decision spec")
    try:
        stored = digest_dir(run_dir)
    except FileNotFoundError:          # no artifact at all; StoredIncomplete (a corrupt one) propagates
        stored = None
    dst = os.path.join(work, os.path.basename(run_dir))
    shutil.copytree(run_dir, dst, symlinks=True)
    links = sorted(os.path.relpath(os.path.join(r, n), dst) for r, ds, fs in os.walk(dst) for n in ds + fs
                   if os.path.islink(os.path.join(r, n)) and not _inside(os.path.join(r, n), dst))
    for rel in ("analysis/decision_tests.yaml", "models/decision_tests"):      # Codex r1 P1-5: nothing stale is read
        q = os.path.join(dst, rel)
        if os.path.islink(q) or os.path.isfile(q):
            os.remove(q)
        elif os.path.isdir(q):
            shutil.rmtree(q)
    from toolkit.analysis import run as RUN
    from toolkit.spec import schema
    cls = type(schema.load(os.path.join(dst, spec_rel)).engine())
    draws = stored_draws(run_dir)
    status, reason = "complete", None
    paused, _CAP["paused"] = _CAP["paused"], True       # a retro inside a capturing self-test is not a golden record
    try:
        with no_engine_jobs(cls) as g:
            try:
                (runner or RUN.run)(dst, spec_rel, unsigned=unsigned, log=log, draws=draws)
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
    digests = {}
    if status == "complete":
        try:
            digests = digest_dir(dst)
        except (FileNotFoundError, StoredIncomplete) as e:
            status, reason = "error", f"the re-run wrote no decision-test artifact or an incomplete one: {e}"
    return {"status": status, "reason": reason, "engine_jobs": g.refused, "draws": draws, "tests": len(digests),
            "digests": digests, "stored": "absent" if stored is None else "present",
            "stored_vs_rerun": (compare(stored, digests, STORED_PARTS) if stored is not None and status == "complete"
                                else None), "links_outside": links, "work": dst}


_CAP = {"on": False, "paused": False, "orig": None, "recs": {}, "counts": {}, "problems": []}
_RUNNERS = {"_run", "run", "run_one", "run_all", "_self_test_retro"}


def _caller() -> str:
    """The run.py self-test function that asked for the record (the nearest frame of run.py outside the runners, by
    qualified name, so same-named helpers of different tests do not share keys)."""
    f = sys._getframe(2)
    tail = os.path.join("toolkit", "analysis", "run.py")
    while f is not None:
        if f.f_code.co_filename.endswith(tail) and f.f_code.co_name not in _RUNNERS:
            return getattr(f.f_code, "co_qualname", f.f_code.co_name)
        f = f.f_back
    return "other"


def start_capture() -> None:
    """Every record decision_tests.run_one returns from here on is digested (check_capture compares them). Callers
    must look run_one up on the module (run.py does), not hold a reference bound before this call."""
    from toolkit.analysis import decision_tests as DTm
    from toolkit.analysis.artifacts import plain
    if _CAP["on"]:
        raise RuntimeError("dt_golden: capture already started")
    orig = DTm.run_one
    _CAP.update(on=True, paused=False, orig=orig, recs={}, counts={}, problems=[])

    def run_one(*a, **k):
        rec = orig(*a, **k)
        if _CAP["on"] and not _CAP["paused"]:
            who, tid = _caller(), str(rec.get("test_id"))
            n = _CAP["counts"].get((who, tid), 0)
            _CAP["counts"][(who, tid)] = n + 1
            _CAP["recs"][f"{who}:{tid}#{n}"] = digest(plain(rec))
            if rec.get("status") == "run" and not all("plan_sha256" in r for r in rec.get("results") or []):
                _CAP["problems"].append(f"{who}:{tid}#{n}: a case without plan_sha256")
        return rec
    DTm.run_one = run_one


def check_capture(ok, failed=()) -> None:
    """Stop capturing and compare the records with GOLDEN. MA_DT_GOLDEN_UPDATE=1 rewrites it instead, only when no
    earlier case of the self-test failed (a partial capture never becomes the baseline)."""
    from toolkit.analysis import decision_tests as DTm
    if not _CAP["on"]:
        ok(False, "DG-R1 the golden capture was not started")
        return
    DTm.run_one = _CAP["orig"]
    obs, probs = dict(sorted(_CAP["recs"].items())), list(_CAP["problems"])
    _CAP.update(on=False, orig=None)
    ok(not probs, f"DG-P1 every run case records its allocation digest (plan_sha256) ({probs[:5]})")
    if os.environ.get(UPDATE_ENV) == "1":
        if failed:
            ok(False, f"DG-R1 {UPDATE_ENV}=1 refused: {len(failed)} earlier case(s) failed, so the capture may be "
                      "partial; the golden file is unchanged")
            return
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
            ok(set(got) == {"DT1", "DT2"} and got["DT1"] == digest(_rec(), tests[0]) and got["DT2"]["decision"]
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

    # ------------------------------------------------ Codex review r1 of #262 (written first; each failed)
    def dg9():
        from toolkit.engines.contract import ToyEngine

        class StateEngine(ToyEngine):
            def initial_state(self, params, unit, package=None, horizon=None):
                return {"population": 100}
        with no_engine_jobs(StateEngine) as g:
            r = _raises(lambda: StateEngine().initial_state({}, None), EngineJobRefused)
            nested = _raises(lambda: no_engine_jobs(ToyEngine).__enter__(), RuntimeError)
        ok(r and g.refused == 1, "DG9 (P1-2) the engine's initial_state is an engine evaluation: refused")
        ok(nested and not hasattr(StateEngine, "__dg_guard__"),
           "DG9b (P2-5) a second guard while one is active is refused (no overlapping retros in one process)")
    case("DG9", dg9)

    def dg10():
        with tempfile.TemporaryDirectory() as d:
            real = os.path.join(d, "real")
            os.makedirs(real)
            os.symlink(real, os.path.join(d, "alias"))
            ok(_raises(lambda: retro(os.path.join(d, "alias"), os.path.join(real, "w"), log=lambda *a: None)),
               "DG10 (P1-3) a work dir inside the run dir through another spelling (a link) is refused")
            ok(_raises(lambda: write_out(os.path.join(d, "alias", "g.json"), "{}", real)),
               "DG10b (P1-3) a --write/--out file inside the run dir is refused")
            os.symlink(os.path.join(d, "target"), os.path.join(d, "out.json"))
            ok(_raises(lambda: write_out(os.path.join(d, "out.json"), "{}", real))
               and not os.path.exists(os.path.join(d, "target")), "DG10c (P1-3) an output that is a link is refused")
            write_out(os.path.join(d, "fine.json"), "{}", real)
            ok(open(os.path.join(d, "fine.json")).read() == "{}", "DG10d an output outside the run dir is written")
    case("DG10", dg10)

    def fixture(d, tests, files):
        os.makedirs(os.path.join(d, "analysis"), exist_ok=True)
        os.makedirs(os.path.join(d, "models", "decision_tests"), exist_ok=True)
        for name, rec in files.items():
            yaml.safe_dump(rec, open(os.path.join(d, "models", "decision_tests", name), "w"))
        yaml.safe_dump({"data": {"tests": tests}}, open(os.path.join(d, "analysis", "decision_tests.yaml"), "w"))

    def dg11():
        run1 = {"test_id": "DT1", "path": "models/decision_tests/DT1.yaml", "status": "run", "verdict": "NO_CHANGE"}
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            yaml.safe_dump(_rec(), open(os.path.join(other, "x.yaml"), "w"))
            fixture(d, [dict(run1, path="../" + os.path.basename(other) + "/x.yaml")], {})
            ok(_raises(lambda: digest_dir(d)), "DG11 (P1-4) a listed result outside models/decision_tests/<id>.yaml "
                                               "is refused")
            fixture(d, [run1], {})
            os.symlink(os.path.join(other, "x.yaml"), os.path.join(d, "models", "decision_tests", "DT1.yaml"))
            ok(_raises(lambda: digest_dir(d)), "DG11b (P1-4) a result file that is a link is refused")
        bad_cases = [([{}], {}, "an entry without a test id"),
                     ([dict(run1, path=None)], {}, "a run test without a result file"),
                     ([run1], {"DT1.yaml": _rec(test_id="DT2")}, "a result file of another test"),
                     ([run1], {"DT1.yaml": _rec(status="not_run")}, "a result whose status differs"),
                     ([dict(run1, status="maybe")], {"DT1.yaml": _rec()}, "an unknown status")]
        for tests, files, what in bad_cases:
            with tempfile.TemporaryDirectory() as d:
                fixture(d, tests, files)
                ok(_raises(lambda: digest_dir(d), ValueError), f"DG11c (P1-6) refused: {what}")
        with tempfile.TemporaryDirectory() as d:
            fixture(d, [run1], {"DT1.yaml": _rec()})
            a = digest_dir(d)["DT1"]
            fixture(d, [dict(run1, verdict="CHANGES_PLAN")], {"DT1.yaml": _rec()})
            ok(digest_dir(d)["DT1"]["decision"] != a["decision"], "DG11d (P1-6) the summary's verdict is decision")
            nr = {"test_id": "DT2", "path": None, "status": "not_run", "threshold_pct": 10.0, "statement": "x"}
            fixture(d, [nr], {})
            b = digest_dir(d)["DT2"]
            fixture(d, [dict(nr, threshold_pct=99.0)], {})
            ok(digest_dir(d)["DT2"]["decision"] != b["decision"], "DG11e (P1-6) a not-run entry's fields are decision")
            fixture(d, [run1], {})
            os.remove(os.path.join(d, "models", "decision_tests", "DT1.yaml"))
            ok(_raises(lambda: digest_dir(d), StoredIncomplete),
               "DG11f (P1-7) a listed result file that is missing is StoredIncomplete, not an absent artifact")
    case("DG11", dg11)

    def dg12():
        from toolkit.spec import schema
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as w:
            schema.toy_spec(d)
            fixture(d, [{"test_id": "DT1", "path": "models/decision_tests/DT1.yaml", "status": "run"}],
                    {"DT1.yaml": _rec()})
            rep = retro(d, os.path.join(w, "work"), unsigned=True, log=lambda *a: None, runner=lambda *a, **k: None)
            ok(rep["status"] == "error" and rep["tests"] == 0 and "no decision-test artifact" in (rep["reason"] or ""),
               f"DG12 (P1-5) the copy's stored decision-test outputs are removed first: a re-run that writes none is "
               f"an error, never the stored results ({rep['status']}: {rep['reason']})")
            ok(write_allowed({"status": "complete", "engine_jobs": 0, "stored_vs_rerun": ["DT1: decision differs"]},
                             False)[0] is False
               and write_allowed({"status": "complete", "engine_jobs": 0, "stored_vs_rerun": ["x"]}, True)[0]
               and write_allowed({"status": "complete", "engine_jobs": 0, "stored_vs_rerun": []}, False)[0],
               "DG12b (P1-7) --write refuses a re-run that disagrees with the stored results unless accepted by name")
            yaml.safe_dump({"data": {"draws": 2}}, open(os.path.join(d, "analysis", "uncertainty.yaml"), "w"))
            seen = {}
            retro(d, os.path.join(w, "work2"), unsigned=True, log=lambda *a: None,
                  runner=lambda *a, **k: seen.update(k))
            ok(seen.get("draws") == 2, f"DG12c (P2-1) the retro re-runs with the run's own number of draws ({seen})")
    case("DG12", dg12)

    def dg13():
        a = digest(_rec(perturbation_identity=[False]))
        ok(digest(_rec(perturbation_identity=[True]))["decision"] != a["decision"],
           "DG13 (P2-2) perturbation_identity is decision (a departure's effectiveness reads it)")
        w = lambda lab: _rec(results=[dict(_rec()["results"][0], value=lab)],  # noqa: E731
                             worst_by_criterion={"share": {"value": lab, "share": 0.125}})
        ok(digest(w("case"))["decision"] == digest(w(0.8))["decision"],
           "DG13b (P2-2) a case label repeated under worst_by_criterion is not decision")
        p = lambda h: _rec(results=[dict(_rec()["results"][0], plan_sha256=h)])  # noqa: E731
        ok(digest(p("aa"))["plans"] != digest(p("bb"))["plans"] and digest(p("aa"))["decision"]
           == digest(p("bb"))["decision"], "DG13c (P2-3) each case's allocation digest is its own part (plans)")
        ok(compare({"A": {"decision": "1"}}, {"A": {"decision": "1", "plans": "2"}}, parts=("decision",)) == [],
           "DG13d compare over named parts (stored records from before plans existed)")
    case("DG13", dg13)

    def dg14():
        ok(canon("C:\\run\\inputs\\results.yaml") == "results.yaml" and
           canon("prior from /tmp/abc/priors.yaml (card)") == "prior from priors.yaml (card)",
           f"DG14 (P2-4) Windows paths and paths inside sentences keep their file name "
           f"({canon('prior from /tmp/abc/priors.yaml (card)')})")
        ok(canon(1e-14) == canon(0) and canon(-1e-13) == 0.0, "DG14b (P2-4) values below 1e-9 in size are zero")
    case("DG14", dg14)

    def dg15():
        seen = []
        _CAP.update(on=True, paused=False, orig=lambda *a, **k: None, recs={"x": {}}, counts={}, problems=[])
        from toolkit.analysis import decision_tests as DTm
        saved = DTm.run_one
        try:
            os.environ[UPDATE_ENV] = "1"
            g0 = open(GOLDEN).read() if os.path.exists(GOLDEN) else None
            check_capture(lambda c, m: seen.append((c, m)), failed=["an earlier case"])
            g1 = open(GOLDEN).read() if os.path.exists(GOLDEN) else None
        finally:
            os.environ.pop(UPDATE_ENV, None)
            DTm.run_one = saved
        ok(g0 == g1 and any(c is False and "refused" in m for c, m in seen),
           f"DG15 (P2-7) update mode refuses to rewrite the golden file after a failed case ({seen})")
    case("DG15", dg15)

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
    r.add_argument("--accept-stored-diff", action="store_true",
                   help="--write even when the re-run differs from the run's stored results (say why in the record)")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--write", help="record the re-run's digests to this file (outside the run dir; kept outside the repo)")
    g.add_argument("--expect", help="compare the re-run's digests with this file")
    a = ap.parse_args(argv)
    if a.cmd == "digest":
        got = digest_dir(a.run_dir)
        text = json.dumps(got, indent=1, sort_keys=True)
        if a.out:
            write_out(a.out, text + "\n", a.run_dir)
        print(text if not a.out else f"{len(got)} decision tests digested to {a.out}")
        return 0
    for f_ in (a.write, a.expect):
        if f_ and _inside(f_, a.run_dir):
            ap.error(f"{f_} is inside the run dir; the collected run is never written")
    rep = retro(a.run_dir, a.work, a.spec, a.unsigned)
    rep["expected_vs_rerun"] = None
    code = 0 if rep["status"] == "complete" and rep["engine_jobs"] == 0 else 2 if rep["status"] == "blocked" else 1
    if code == 0 and a.write:
        allowed, why = write_allowed(rep, a.accept_stored_diff)
        if allowed:
            write_out(a.write, json.dumps(rep["digests"], indent=1, sort_keys=True) + "\n", a.run_dir)
        else:
            rep["write_refused"], code = why, 1
    if code == 0 and a.expect:
        with open(a.expect) as f:
            rep["expected_vs_rerun"] = compare(json.load(f), rep["digests"])
        code = 1 if rep["expected_vs_rerun"] else 0
    print(json.dumps({k: v for k, v in rep.items() if k != "digests"}, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
