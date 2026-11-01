#!/usr/bin/env python3
"""Contract checks on the analysis engine's output (design §3): schema and provenance, not numbers.

    python3 toolkit/analysis/contract.py <run_dir>
    python3 toolkit/analysis/contract.py --self-test      # (the end-to-end cases are in run.py --self-test)

Every artifact the report renders is harness-produced, so the checks verify that it is what the
harness wrote, for the spec that was signed:
  analysis_contract_missing    the manifest, an artifact or a deliverable it lists is missing
  analysis_contract_tampered   a file's hash differs from the manifest's (edited after the harness wrote it)
  analysis_contract_schema     an envelope or payload field is missing, or produced_by is not the harness
  analysis_contract_stale      the artifacts were computed for another spec than the current one
  analysis_contract_unsigned   the spec the artifacts were computed for was not signed
  analysis_contract_decision_test  a decision-test result file fails the Phase 49 loader, or is not harness-produced
  analysis_contract_claims     a ledger claim is not harness-produced or cites a file the manifest does not list
  analysis_contract_figure     a figure's sources changed after it was drawn
All HIGH, none scope-declarable; each fails closed (an unreadable file is a finding).
"""
from __future__ import annotations

import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis.artifacts import (ENVELOPE, EXPECTED_KINDS, EXPECTED_ROLES, KINDS, MANIFEST,  # noqa: E402
                                        PRODUCED_BY, PRODUCER_PREFIX, sha256)

SPEC = "decision_spec.yaml"
ENGINE_CHOICE = "engine_choice.json"     # toolkit/spec/schema.py ENGINE_CHOICE


def _f(kind, claim, key=""):
    return {"kind": kind, "severity": "HIGH", "stage": "ANALYZE", "claim": claim, "key": key,
            "scope_declarable": False, "source": "toolkit/analysis/contract.py"}


