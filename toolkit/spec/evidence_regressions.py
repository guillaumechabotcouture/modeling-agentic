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
DIFFERS_HEADING = "Inputs whose chosen value or range differs from what execution will use"


def _fixture_yaml(d, rel, fn):
    import yaml
    p = os.path.join(d, rel)
    doc = yaml.safe_load(open(p)) if os.path.exists(p) else {}
    fn(doc)                         # edits the document in place; its return value is ignored
    yaml.safe_dump(doc, open(p, "w"), sort_keys=False)


def _fixture_json(d, rel, fn):
    p = os.path.join(d, rel)
    doc = json.load(open(p))
    fn(doc)
    json.dump(doc, open(p, "w"), indent=1)


def _fixture_write(d, rel, text, mode="w"):
    """A self-test fixture write in the temporary run dir the table creates."""
    with open(os.path.join(d, rel), mode) as f:
        f.write(text)


def _fixture_remove(d, rel):
    os.remove(os.path.join(d, rel))


def _entries(d, fn):
    _fixture_yaml(d, DOSSIER, lambda doc: fn(doc["inputs"]))


def _spec(d, fn):
    _fixture_yaml(d, SPEC, fn)


PATCH: dict = {}                    # the toy declaration a row patched, written into the archive's code too


def _patch_decl(new):
    from toolkit.spec import toy_engines as T
    old = T.ToyDossierEngine.DECISION_INPUTS
    T.ToyDossierEngine.DECISION_INPUTS = new
    PATCH["decl"] = new

    def undo():
        T.ToyDossierEngine.DECISION_INPUTS = old
        PATCH.pop("decl", None)
    return undo


_CODE: dict = {}


def code_files() -> dict:
    """{path: bytes} of the code a launch archive ships (tracked toolkit/, agents/, scripts/, cloud/): the launch
    check runs the archive's own code, so the table's archives carry it."""
    if not _CODE:
        import subprocess
        names = subprocess.run(["git", "-C", ROOT, "ls-files", "toolkit", "agents", "scripts", "cloud"],
                               capture_output=True, text=True, check=True).stdout.split()
        for n in names:
            p = os.path.join(ROOT, n)
            if os.path.isfile(p):
                _CODE[n] = open(p, "rb").read()
    out = dict(_CODE)
    extra = ""
    if "decl" in PATCH:                 # the declaration change the row made, in the shipped code
        extra += f"\n\nToyDossierEngine.DECISION_INPUTS = {PATCH['decl']!r}\n"
    for name, value in sorted((PATCH.get("attrs") or {}).items()):     # engine attributes a row set
        extra += f"\nToyDossierEngine.{name} = {value!r}\n"
    if extra:
        out["toolkit/spec/toy_engines.py"] = out["toolkit/spec/toy_engines.py"] + extra.encode()
    for path, text in sorted((PATCH.get("append") or {}).items()):       # other shipped-code changes a row made
        out[path] = out[path] + text.encode()
    return out


BENCH_FAIL = "engine benchmarks: toy benchmark verdict changed after signing (row R8)"


def _benchmark_fails(d):
    """#215's benchmark check fails after signing (an engine restamp or a coverage change would): in-process, and in
    the shipped preconditions.py the launch checks."""
    from toolkit.spec import preconditions as PC
    old = PC.CHECKS["benchmark_eligibility"]
    PC.CHECKS["benchmark_eligibility"] = lambda run_dir, spec, signer: [BENCH_FAIL]
    PATCH.setdefault("append", {})["toolkit/spec/preconditions.py"] = (
        f"\n\nCHECKS['benchmark_eligibility'] = lambda run_dir, spec, signer: [{BENCH_FAIL!r}]\n")

    def undo():
        PC.CHECKS["benchmark_eligibility"] = old
        PATCH.get("append", {}).pop("toolkit/spec/preconditions.py", None)
    return undo


