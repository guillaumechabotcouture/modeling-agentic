#!/usr/bin/env python3
"""The evidence-dossier regression table (#218 review rounds 1-3): every bypass the reviewers found, driven through
every path that can let a run reach compute. Each must refuse it.

    python3 toolkit/spec/evidence_regressions.py --self-test

Pre-sign rows go through the signers (toolkit/spec/signoff.py sign, scripts/decision_brief.py sign,
agents/stage_machine.py sign) and the launch check of a run at SIGN (cloud/run_cluster.py stale_sheet_problems).
Post-sign rows go through signoff.status, the launch check past SIGN, and the stage machine's check on resuming past
ENGINE_CHOICE. A control row (nothing changed) must pass every path, so a path that refuses everything fails too.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (ROOT, os.path.join(ROOT, "scripts"), os.path.join(ROOT, "cloud")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

R = "runs/r1"
SPEC, DOSSIER, SHEET = "decision_spec.yaml", "evidence_dossier.yaml", "decision_spec.signoff_sheet.md"


def _yaml(d, rel, fn):
    import yaml
    p = os.path.join(d, rel)
    doc = yaml.safe_load(open(p)) if os.path.exists(p) else {}
    fn(doc)                         # edits the document in place; its return value is ignored
    yaml.safe_dump(doc, open(p, "w"), sort_keys=False)


def _json(d, rel, fn):
    p = os.path.join(d, rel)
    doc = json.load(open(p))
    fn(doc)
    json.dump(doc, open(p, "w"), indent=1)


def _entries(d, fn):
    _yaml(d, DOSSIER, lambda doc: fn(doc["inputs"]))


def _spec(d, fn):
    _yaml(d, SPEC, fn)


def _patch_decl(new):
    from toolkit.spec import toy_engines as T
    old = T.ToyDossierEngine.DECISION_INPUTS
    T.ToyDossierEngine.DECISION_INPUTS = new

    def undo():
        T.ToyDossierEngine.DECISION_INPUTS = old
    return undo


def _tar(d, state):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as t:
        files = {"run_state.json": json.dumps({"state": state}).encode()}
        for root, _, fs in os.walk(d):
            for f in fs:
                rel = os.path.relpath(os.path.join(root, f), d)
                if rel != "run_state.json":
                    files[rel] = open(os.path.join(root, f), "rb").read()
        for rel, data in files.items():
            ti = tarfile.TarInfo(f"{R}/{rel}")
            ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    return bio.getvalue()


# ----------------------------------------------------------------------------------------------- paths
def _refusal(fn) -> str:
    try:
        fn()
        return ""
    except ValueError as e:
        return str(e) or "refused"


def p_signoff(d):
    from toolkit.spec import signoff as SO
    return _refusal(lambda: SO.sign(d, "owner", note="n"))


def p_brief(d):
    import decision_brief as DB
    import yaml
    yaml.safe_dump(DB.example_brief(), open(os.path.join(d, DB.BRIEF), "w"))
    yaml.safe_dump({"hard_blockers": [{"id": "HB-001", "text": "t"}]}, open(os.path.join(d, DB.CRITERIA), "w"))
    return _refusal(lambda: DB.sign(d, "owner", note="n"))


def p_stage(d):
    from agents import stage_machine as SM
    return _refusal(lambda: SM.sign(d, "owner", "n"))


def p_launch_sign(d):
    import run_cluster as RC
    return "; ".join(RC.stale_sheet_problems(_tar(d, "SIGN"), ("--resume", R)))


def p_status(d):
    from toolkit.spec import signoff as SO
    s = SO.status(d)
    return "" if s == "signed" else s


def p_launch_past(d):
    import run_cluster as RC
    return "; ".join(RC.stale_sheet_problems(_tar(d, "ANALYZE"), ("--resume", R)))


def p_resume(d):
    from agents import stage_machine as SM
    return "; ".join(SM.evidence_gate(d))


PRE_PATHS = {"signoff.sign": p_signoff, "decision_brief.sign": p_brief, "stage_machine.sign": p_stage,
             "launch at SIGN": p_launch_sign}
POST_PATHS = {"signoff.status": p_status, "launch past SIGN": p_launch_past, "resume past ENGINE_CHOICE": p_resume}


# ----------------------------------------------------------------------------------------------- rows
def _set(m, k, v):
    m[k] = v


TOY_K = {"model_value": 0.8, "model_source": "card", "no_data": {"reason": "toy", "borrowing_rule": "the range"},
         "chosen": {"range": [0.7, 0.9], "rule": "the toy range"}, "decision_test": "prior_range_k"}

# (id, what the reviewers found, mutate(d) -> undo or None, regenerate the sheet afterwards)
PRE_ROWS = [
    ("control", "nothing changed: every path accepts", lambda d: None, True),
    ("R1-no-dossier", "the options need inputs and the spec names no dossier",
     lambda d: _spec(d, lambda s: s.pop("evidence")), True),
    ("R1-missing-file", "the spec names a dossier that does not exist",
     lambda d: os.remove(os.path.join(d, DOSSIER)), True),
    ("R1-uncovered", "an input has no entry", lambda d: _entries(d, lambda e: e.pop("unit_attribute:x")), True),
    ("R1-blank", "a blank entry", lambda d: _entries(d, lambda e: _set(e, "unit_attribute:x", None)), True),
    ("R1-unparseable", "the dossier does not parse",
     lambda d: open(os.path.join(d, DOSSIER), "w").write("inputs: [unclosed\n"), True),
    ("R1-stale-sheet", "the dossier changed after the sheet",
     lambda d: open(os.path.join(d, DOSSIER), "a").write("# edited\n"), False),
    ("R3-no-sheet", "no sheet showed the dossier", lambda d: os.remove(os.path.join(d, SHEET)), False),
    ("R1-malformed-decl", "a component declared {} (no inputs, no reason)",
     lambda d: _patch_decl({"a": {}, "b": {"inputs": [], "none_reason": "r"}}), True),
    ("R1-non-dict-input", "an input entry that is not a mapping",
     lambda d: _patch_decl({"a": {"inputs": ["eff_a"]}, "b": {"inputs": [], "none_reason": "r"}}), True),
    ("R1-engine-map", "a used component with no engine mapping",
     lambda d: _spec(d, lambda s: s["options"]["engine_map"].pop("a")), True),
    ("R1-nested-blank", "a nested blank chosen value",
     lambda d: _entries(d, lambda e: _set(e["unit_attribute:x"]["chosen"], "value", [None, " "])), True),
    ("R1-test-other-quantity", "a range whose test varies another quantity",
     lambda d: _entries(d, lambda e: _set(e["parameter:eff_a"], "decision_test", "DT1")), True),
    ("R1-test-narrow", "a range wider than its test's values",
     lambda d: _entries(d, lambda e: _set(e["parameter:eff_a"]["chosen"], "range", [0.2, 0.6])), True),
    ("R2-excluded-prior", "prior_range_<p> cited for a prior the draw plan excludes",
     lambda d: (_spec(d, lambda s: (s["uncertainty"].__setitem__("priors", {"k": {
         "dist": "uniform", "range": [0.7, 0.9], "source": "toy"}}), s["uncertainty"]["excluded"].append(
         {"parameter": "k", "reason": "held"}))), _entries(d, lambda e: _set(e, "parameter:k", dict(TOY_K)))), True),
    ("R2-explicit-value", "the chosen value is not the spec's engine_parameters value (0.1 vs 0.9)",
     lambda d: _spec(d, lambda s: _set(s, "engine_parameters", {"k": {
         "value": 0.9, "source": "toy", "reason": "toy", "basis": "setting_data"}})), True),
    ("R3-default-value", "the chosen value is not the card default the engine runs (eff_a 0.1 vs 0.5)",
     lambda d: _entries(d, lambda e: (_set(e["parameter:eff_a"], "model_value", 0.1),
                                      _set(e["parameter:eff_a"], "chosen", {"value": 0.1, "rule": "r"}),
                                      e["parameter:eff_a"].pop("decision_test"))), True),
    ("R3-structured-literal", "a unit attribute bound to [0.8, 0.9] and a dossier saying [0.1, 0.2]",
     lambda d: (_spec(d, lambda s: _set(s["decision_units"]["engine_attributes"], "x", {"value": [0.8, 0.9],
                                                                                         "source": "toy"})),
                _entries(d, lambda e: (_set(e["unit_attribute:x"], "model_value", [0.1, 0.2]),
                                       _set(e["unit_attribute:x"], "chosen", {"value": [0.1, 0.2], "rule": "r"})))),
     True),
    ("R3-range-inside-draws", "the chosen range does not contain the draw support ([0.7, 0.9] vs [0.6, 1.0])",
     lambda d: (_spec(d, lambda s: _set(s, "engine_parameters", {"k": {
         "value": 0.8, "range": [0.6, 1.0], "dist": "uniform", "source": "toy", "reason": "toy",
         "basis": "setting_data"}})), _entries(d, lambda e: _set(e, "parameter:k", dict(TOY_K)))), True),
    ("R3-test-not-executable", "a parameter_value test the emulator path cannot run (k neither drawn nor set)",
     lambda d: (_spec(d, lambda s: s["analysis"]["decision_tests"].append({
         "id": "DT_k", "type": "parameter_value", "parameter": "k", "values": [0.7, 0.9],
         "text": "Does the plan change across the toy slope?"})),
                _entries(d, lambda e: _set(e, "parameter:k", dict(TOY_K, decision_test="DT_k")))), True),
]

# post-sign rows: signed from the control first; mutate(d) -> undo or None
POST_ROWS = [
    ("control", "nothing changed: every path accepts", lambda d: None),
    ("R1-dossier-edited", "the dossier edited after signing",
     lambda d: open(os.path.join(d, DOSSIER), "a").write("# edited after signing\n")),
    ("R1-spec-edited", "the spec edited after signing (evidence removed with it)",
     lambda d: _spec(d, lambda s: s.pop("evidence"))),
    ("R1-legacy-record", "a sign-off made before the requirement (no evidence binding)",
     lambda d: _json(d, "decision_spec.signoff.json", lambda r: r.pop("evidence"))),
    ("R3-empty-evidence", "a sign-off whose evidence binding is {}",
     lambda d: _json(d, "decision_spec.signoff.json", lambda r: _set(r, "evidence", {}))),
    ("R3-partial-evidence", "a sign-off whose evidence binding lacks its sha256",
     lambda d: _json(d, "decision_spec.signoff.json", lambda r: r["evidence"].pop("sha256"))),
    ("R3-declaration-grew", "a declaration that now requires an input the signed dossier lacks",
     lambda d: _patch_decl({"a": {"inactive": [0], "inputs": [
         {"kind": "parameter", "name": "eff_a", "why": "w"}, {"kind": "unit_attribute", "name": "x", "why": "w"},
         {"kind": "parameter", "name": "base", "why": "w"}]},
         "b": {"inactive": [0], "inputs": [{"kind": "parameter", "name": "k", "why": "w"}]}})),
]


# Why each row must be refused (any of these, case-insensitive), so a refusal for some other reason does not count.
WANT = {
    "R1-no-dossier": ["names no evidence dossier"], "R1-missing-file": ["does not exist"],
    "R1-uncovered": ["no entry for unit_attribute:x"], "R1-blank": ["entry unit_attribute:x is blank"],
    "R1-unparseable": ["does not parse"], "R1-stale-sheet": ["another evidence dossier"],
    "R3-no-sheet": ["no sign-off sheet"], "R1-malformed-decl": ["malformed"], "R1-non-dict-input": ["malformed"],
    "R1-engine-map": ["no engine mapping"], "R1-nested-blank": ["chosen.value is blank"],
    "R1-test-other-quantity": ["does not vary"], "R1-test-narrow": ["do not cover"],
    "R2-excluded-prior": ["not drawn"], "R2-explicit-value": ["= 0.9"], "R3-default-value": ["= 0.5"],
    "R3-structured-literal": ["[0.8, 0.9]"], "R3-range-inside-draws": ["draw support"],
    "R3-test-not-executable": ["does not execute"],
    "R1-dossier-edited": ["changed", "evidence dossier"], "R1-spec-edited": ["changed", "not the one signed"],
    "R1-legacy-record": ["unsigned_evidence"], "R3-empty-evidence": ["unsigned_evidence"],
    "R3-partial-evidence": ["unsigned_evidence"], "R3-declaration-grew": ["evidence_incomplete", "parameter:base"],
}


def _right(rid: str, why: str) -> bool:
    if rid == "control":
        return not why
    return bool(why) and any(w.lower() in why.lower() for w in WANT[rid])


def run_table(ok) -> None:
    import tempfile
    from toolkit.spec import evidence_dossier as ED, signoff as SO
    from toolkit.spec import toy_engines as T
    T.enable_for_test()
    for rid, what, mutate, regen in PRE_ROWS:
        for pname, path in PRE_PATHS.items():
            with tempfile.TemporaryDirectory() as d:
                ED.toy_dossier_spec(d)
                SO.write_sheet(d)
                undo = mutate(d)
                try:
                    if regen and os.path.exists(os.path.join(d, SPEC)):
                        try:
                            SO.write_sheet(d)
                        except Exception:  # noqa: BLE001 -- a row the sheet cannot even render: the path decides
                            pass
                    why = path(d)
                finally:
                    if callable(undo):
                        undo()
            want_refused = rid != "control"
            ok(_right(rid, why),
               f"ET {rid} via {pname}: {'refused' if want_refused else 'accepted'} ({what}): {why[:160]!r}")
    for rid, what, mutate in POST_ROWS:
        for pname, path in POST_PATHS.items():
            with tempfile.TemporaryDirectory() as d:
                ED.toy_dossier_spec(d)
                SO.write_sheet(d)
                SO.sign(d, "owner", note="n")
                undo = mutate(d)
                try:
                    why = path(d)
                finally:
                    if callable(undo):
                        undo()
            want_refused = rid != "control"
            ok(_right(rid, why),
               f"ET {rid} via {pname}: {'refused' if want_refused else 'accepted'} ({what}): {why[:160]!r}")
    # relaunch submits paid work: it runs the same launch check as launch (the archive it would ship)
    import inspect
    import run_cluster as RC
    src = inspect.getsource(RC.relaunch)
    ok("stale_sheet_problems(" in src and src.index("stale_sheet_problems(") < src.index("upload_file("),
       "ET relaunch runs the launch check (stale_sheet_problems) on the archive before it submits anything")


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    try:
        run_table(ok)
    except Exception as e:  # noqa: BLE001 -- before the paths exist (the tests-first commit) this is the failure
        import traceback
        traceback.print_exc()
        ok(False, f"the regression table: {type(e).__name__}: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