def gate_findings(run_dir: str) -> list[dict]:
    mp = os.path.join(run_dir, MANIFEST)
    if not os.path.exists(mp):
        return [] if not os.path.exists(os.path.join(run_dir, SPEC)) else [
            _f("analysis_contract_missing", f"{SPEC} exists but {MANIFEST} does not: the analysis engine has not run "
                                            "(or its output was removed)")]
    try:
        man = json.load(open(mp))
    except (OSError, ValueError) as e:
        return [_f("analysis_contract_missing", f"{MANIFEST} unreadable: {e}")]
    out = []
    cal_doc, unc_doc = None, None
    # Codex P1-9: only a manifest the harness published at the end of a complete run certifies anything.
    if man.get("status") == "blocked":
        return [_f("analysis_contract_blocked", f"the analysis stopped before publishing a plan: {man.get('reason')} "
                                                "(analysis/blocked.yaml)")]
    if man.get("status") != "complete" or man.get("complete") is not True:
        return [_f("analysis_contract_incomplete", f"{MANIFEST} does not record a completed run (status "
                                                   f"{man.get('status')!r})")]
    exp = man.get("expected") or {}
    kinds = {e.get("kind") for e in (man.get("artifacts") or {}).values()}
    roles = {e.get("role") for e in (man.get("deliverables") or {}).values()}
    need_k = set(EXPECTED_KINDS) | set(exp.get("kinds") or [])
    for k in sorted(need_k - kinds):
        out.append(_f("analysis_contract_incomplete", f"the completed run has no {k} artifact", k))
    for r in sorted(set(EXPECTED_ROLES) - roles):
        out.append(_f("analysis_contract_incomplete", f"the completed run has no {r} deliverable", r))
    listed = set(man.get("deliverables") or {})
    for tid, rel in sorted((exp.get("decision_tests") or {}).items()):
        if rel and rel not in listed:
            out.append(_f("analysis_contract_incomplete", f"decision test {tid}: result file {rel} is not a deliverable",
                          tid))
    cur = sha256(os.path.join(run_dir, SPEC))
    if man.get("spec_sha256") != cur:
        out.append(_f("analysis_contract_stale", f"the artifacts were computed for spec {str(man.get('spec_sha256'))[:12]}, "
                                                 f"the run's {SPEC} is {str(cur)[:12]}: re-run the analysis"))
    cur_set = sha256(os.path.join(run_dir, "analysis_settings.yaml"))
    if man.get("settings_sha256") != cur_set:
        out.append(_f("analysis_contract_stale", "analysis_settings.yaml changed (or appeared or vanished) after the "
                                                 "analysis ran: re-run the analysis"))
    cur_ch = sha256(os.path.join(run_dir, ENGINE_CHOICE))
    if man.get("engine_choice_sha256") != cur_ch:
        out.append(_f("analysis_contract_stale", f"{ENGINE_CHOICE} changed (or appeared or vanished) after the analysis "
                                                 "ran: re-run the analysis"))
    if not man.get("spec_signed"):
        out.append(_f("analysis_contract_unsigned", "the analysis ran on an unsigned spec (--unsigned: test runs only)"))
    for rel, ent in sorted((man.get("artifacts") or {}).items()):
        p = os.path.join(run_dir, rel)
        if not os.path.exists(p):
            out.append(_f("analysis_contract_missing", f"artifact {rel} is missing", rel))
            continue
        if sha256(p) != ent.get("sha256"):
            out.append(_f("analysis_contract_tampered", f"artifact {rel} changed after the harness wrote it", rel))
            continue
        try:
            doc = yaml.safe_load(open(p))
        except (OSError, yaml.YAMLError) as e:
            out.append(_f("analysis_contract_schema", f"artifact {rel} unreadable: {e}", rel))
            continue
        kind = ent.get("kind")
        miss = [k for k in ENVELOPE if k not in (doc or {})]
        if miss or kind not in KINDS:
            out.append(_f("analysis_contract_schema", f"artifact {rel}: envelope lacks {miss} or kind {kind!r} unknown", rel))
            continue
        if doc["produced_by"] != PRODUCED_BY or not str(doc["producer"]).startswith(PRODUCER_PREFIX):
            out.append(_f("analysis_contract_schema", f"artifact {rel}: produced_by {doc['produced_by']!r}, producer "
                                                      f"{doc['producer']!r} (must be the harness)", rel))
        if doc["schema"] != f"analysis.{kind}/1":
            out.append(_f("analysis_contract_schema", f"artifact {rel}: schema {doc['schema']!r}", rel))
        pm = [k for k in KINDS[kind] if k not in (doc.get("data") or {})]
        if pm:
            out.append(_f("analysis_contract_schema", f"artifact {rel}: payload lacks {pm}", rel))
        if doc["spec_sha256"] != man.get("spec_sha256"):
            out.append(_f("analysis_contract_stale", f"artifact {rel} was computed for another spec", rel))
        if doc.get("engine_choice_sha256") != cur_ch:
            out.append(_f("analysis_contract_stale", f"artifact {rel} was computed for another engine choice", rel))
        for src, h in sorted((doc.get("inputs") or {}).items()):     # Codex P1-10: every recorded input still current
            cur_h = sha256(src if os.path.isabs(src) else os.path.join(run_dir, src))
            if cur_h != h:
                out.append(_f("analysis_contract_stale", f"artifact {rel}: input {src} changed (or vanished) after it "
                                                         "was computed", f"{rel}:{src}"))
        if kind == "calibration":
            out += _calibration(rel, doc.get("data") or {})
            cal_doc = (rel, doc.get("data") or {})
        if kind == "uncertainty":
            unc_doc = doc.get("data") or {}
        if kind == "point" and ((doc.get("data") or {}).get("model_corrections") or {}).get("on_plan"):
            out.append(_f("analysis_contract_model_domain", f"{rel}: model corrections bear on the plan", rel))
    for rel, ent in sorted((man.get("deliverables") or {}).items()):
        p = os.path.join(run_dir, rel)
        if not os.path.exists(p):
            out.append(_f("analysis_contract_missing", f"deliverable {rel} is missing", rel))
        elif sha256(p) != ent.get("sha256"):
            out.append(_f("analysis_contract_tampered", f"deliverable {rel} ({ent.get('role')}) changed after the "
                                                        "harness wrote it", rel))
    if cal_doc and cal_doc[1].get("method") == "posterior" and unc_doc is not None:
        out += _posterior_provenance(cal_doc[0], cal_doc[1], unc_doc)
    out += _decision_tests(run_dir, man)
    out += _claims(run_dir, man)
    out += _figures(run_dir, man)
    return out