def _patch_attr(name, value):
    """Set a toy dossier engine attribute in-process and in the shipped code (the launch checks the archive)."""
    from toolkit.spec import toy_engines as T
    had, old = name in T.ToyDossierEngine.__dict__, T.ToyDossierEngine.__dict__.get(name)
    setattr(T.ToyDossierEngine, name, value)
    PATCH.setdefault("attrs", {})[name] = value

    def undo():
        if had:
            setattr(T.ToyDossierEngine, name, old)
        else:
            delattr(T.ToyDossierEngine, name)
        PATCH.get("attrs", {}).pop(name, None)
    return undo


def _tar(d, state, signoff="owner"):
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w:gz") as t:
        files = {f"{R}/run_state.json": json.dumps({"state": state, "signoff": signoff}).encode(), **code_files()}
        for root, _, fs in os.walk(d):
            for f in fs:
                rel = os.path.relpath(os.path.join(root, f), d)
                if rel != "run_state.json":
                    files[f"{R}/{rel}"] = open(os.path.join(root, f), "rb").read()
        for name, data in files.items():
            ti = tarfile.TarInfo(name)
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


def p_brief_fixture(d):
    import decision_brief as DB
    import yaml
    yaml.safe_dump(DB.example_brief(), open(os.path.join(d, DB.BRIEF), "w"))
    yaml.safe_dump({"hard_blockers": [{"id": "HB-001", "text": "t"}]}, open(os.path.join(d, DB.CRITERIA), "w"))
    return _refusal(lambda: DB.sign(d, "owner", note="n"))


def p_stage(d):
    from agents import stage_machine as SM
    return _refusal(lambda: SM.sign(d, "owner", "n"))


def _launch(d, state, signoff="owner"):
    import run_cluster as RC
    from toolkit.spec import toy_engines as T
    old, RC.TEST_ENGINE_REFS = RC.TEST_ENGINE_REFS, T.REFS      # the toy engines, in the checker's process too
    try:
        return "; ".join(RC.stale_sheet_problems(_tar(d, state, signoff), ("--resume", R)))
    finally:
        RC.TEST_ENGINE_REFS = old


def p_launch_sign(d):
    return _launch(d, "SIGN")


def p_status(d):
    from toolkit.spec import signoff as SO
    s = SO.status(d)
    return "" if s == "signed" else s


def p_launch_past(d):
    return _launch(d, "ANALYZE")


class _Reached(Exception):
    """The path reached compute (a backend call, or the submission)."""


def p_resume(d, signoff="owner"):
    """The real entry point: Machine.run on a run resumed past ENGINE_CHOICE. Refused: it ends run_failed before any
    backend call. Accepted: it reaches the backend (compute)."""
    from agents import stage_machine as SM

    class Backend:
        def __getattr__(self, name):
            def call(*a, **k):
                raise _Reached(name)
            return call
    st = SM.init_run(d, "toy question", 3, signoff, "v2", None, derived_inputs=False)
    st.state = "CONFIGURE"
    SM.save_state(d, st)
    m = SM.Machine(d, None, None, backend=Backend(), log=lambda *a: None, cross_engine_check=False)
    try:
        status = m.run()
    except _Reached:
        return ""
    why = (m.st.history[-1] or {}).get("why", "") if m.st.history else ""
    return why or f"ended {status} without reaching compute"


