#!/usr/bin/env python3
"""The run dossier: what a run is made of, for the run console (docs/design/v2-run-console.md §15).

    python3 tools/run_console/dossier.py --self-test

Four read-only tabs beside Stream and Figures: Spec, Evidence, Engine and Calibration. Each is built from files the
harness already writes, read through the console's confined reader (run_console._load_rel: a plain relative path,
no link at any component, one no-follow fd). Paths named inside artifacts are untrusted and go through the same
reader. No harness loader runs and no engine is imported. The one read outside the run directory is this checkout's
toolkit/engines/<name>/benchmarks/results.yaml, only when its sha256 equals the one the run recorded. Every part
is data or {"available": False, "why": ...}; the page shows the reason, never an empty panel. Nothing is written.
"""
from __future__ import annotations

import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import run_console as RC  # noqa: E402

REPO = RC.REPO
JS = os.path.join(HERE, "dossier.js")


def na(why: str) -> dict:
    """A part that cannot be shown, and why."""
    return {"available": False, "why": why}


def spec_tab(run_dir: str) -> dict:
    raise NotImplementedError("β")


def evidence_tab(run_dir: str) -> dict:
    raise NotImplementedError("β")


def engine_tab(run_dir: str, repo: str = REPO) -> dict:
    raise NotImplementedError("β")


def calibration_tab(run_dir: str) -> dict:
    raise NotImplementedError("β")


def dossier(run_dir: str, repo: str = REPO) -> dict:
    """{spec, evidence, engine, calibration}; a builder that fails is shown as its error, not hidden."""
    out = {}
    for name, fn in (("spec", spec_tab), ("evidence", evidence_tab), ("engine", lambda d: engine_tab(d, repo)),
                     ("calibration", calibration_tab)):
        try:
            out[name] = fn(run_dir)
        except Exception as e:  # noqa: BLE001 -- a reader bug is shown on the page
            out[name] = na(f"the {name} tab could not be built: {type(e).__name__}: {str(e)[:300]}")
    return out


# ------------------------------------------------------------------ self-test
def _toy_w(d: str, rel: str, data) -> str:
    p = os.path.join(d, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, (dict, list)):
        data = json.dumps(data, indent=1) if rel.endswith(".json") else yaml.safe_dump(data, sort_keys=False)
    with open(p, "w" if isinstance(data, str) else "wb") as f:
        f.write(data)
    return p


def _sha_file(p: str) -> str:
    import hashlib
    with open(p, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _toy_spec(horizon_stop: float = 2028.0, tests=("DT1", "DT2"), note_hostile: str = "") -> dict:
    """A toy decision spec (generic names, toy numbers; no data from any real source)."""
    dts = {"DT1": {"id": "DT1", "type": "parameter_value", "parameter": "p_half", "values": [100.0],
                   "finding": "benchmark_drift:B2", "text": "Does the plan change at a shorter half-life?"},
           "DT2": {"id": "DT2", "type": "interaction_rescale", "components": ["c"], "with_any": ["a", "b"],
                   "benchmark": "B1", "values": [2.0, 1.5], "text": "Does the plan change at the trial interaction?"},
           "DT3": {"id": "DT3", "type": "group_rule", "add": {"id": "same_g", "components": ["a", "b"], "by": "region",
                                                              "rule": "same_choice", "reason": "one choice per region"},
                   "text": "Does the plan change when a region must make one choice?"}}
    return {
        "schema_version": 1,
        "question": "Toy question: choose one package per unit within a budget." + note_hostile,
        "decision_maker": "Toy programme manager", "decision_date": 2024,
        "engine": {"auto": True, "candidates": ["toolkit.engines.contract:ToyEngine"]},
        "horizon": {"start": 2016.0, "stop": horizon_stop},
        "decision_units": {
            "csv": "data/derived/units.csv", "id": "unit_id", "label": "unit_id", "region": "region",
            "engine_attributes": {"season": {"columns": ["s1", "s2"]}, "m0_hint": {"column": "pop"},
                                  "use": {"value": 0.5, "source": "data/inputs/toy/params_registry.yaml use_a"},
                                  "years": {"column": "years", "split": " "}},
            "defaults_accepted": {"month": "no month schedule staged; the engine's default is used"},
            "populations": {"all": {"column": "pop", "age_groups": ["u5", "5p"]}}},
        "options": {
            "populations": {"total": "pop", "elig": "elig_pop"},
            "components": {"a": {"group": "g"}, "b": {"group": "g"}, "c": {"eligible": "elig"}, "d": {}},
            "packages": {"generate": True},
            "engine_map": {"a": {"g": "a"}, "b": {"g": "b"}, "c": {"c": 1}, "d": {"d": 1}},
            "prices": {"base_year": "2024", "index_name": "toy index", "index": {"2020": 100.0, "2024": 125.0}},
            "cost_items": [
                {"id": "a_commodity", "component": "a", "value": 2.0, "price_year": "2020", "price_rule": "index",
                 "basis": "per_commodity", "people_per_commodity": 2.0, "includes": ["commodity", "delivery"]},
                {"id": "b_commodity", "component": "b", "value": 3.0, "price_year": "2024",
                 "price_rule": "as_published", "basis": "per_commodity", "people_per_commodity": 2.0,
                 "includes": ["commodity"]},
                {"id": "c_all", "component": "c", "value": 3.0, "price_year": "2024", "price_rule": "as_published",
                 "basis": "per_eligible", "years": 3, "includes": ["drug", "delivery"]},
                {"id": "d_case", "component": "d", "value": 10.0, "price_year": "2024", "price_rule": "as_published",
                 "basis": "per_outcome", "outcome": "treated", "incremental": True, "includes": ["treatment"]}]},
        "objective": {"outcome": "dalys", "sense": "avert", "horizon": [2024.0, horizon_stop],
                      "delivery": [2024.0, 2027.0], "discounting": "none"},
        "constraints": {"envelope_usd": 1000000, "fixed_costs_usd": {"d_floor": 100000.0},
                        "eligibility": {"c": {"column": "elig"}}},
        "comparator": {"package": "none", "text": "no programme in the window"},
        "calibration": {"method": "emulator_inversion", "attribute": "m0", "scale": "log", "range": [-1.0, 2.0],
                        "target_column": "target", "window": [2021.0, 2022.0], "metric": "prev",
                        "target_uncertainty": {"region_sd_logit": 0.15, "note": "toy common-mode error"},
                        "target_frame": {"band": "prev", "source": "toy survey frame"}},
        "uncertainty": {"level": 0.95, "draws": 8, "seed": 1,
                        "sources": [{"kind": "priors"}, {"kind": "costs", "items": {"c_all": [0.8, 1.2]}}],
                        "excluded": []},
        "analysis": {"evaluation": "emulator", "emulator": {"points": 16, "holdout_share": 0.1, "seed": 7},
                     "decision_tests": [dts[t] for t in tests]},
        "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation.csv"},
    }