def _calibration(rel, d) -> list[dict]:
    """Codex P1-8: a calibration that did not converge, hit a bound, left targets outside what the model reaches
    (clamped or unsupported), or left a parameter or unit unidentified does not certify a plan."""
    out = []
    m, dg, idf = d.get("method"), d.get("diagnostics") or {}, d.get("identifiability") or {}
    if m == "emulator_inversion":
        if dg.get("clamped_units"):
            out.append(_f("analysis_contract_calibration", f"{rel}: {dg['clamped_units']} units' targets lie outside "
                                                           "what the model reaches over the calibration range", rel))
        if idf.get("units_unidentified"):
            out.append(_f("analysis_contract_calibration", f"{rel}: {len(idf['units_unidentified'])} units' targets "
                                                           "hardly constrain their attribute (flat slope)", rel))
    elif m == "likelihood":
        if dg.get("converged") is not True:
            out.append(_f("analysis_contract_calibration", f"{rel}: the likelihood fit did not converge "
                                                           f"({dg.get('message')})", rel))
        if dg.get("at_bound"):
            out.append(_f("analysis_contract_calibration", f"{rel}: parameters at a bound: {dg['at_bound'][:5]}", rel))
        if dg.get("unsupported_targets"):
            out.append(_f("analysis_contract_calibration", f"{rel}: {dg['unsupported_targets']} targets the model "
                                                           "cannot produce (observed above zero, expected zero)", rel))
        bad = [q for q, v in (idf.get("parameters") or {}).items() if v.get("status") == "unidentified"]
        if bad:
            out.append(_f("analysis_contract_calibration", f"{rel}: unidentified parameters {bad}", rel))
    elif m == "posterior":
        # v2d §3.1-§3.3: the sampler met its floors (it fails closed otherwise), the canonical sample is recorded with
        # its id, and the point is a converged posterior mode
        if dg.get("converged") is not True:
            out.append(_f("analysis_contract_calibration", f"{rel}: the posterior mode did not converge "
                                                           f"({dg.get('message')})", rel))
        smp = d.get("sample") or {}
        if not (smp.get("sample_id") and smp.get("rows") and smp.get("names")):
            out.append(_f("analysis_contract_uncovered_parameter", f"{rel}: no canonical posterior sample recorded",
                          rel))
        else:
            import numpy as np
            from toolkit.analysis import calibration as C
            if C.check_provenance({"sample_id": smp["sample_id"], "names": smp["names"],
                                   "rows": np.asarray(smp["rows"], float)}, []):
                out.append(_f("analysis_contract_uncovered_parameter", f"{rel}: the canonical sample does not match "
                                                                       "its recorded id", rel))
        if not idf.get("classes"):
            out.append(_f("analysis_contract_calibration", f"{rel}: no identifiability classes", rel))
    elif m != "none":
        out.append(_f("analysis_contract_calibration", f"{rel}: unknown calibration method {m!r}", rel))
    return out


def _posterior_provenance(rel, cal: dict, unc: dict) -> list[dict]:
    """v2d §3.3 (Codex r1-4): when the interval covers a posterior calibration, every uncertainty draw is a row of the
    canonical sample: one record per draw, citing the sample's id, with the applied values of every estimated name
    equal to that row (re-read here, fail closed)."""
    if "calibration" not in (unc.get("covers") or []):
        return []
    import numpy as np
    from toolkit.analysis import calibration as C
    smp = cal.get("sample") or {}
    prov = unc.get("draw_provenance")
    kind = "analysis_contract_uncovered_parameter"
    if not isinstance(prov, dict) or not isinstance(prov.get("applied"), list):
        return [_f(kind, f"{rel}: the uncertainty draws record no provenance against the posterior sample", rel)]
    if len(prov["applied"]) != unc.get("draws") or prov.get("sample_id") != smp.get("sample_id"):
        return [_f(kind, f"{rel}: {len(prov['applied'])} provenance records for {unc.get('draws')} draws, or another "
                         "sample cited", rel)]
    try:
        found = C.check_provenance({"sample_id": smp["sample_id"], "names": smp["names"],
                                    "rows": np.asarray(smp["rows"], float)}, prov["applied"])
    except (KeyError, TypeError, ValueError) as e:
        return [_f(kind, f"{rel}: the canonical sample is unreadable ({e})", rel)]
    return [_f(kind, f"{rel}: {len(found)} draw values are not the canonical sample's rows (e.g. {found[0]})", rel)] \
        if found else []