def p_relaunch_fixture(d):
    """The real entry point: run_cluster.relaunch with its cloud calls stubbed. Refused: SystemExit before the first
    upload; accepted: it reaches the upload (the submission)."""
    import types
    import contextlib
    import run_cluster as RC
    from toolkit.spec import toy_engines as T
    cfg = types.SimpleNamespace(name="n", out_dir=os.path.join(d, ".out"), state_file=os.path.join(d, ".state.json"),
                                storage_prefix="p", storage_account="a", flags=("--resume", R), tree=ROOT,
                                question_file=os.path.join(d, ".q"))
    json.dump({"l1": {"launch_id": "x", "worker": "w"}}, open(cfg.state_file, "w"))
    open(cfg.question_file, "w").write("q")
    code = _tar(d, "ANALYZE")

    class Client:
        def upload_file(self, *a, **k):
            raise _Reached("upload_file")
    saved = {k: getattr(RC, k) for k in ("_config_lock", "budget_guard", "attach", "_on_worker", "relaunch_refusal",
                                         "code_tarball", "pipeline_preflight", "TEST_ENGINE_REFS")}
    saved_rb, saved_rs = RC.ws.record_blob, sys.modules.get("results_store")
    try:
        RC._config_lock = lambda cfg: contextlib.nullcontext()
        RC.budget_guard = lambda *a, **k: None
        RC.attach = lambda name: types.SimpleNamespace(get_client=lambda: Client())
        RC._on_worker = lambda *a, **k: {"state": "idle"}
        RC.relaunch_refusal = lambda *a, **k: None
        RC.code_tarball = lambda cfg: (code, "abc1234")
        RC.pipeline_preflight = lambda cfg, code: "v2"
        RC.TEST_ENGINE_REFS = T.REFS
        RC.ws.record_blob = lambda *a, **k: "none"
        sys.modules["results_store"] = types.SimpleNamespace(list_blobs=lambda *a, **k: [])
        try:
            RC.relaunch(cfg, "l1")
            return "relaunch returned without reaching the submission"
        except _Reached:
            return ""
        except SystemExit as e:
            return str(e)
    finally:
        for k, v in saved.items():
            setattr(RC, k, v)
        RC.ws.record_blob = saved_rb
        if saved_rs is None:
            sys.modules.pop("results_store", None)
        else:
            sys.modules["results_store"] = saved_rs


PRE_PATHS = {"signoff.sign": p_signoff, "decision_brief.sign": p_brief_fixture, "stage_machine.sign": p_stage,
             "launch at SIGN": p_launch_sign}
POST_PATHS = {"signoff.status": p_status, "launch past SIGN": p_launch_past, "relaunch": p_relaunch_fixture,
              "Machine.run (resume past ENGINE_CHOICE)": p_resume}


# ----------------------------------------------------------------------------------------------- rows
def _set(m, k, v):
    m[k] = v


TOY_K = {"model_value": 0.8, "model_source": "card", "no_data": {"reason": "toy", "borrowing_rule": "the range"},
         "chosen": {"range": [0.7, 0.9], "rule": "the toy range"}, "decision_test": "prior_range_k"}


def _with_group(extra_entry, binding=None):
    """The toy declaration with the unit attribute `group` an input of a (the binding's default, a column or a value),
    and the dossier's group entry."""
    from toolkit.spec import toy_engines as T

    def mutate(d):
        decl = {c: dict(v) for c, v in T.ToyDossierEngine.DECISION_INPUTS.items()}
        decl["a"] = dict(decl["a"], inputs=list(decl["a"]["inputs"]) + [
            {"kind": "unit_attribute", "name": "group", "why": "the toy's group label"}])
        if binding is not None:
            _spec(d, lambda s: _set(s["decision_units"]["engine_attributes"], "group", binding))
        else:
            _spec(d, lambda s: s["decision_units"].setdefault("defaults_accepted", {}).__setitem__(
                "group", "the toy has no groups"))
        _entries(d, lambda e: _set(e, "unit_attribute:group", extra_entry))
        return _patch_decl(decl)
    return mutate


def _group_entry(model, chosen):
    return {"model_value": model, "model_source": "toy", "no_data": {"reason": "toy", "borrowing_rule": "the label"},
            "chosen": {"value": chosen, "rule": "the label"}}