UNITS_CSV = ("unit_id,region,pop,elig,elig_pop,target,s1,s2,years\n"
             "u1,R1,100,1,20,0.10,0.5,1.5,2017 2020\nu2,R1,200,1,40,0.20,0.5,1.5,2017 2020\n"
             "u3,R1,300,0,0,0.30,0.5,1.5,2017 2020\nu4,R2,400,1,80,0.40,1.0,1.0,2018\n"
             "u5,R2,500,0,0,0.50,1.0,1.0,2018\nu6,R2,600,1,120,0.60,1.0,1.0,2018\n")


def _toy_dossier(d: str, repo: str, hostile: str = "") -> str:
    """A synthetic run dir with every dossier artifact, and a toy checkout (`repo`) with the engine's results file."""
    from types import SimpleNamespace as NS  # noqa: F401
    RC._toy_run(d, state="ANALYZE", paused=None)
    h = hostile
    _toy_w(d, "data/inputs/toy/units_raw.csv", "id,region,pop,notes\nu1,R1,100,x\n")
    _toy_w(d, "data/inputs/toy/changed.csv", "now,different\n")
    _toy_w(d, "data/inputs/toy/params_registry.yaml", {"parameters": [
        {"name": "cost_a", "value": 2.0, "ci_low": 1.8, "ci_high": 2.2, "kind": "cost_usd", "source": "T1",
         "subgroup": "toy list price" + h},
        {"name": "use_a", "value": 0.5, "kind": "share", "source": "T2"}]})
    _toy_w(d, "data/inputs/toy/citations_t.txt", "# toy citations\n\n## [T1] Toy price list" + h + "\n- **Value**: 2.0 per "
       "item\n- **Source**: Toy 2020\n- **DOI/PMCID**: 10.0000/toy\n\n## [T2] Toy use survey\n- **Value**: 50%\n"
       "- **Source**: Toy survey 2021\n")
    _toy_w(d, "data/derive/units.py", "# toy derivation script\n")
    units = _toy_w(d, "data/derived/units.csv", UNITS_CSV)
    raw = os.path.join(d, "data/inputs/toy/units_raw.csv")
    _toy_w(d, "data/inputs_provenance.json", {"inputs": {
        "data/inputs/toy/units_raw.csv": {"source": "toy/units_raw.csv", "sha256": _sha_file(raw), "bytes": 40},
        "data/inputs/toy/params_registry.yaml": {"source": "toy/params_registry.yaml",
                                                 "sha256": _sha_file(os.path.join(d, "data/inputs/toy/params_registry.yaml")),
                                                 "bytes": 200},
        "data/inputs/toy/changed.csv": {"source": "toy/changed.csv", "sha256": "0" * 64, "bytes": 12},
        "data/inputs/toy/old.csv": {"source": "toy/old.csv", "sha256": "1" * 64, "bytes": 9}},
        "cited": {"data/derived/units.csv": {"sha256": _sha_file(units)}}})
    _toy_w(d, "data/derivations.yaml", {"derivations": [{
        "id": "units", "script": "data/derive/units.py", "inputs": ["data/inputs/toy/units_raw.csv"],
        "outputs": ["data/derived/units.csv"],
        "columns": {"uses": {"data/inputs/toy/units_raw.csv": ["id", "region", "pop"]},
                    "ignores": {"data/inputs/toy/units_raw.csv": {"notes": "not used: removing it leaves the units "
                                                                           "table unchanged" + h}}}}],
        "ignored_inputs": {"data/inputs/toy/changed.csv": "not read by the units derivation"}})
    rec = _toy_w(d, "data/derivations_record.json", {"producer": "scripts/pipeline.py derive", "version": 1, "derivations": {
        "units": {"id": "units", "ok": True, "runs": 3, "runtime_s": 0.5, "python": "3.14.3",
                  "sandbox": {"kind": "bwrap", "limits": {"wall_s": 900}}, "libraries": {"pandas": "3.0.2"},
                  "script": {"path": "data/derive/units.py", "sha256": _sha_file(os.path.join(d, "data/derive/units.py"))},
                  "inputs": {"data/inputs/toy/units_raw.csv": _sha_file(raw)},
                  "outputs": {"data/derived/units.csv": _sha_file(units)}, "reason": ""}}})
    _toy_w(d, "data/input_checks.json", {"producer": "toolkit/spec/geometry.py", "version": 2, "status": "ran",
                                     "reason": None, "counts": {"units": 6, "points_checked": 6, "reciprocal_swaps": 1},
                                     "checks": {"point": "ran"}, "findings": [
                                         {"id": "reciprocal_swap:u1+u2", "code": "reciprocal_swap", "severity": "HIGH",
                                          "units": ["u1", "u2"], "claim": "units u1 and u2 look exchanged" + h,
                                          "accepted": None}], "acceptance_problems": []})
    _toy_w(d, "evidence/registry.yaml", {"status": "checked", "entries": [
        {"id": "p_half", "value": 120, "units": "days", "source": "Toy trial" + h, "citation": "T1",
         "used_in": ["engine.p_half"], "status": "matched"}]})
    # sign-off history: two archived sign-offs and the current one
    specs = [_toy_spec(2027.0, ("DT1",)), _toy_spec(2028.0, ("DT1",)), _toy_spec(2028.0, ("DT1", "DT2"), h)]
    s1 = _toy_w(d, "evidence_planner/superseded_signoff_2026-01-01/decision_spec.signed_aa.yaml", specs[0])
    s2 = _toy_w(d, "evidence_planner/superseded_signoff_2026-01-02/decision_brief.signed/decision_spec.yaml", specs[1])
    cur = _toy_w(d, "decision_spec.yaml", specs[2])
    for folder, p, at, note in (("superseded_signoff_2026-01-01", s1, "2026-01-01T10:00:00+00:00", "first sign-off"),
                                ("superseded_signoff_2026-01-02", s2, "2026-01-02T10:00:00+00:00", "window extended")):
        _toy_w(d, f"evidence_planner/{folder}/decision_spec.signoff.json", {
            "signed_by": "owner", "signed_at": at, "note": note,
            "sha256": {"decision_spec.yaml": _sha_file(p), "data/derivations_record.json": _sha_file(rec)},
            "producer": "toolkit/spec/signoff.py", "schema_version": 1})
    _toy_w(d, "decision_spec.signoff.json", {"signed_by": "owner", "signed_at": "2026-01-03T10:00:00+00:00",
                                         "note": "test DT2 added" + h,
                                         "sha256": {"decision_spec.yaml": _sha_file(cur),
                                                    "data/derivations_record.json": _sha_file(rec)},
                                         "producer": "toolkit/spec/signoff.py", "schema_version": 1})
    # engine: choice, the harness's card snapshot, a toy checkout's results file re-stamped by proof
    res = _toy_w(repo, "toolkit/engines/toy/benchmarks/results.yaml", {
        "producer": "toolkit/engines/benchmark.py",
        "engine_identity": {"name": "toy", "version": "0.2", "code_sha256": "c0de" * 16},
        "benchmarks": [{"id": "B1"}, {"id": "B2"}],
        "restamped": {"by": "toolkit/engines/toy/benchmarks/restamp_proof.py (byte-identity proof, not recomputed)",
                      "proof_identical": True, "old_engine_version": "0.1",
                      "confirmation": {"validate_once": "pending", "evidence": None}}})
    _toy_w(d, "engine_choice.json", {"at": "2026-01-03T11:00:00+00:00", "engine": {"ref": "toolkit.engines.toy:Toy",
                                                                               "config": {}},
                                 "why": "the only eligible engine" + h, "candidates": [
        {"ref": "toolkit.engines.contract:ToyEngine", "name": "closed", "version": "1", "eligible": False,
         "why_not": ["no component c", "no window metric prev"], "mechanisms": ["closed-form burden"], "benchmarks": 0},
        {"ref": "toolkit.engines.toy:Toy", "name": "toy", "version": "0.2", "eligible": True, "why_not": [],
         "mechanisms": ["toy transmission", "toy interventions"], "benchmarks": 2}]})
    eng = {"name": "toy", "version": "0.2", "card_sha256": "ca7d" * 16}
    _toy_w(d, "analysis/engine.yaml", {
        "schema": "analysis.engine/1", "produced_by": "harness", "producer": "toolkit/analysis/run.py",
        "spec_sha256": _sha_file(cur), "spec_signed": True, "engine": eng, "engine_choice_sha256": "c401" * 16,
        "inputs": {"decision_spec.yaml": _sha_file(cur), "../../toolkit/engines/toy/benchmarks/results.yaml": _sha_file(res)},
        "data": {"card": {
            "name": "toy", "version": "0.2", "mechanisms": ["toy transmission" + h, "toy interventions"],
            "components": {"g": ["none", "a", "b"], "c": [0, 1], "d": [0, 1]},
            "unit_attributes": {"m0": "density per person, [0.1, 10]", "season": "12 monthly values",
                                "use": "use after a campaign [0, 1]", "month": "optional month"},
            "valid_ranges": {"m0": [0.1, 10.0]},
            "outputs": {"per_capita": ["dalys", "treated"], "windows": ["prev"], "cost_drivers": ["given"]},
            "age_groups": ["u5", "5p"],
            "fitted": {"k": {"value": 0.4, "lo": 0.3, "hi": 0.5, "box": [0.0, 1.0], "benchmarks": ["B1"]}},
            "benchmarks": [
                {"id": "B1", "metric": "toy ratio at 12 m", "role": "fit", "target": 0.8, "band": [0.7, 0.9],
                 "computed": 0.81, "in_band": True, "status": "in_band", "source": "T1"},
                {"id": "B2", "metric": "toy incidence ratio" + h, "role": "held_out", "target": 0.75, "band": [0.6, 0.9],
                 "computed": 0.5, "in_band": False, "status": "out_of_band", "source": "T2"}],
            "notes": ["toy note: an unverified assumption"]},
            "parameters": [
                {"name": "k", "value": 0.4, "kind": "fitted", "units": "probability", "lo": 0.3, "hi": 0.5,
                 "source": "fitted once to B1", "verified": "unverified"},
                {"name": "p_half", "value": 120.0, "kind": "fixed", "units": "days", "lo": 90.0, "hi": 150.0,
                 "source": "Toy trial" + h, "verified": "matched"}]}})
    _toy_w(d, "analysis/batch/points.jsonl.summary.json", {"n": 4, "computed": 4, "code_fp": "fp0123456789"})
    # calibration and emulator
    units_cal = {f"u{i}": {"z": 0.1 * i, "clamped": i == 6, "slope": 1.0 / i} for i in range(1, 7)}
    _toy_w(d, "analysis/calibration.yaml", {"schema": "analysis.calibration/1", "producer": "toolkit/analysis/calibration.py",
                                        "engine": eng, "data": {
        "method": "emulator_inversion", "units": units_cal,
        "diagnostics": {"units": 6, "clamped_units": 1, "grid_points": 10, "range": [-1.0, 2.0],
                        "target_logit_residual_max": 0.0001, "slope_median": 0.3, "slope_min": 1.0 / 6},
        "identifiability": {"design": "one attribute per unit, one target per unit (exactly determined)",
                            "flat_slope_threshold": 0.05, "units_unidentified": [], "status": "well_identified"}}})
    _toy_w(d, "analysis/emulator.yaml", {"schema": "analysis.emulator/1", "producer": "toolkit/analysis/model.py",
                                     "engine": eng, "data": {
        "accuracy": {"holdout_points": 3, "training_points": 13, "cal_logit_rmse": 0.12, "reduction_r2_median": 0.9,
                     "reduction_r2_p10": 0.8, "reduction_mae_median": 0.02, "base_log_rmse": 0.05,
                     "error_model": {"model": "toy common plus per-unit", "rmse_median": 0.03}},
        "features": ["s1", "pop"], "fit_seconds": 1.5}})
    return d


