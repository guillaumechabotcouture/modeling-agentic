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
from toolkit.analysis.artifacts import ENVELOPE, KINDS, MANIFEST, PRODUCED_BY, PRODUCER_PREFIX, sha256  # noqa: E402

SPEC = "decision_spec.yaml"


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
    cur = sha256(os.path.join(run_dir, SPEC))
    if man.get("spec_sha256") != cur:
        out.append(_f("analysis_contract_stale", f"the artifacts were computed for spec {str(man.get('spec_sha256'))[:12]}, "
                                                 f"the run's {SPEC} is {str(cur)[:12]}: re-run the analysis"))
    cur_set = sha256(os.path.join(run_dir, "analysis_settings.yaml"))
    if man.get("settings_sha256") != cur_set:
        out.append(_f("analysis_contract_stale", "analysis_settings.yaml changed (or appeared or vanished) after the "
                                                 "analysis ran: re-run the analysis"))
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
    for rel, ent in sorted((man.get("deliverables") or {}).items()):
        p = os.path.join(run_dir, rel)
        if not os.path.exists(p):
            out.append(_f("analysis_contract_missing", f"deliverable {rel} is missing", rel))
        elif sha256(p) != ent.get("sha256"):
            out.append(_f("analysis_contract_tampered", f"deliverable {rel} ({ent.get('role')}) changed after the "
                                                        "harness wrote it", rel))
    out += _decision_tests(run_dir, man)
    out += _claims(run_dir, man)
    out += _figures(run_dir, man)
    return out


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
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    with tempfile.TemporaryDirectory() as d:
        ok(gate_findings(d) == [], "K1 a run without a spec or analysis: nothing to check")
        open(os.path.join(d, SPEC), "w").write("x: 1\n")
        ok([x["kind"] for x in gate_findings(d)] == ["analysis_contract_missing"],
           "K2 a spec without the analysis output fails closed")
        os.makedirs(os.path.join(d, "analysis"))
        open(os.path.join(d, MANIFEST), "w").write("{not json")
        ok([x["kind"] for x in gate_findings(d)] == ["analysis_contract_missing"], "K3 an unreadable manifest fails closed")
        from toolkit.analysis.artifacts import Store
        S = Store(d, sha256(os.path.join(d, SPEC)), True, {"name": "t", "version": "1", "card_sha256": "x"})
        rel = S.write("sensitivity", {"rows": []}, "toolkit/analysis/sensitivity.py")
        ok(gate_findings(d) == [], f"K4 a harness artifact for the current signed spec passes: {gate_findings(d)}")
        doc = yaml.safe_load(open(os.path.join(d, rel)))
        doc["produced_by"] = "agent"
        yaml.safe_dump(doc, open(os.path.join(d, rel), "w"))
        S.manifest["artifacts"][rel]["sha256"] = sha256(os.path.join(d, rel))
        S.save()
        ok("analysis_contract_schema" in [x["kind"] for x in gate_findings(d)],
           "K5 an artifact not produced by the harness fails (even with a matching hash)")
        open(os.path.join(d, SPEC), "a").write("y: 2\n")
        ok("analysis_contract_stale" in [x["kind"] for x in gate_findings(d)], "K6 a spec edited after the analysis: stale")
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