# (id, what the reviewers found, mutate(d) -> undo or None, regenerate the sheet afterwards)
PRE_ROWS = [
    ("control", "nothing changed: every path accepts", lambda d: None, True),
    ("R1-no-dossier", "the options need inputs and the spec names no dossier",
     lambda d: _spec(d, lambda s: s.pop("evidence")), True),
    ("R1-missing-file", "the spec names a dossier that does not exist", lambda d: _fixture_remove(d, DOSSIER), True),
    ("R1-uncovered", "an input has no entry", lambda d: _entries(d, lambda e: e.pop("unit_attribute:x")), True),
    ("R1-blank", "a blank entry", lambda d: _entries(d, lambda e: _set(e, "unit_attribute:x", None)), True),
    ("R1-unparseable", "the dossier does not parse", lambda d: _fixture_write(d, DOSSIER, "inputs: [unclosed\n"), True),
    ("R1-stale-sheet", "the dossier changed after the sheet",
     lambda d: _fixture_write(d, DOSSIER, "# edited\n", "a"), False),
    ("R3-no-sheet", "no sheet showed the dossier", lambda d: _fixture_remove(d, SHEET), False),
    ("R4-sheet-other-spec", "the sheet was generated for another spec (the dossier unchanged)",
     lambda d: _spec(d, lambda s: _set(s, "question", "Another question?")), False),
    ("R5-sheet-other-inputs", "an input file the run reads changed after the sheet (the units table)",
     lambda d: _fixture_write(d, "units.csv", "\n", "a"), False),
    ("R1-malformed-decl", "a component declared {} (no inputs, no reason)",
     lambda d: _patch_decl({"a": {}, "b": {"inputs": [], "none_reason": "r"}}), True),
    ("R1-non-dict-input", "an input entry that is not a mapping",
     lambda d: _patch_decl({"a": {"inputs": ["eff_a"]}, "b": {"inputs": [], "none_reason": "r"}}), True),
    ("R1-engine-map", "a used component with no engine mapping",
     lambda d: _spec(d, lambda s: s["options"]["engine_map"].pop("a")), True),
    ("R1-nested-blank", "a nested blank chosen value",
     lambda d: _entries(d, lambda e: _set(e["parameter:k"]["chosen"], "value", [None, " "])), True),
    ("R5-observation-mapping", "a single observation given as a mapping, not a list",
     lambda d: _entries(d, lambda e: _set(e["parameter:eff_a"], "observations", e["parameter:eff_a"]["observations"][0])),
     True),
    ("R1-test-other-quantity", "a range citing a test of another quantity",
     lambda d: _entries(d, lambda e: _set(e["parameter:eff_a"], "decision_test", "DT1")), True),
    ("R4-generated-name", "prior_range_<p> cited where the runner generates fitted_range_<p>",
     lambda d: _entries(d, lambda e: _set(e["parameter:eff_a"], "decision_test", "prior_range_eff_a")), True),
    ("R2-excluded-prior", "prior_range_<p> cited for a prior the draw plan excludes",
     lambda d: (_spec(d, lambda s: (s["uncertainty"].__setitem__("priors", {"k": {
         "dist": "uniform", "range": [0.7, 0.9], "source": "toy"}}), s["uncertainty"]["excluded"].append(
         {"parameter": "k", "reason": "held"}))), _entries(d, lambda e: _set(e, "parameter:k", dict(TOY_K)))), True),
    ("R7-resolver-error", "an input resolver that raises (calibration.targets given as a string) is a refusal",
     lambda d: _spec(d, lambda s: _set(s["calibration"], "targets", "unused")), True),
    ("R6-emulator-parameters", "calibration.parameters under emulator_inversion (which fits only the attribute) "
                               "used to exempt a range from its decision test",
     lambda d: (_spec(d, lambda s: _set(s["calibration"], "parameters", ["k"])),
                _entries(d, lambda e: _set(e, "parameter:k", {k: v for k, v in TOY_K.items() if k != "decision_test"}))),
     True),
    ("R3-test-not-executable", "a parameter_value test the emulator path cannot run (k neither drawn nor set)",
     lambda d: (_spec(d, lambda s: s["analysis"]["decision_tests"].append({
         "id": "DT_k", "type": "parameter_value", "parameter": "k", "values": [0.7, 0.9],
         "text": "Does the plan change across the toy slope?"})),
                _entries(d, lambda e: _set(e, "parameter:k", dict(TOY_K, decision_test="DT_k")))), True),
]