def _decision_tests(run_dir, man) -> list[dict]:
    out = []
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import mechanism_manifest as MM
        loader = getattr(MM, "load_test_result", None)
    except Exception:  # noqa: BLE001
        loader = None
    for rel, ent in (man.get("deliverables") or {}).items():
        if ent.get("role") != "decision_test" or not os.path.exists(os.path.join(run_dir, rel)):
            continue
        try:
            doc = yaml.safe_load(open(os.path.join(run_dir, rel)))
        except (OSError, yaml.YAMLError) as e:
            out.append(_f("analysis_contract_decision_test", f"{rel} unreadable: {e}", rel))
            continue
        if not isinstance(doc, dict) or doc.get("produced_by") != "harness":
            out.append(_f("analysis_contract_decision_test", f"{rel}: produced_by is not harness", rel))
            continue
        if loader is not None:
            res, probs = loader(run_dir, rel)
            if probs:
                out.append(_f("analysis_contract_decision_test", f"{rel}: {probs}", rel))
        else:
            need = ("finding", "varied", "criterion", "decision_changes", "recommendation")
            miss = [k for k in need if k not in doc]
            if miss or not isinstance(doc.get("decision_changes"), bool):
                out.append(_f("analysis_contract_decision_test", f"{rel}: lacks {miss} or a boolean verdict", rel))
            elif (doc["share_units_changed"] > doc["share_threshold"]) != doc["decision_changes"]:
                out.append(_f("analysis_contract_decision_test", f"{rel}: share and threshold contradict the verdict", rel))
    return out


def _claims(run_dir, man) -> list[dict]:
    lp = os.path.join(run_dir, "models", "claims_ledger.yaml")
    if not any(e.get("role") == "claims_ledger" for e in (man.get("deliverables") or {}).values()):
        return []
    try:
        doc = yaml.safe_load(open(lp)) or {}
    except (OSError, yaml.YAMLError) as e:
        return [_f("analysis_contract_claims", f"claims ledger unreadable: {e}")]
    listed = set(man.get("artifacts") or {}) | set(man.get("deliverables") or {})
    out = []
    for c in doc.get("claims") or []:
        if c.get("produced_by") != "harness":
            out.append(_f("analysis_contract_claims", f"claim {c.get('id')} is not harness-produced", str(c.get("id"))))
        elif c.get("source_artifact") not in listed:
            out.append(_f("analysis_contract_claims", f"claim {c.get('id')} cites {c.get('source_artifact')}, which the "
                                                      "manifest does not list", str(c.get("id"))))
    return out


def _figures(run_dir, man) -> list[dict]:
    out = []
    for rel, ent in (man.get("deliverables") or {}).items():
        if ent.get("role") != "figure":
            continue
        sp = os.path.join(run_dir, rel) + ".provenance.json"
        try:
            sc = json.load(open(sp))
        except (OSError, ValueError):
            out.append(_f("analysis_contract_figure", f"{rel}: provenance sidecar missing or unreadable", rel))
            continue
        for src, h in (sc.get("source_hashes") or {}).items():
            if sha256(src) != h:
                out.append(_f("analysis_contract_figure", f"{rel}: source {os.path.relpath(src, run_dir)} changed after "
                                                          "the figure was drawn", rel))
    return out