def _self_test() -> int:
    import builtins
    import re
    import shutil
    import subprocess
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def case(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok(False, f"{name}: {type(e).__name__}: {str(e)[:300]}")

    td = tempfile.mkdtemp(prefix="dossier_st_")
    try:
        repo = os.path.join(td, "repo")
        run = _toy_dossier(os.path.join(td, "runs", "r1"), repo)

        # DS1 each tab's data model on a toy run with every artifact
        def ds1():
            sp = spec_tab(run)
            u = sp["units"]
            ok(u.get("count") == 6 and u.get("regions") == 2 and u.get("id") == "unit_id" and u.get("region") == "region",
               f"DS1a units: 6 units in 2 regions from the units table the spec names ({u})")
            b = {r["attribute"]: r for r in sp["bindings"]}
            ok(b["season"]["how"] == "columns" and b["use"]["how"] == "value" and "params_registry" in b["use"]["source"]
               and b["years"]["split"] == " " and sp["defaults"][0]["name"] == "month",
               f"DS1b bindings (column, columns, value with source, split) and defaults accepted ({list(b)})")
            ci = {r["id"]: r for r in sp["cost_items"]}
            ok(abs(ci["a_commodity"]["base_value"] - 2.5) < 1e-9 and abs(ci["a_commodity"]["per_person"] - 1.25) < 1e-9
               and abs(ci["c_all"]["per_person"] - 9.0) < 1e-9 and ci["d_case"]["per_person"] is None
               and ci["a_commodity"]["includes"] == ["commodity", "delivery"],
               f"DS1c cost items: base-year value 2.5 and 1.25 per person (index rule, 2 people per item), 9.0 over "
               f"3 years per eligible, none for a per-outcome item ({ci.get('a_commodity')})")
            dt = {t["id"]: t for t in sp["decision_tests"]}
            ok(dt["DT1"]["unit"] == "days" and dt["DT1"]["values"] == [100.0] and "p_half" in dt["DT1"]["varied"]
               and dt["DT2"]["unit"] == "×" and "c" in dt["DT2"]["varied"],
               f"DS1d decision tests: units from the card's parameter table, × for an interaction ({dt})")
            ok(sp["objective"]["window"] == [2024.0, 2028.0] and sp["constraints"]["envelope_usd"] == 1000000
               and sp["constraints"]["fixed"][0]["name"] == "d_floor" and sp["comparator"]["package"] == "none"
               and sp["calibration"]["method"] == "emulator_inversion" and sp["uncertainty"]["draws"] == 8,
               "DS1e objective, constraints, comparator, calibration and uncertainty")
            so = sp["signoffs"]
            ok([s["signed_at"][:10] for s in so] == ["2026-01-01", "2026-01-02", "2026-01-03"] and so[-1]["current"]
               and so[-1]["matches"] is True and so[0]["spec_sha256"] and so[0]["note"] == "first sign-off",
               f"DS1f sign-off history in time order, the current one matching the spec ({[s.get('signed_at') for s in so]})")
            d0, d1 = so[0]["diff"], so[1]["diff"]
            f0 = {r["field"]: r for r in d0.get("rows") or []}
            f1 = {r["field"]: r for r in d1.get("rows") or []}
            ok("objective.horizon" in f0 and f0["objective.horizon"]["section"] == "objective"
               and any(k.startswith("analysis.decision_tests.DT2") and f1[k]["before"] is None for k in f1)
               and not any(k.startswith("analysis.decision_tests.DT1") for k in f1) and "diff" not in so[-1],
               f"DS1g each archived sign-off is diffed against the next: a changed field, a test added by id "
               f"({list(f0)[:4]}; {list(f1)[:4]})")
            ev = evidence_tab(run)
            reg = ev["registry"]
            ok(reg["entries"][0]["id"] == "p_half" and reg["entries"][0]["status"] == "matched" and not reg["stub"],
               f"DS1h the evidence registry's entries ({reg})")
            pr = {e["name"]: e for e in ev["param_registries"][0]["entries"]}
            ok(pr["cost_a"]["ci"] == [1.8, 2.2] and pr["cost_a"]["citation_title"].startswith("Toy price list")
               and any("engine_attributes.use" in f for f in pr["use_a"]["used_in"]),
               f"DS1i a staged parameter registry: interval, citation title, where the spec uses it ({pr})")
            cit = {c["id"]: c for c in ev["citations"]["entries"]}
            ok(cit["T1"]["fields"].get("Source") == "Toy 2020" and "cost_a" in cit["T1"]["cited_by"],
               f"DS1j citations parsed, with the entries that cite them ({cit.get('T1')})")
            st = {r["path"]: r for r in ev["inputs"]["rows"]}
            ok(st["data/inputs/toy/units_raw.csv"]["state"] == "match" and st["data/inputs/toy/changed.csv"]["state"]
               == "changed" and st["data/inputs/toy/old.csv"]["state"] == "missing"
               and st["data/inputs/toy/units_raw.csv"]["read_by"] == ["units"]
               and "not read" in st["data/inputs/toy/changed.csv"]["ignored"],
               f"DS1k staged inputs: match, changed, missing; read by a derivation or ignored with a reason ({st})")
            dv = ev["derivations"]["rows"][0]
            inp = dv["inputs"][0]
            ok(inp["uses"] == ["id", "region", "pop"] and inp["ignores"][0]["column"] == "notes"
               and "unchanged" in inp["ignores"][0]["reason"] and dv["outputs"][0]["columns"][:2] == ["unit_id", "region"]
               and dv["outputs"][0]["state"] == "match" and dv["record"]["ok"] is True and dv["script"] == "data/derive/units.py",
               f"DS1l derivation: input → script → output columns, used and ignored columns with reasons ({dv})")
            ic = ev["input_checks"]
            ok(ic["status"] == "ran" and ic["findings"][0]["severity"] == "HIGH" and ic["counts"]["reciprocal_swaps"] == 1,
               f"DS1m input checks summarised ({ic})")
            en = engine_tab(run, repo)
            ch = en["choice"]
            ok(ch["chosen"] == "toolkit.engines.toy:Toy" and [c["eligible"] for c in ch["candidates"]] == [False, True]
               and ch["candidates"][0]["why_not"] == ["no component c", "no window metric prev"],
               f"DS1n engine choice: candidates, why each was ineligible, the chosen engine ({ch})")
            at = {a["name"]: a for a in en["card"]["attributes"]}
            ok(at["m0"]["range"] == [0.1, 10.0] and at["season"]["binding"].startswith("columns")
               and at["month"]["binding"].startswith("default accepted"),
               f"DS1o card attributes with valid ranges and the spec's bindings ({at})")
            pa = {p["name"]: p for p in en["parameters"]["rows"]}
            ok(pa["k"]["kind"] == "fitted" and pa["k"]["box"] == [0.0, 1.0] and pa["k"]["fitted_to"] == ["B1"]
               and pa["p_half"]["units"] == "days" and pa["p_half"]["lo"] == 90.0,
               f"DS1p parameters: kind, prior range, fit box and benchmarks ({pa})")
            bm = en["benchmarks"]
            b2 = next(r for r in bm["rows"] if r["id"] == "B2")
            ok(bm["out_of_band"] == 1 and b2["flag"] and b2["role"] == "held_out" and b2["computed"] == 0.5,
               f"DS1q benchmarks: the out-of-band one flagged ({bm})")
            rs = en["restamp"]
            ok(rs.get("state") == "proof" and rs.get("validate_once") == "pending" and rs.get("old_version") == "0.1",
               f"DS1r re-stamp by proof, validate_once pending, from the checkout file the run recorded ({rs})")
            idn = en["identity"]
            ok(idn["version"] == "0.2" and idn["card_sha256"] == "ca7d" * 16 and "fp0123456789" in idn["code_fp"]
               and idn.get("code_sha256") == "c0de" * 16,
               f"DS1s engine identity: version, card sha256, code fingerprint ({idn})")
            ca = calibration_tab(run)
            rg = {r["region"]: r for r in ca["regions"]}
            ok(rg["R1"]["n"] == 3 and abs(rg["R1"]["target_mean"] - 0.2) < 1e-9 and rg["R2"]["clamped"] == 1
               and len(ca["units"]) == 6 and ca["diagnostics"]["clamped_units"] == 1
               and ca["identifiability"]["status"] == "well_identified" and ca["emulator"]["holdout_points"] == 3
               and "not stored" in ca["model_per_unit"],
               f"DS1t calibration: per-region rows, clamped units, identifiability, emulator held-out accuracy ({rg})")
        case("DS1", ds1)

        # DS2 fail closed: missing, unparsable, an archived spec not found, a checkout file with another hash
        def ds2():
            e = os.path.join(td, "runs", "empty")
            RC._toy_run(e, state="SPEC", paused=None)
            dd = dossier(e, repo)
            sp, ev, en, ca = dd["spec"], dd["evidence"], dd["engine"], dd["calibration"]
            parts = [sp, ev["registry"], ev["inputs"], ev["derivations"], ev["input_checks"], ev["citations"],
                     en["choice"], en["card"], en["parameters"], en["benchmarks"], en["restamp"], ca]
            ok(all(p.get("available") is False and "not written" in p.get("why", "") for p in parts),
               f"DS2a every part of a run without the artifacts is not available, with a reason "
               f"({[p.get('why') for p in parts]})")
            b = os.path.join(td, "runs", "bad")
            _toy_dossier(b, repo)
            _toy_w(b, "evidence/registry.yaml", "entries: [unclosed\n")
            _toy_w(b, "analysis/engine.yaml", "data: {card: [\n")
            os.remove(os.path.join(b, "evidence_planner/superseded_signoff_2026-01-01/decision_spec.signed_aa.yaml"))
            reg, en2 = evidence_tab(b)["registry"], engine_tab(b, repo)
            so = spec_tab(b)["signoffs"]
            ok(reg["available"] is False and "Error" in reg["why"] and en2["card"]["available"] is False
               and "Error" in en2["card"]["why"] and so[0]["diff"]["available"] is False
               and "no spec" in so[0]["diff"]["why"],
               f"DS2b an unparsable file and an archived spec not found by hash each give their reason "
               f"({reg.get('why')}; {so[0]['diff']})")
            c = os.path.join(td, "runs", "other_checkout")
            repo2 = os.path.join(td, "repo2")
            _toy_dossier(c, repo2)
            _toy_w(repo2, "toolkit/engines/toy/benchmarks/results.yaml", "producer: x\nbenchmarks: []\n")
            rs = engine_tab(c, repo2)["restamp"]
            ok(rs["available"] is False and "differs" in rs["why"],
               f"DS2c a checkout results file whose sha256 differs from the run's shows no re-stamp status ({rs})")
        case("DS2", ds2)

        # DS3 no reads outside the run dir
        def ds3():
            out = os.path.join(td, "outside")
            os.makedirs(out, exist_ok=True)
            secret = "SENTINEL-" + "7f3a9c"
            _toy_w(out, "units.csv", "unit_id,region\n" + secret + ",R\n")
            _toy_w(out, "signoff/decision_spec.signoff.json", {"signed_by": secret, "signed_at": "2026-01-01T00:00:00Z",
                                                            "note": secret, "sha256": {}})
            h = os.path.join(td, "runs", "hostile")
            _toy_dossier(h, repo)
            spec = yaml.safe_load(open(os.path.join(h, "decision_spec.yaml")))
            spec["decision_units"]["csv"] = "../../outside/units.csv"
            _toy_w(h, "decision_spec.yaml", spec)
            der = yaml.safe_load(open(os.path.join(h, "data/derivations.yaml")))
            der["derivations"][0]["inputs"] = ["/etc/hosts", "data/inputs/toy/link.csv"]
            der["derivations"][0]["outputs"] = ["../outside/units.csv"]
            _toy_w(h, "data/derivations.yaml", der)
            os.symlink(os.path.join(out, "units.csv"), os.path.join(h, "data/inputs/toy/link.csv"))
            prov = json.load(open(os.path.join(h, "data/inputs_provenance.json")))
            prov["inputs"]["../../outside/units.csv"] = {"source": "x", "sha256": "2" * 64}
            prov["inputs"]["data/inputs/toy/link.csv"] = {"source": "x", "sha256": "3" * 64}
            _toy_w(h, "data/inputs_provenance.json", prov)
            os.symlink(os.path.join(out, "signoff"), os.path.join(h, "evidence_planner", "superseded_signoff_link"))
            from lib import safe_io
            seen, real_open, real_nf, real_dfd = [], builtins.open, safe_io.open_nofollow, safe_io.dir_fd_nofollow
            real_os_open, real_scandir, real_listdir = os.open, os.scandir, os.listdir

            def t_open(p, *a, **k):
                if not isinstance(p, int):
                    seen.append(("open", os.path.realpath(p)))
                return real_open(p, *a, **k)

            def t_nf(base, rel):
                seen.append(("nofollow", os.path.realpath(base), rel))
                return real_nf(base, rel)

            def t_dfd(base, parts, create=False):
                seen.append(("dirfd", os.path.realpath(base), "/".join(parts)))
                return real_dfd(base, parts, create)

            def t_os_open(p, flags, *a, **k):
                if k.get("dir_fd") is None and not isinstance(p, int):
                    seen.append(("os.open", os.path.realpath(p)))
                return real_os_open(p, flags, *a, **k)

            def t_scan(p="."):
                if not isinstance(p, int):
                    seen.append(("scandir", os.path.realpath(p)))
                return real_scandir(p)

            def t_list(p="."):
                if not isinstance(p, int):
                    seen.append(("listdir", os.path.realpath(p)))
                return real_listdir(p)
            builtins.open, safe_io.open_nofollow, safe_io.dir_fd_nofollow = t_open, t_nf, t_dfd
            os.open, os.scandir, os.listdir = t_os_open, t_scan, t_list
            RC._CACHE.clear()
            try:
                dd = dossier(h, repo)
            finally:
                builtins.open, safe_io.open_nofollow, safe_io.dir_fd_nofollow = real_open, real_nf, real_dfd
                os.open, os.scandir, os.listdir = real_os_open, real_scandir, real_listdir
            blob = json.dumps(dd, default=str)
            ok(secret not in blob, "DS3a the sentinel outside the run dir never appears in the dossier")
            u = dd["spec"]["units"]
            ok(u.get("count") is None and "relative path" in (u.get("count_why") or ""),
               f"DS3b a units table named outside the run dir is refused with a reason ({u})")
            ins = {i["path"]: i for i in dd["evidence"]["derivations"]["rows"][0]["inputs"]}
            st = {r["path"]: r for r in dd["evidence"]["inputs"]["rows"]}
            ok(ins["/etc/hosts"]["state"] == "refused" and ins["data/inputs/toy/link.csv"]["state"] == "refused"
               and st["../../outside/units.csv"]["state"] == "refused" and st["data/inputs/toy/link.csv"]["state"] == "refused",
               f"DS3c derivation inputs and provenance paths outside the run dir, or links, are refused ({ins}; "
               f"{ {k: v['state'] for k, v in st.items()} })")
            root, rrepo = os.path.realpath(h), os.path.realpath(repo)
            allowed_repo = {("nofollow", rrepo, "toolkit/engines/toy/benchmarks/results.yaml")}

            def inside(p):
                return p == root or p.startswith(root + os.sep)
            stray = [s for s in seen if not inside(s[1]) and s not in allowed_repo
                     and not (s[0] == "dirfd" and s[1] == rrepo)
                     and not (s[0] == "os.open" and s[1] == rrepo)]
            ok(seen and not stray, f"DS3d every file the dossier opens or lists is under the run dir, or the checkout's "
                                   f"results file the run recorded ({len(seen)} traced; outside: {stray[:5]})")
        case("DS3", ds3)

        # DS4 escaping: the attribute lint covers dossier.js; hostile strings render escaped
        def ds4():
            src = open(JS, encoding="utf-8").read()
            bad = [(a, e) for a, e in RC._attr_interpolations(src) if not RC._SAFE_ATTR.match(e)
                   and not RC._LIT_TERNARY.match(e)]
            ok(not bad and "dossier.js" in RC._page_sources() + "".join(open(RC.HTML).read()),
               f"DS4a every ${{...}} in an HTML attribute of dossier.js is esc(), ref(), q() or a ternary of literals; "
               f"the page sources include it ({bad[:6]})")
            node = shutil.which("node")
            if not node:
                ok(True, "DS4b skipped: node is not installed")
                return
            hostile = '<img src=x onerror=alert(1)>" onmouseover="alert(2)'
            hr = _toy_dossier(os.path.join(td, "runs", "xss"), os.path.join(td, "repo_x"), hostile)
            dd = dossier(hr, os.path.join(td, "repo_x"))
            stub = (
                "const _h = { get: (t, k) => k === Symbol.iterator ? function* () {} : k === Symbol.toPrimitive ? "
                "() => '' : k === 'forEach' ? () => {} : k === 'search' ? '' : k === 'content' ? 'tok' : P, "
                "set: () => true, apply: () => P };\nconst P = new Proxy(function () {}, _h);\n"
                "globalThis.document = P; globalThis.window = P; globalThis.location = { search: '' };\n"
                "globalThis.matchMedia = () => ({ matches: false }); globalThis.setInterval = () => 0;\n"
                "globalThis.fetch = () => new Promise(() => {});\n")
            js = open(os.path.join(HERE, "run_console.js"), encoding="utf-8").read() + "\n" + src
            test = (f"\nS.meta = {{ glossary: {json.dumps({k: {'text': v[0], 'source': v[1]} for k, v in RC.GLOSSARY.items()})} }};\n"
                    f"const D = {json.dumps(dd, default=str)};\n"
                    "const out = {}; for (const t of ['spec', 'evidence', 'engine', 'calibration']) out[t] = dossierHTML(t, D[t]);\n"
                    "process.stdout.write(JSON.stringify(out));\n")
            p = subprocess.run([node, "-e", stub + js + test], capture_output=True, text=True, timeout=60)
            try:
                html = json.loads(p.stdout)
            except ValueError:
                ok(False, f"DS4b node rendered the tabs ({p.stderr[-400:]})")
                return
            leaks = {t: [m for m in ("<img src=x", '" onmouseover="') if m in h] for t, h in html.items()}
            esc_seen = {t: "&lt;img src=x" in h for t, h in html.items()}
            ok(not any(leaks.values()) and all(esc_seen.values()),
               f"DS4b hostile strings in the spec, registry, card and sign-off note reach each tab's HTML only "
               f"escaped (raw: {leaks}; escaped present: {esc_seen})")
        case("DS4", ds4)

        # DS5 the endpoint, the script route and the glossary
        def ds5():
            con = RC.Console([os.path.join(td, "runs")], log=lambda *a: None)
            code, ctype, body = con.handle("GET", "/api/dossier", {"run": [run]}, {})
            ok(code == 200 and ctype == "json" and set(body) >= {"spec", "evidence", "engine", "calibration"},
               f"DS5a /api/dossier returns the four sections ({code}, {sorted(body) if isinstance(body, dict) else body})")
            try:
                con.handle("GET", "/api/dossier", {"run": [os.path.join(td, "outside")]}, {})
                r = "served"
            except ValueError as e:
                r = str(e)
            ok(r != "served", f"DS5b an unknown run is refused ({r})")
            code, ctype, js = con.handle("GET", "/dossier.js", {}, {})
            page = con.handle("GET", "/", {}, {})[2].decode()
            ok(code == 200 and ctype.startswith("text/javascript") and '<script src="/dossier.js"></script>' in page,
               "DS5c the page loads /dossier.js from the console server (script-src 'self')")
            src = open(JS, encoding="utf-8").read()
            used = set(re.findall(r'\bT\("([^"]+)"', src))
            missing = sorted(k for k in used if k not in RC.GLOSSARY)
            ok(used and not missing, f"DS5d every glossary term the dossier marks has a definition and a source "
                                     f"({len(used)} used; missing {missing})")
        case("DS5", ds5)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