# advisory rows (#218 round 5): a dossier value or range that differs from what execution will use signs (the owner
# decides) and the sheet flags the row as differing, at its top; (id, what, mutate, the input flagged)
ADVISORY_ROWS = [
    ("R2-explicit-value", "the chosen value 0.8 against the spec's engine_parameters value 0.9",
     lambda d: _spec(d, lambda s: _set(s, "engine_parameters", {"k": {
         "value": 0.9, "source": "toy", "reason": "toy", "basis": "setting_data"}})), "parameter:k"),
    ("R3-default-value", "the chosen value 0.1 against the engine default 0.8",
     lambda d: _entries(d, lambda e: (_set(e["parameter:k"], "model_value", 0.1),
                                      _set(e["parameter:k"], "chosen", {"value": 0.1, "rule": "r"}))), "parameter:k"),
    ("R4-default-attribute", "an attribute left at its card default (\"\") and a dossier choosing \"g1\"",
     _with_group(_group_entry("g1", "g1")), "unit_attribute:group"),
    ("R4-column-attribute", "an attribute bound to a numeric column and a dossier choosing a value outside it",
     _with_group(_group_entry({"min": 0, "median": 0, "max": 0}, 999.0), {"column": "cov"}), "unit_attribute:group"),
    ("R3-structured-literal", "an attribute bound to a literal and a dossier choosing another literal",
     _with_group(_group_entry(["g1", "g2"], ["g1", "g2"]), {"value": ["g3", "g4"], "source": "toy"}),
     "unit_attribute:group"),
    ("R3-range-inside-draws", "a drawn parameter's chosen range [0.7, 0.9] against its draw range [0.6, 1.0]",
     lambda d: (_spec(d, lambda s: _set(s, "engine_parameters", {"k": {
         "value": 0.8, "range": [0.6, 1.0], "dist": "uniform", "source": "toy", "reason": "toy",
         "basis": "setting_data"}})), _entries(d, lambda e: _set(e, "parameter:k", dict(TOY_K)))), "parameter:k"),
    ("R1-test-narrow", "a fitted parameter's chosen range [0.2, 0.6] against its fitted range [0.3, 0.7]",
     lambda d: _entries(d, lambda e: _set(e["parameter:eff_a"]["chosen"], "range", [0.2, 0.6])), "parameter:eff_a"),
    ("R5-calibration-range", "the calibration attribute's chosen range [0, 1] against its search range [-2, 2]",
     lambda d: _entries(d, lambda e: _set(e["unit_attribute:x"]["chosen"], "range", [0, 1])), "unit_attribute:x"),
]

# post-sign rows: signed from the control first; mutate(d) -> undo or None
POST_ROWS = [
    ("control", "nothing changed: every path accepts", lambda d: None),
    ("R1-dossier-edited", "the dossier edited after signing",
     lambda d: _fixture_write(d, DOSSIER, "# edited after signing\n", "a")),
    ("R1-spec-edited", "the spec edited after signing (evidence removed with it)",
     lambda d: _spec(d, lambda s: s.pop("evidence"))),
    ("R5-units-edited", "the units table (and its calibration targets) edited after signing",
     lambda d: _fixture_write(d, "units.csv", open(os.path.join(d, "units.csv")).read().replace("0.", "0.9", 1))),
    ("R1-legacy-record", "a sign-off made before the requirement (no evidence binding)",
     lambda d: _fixture_json(d, "decision_spec.signoff.json", lambda r: r.pop("evidence"))),
    ("R3-empty-evidence", "a sign-off whose evidence binding is {}",
     lambda d: _fixture_json(d, "decision_spec.signoff.json", lambda r: _set(r, "evidence", {}))),
    ("R3-partial-evidence", "a sign-off whose evidence binding lacks its sha256",
     lambda d: _fixture_json(d, "decision_spec.signoff.json", lambda r: r["evidence"].pop("sha256"))),
    ("R3-declaration-grew", "a declaration that now requires an input the signed dossier lacks",
     lambda d: _patch_decl({"a": {"inactive": [0], "inputs": [
         {"kind": "parameter", "name": "eff_a", "why": "w"}, {"kind": "unit_attribute", "name": "x", "why": "w"},
         {"kind": "parameter", "name": "base", "why": "w"}]},
         "b": {"inactive": [0], "inputs": [{"kind": "parameter", "name": "k", "why": "w"}]}})),
    ("R4-declaration-changed", "a declaration changed since signing (the same inputs, another content)",
     lambda d: _patch_decl({"a": {"inactive": [0], "inputs": [
         {"kind": "parameter", "name": "eff_a", "why": "another reason"},
         {"kind": "unit_attribute", "name": "x", "why": "w"}]},
         "b": {"inactive": [0], "inputs": [{"kind": "parameter", "name": "k", "why": "w"}]}})),
    ("R8-benchmark-failed", "the benchmark check (#215) fails after signing: status, launch, relaunch and resume run it",
     _benchmark_fails),
    ("R5-declaration-shrank", "every declared input removed since signing (none_reason everywhere)",
     lambda d: _patch_decl({"a": {"inputs": [], "none_reason": "none now"},
                            "b": {"inputs": [], "none_reason": "none now"}})),
]


