#!/usr/bin/env python3
"""Byte-identity proof between compartmental engine versions, and the coverage-pack re-stamp it allows (v0.2).

    python3 toolkit/engines/compartmental/benchmarks/compat_proof.py prove [--old-ref origin/v2]
    python3 toolkit/engines/compartmental/benchmarks/compat_proof.py restamp     # only after a passing proof
    python3 toolkit/engines/compartmental/benchmarks/compat_proof.py --self-test

v0.2 adds fields that each act only when a configuration or a component asks for them (time_unit, per-group
parameters, coverage_scale, the SEIR generation interval, a component's day, the figure hooks). The analytic pack
(results.yaml) is re-run in full on the new code (about two minutes locally). The coverage pack
(coverage_results.yaml, 200 replicates per case on a VM) is not: `prove` shows that every configuration it and
the analytic pack use gives byte-identical outputs under the old code (`git archive <old-ref>`) and the working
tree, one process each: every pack setting of a configuration the old pack declared, all of its arms in one
run_batch, plus each setting's initial_state() and each arm's run(). It writes compat_proof.yaml (scope, both
engine identities, one sha256 per case and side, verdict). `restamp` then replaces only the identity fields of
coverage_results.yaml, recording `restamped: {by, proof, old_identity, note}`, and only when the proof is for the
current code and says identical. Nothing here names a disease.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PACK = os.path.join(HERE, "pack.yaml")
OUT = os.path.join(HERE, "compat_proof.yaml")
COVERAGE = os.path.join(HERE, "coverage_results.yaml")

RUNNER = r'''
import hashlib, json, sys
root, cases = sys.argv[1], json.loads(sys.stdin.read())
sys.path.insert(0, root)
from toolkit.engines import benchmark as B
from toolkit.engines.contract import Horizon, Package, UnitSpec, engine_identity
out = {}
ident = None
for c in cases:
    eng = B.load_engine(c["ref"], c["config"])
    if ident is None and c.get("identity"):
        ident = engine_identity(eng)
    h = c["horizon"]
    hz = Horizon(float(h["start"]), float(h["stop"]), tuple(h["window"]), dict(h.get("windows") or {}))
    u = UnitSpec("u", c["unit"])
    pks = [Package(a, dict(p)) for a, p in c["arms"]]
    res = []
    for o in eng.run_batch([dict(c["params"])] * len(pks), u, pks, hz, int(c["seed"])):
        res.append({"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta})
    for p in pks:
        o = eng.run(dict(c["params"]), u, p, hz, int(c["seed"]))
        res.append({"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta})
    if hasattr(eng, "initial_state"):
        res.append(eng.initial_state(dict(c["params"]), u, pks[0]))
    out[c["id"]] = hashlib.sha256(json.dumps(res, sort_keys=True, default=repr).encode()).hexdigest()
print(json.dumps({"digests": out}))
'''


COVERAGE_PACK = os.path.join(HERE, "coverage.yaml")
# The coverage pack's configurations have no settings (its generators make the units): one representative unit and
# parameter set each, at the generators' scales, with the arms its calibrator runs.
COVERAGE_CASES = {
    "renewal": {"unit": {"N": [1.0e+6], "s0": [1.0], "seed": 1.0e-6},
                "params": {"r0": 1.8, "forcing_amplitude": 0.0, "forcing_phase": 0.0},
                "horizon": {"start": 0, "stop": 210, "window": [0, 210], "windows": {"all": [0, 210]}},
                "arms": [["none", {}]]},
    "renewal_forced": {"config": "renewal", "unit": {"N": [1.0e+6], "s0": [0.7], "seed": 1.0e-5},
                       "params": {"r0": 2.2, "forcing_amplitude": 0.3, "forcing_phase": 40.0},
                       "horizon": {"start": 0, "stop": 210, "window": [0, 210], "windows": {"all": [0, 210]}},
                       "arms": [["none", {}]]},
    "cohorts": {"unit": {"N": [1.0e+5, 1.0e+5, 1.0e+5, 1.0e+5, 1.0e+5], "birth_year": [2021, 2022, 2023, 2024, 2025],
                         "decision_year": 2026, "coverage:d1": [0.8, 0.85, 0.7, 0.9, 0.6], "contact_rate": 10.0,
                         "seed": 1.0e-9, "intro": "b2025"},
                "params": {"seroconversion:d1": 0.9, "r0": 8.0},
                "horizon": {"start": 0, "stop": 200000, "window": [0, 200000], "windows": {"all": [0, 200000]}},
                "arms": [["none", {}]]},
    "groups3": {"unit": {"N": [3.0e+5, 2.0e+5, 1.0e+5], "s0": [1.0, 0.9, 0.8], "seed": 1.0e-9, "intro": "a",
                         "contact": [[1.2, 0.5, 0.3], [0.5, 1.5, 0.4], [0.3, 0.4, 1.0]],
                         "ratio:hospitalisations": [0.01, 0.02, 0.05]},
                "params": {"q": 0.7, "efficacy:plan": 0.8},
                "horizon": {"start": 0, "stop": 200000, "window": [0, 200000], "windows": {"all": [0, 200000]}},
                "arms": [["none", {}], ["plan", {"plan": 1}]]},
}


def coverage_cases(cov_path: str = COVERAGE_PACK, seeds=(1,)) -> list[dict]:
    cv = yaml.safe_load(open(cov_path))
    out = []
    for name, c in COVERAGE_CASES.items():
        cfg = c.get("config", name)
        for sd in seeds:
            out.append({"id": f"coverage:{name}#seed{sd}", "ref": cv["engine"], "config": cv["configs"][cfg],
                        "unit": c["unit"], "params": c["params"], "horizon": c["horizon"], "arms": c["arms"],
                        "seed": sd})
    return out


def cases(pack_path: str = PACK, old_configs=None, seeds=(1,)) -> list[dict]:
    """One case per (setting, seed) whose configuration the old pack declared, with every arm of the setting."""
    pk = yaml.safe_load(open(pack_path))
    keep = set(pk["configs"]) if old_configs is None else set(old_configs)
    arms = {}
    for a, d in pk["arms"].items():
        arms.setdefault(d["setting"], []).append([a, d.get("package") or {}])
    out = []
    for name, st in sorted(pk["settings"].items()):
        cfg = st.get("config") or pk.get("default_config")
        if cfg not in keep:
            continue
        for sd in seeds:
            out.append({"id": f"{name}#seed{sd}", "ref": pk["engine"], "config": pk["configs"][cfg],
                        "unit": st["unit"], "params": st.get("params") or {}, "horizon": st.get("horizon") or pk["horizon"],
                        "arms": arms.get(name) or [["none", {}]], "seed": sd})
    return out


def _run(root: str, cs: list, python: str) -> dict:
    p = subprocess.run([python, "-c", RUNNER, root], input=json.dumps(cs), capture_output=True, text=True,
                       env=dict(os.environ, PYTHONHASHSEED="0"))
    if p.returncode != 0:
        raise RuntimeError(f"case runner failed under {root}: {p.stderr[-2000:]}")
    return json.loads(p.stdout.strip().splitlines()[-1])["digests"]


def compare(old: dict, new: dict) -> dict:
    ids = sorted(set(old) | set(new))
    diff = [i for i in ids if old.get(i) != new.get(i)]
    return {"cases": len(ids), "identical_cases": len(ids) - len(diff), "differing": diff, "identical": not diff}


# Codex r1 P1-13: the coverage rows are bound to their calibration, observation and generator code
# (benchmark.calibrator_code_digest). Those files may change only where the coverage path does not go: these
# top-level definitions may change, and definitions may be added; nothing else may differ.
CALIBRATOR_FILES = {
    "toolkit/analysis/calibration.py": {"read_targets", "fit_posterior_panel"},   # dispatch to observed_targets
    "toolkit/analysis/observation.py": set(),
    "toolkit/engines/compartmental/benchmarks/oracles.py": set(),
}
IDENTITY_RUNNER = r'''
import json, sys, yaml
root, cov = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
from toolkit.engines import benchmark as B
from toolkit.engines.contract import engine_identity
cv = yaml.safe_load(open(cov))
rows = {b["id"]: B.calibrator_code_digest(cv, b["estimand"]) for b in cv["benchmarks"]
        if b["estimand"]["type"] == "coverage"}
print(json.dumps({"identity": engine_identity(B.load_engine(cv["engine"], cv["configs"][cv["default_config"]])),
                  "row_code_sha256": rows}))
'''


def file_hashes(root: str = ROOT) -> dict:
    import hashlib
    return {rel: hashlib.sha256(open(os.path.join(root, rel), "rb").read()).hexdigest() for rel in CALIBRATOR_FILES}


def definitions(src: str) -> dict:
    """{top-level function or class name: its AST dump} (comments and formatting do not count)."""
    import ast
    return {n.name: ast.dump(n) for n in ast.parse(src).body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def code_changes(old_root: str, new_root: str = ROOT) -> dict:
    """Per calibrator file: the top-level definitions changed, removed and added; ok when only allowed ones changed
    and none was removed, and the module-level statements other than definitions are unchanged."""
    import ast
    out, ok = {}, True
    for rel, allowed in CALIBRATOR_FILES.items():
        a, b = open(os.path.join(old_root, rel)).read(), open(os.path.join(new_root, rel)).read()
        da, db = definitions(a), definitions(b)
        changed = sorted(k for k in da if k in db and da[k] != db[k])
        removed = sorted(set(da) - set(db))
        added = sorted(set(db) - set(da))
        rest = lambda s: [ast.dump(n) for n in ast.parse(s).body  # noqa: E731
                          if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        module_same = rest(a) == rest(b)
        good = set(changed) <= allowed and not removed and module_same
        ok = ok and good
        out[rel] = {"changed": changed, "removed": removed, "added": added, "module_statements_same": module_same,
                    "allowed_changes": sorted(allowed), "ok": good}
    return {"files": out, "ok": ok}


def prove(old_ref: str, python: str = sys.executable) -> dict:
    from toolkit.engines import benchmark as B
    from toolkit.engines.contract import engine_identity
    with tempfile.TemporaryDirectory() as d:
        tar = subprocess.run(["git", "-C", ROOT, "archive", old_ref, "toolkit"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", d], input=tar, check=True)
        old_pack = os.path.join(d, os.path.relpath(PACK, ROOT))
        old_cfgs = list(yaml.safe_load(open(old_pack))["configs"])
        cs = cases(PACK, old_cfgs) + coverage_cases()
        dig_old, dig_new = _run(d, cs, python), _run(ROOT, cs, python)
        cc = code_changes(d)
        ip = subprocess.run([python, "-c", IDENTITY_RUNNER, d, os.path.join(d, os.path.relpath(COVERAGE_PACK, ROOT))],
                            capture_output=True, text=True, check=True)
        old_side = json.loads(ip.stdout.strip().splitlines()[-1])
        old_cov_identity = old_side["identity"]
        sys.path.insert(0, d)
        old_commit = subprocess.run(["git", "-C", ROOT, "rev-parse", old_ref], capture_output=True, text=True,
                                    check=True).stdout.strip()
    pk = yaml.safe_load(open(PACK))
    eng = B.load_engine(pk["engine"], pk["configs"][pk["default_config"]])
    rec = {"producer": "toolkit/engines/compartmental/benchmarks/compat_proof.py",
           "scope": "every pack setting of a configuration the old pack declared, and one representative unit per "
                    "coverage-pack configuration (all arms in one run_batch, each arm's run(), and initial_state()), "
                    "seed 1",
           "old_ref": old_ref, "old_commit": old_commit, "old_configs": old_cfgs,
           "new_identity": engine_identity(eng), "old_coverage_identity": old_cov_identity,
           "calibrator_code": cc,
           # Codex r2 P1-3: what the restamp re-checks before it moves any stamp
           "old_row_code_sha256": old_side["row_code_sha256"],
           "new_calibrator_file_sha256": file_hashes(),
           "digests": {c["id"]: {"old": dig_old.get(c["id"]), "new": dig_new.get(c["id"])} for c in cs}}
    rec.update(compare(dig_old, dig_new))
    rec["identical"] = bool(rec["identical"] and cc["ok"])
    yaml.safe_dump(rec, open(OUT, "w"), sort_keys=False, width=110)
    return rec


def restamp() -> dict:
    from toolkit.engines import benchmark as B
    from toolkit.engines.contract import engine_identity
    proof = yaml.safe_load(open(OUT))
    pk = yaml.safe_load(open(PACK))
    eng = B.load_engine(pk["engine"], pk["configs"][pk["default_config"]])
    if not proof.get("identical"):
        raise SystemExit("refused: the stored proof does not say identical")
    if proof.get("new_identity", {}).get("code_sha256") != engine_identity(eng)["code_sha256"]:
        raise SystemExit("refused: the proof is for other engine code; run prove again")
    cov = yaml.safe_load(open(COVERAGE))
    old = cov.get("engine_identity")
    computed_by = (cov.get("restamped") or {}).get("old_identity", old)    # the code that computed the values
    if computed_by != proof.get("old_coverage_identity"):
        raise SystemExit("refused: coverage_results.yaml was computed by other code than the proof's old side "
                         f"({(computed_by or {}).get('code_sha256', '?')[:12]} vs "
                         f"{(proof.get('old_coverage_identity') or {}).get('code_sha256', '?')[:12]})")
    if not (proof.get("calibrator_code") or {}).get("ok"):
        raise SystemExit("refused: the calibrator, observation or generator code changed on the coverage path")
    if proof.get("new_calibrator_file_sha256") != file_hashes():
        raise SystemExit("refused: the calibrator, observation or generator files changed after the proof; run prove "
                         "again")
    old_rows = proof.get("old_row_code_sha256") or {}
    unbound = []           # rows computed by calibrator code other than the proof's old side: never re-bound
    for r in cov.get("benchmarks") or []:
        if "code_sha256" in r:
            hist = r.get("computed_code_sha256", r["code_sha256"])
            if old_rows.get(r["id"]) != hist:
                unbound.append(r["id"])
    cv = yaml.safe_load(open(COVERAGE_PACK))
    eng = B.load_engine(cv["engine"], cv["configs"][cv["default_config"]])        # the coverage pack's own identity
    ident = engine_identity(eng)
    if old == ident:
        raise SystemExit("refused: coverage_results.yaml is already stamped for this code")
    cov["engine_identity"] = ident
    cov["engine_config_sha256"] = B.config_digest(eng) if hasattr(B, "config_digest") else cov.get("engine_config_sha256")
    prev = cov.get("restamped") or {}
    if prev:     # re-stamped before: the identity of the code that computed the values is the earliest one
        old = prev.get("old_identity", old)
    for r in cov.get("benchmarks") or []:
        if "code_sha256" not in r:
            continue
        if r["id"] in unbound:
            # Codex r2 P1-3: the row keeps the digest of the code that computed it, so benchmark.py's rescore and merge
            # refuse it until the coverage pack is re-run (fail closed)
            r["code_sha256"] = r.pop("computed_code_sha256", r["code_sha256"])
            continue
        b = next((x for x in cv["benchmarks"] if x["id"] == r["id"]), None)
        if b is not None and b["estimand"]["type"] == "coverage":   # proven on the old side: re-bound, the old kept
            new = B.calibrator_code_digest(cv, b["estimand"])
            if r["code_sha256"] != new:
                r.setdefault("computed_code_sha256", r["code_sha256"])
                r["code_sha256"] = new
    cov["restamped"] = {"by": "compat_proof.py", "proof": os.path.basename(OUT),
                        "proof_cases": proof["cases"], "old_identity": old, "old_ref": proof["old_ref"],
                        "old_commit": proof["old_commit"],
                        "rows_not_rebound": unbound,
                        "note": "coverage values computed by the old code, carried by a byte-identity proof of the engine "
                                "on every configuration the packs use; not recomputed. Rows in rows_not_rebound were "
                                "computed by calibrator code other than the proof's old side (before this change), so "
                                "they keep that code's digest and stay unusable for rescore and merge until re-run"}
    yaml.safe_dump(cov, open(COVERAGE, "w"), sort_keys=False, width=110)
    return cov["restamped"]


def _self_test() -> int:
    global OUT, COVERAGE
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    cs = cases(PACK)
    ok(cs and all(c["arms"] for c in cs), f"CP1 one case per pack setting with its arms ({len(cs)} cases)")
    cv = yaml.safe_load(open(COVERAGE_PACK))
    ok(set(cv["configs"]) <= {c.get("config", k) for k, c in COVERAGE_CASES.items()} and len(coverage_cases()) ==
       len(COVERAGE_CASES), "CP1b every coverage-pack configuration has a representative case")
    v01 = cases(PACK, ["single_day", "multi3"])
    ok(v01 and all(c["config"] in (yaml.safe_load(open(PACK))["configs"]["single_day"],
                                   yaml.safe_load(open(PACK))["configs"]["multi3"]) for c in v01),
       "CP2 cases are restricted to the old pack's configurations")
    ok(compare({"a": "1", "b": "2"}, {"a": "1", "b": "2"})["identical"]
       and compare({"a": "1"}, {"a": "1", "b": "2"})["differing"] == ["b"]
       and compare({"a": "1"}, {"a": "3"})["differing"] == ["a"], "CP3 a missing or different digest is a difference")
    small = [c for c in v01 if c["id"].startswith("r0_2#")][:1]
    d = _run(ROOT, small, sys.executable)
    ok(len(d) == 1 and d == _run(ROOT, small, sys.executable), "CP4 the case runner is deterministic (same digests)")
    with tempfile.TemporaryDirectory() as d:          # CP7 (Codex r1 P1-13): calibrator code changes are classified
        for rel in CALIBRATOR_FILES:
            os.makedirs(os.path.dirname(os.path.join(d, "old", rel)), exist_ok=True)
            os.makedirs(os.path.dirname(os.path.join(d, "new", rel)), exist_ok=True)
            src = open(os.path.join(ROOT, rel)).read()
            open(os.path.join(d, "old", rel), "w").write(src)
            open(os.path.join(d, "new", rel), "w").write(src + "\n\ndef added_helper():\n    return 1\n")
        same = code_changes(os.path.join(d, "old"), os.path.join(d, "new"))
        rel = "toolkit/analysis/calibration.py"
        open(os.path.join(d, "new", rel), "a").write("\n\ndef posterior(*a, **k):\n    return None\n")
        changed = code_changes(os.path.join(d, "old"), os.path.join(d, "new"))
        ok(same["ok"] and not changed["ok"] and changed["files"][rel]["changed"] == ["posterior"],
           "CP7 an added definition is allowed; a changed one on the coverage path (posterior) is not")
    if os.path.exists(OUT):
        rec = yaml.safe_load(open(OUT))
        ok(rec.get("calibrator_code", {}).get("ok") is True and rec.get("old_coverage_identity")
           and rec.get("old_row_code_sha256") and rec.get("new_calibrator_file_sha256") == file_hashes(),
           "CP8 the committed proof binds the old coverage identity, the old row digests and the live calibrator files")
        # CP9 (Codex r2 P1-3): restamp refuses a row computed by other calibrator code, and changed live files

        saved = (OUT, COVERAGE)
        with tempfile.TemporaryDirectory() as d:
            pr, cv = os.path.join(d, "proof.yaml"), os.path.join(d, "cov.yaml")
            rec2 = dict(rec)
            yaml.safe_dump(rec2, open(pr, "w"))
            cov = yaml.safe_load(open(COVERAGE))
            cov["engine_identity"] = {"stale": True}
            for r in cov["benchmarks"]:
                if "code_sha256" in r:
                    r["computed_code_sha256"] = "UNRELATED_CALIBRATOR"
                    break
            yaml.safe_dump(cov, open(cv, "w"))
            OUT, COVERAGE = pr, cv
            rs = restamp()
            c2 = yaml.safe_load(open(cv))
            r0 = next(r for r in c2["benchmarks"] if "code_sha256" in r)
            ok(r0["id"] in rs["rows_not_rebound"] and r0["code_sha256"] == "UNRELATED_CALIBRATOR",
               "CP9 a row computed by other calibrator code is not re-bound: it keeps that code's digest")
            yaml.safe_dump(cov, open(cv, "w"))
            rec2["new_calibrator_file_sha256"] = {k: "0" * 64 for k in rec["new_calibrator_file_sha256"]}
            yaml.safe_dump(rec2, open(pr, "w"))
            try:
                restamp()
                ok(False, "CP10 calibrator files changed after the proof refuse the restamp")
            except SystemExit as e:
                ok("changed after the proof" in str(e), f"CP10 calibrator files changed after the proof refuse "
                                                        f"the restamp: {e}")
        OUT, COVERAGE = saved
        ok(rec.get("identical") is True and rec.get("cases", 0) > 0,
           f"CP5 the committed proof says identical ({rec.get('identical_cases')}/{rec.get('cases')} cases)")
        cov = yaml.safe_load(open(COVERAGE))
        ok((cov.get("restamped") or {}).get("proof") == os.path.basename(OUT)
           and cov["engine_identity"]["code_sha256"] == rec["new_identity"]["code_sha256"]
           and any(k.startswith("coverage:") for k in rec["digests"]),
           "CP6 coverage_results.yaml is re-stamped by the proof for the proof's code")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("prove", "restamp"))
    ap.add_argument("--old-ref", default="origin/v2")
    a = ap.parse_args(argv)
    if a.cmd == "prove":
        r = prove(a.old_ref)
        print(f"{r['identical_cases']}/{r['cases']} identical; differing: {r['differing']}")
        return 0 if r["identical"] else 1
    print(restamp())
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