def _self_test() -> int:
    import tempfile
    from toolkit.analysis.artifacts import Store
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def kinds(d):
        return {x["kind"] for x in gate_findings(d)}

    def complete_run(d, cal=None, drop_kind=None, finalize=True):
        """A minimal completed run: every expected artifact kind and deliverable role."""
        S = Store(d, sha256(os.path.join(d, SPEC)), True, {"name": "t", "version": "1", "card_sha256": "x"})
        open(os.path.join(d, "units.csv"), "w").write("id\n1\n")
        pay = {"engine": {"card": {}, "parameters": []}, "batch": {"evaluation": "direct"},
               "calibration": cal or {"method": "none", "units": {}, "diagnostics": {}, "identifiability": {}},
               "point": {"plan": {}, "averted": {}, "cost_allocated": 0, "mix": {}, "solver": {}},
               "uncertainty": {"draws": 2, "covers": [], "excludes": [], "label": "", "fixed_plan": {}},
               "stability": {"method": "m", "folds": [], "metrics": {}, "verdict": "NOT_RUN", "value_verdict": "NOT_RUN"},
               "sensitivity": {"rows": []}, "decision_tests": {"tests": []}, "figures": {"figures": []},
               "claims": {"n_claims": 0, "unresolved": []}}
        for k, v in pay.items():
            if k != drop_kind:
                S.write(k, v, f"toolkit/analysis/{k}.py", ["units.csv"] if k == "calibration" else [])
        for rel, role in (("alloc.csv", "allocation"), ("uncertainty_report.yaml", "uncertainty_report"),
                          ("claims_spec.yaml", "claims_spec"), ("results.md", "results")):
            open(os.path.join(d, rel), "w").write("x\n")
            S.deliverable(os.path.join(d, rel), "toolkit/analysis/run.py", role)
        os.makedirs(os.path.join(d, "models"), exist_ok=True)
        yaml.safe_dump({"claims": []}, open(os.path.join(d, "models", "claims_ledger.yaml"), "w"))
        S.deliverable(os.path.join(d, "models", "claims_ledger.yaml"), "toolkit/analysis/claims.py", "claims_ledger")
        S.manifest["expected"] = {"kinds": list(EXPECTED_KINDS), "roles": list(EXPECTED_ROLES), "decision_tests": {}}
        if finalize:
            S.finalize("complete")
        return S

    with tempfile.TemporaryDirectory() as d:
        ok(gate_findings(d) == [], "K1 a run without a spec or analysis: nothing to check")
        open(os.path.join(d, SPEC), "w").write("x: 1\n")
        ok(kinds(d) == {"analysis_contract_missing"}, "K2 a spec without the analysis output fails closed")
        os.makedirs(os.path.join(d, "analysis"))
        open(os.path.join(d, MANIFEST), "w").write("{not json")
        ok(kinds(d) == {"analysis_contract_missing"}, "K3 an unreadable manifest fails closed")
        S = complete_run(d)
        ok(gate_findings(d) == [], f"K4 a completed harness run for the current signed spec passes: {gate_findings(d)}")
        rel = "analysis/sensitivity.yaml"
        doc = yaml.safe_load(open(os.path.join(d, rel)))
        doc["produced_by"] = "agent"
        yaml.safe_dump(doc, open(os.path.join(d, rel), "w"))
        S.manifest["artifacts"][rel]["sha256"] = sha256(os.path.join(d, rel))
        S.finalize("complete")
        ok("analysis_contract_schema" in kinds(d), "K5 an artifact not produced by the harness fails (matching hash)")
        open(os.path.join(d, SPEC), "a").write("y: 2\n")
        ok("analysis_contract_stale" in kinds(d), "K6 a spec edited after the analysis: stale")
    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, SPEC), "w").write("x: 1\n")
        complete_run(d, finalize=False)
        ok(kinds(d) == {"analysis_contract_missing"}, "K7 a run that never finished publishes no manifest (Codex P1-9)")
        S = complete_run(d, drop_kind="sensitivity")
        ok("analysis_contract_incomplete" in kinds(d), "K8 a completed run missing an expected artifact (Codex P1-9)")
        S = complete_run(d)
        S.finalize("blocked", "corrections bear on the plan")
        ok(kinds(d) == {"analysis_contract_blocked"}, "K9 a blocked run is reported as blocked")
        complete_run(d)
        open(os.path.join(d, "units.csv"), "a").write("2\n")
        ok("analysis_contract_stale" in kinds(d), "K10 an input changed after the artifact was computed (Codex P1-10)")
        for cal, why in (({"method": "likelihood", "units": {}, "identifiability": {},
                           "diagnostics": {"converged": False, "at_bound": []}}, "did not converge"),
                         ({"method": "likelihood", "units": {}, "identifiability": {},
                           "diagnostics": {"converged": True, "at_bound": ["z:u1"]}}, "at a bound"),
                         ({"method": "likelihood", "units": {}, "identifiability": {},
                           "diagnostics": {"converged": True, "at_bound": [], "unsupported_targets": 2}}, "cannot produce"),
                         ({"method": "likelihood", "units": {}, "diagnostics": {"converged": True, "at_bound": []},
                           "identifiability": {"parameters": {"p": {"status": "unidentified"}}}}, "unidentified"),
                         ({"method": "emulator_inversion", "units": {}, "identifiability": {},
                           "diagnostics": {"clamped_units": 3}}, "outside what the model reaches")):
            complete_run(d, cal=cal)
            f = [x for x in gate_findings(d) if x["kind"] == "analysis_contract_calibration"]
            ok(f and why in f[0]["claim"], f"K11 calibration gated (Codex P1-8): {why}")
        # engine choice (engine: auto): its hash is in the manifest and in every artifact header
        cp = os.path.join(d, "engine_choice.json")
        json.dump({"engine": {"ref": "x:Y"}, "why": "w"}, open(cp, "w"))
        try:
            import inspect
            from toolkit.analysis import artifacts as AR
            ok("engine_choice_sha" in inspect.signature(AR.Store.__init__).parameters,
               "K12a the store takes the engine choice's hash")
            orig_init = AR.Store.__init__

            def with_choice(self, *a, **k):
                orig_init(self, *a, engine_choice_sha=sha256(cp), **k)
            AR.Store.__init__ = with_choice
            try:
                complete_run(d)
            finally:
                AR.Store.__init__ = orig_init
            doc = yaml.safe_load(open(os.path.join(d, "analysis", "point.yaml")))
            clean = gate_findings(d) == [] and doc.get("engine_choice_sha256") == sha256(cp)
            json.dump({"engine": {"ref": "x:Z"}, "why": "w"}, open(cp, "w"))
            st = [x for x in gate_findings(d) if x["kind"] == "analysis_contract_stale"]
            ok(clean and len(st) > len(EXPECTED_KINDS),
               f"K12 a changed engine choice makes the manifest and every artifact stale ({len(st)} findings)")
        except Exception as e:  # noqa: BLE001
            ok(False, f"K12 engine choice hashed in the manifest and artifact headers: {type(e).__name__}: {e}")
    # v2d β (Codex r1-4): the gate re-reads the canonical posterior sample against the uncertainty draws
    try:
        import numpy as np
        from toolkit.analysis import calibration as C
        rows = np.array([[0.1, 1.0], [0.2, 2.0], [0.3, 3.0]])
        sid = C._sample_id(["a", "z:u"], rows)
        cal = {"method": "posterior", "sample": {"sample_id": sid, "names": ["a", "z:u"], "rows": rows.tolist()}}
        rec = [{"sample_id": sid, "row": 1, "values": {"a": 0.2, "z:u": 2.0}},
               {"sample_id": sid, "row": 2, "values": {"a": 0.3, "z:u": 3.0}}]
        unc = {"draws": 2, "covers": ["calibration"], "draw_provenance": {"sample_id": sid, "draws": 2, "applied": rec}}
        good = _posterior_provenance("analysis/calibration.yaml", cal, unc)
        half = dict(unc, draw_provenance=dict(unc["draw_provenance"], applied=[dict(rec[0], values={"a": 0.15,
                                                                                                    "z:u": 2.0}),
                                                                             rec[1]]))
        bad1 = _posterior_provenance("analysis/calibration.yaml", cal, half)
        bad2 = _posterior_provenance("analysis/calibration.yaml", cal, dict(unc, draw_provenance=None))
        bad3 = _posterior_provenance("analysis/calibration.yaml", cal, dict(unc, draws=3))
        ok(good == [] and all(b and b[0]["kind"] == "analysis_contract_uncovered_parameter" for b in (bad1, bad2, bad3)),
           f"K13 (Codex r1-4) the gate checks every uncertainty draw against the canonical sample "
           f"({len(bad1)}, {len(bad2)}, {len(bad3)} findings)")
    except Exception as e:  # noqa: BLE001
        ok(False, f"K13 posterior provenance in the gate: {type(e).__name__}: {e}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    fs = gate_findings(sys.argv[1])
    for x in fs:
        print(f"{x['severity']} {x['kind']}: {x['claim']}")
    print(f"{len(fs)} finding(s)")
    sys.exit(1 if fs else 0)