def _fixture_extensionless_units(d):
    import shutil
    shutil.move(os.path.join(d, "units.csv"), os.path.join(d, "units"))
    _spec(d, lambda s: _set(s["decision_units"], "csv", "units"))


def _no_dossier_run(d):
    from toolkit.spec import evidence_dossier as ED
    ED.toy_dossier_spec(d, ref="toolkit.engines.contract:ToyEngine", dossier=False)
    _fixture_remove(d, DOSSIER) if os.path.exists(os.path.join(d, DOSSIER)) else None


def _produce_allocation(d):
    import yaml
    out = (yaml.safe_load(open(os.path.join(d, SPEC)))["deliverable"] or {}).get("allocation_file")
    _fixture_write(d, out, "unit,package,dalys_averted\n0,a,1.0\n")


_UNDO: list = []


def _toy_priors_file(d):
    """The chosen engine gets a priors file (an engine-side input the closure binds) for the row."""
    p = os.path.join(d, "toy_priors.yaml")
    _fixture_write(d, "toy_priors.yaml", "priors: {}\nfixed: {base: toy, k: toy, noise: toy}\n")
    _UNDO.append(_patch_attr("priors_path", p))


def _two_candidates(d):
    """engine: auto with two eligible candidates whose engine files differ (the toy dossier engine; the toy spec
    engine with its benchmark pack)."""
    _spec(d, lambda s: _set(s, "engine", {"auto": True, "candidates": [
        "toolkit.spec.toy_engines:ToyDossierEngine", "toolkit.spec.toy_engines:ToySpecEngine"]}))


def _choose_toy(d):
    _fixture_write(d, "engine_choice.json", json.dumps({"engine": {"ref": "toolkit.spec.toy_engines:ToyDossierEngine"}}))


# post-sign rows with a setup before signing (review r6): (id, what, mutate, setup)
POST_SETUP_ROWS = [
    ("R6-allocation-produced", "the lifecycle: sign, the analysis writes its allocation (an output), then resume",
     _produce_allocation, None),
    ("R6-extensionless-units", "an extensionless units path (execution resolves it) edited after signing",
     lambda d: _fixture_write(d, "units", "\n", "a"), _fixture_extensionless_units),
    ("R6-no-dossier-edited", "a run that needs no dossier, its units table edited after signing",
     lambda d: _fixture_write(d, "units.csv", "\n", "a"), _no_dossier_run),
]
POST_SETUP_ROWS += [
    ("R7-engine-file-deleted", "the chosen engine's priors file deleted after signing (its parameters stop being drawn)",
     lambda d: _fixture_remove(d, "toy_priors.yaml"), _toy_priors_file),
    ("R7-engine-chosen", "engine: auto signed with two candidates, then ENGINE_CHOICE picks one (its files a subset)",
     _choose_toy, _two_candidates),
]
ACCEPTED_ROWS = ("R6-allocation-produced", "R7-engine-chosen")

