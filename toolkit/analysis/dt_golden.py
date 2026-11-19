#!/usr/bin/env python3
"""Golden records of decision tests (simplification pass S1): digests that pin what each decision test decided, so a
refactor of the decision-test code (one generic `perturb` type, design note S2-S3) can show it changed nothing.

    python3 toolkit/analysis/dt_golden.py --self-test
    python3 toolkit/analysis/dt_golden.py digest <run_dir>

Parts of a record's digest (sha256 prefixes of canonical JSON; parts()): `decision` (status, plan changes, shares,
regret, dollars moved, recommendations, benefit changes, identities, each case's results, the run's summary entry;
not `type` or `perturb`, and any field not named here), `detail` (case labels and the type's own block), `words`
(the sentences) and `plans` (each case's allocation digest, decision_tests.plan_sha256). Numbers: 6 significant
digits, below 1e-9 in size zero; an absolute path keeps its file name. Known limits: a path holding a
space keeps its tail, and two values either side of a 6th-digit rounding boundary differ.

`digest` reads analysis/decision_tests.yaml and models/decision_tests/<id>.yaml, and fails closed (digest_dir).

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
TINY = 1e-9
_PATH = re.compile(r"(?:(?<=^)|(?<=[\s'\"(\[=,]))(?:[A-Za-z]:)?[\\/](?:[^\s\\/'\"()]+[\\/])+([^\s\\/'\"()]+)")


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
        detail["worst_labels"] = {k: v.get("value") for k, v in wbc.items() if isinstance(v, dict)}
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


def _plain_name(tid: str, pathmod=os.path) -> bool:
    """Whether `<tid>.yaml` is one plain file name under a directory with this platform's path rules: no separator,
    no `.`/`..`, no drive (Windows `C:x`), so joining it never leaves the directory (Codex r3 P1-3, P2-1: any id the
    writers accept otherwise, spaces and colons included)."""
    base, name = pathmod.join("base", "dir"), f"{tid}.yaml"
    j = pathmod.join(base, name)
    return bool(tid) and "/" not in tid and "\\" not in tid and name not in (".", "..") \
        and pathmod.dirname(j) == base and pathmod.basename(j) == name


def _tid(v):
    """A test id as run_all names its file (f"{id}.yaml"): a string, or a number, bool or YAML date in its str form; None (no
    identity) for anything else, null included (Codex r4 P2)."""
    import datetime
    return v if isinstance(v, str) else str(v) if isinstance(v, (bool, int, float, datetime.date)) else None


class _pinned:
    """One descriptor on the run dir for a whole digest (Codex r6 P1-1): every read is relative to it, so a directory
    replaced by a link mid-digest cannot mix two runs."""

    def __init__(self, run_dir):
        self.path = os.path.realpath(run_dir)

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError as e:
            raise FileNotFoundError(f"{self.path}: no run dir") from e
        return self.fd

    def __exit__(self, *exc):
        os.close(self.fd)
        return False


def _read(dfd: int, rel: str, missing=StoredIncomplete):
    """The YAML of rel under the pinned run-dir descriptor, each component opened relative to the last with
    O_NOFOLLOW (no link followed anywhere, no check-then-open race; Codex r2 P1-1, r4 P1, r6 P1-1); `missing` is raised
    when nothing is there."""
    import errno
    import stat
    import yaml
    parts_ = rel.split("/")
    if any(c in ("", ".", "..") for c in parts_):
        raise StoredIncomplete(f"{rel}: not a plain path under the run dir")
    nf, opened, cur = getattr(os, "O_NOFOLLOW", 0), [], dfd
    try:
        for c in parts_[:-1]:
            cur = os.open(c, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nf, dir_fd=cur)
            opened.append(cur)
        fd = os.open(parts_[-1], os.O_RDONLY | nf | getattr(os, "O_NONBLOCK", 0), dir_fd=cur)
    except FileNotFoundError as e:
        raise missing(f"{rel}: missing") from e
    except OSError as e:
        why = "a link" if e.errno in (errno.ELOOP, errno.ENOTDIR, getattr(errno, "EMLINK", -1)) else e.strerror
        raise StoredIncomplete(f"{rel}: not read ({why}; a result is read from the run dir only)") from e
    finally:
        for x in opened:
            os.close(x)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise StoredIncomplete(f"{rel}: not a regular file")
        chunks = []
        while True:
            b = os.read(fd, 1 << 20)
            if not b:
                break
            chunks.append(b)
    finally:
        os.close(fd)
    try:
        return yaml.safe_load(b"".join(chunks))
    except yaml.YAMLError as e:
        raise StoredIncomplete(f"{rel}: not YAML ({e})") from e


def digest_dir(run_dir: str) -> dict:
    """{test id: digest} of a finished run's decision tests, each with its summary entry. Fail closed: no artifact
    raises FileNotFoundError; a malformed artifact, an entry without an id or with an unknown status, a repeated id,
    a run test without its result file, a result file elsewhere than models/decision_tests/<id>.yaml or through a link,
    or a result whose test id or status differs from its entry raises StoredIncomplete."""
    with _pinned(run_dir) as rd:
        return _digest_dir(rd)


def _digest_dir(rd: int) -> dict:
    p = "analysis/decision_tests.yaml"
    doc = _read(rd, p, missing=FileNotFoundError)
    data = doc.get("data") if isinstance(doc, dict) else None
    tests = data.get("tests") if isinstance(data, dict) else None
    if not isinstance(tests, list):
        raise StoredIncomplete(f"{p}: data.tests is not a list")
    out = {}
    for t in tests:
        tid = _tid(t.get("test_id")) if isinstance(t, dict) else None
        if not isinstance(tid, str) or not _plain_name(tid):
            raise StoredIncomplete(f"{p}: an entry without a plain test id ({t!r:.120})")
        if tid in out:
            raise StoredIncomplete(f"{p}: test id {tid} is listed twice")
        st = t.get("status")
        if st not in ("run", "not_run"):
            raise StoredIncomplete(f"{p}: test {tid}: status {st!r} is neither run nor not_run")
        if st == "not_run":
            if t.get("path") or t.get("verdict", "NOT_RUN") != "NOT_RUN":
                raise StoredIncomplete(f"{p}: test {tid} was not run but lists a result file or another verdict")
            out[tid] = digest({"status": st, "test_id": tid}, t)
            continue
        want = f"models/decision_tests/{tid}.yaml"
        if str(t.get("path") or "").replace("\\", "/") != want:
            raise StoredIncomplete(f"{p}: test {tid}: result file {t.get('path')!r}, not {want}")
        rec = _read(rd, want)
        if not isinstance(rec, dict) or _tid(rec.get("test_id")) != tid or rec.get("status") != st \
                or not isinstance(rec.get("decision_changes"), bool):
            raise StoredIncomplete(f"{want}: holds {rec!r:.80}, not test {tid} with status {st} and decision_changes")
        vd = "CHANGES_PLAN" if rec.get("decision_changes") else "NO_CHANGE"
        if "verdict" in t and t["verdict"] != vd:          # Codex r2 P2-2: the summary contradicts its result
            raise StoredIncomplete(f"{p}: test {tid}: verdict {t['verdict']!r}, its result says {vd}")
        out[tid] = digest(rec, t)
    return out


def compare(expected: dict, got: dict, parts: tuple | None = None) -> list[str]:
    """One line per test missing, not expected, or with a part that differs (every part, the named ones, or with
    parts="expected" those the expected record holds: a stored record from before plans existed has none)."""
    out = []
    for k in sorted(set(expected) | set(got)):
        if k not in got:
            out.append(f"{k}: missing")
        elif k not in expected:
            out.append(f"{k}: not expected")
        else:
            e, g = expected[k] or {}, got[k] or {}
            ps = sorted(e) if parts == "expected" else parts or sorted(set(e) | set(g))
            out += [f"{k}: {p} differs" for p in ps if e.get(p) != g.get(p)]
    return out


_CAP = {"on": False, "paused": False, "orig": None, "recs": {}, "counts": {}, "problems": []}
_RUNNERS = {"_run", "run", "run_one", "run_all"}


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
    _CAP.update(on=True, paused=False, orig=orig, recs={}, counts={}, problems=[], parts={})

    def run_one(*a, **k):
        rec = orig(*a, **k)
        if _CAP["on"] and not _CAP["paused"]:
            who, tid = _caller(), str(rec.get("test_id"))
            n = _CAP["counts"].get((who, tid), 0)
            _CAP["counts"][(who, tid)] = n + 1
            pr = parts(plain(rec))          # hashed once (Codex r6 P1-2); its canonical form kept for the report
            _CAP["recs"][f"{who}:{tid}#{n}"] = {k: _h(v) for k, v in pr.items()}
            _CAP["parts"][f"{who}:{tid}#{n}"] = canon(pr)
            if rec.get("status") == "run" and not all("plan_sha256" in r for r in rec.get("results") or []):
                _CAP["problems"].append(f"{who}:{tid}#{n}: a case without plan_sha256")
        return rec
    DTm.run_one = run_one


def golden_text(obs: dict) -> str:
    """The golden file: one line per self-test function, {"<id>#<n>": "decision.detail.words[.plans]"}."""
    by = {}
    for k, v in obs.items():
        fn, rest = k.rsplit(":", 1)
        by.setdefault(fn, {})[rest] = ".".join(v[p] for p in PARTS if p in v)
    return "{\n" + ",\n".join(f" {json.dumps(fn)}: {json.dumps(r, sort_keys=True)}" for fn, r in sorted(by.items())) \
        + "\n}\n"


def golden_read(text: str) -> dict:
    return {f"{fn}:{k}": dict(zip(PARTS, v.split("."))) for fn, r in json.loads(text).items() for k, v in r.items()}


def platform_line() -> dict:
    """The CPU and BLAS this process computes on (a golden mismatch across machines is read against it)."""
    import platform
    out = {"machine": platform.machine(), "python": platform.python_version()}
    try:
        info = open("/proc/cpuinfo").read()
        out["cpu"] = next((ln.split(":", 1)[1].strip() for ln in info.splitlines() if ln.startswith("model name")), None)
        flags = next((ln for ln in info.splitlines() if ln.startswith("flags")), "")
        out["avx512f"], out["avx2"] = " avx512f" in flags, " avx2" in flags
    except OSError:
        out["cpu"] = platform.processor()
    try:
        from threadpoolctl import threadpool_info
        out["blas"] = [{k: x.get(k) for k in ("internal_api", "version", "architecture", "num_threads")}
                       for x in threadpool_info()]
    except Exception:  # noqa: BLE001 -- optional
        out["blas"] = None
    return out


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
        if failed or not obs:
            ok(False, f"DG-R1 {UPDATE_ENV}=1 refused: {len(failed)} earlier case(s) failed or nothing was captured, so "
                      "the capture may be partial; the golden file is unchanged")
            return
        with open(GOLDEN, "w") as f:
            f.write(golden_text(obs))
        ok(True, f"DG-R1 {len(obs)} golden records written to {GOLDEN} ({UPDATE_ENV}=1)")
        return
    try:
        with open(GOLDEN) as f:
            exp = golden_read(f.read())
    except (OSError, ValueError) as e:
        exp, err = {}, f"{type(e).__name__}: {e}"
    else:
        err = None
    diff = compare(exp, obs)
    print("DT_GOLDEN_PLATFORM " + json.dumps(platform_line(), sort_keys=True))
    ok(obs and not diff and err is None, f"DG-R1 every decision-test record of this self-test matches its golden "
                                         f"record ({len(obs)} records; {err or ''} {diff[:12]})")
    if diff or err:
        print("DT_GOLDEN_OBSERVED " + json.dumps(obs, sort_keys=True))
        bad = sorted({d_.split(": ")[0] for d_ in diff} & set(_CAP.get("parts") or {}))
        print("DT_GOLDEN_PARTS " + json.dumps({k: _CAP["parts"][k] for k in bad}, sort_keys=True))


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

    def dg5():
        e = {"A": {"decision": "1", "detail": "2", "words": "3"}, "B": {"decision": "1", "detail": "2", "words": "3"}}
        g = {"A": {"decision": "1", "detail": "9", "words": "3"}, "C": {"decision": "1", "detail": "2", "words": "3"}}
        diff = compare(e, g)
        ok(sorted(diff) == ["A: detail differs", "B: missing", "C: not expected"] and compare(e, e) == [],
           f"DG5 compare lists each differing part, missing and unexpected test ({diff})")

    def dg6():
        obs = {"f.<locals>.one:DT1#0": {"decision": "a", "detail": "b", "words": "c"},
               "g:DT1#0": {"decision": "a", "detail": "b", "words": "c", "plans": "d"}}
        ok(golden_read(golden_text(obs)) == obs, "DG6 the golden file round-trips (one line per self-test function)")

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
            ok(_raises(lambda: digest_dir(d), FileNotFoundError), "DG11g a run without the artifact is refused")
            fixture(d, [run1], {"DT1.yaml": _rec()})
            a = digest_dir(d)["DT1"]
            ok(a == digest(_rec(), run1), "DG11h a listed result is digested with its summary entry")
            fixture(d, [dict(run1, share_pct=99.0)], {"DT1.yaml": _rec()})
            ok(digest_dir(d)["DT1"]["decision"] != a["decision"], "DG11d (P1-6) the summary's fields are decision")
            nr = {"test_id": "DT2", "path": None, "status": "not_run", "threshold_pct": 10.0, "statement": "x"}
            fixture(d, [nr], {})
            b = digest_dir(d)["DT2"]
            fixture(d, [dict(nr, threshold_pct=99.0)], {})
            ok(digest_dir(d)["DT2"]["decision"] != b["decision"], "DG11e (P1-6) a not-run entry's fields are decision")
            fixture(d, [run1], {})
            os.remove(os.path.join(d, "models", "decision_tests", "DT1.yaml"))
            ok(_raises(lambda: digest_dir(d), StoredIncomplete),
               "DG11f (P1-7) a listed result file that is missing is StoredIncomplete, not an absent artifact")

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

    def dg14():
        ok(canon("C:\\run\\inputs\\results.yaml") == "results.yaml" and
           canon("prior from /tmp/abc/priors.yaml (card)") == "prior from priors.yaml (card)",
           f"DG14 (P2-4) Windows paths and paths inside sentences keep their file name "
           f"({canon('prior from /tmp/abc/priors.yaml (card)')})")
        ok(canon(1e-14) == canon(0) and canon(-1e-13) == 0.0, "DG14b (P2-4) values below 1e-9 in size are zero")
        ok(digest(_rec(arm_source="/tmp/a/results.yaml")) == digest(_rec(arm_source="/var/b/results.yaml")),
           "DG4 an absolute path keeps only its file name")

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

    # ------------------------------------------------ Codex review r2 of #262 (written first; each failed)
    def dg16():
        run1 = {"test_id": "DT1", "path": "models/decision_tests/DT1.yaml", "status": "run", "verdict": "NO_CHANGE"}
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as side:
            fixture(side, [], {})
            os.makedirs(os.path.join(d, "models"))
            os.symlink(os.path.join(side, "analysis"), os.path.join(d, "analysis"))
            ok(_raises(lambda: digest_dir(d), StoredIncomplete),
               "DG16 (r2 P1-4) a summary under a linked directory is refused, never read as absent")
        with tempfile.TemporaryDirectory() as d:
            fixture(d, [dict(run1, test_id="../x", path="models/decision_tests/../x.yaml")], {})
            yaml.safe_dump(_rec(test_id="../x"), open(os.path.join(d, "models", "x.yaml"), "w"))
            ok(_raises(lambda: digest_dir(d), StoredIncomplete), "DG16d (r2 P1-3) a test id with path parts is refused")
            fixture(d, [dict(run1, test_id=7, path="models/decision_tests/7.yaml")], {"7.yaml": _rec(test_id=7)})
            ok(set(digest_dir(d)) == {"7"}, "DG16e (r2 P2-1) an integer test id, as run_all writes it, is read")
            fixture(d, [dict(run1, verdict="NOT_RUN")], {"DT1.yaml": _rec()})
            ok(_raises(lambda: digest_dir(d), StoredIncomplete), "DG16f (r2 P2-2) a verdict contradicting its result")
            fixture(d, [run1], {"DT1.yaml": ["a"]})
            ok(_raises(lambda: digest_dir(d), StoredIncomplete), "DG16g (r2 P3) a result that is not a mapping")
        e = {"A": {"decision": "1", "detail": "2", "words": "3", "plans": "4"}, "B": {"decision": "1"}}
        g = {"A": {"decision": "1", "detail": "2", "words": "3", "plans": "5"}, "B": {"decision": "1", "plans": "6"}}
        ok(compare(e, g, parts="expected") == ["A: plans differs"],
           "DG16i (r2 P1-5) stored records are compared on the parts they hold, plans included when present")
        w_ = lambda lab: _rec(worst_by_criterion={"share": {"value": lab}})  # noqa: E731
        ok(digest(w_(0.5))["detail"] != digest(w_(0.8))["detail"], "DG16j (r2 P2-3) the worst case's label is detail")
        ok(canon("north/a/site") != canon("north/b/site") and canon("(/tmp/a/p.yaml)") == "(p.yaml)",
           f"DG16k (r2 P2-4) only absolute paths are shortened ({canon('north/a/site')})")
        seen = []
        _CAP.update(on=True, paused=False, orig=lambda *a, **k: None, recs={}, counts={}, problems=[])
        from toolkit.analysis import decision_tests as DTm
        saved, g0 = DTm.run_one, open(GOLDEN).read() if os.path.exists(GOLDEN) else None
        try:
            os.environ[UPDATE_ENV] = "1"
            check_capture(lambda c, m: seen.append((c, m)))
        finally:
            os.environ.pop(UPDATE_ENV, None)
            DTm.run_one = saved
        ok(g0 == (open(GOLDEN).read() if os.path.exists(GOLDEN) else None),
           "DG16m (r2 P2-6) an empty capture never overwrites the golden file")

    def dg17():          # Codex review r3 of #262 (written first; each failed)
        run1 = {"test_id": "DT1", "path": "models/decision_tests/DT1.yaml", "status": "run", "verdict": "NO_CHANGE"}
        with tempfile.TemporaryDirectory() as d:
            tid = "prior_range_z:North East"
            fixture(d, [dict(run1, test_id=tid, path=f"models/decision_tests/{tid}.yaml")],
                    {f"{tid}.yaml": _rec(test_id=tid)})
            ok(set(digest_dir(d)) == {tid}, "DG17 (r3 P2-1) an automatic id with a colon and a space, as run_all writes "
                                            "it, is read")
            nr = {"test_id": "DT2", "path": None, "status": "not_run", "verdict": "CHANGES_PLAN"}
            none = dict(run1, test_id="None", path="models/decision_tests/None.yaml")
            for tests, files, what in (
                    ([nr], {}, "a not-run entry with a verdict other than NOT_RUN"),
                    ([run1], {"DT1.yaml": {k: v for k, v in _rec().items() if k != "decision_changes"}},
                     "a run result without decision_changes"),
                    ([none], {"None.yaml": {k: v for k, v in _rec().items() if k != "test_id"}},
                     "a result without a test id")):
                for f_ in os.listdir(os.path.join(d, "models", "decision_tests")):
                    os.remove(os.path.join(d, "models", "decision_tests", f_))
                fixture(d, tests, files)
                ok(_raises(lambda: digest_dir(d), StoredIncomplete), f"DG17b (r3 P2-2) refused: {what}")
        import ntpath
        ok(_plain_name("C:trial", ntpath) is False and _plain_name("prior_range_z:North East", os.path) is True,
           "DG17c (r3 P1-3) a test id that would name a drive on Windows is not a plain file name there")

    def dg19():          # Codex review r4 of #262 (written first; each failed)
        run1 = {"path": "models/decision_tests/DT1.yaml", "status": "run", "verdict": "NO_CHANGE"}
        for raw, name in ((1.5, "1.5"), (True, "True"), (".", "."), ("..", "..")):
            with tempfile.TemporaryDirectory() as d:
                fixture(d, [dict(run1, test_id=raw, path=f"models/decision_tests/{name}.yaml")],
                        {f"{name}.yaml": _rec(test_id=raw)})
                try:
                    got = set(digest_dir(d))
                except Exception as e:  # noqa: BLE001
                    got = e
                ok(got == {name}, f"DG19 (r4 P2-1) id {raw!r}, as run_all writes it ({name}.yaml), is read ({got})")
        with tempfile.TemporaryDirectory() as d:
            fixture(d, [dict(run1, test_id="None", path="models/decision_tests/None.yaml")],
                    {"None.yaml": _rec(test_id=None)})
            ok(_raises(lambda: digest_dir(d), StoredIncomplete), "DG19b (r4 P2-2) a null test id is no identity")
        import inspect
        ok("O_NOFOLLOW" in inspect.getsource(_read) and " open(" not in inspect.getsource(_digest_dir),
           "DG19c (r4 P1) every read goes through lib.safe_io.read_nofollow (one descriptor, no link followed)")

    def dg20():          # Codex review r5 of #262, and the CI recording that differed (written first; each failed)
        import datetime
        run1 = {"path": "models/decision_tests/2026-11-18.yaml", "status": "run", "verdict": "NO_CHANGE"}
        with tempfile.TemporaryDirectory() as d:
            day = datetime.date(2026, 11, 18)
            fixture(d, [dict(run1, test_id=day)], {"2026-11-18.yaml": _rec(test_id=day)})
            try:
                got = set(digest_dir(d))
            except Exception as e:  # noqa: BLE001
                got = e
            ok(got == {"2026-11-18"}, f"DG20 (r5 P1-2) an unquoted YAML date id, as run_all names its file ({got})")
        import inspect
        src = inspect.getsource(digest_dir)
        ok("_pinned(run_dir)" in src and "realpath" not in inspect.getsource(_read),
           "DG20b (r5 P1-1) the run dir is resolved once per digest, so a retargeted alias cannot swap runs mid-read")
        printed = []
        _CAP.update(on=True, paused=False, orig=lambda *a, **k: None, recs={"f:X#0": {"decision": "1"}}, counts={},
                    problems=[], parts={"f:X#0": {"decision": {"share": 0.5}}})
        from toolkit.analysis import decision_tests as DTm
        saved, out = DTm.run_one, sys.stdout
        import io
        sys.stdout = buf = io.StringIO()
        try:
            check_capture(lambda c, m: printed.append(c))
        finally:
            sys.stdout, DTm.run_one = out, saved
        ok("DT_GOLDEN_PARTS" in buf.getvalue() and '"share": 0.5' in buf.getvalue(),
           "DG20c a mismatch prints the canonical parts of each differing record (the field that moved can be read)")

    def dg21():          # Codex review r6 of #262 (written first; each failed)
        from toolkit.analysis import decision_tests as DTm
        rec = _rec(question=b"see /tmp/file")
        saved = DTm.run_one
        _CAP.update(on=False)
        DTm.run_one = lambda *a, **k: rec
        try:
            start_capture()
            DTm.run_one()
            got = dict(_CAP["recs"])
        finally:
            DTm.run_one = saved
            _CAP.update(on=False, orig=None)
        from toolkit.analysis.artifacts import plain
        ok(list(got.values()) == [digest(plain(rec))], "DG21 (r6 P1-2) the capture's digest is digest(plain(rec)), once")
        import inspect
        src = inspect.getsource(digest_dir)
        ok("_pinned" in src and "with _pinned(" in src,
           "DG21b (r6 P1-1) one directory descriptor is pinned for the whole digest")

    for fn in (dg1, dg2, dg3, dg5, dg6, dg11, dg13, dg14, dg15, dg16, dg17, dg19, dg20, dg21):
        case(fn.__name__.upper(), fn)
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
    sub.add_parser("digest", help="digest a finished run's decision tests (JSON on stdout)").add_argument("run_dir")
    a = ap.parse_args(argv)
    print(json.dumps(digest_dir(a.run_dir), indent=1, sort_keys=True))
    return 0

if __name__ == "__main__":
    sys.exit(main())