# Why each row must be refused (any of these, case-insensitive), so a refusal for some other reason does not count.
WANT = {
    "R1-no-dossier": ["names no evidence dossier"], "R1-missing-file": ["does not exist"],
    "R1-uncovered": ["no entry for unit_attribute:x"], "R1-blank": ["entry unit_attribute:x is blank"],
    "R1-unparseable": ["does not parse"], "R1-stale-sheet": ["another evidence dossier"],
    "R3-no-sheet": ["no sign-off sheet"], "R4-sheet-other-spec": ["another spec", "another decision_spec"],
    "R5-sheet-other-inputs": ["other inputs", "input closure"],
    "R1-malformed-decl": ["malformed"], "R1-non-dict-input": ["malformed"],
    "R1-engine-map": ["no engine mapping"], "R1-nested-blank": ["chosen.value is blank"],
    "R5-observation-mapping": ["observations must be a list"],
    "R1-test-other-quantity": ["does not vary"], "R4-generated-name": ["fitted_range_eff_a"],
    "R2-excluded-prior": ["not drawn", "not a test the runner generates"],
    "R3-test-not-executable": ["does not execute"],
    "R1-dossier-edited": ["changed", "evidence dossier"], "R1-spec-edited": ["changed", "not the one signed"],
    "R5-units-edited": ["changed", "units.csv"],
    "R1-legacy-record": ["unsigned_evidence"], "R3-empty-evidence": ["unsigned_evidence"],
    "R3-partial-evidence": ["unsigned_evidence"], "R3-declaration-grew": ["evidence_incomplete", "parameter:base"],
    "R4-declaration-changed": ["declaration", "evidence_incomplete", "changed"],
    "R5-declaration-shrank": ["declaration", "evidence_incomplete", "changed"],
    "R6-extensionless-units": ["changed", "units"], "R6-no-dossier-edited": ["changed", "units.csv"],
    "R6-emulator-parameters": ["needs its decision_test"],
    "R7-resolver-error": ["resolver"], "R8-benchmark-failed": ["engine benchmarks", "preconditions_failed"], "R7-engine-file-deleted": ["changed", "toy_priors.yaml"],
}


def _right(rid: str, why: str) -> bool:
    if rid == "control":
        return not why
    return bool(why) and any(w.lower() in why.lower() for w in WANT[rid])


def _self_test_table(ok) -> None:
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
    for rid, what, mutate, key in ADVISORY_ROWS:
        for pname, path in PRE_PATHS.items():
            with tempfile.TemporaryDirectory() as d:
                ED.toy_dossier_spec(d)
                undo = mutate(d)
                try:
                    SO.write_sheet(d)
                    md = open(os.path.join(d, SHEET)).read()
                    top = md.split(DIFFERS_HEADING)[1].split("\n\n")[0] if DIFFERS_HEADING in md else ""
                    why = path(d)
                finally:
                    if callable(undo):
                        undo()
            ok(not why and key in top,
               f"ET {rid} via {pname}: signs, and the sheet lists {key} as differing ({what}): "
               f"{why[:120]!r} | {top[:160]!r}")
    for row in POST_ROWS + POST_SETUP_ROWS:
        rid, what, mutate = row[:3]
        setup = row[3] if len(row) > 3 else None
        for pname, path in POST_PATHS.items():
            with tempfile.TemporaryDirectory() as d:
                ED.toy_dossier_spec(d)
                if setup:
                    setup(d)
                SO.write_sheet(d)
                SO.sign(d, "owner", note="n")
                undo = mutate(d)
                try:
                    why = path(d)
                finally:
                    if callable(undo):
                        undo()
                    while _UNDO:
                        _UNDO.pop()()
            accept = rid == "control" or rid in ACCEPTED_ROWS
            ok((not why) if accept else _right(rid, why),
               f"ET {rid} via {pname}: {'accepted' if accept else 'refused'} ({what}): {why[:160]!r}")
    # signoff: none (nobody signs): the launch preflight and the runtime gate apply one rule, coverage without a
    # record: a complete dossier passes both, an uncovered one is refused by both
    for rid, mutate in (("control", lambda d: None),
                        ("R4-signoff-none-uncovered", lambda d: _entries(d, lambda e: e.pop("unit_attribute:x")))):
        for pname, path in (("launch past SIGN", lambda d: _launch(d, "ANALYZE", signoff="none")),
                            ("Machine.run", lambda d: p_resume(d, signoff="none"))):
            with tempfile.TemporaryDirectory() as d:
                ED.toy_dossier_spec(d)
                mutate(d)
                SO.write_sheet(d)
                why = path(d)
            ok((not why) if rid == "control" else ("no entry for unit_attribute:x" in why),
               f"ET signoff none, {rid} via {pname}: {'accepted' if rid == 'control' else 'refused'}: {why[:160]!r}")


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    try:
        _self_test_table(ok)
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
