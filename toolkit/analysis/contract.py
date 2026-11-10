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
  analysis_contract_figure     a figure's sources changed after it was drawn, or are recorded outside the run dir
                               (sources are recorded relative to the run dir, so a moved run dir still checks)
All HIGH, none scope-declarable; each fails closed (an unreadable file is a finding).
"""
from __future__ import annotations

import json
import math
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis.artifacts import (ENVELOPE, EXPECTED_KINDS, EXPECTED_ROLES, KINDS, MANIFEST,  # noqa: E402
                                        PRODUCED_BY, PRODUCER_PREFIX, sha256)
from toolkit.analysis.figures import presentation_findings, resolve_source  # noqa: E402

SPEC = "decision_spec.yaml"
ENGINE_CHOICE = "engine_choice.json"     # toolkit/spec/schema.py ENGINE_CHOICE


def _f(kind, claim, key=""):
    return {"kind": kind, "severity": "HIGH", "stage": "ANALYZE", "claim": claim, "key": key,
            "scope_declarable": False, "source": "toolkit/analysis/contract.py"}


def _names_engine(run_dir: str) -> bool:
    """The signed spec names its engine (anything but auto), so the analysis ignores engine_choice.json, as
    toolkit/spec/schema.py does, and records no choice hash. An unreadable spec or one without an engine is not
    taken as naming one: the choice file is then compared (fail closed)."""
    try:
        eng = (yaml.safe_load(open(os.path.join(run_dir, SPEC))) or {}).get("engine")
    except (OSError, yaml.YAMLError, AttributeError):
        return False
    return eng is not None and not (eng == "auto" or (isinstance(eng, dict) and eng.get("auto") is True))


def _affordability(run_dir: str, unc: dict) -> list[dict]:
    """F6 S1-1: the signed spec's affordability rule, judged here from the artifact's counts (Codex round 1 P1): a
    declared rule with no result or no draws, another rule in the artifact, a verdict the counts do not support, or a
    share above the rule is a finding. Without a declared rule the share is reported in the artifact, not judged."""
    try:
        raw = yaml.safe_load(open(os.path.join(run_dir, SPEC))) or {}
        rule = ((raw.get("uncertainty") or {}) if isinstance(raw, dict) else {}).get("affordability")
    except (OSError, yaml.YAMLError) as e:
        return [_f("analysis_contract_affordability", f"{SPEC} unreadable for the affordability rule: {e}",
                   "affordability")]
    if not rule:
        return []
    af = unc.get("affordability")
    F = lambda c: [_f("analysis_contract_affordability", f"analysis/uncertainty.yaml: {c}", "affordability")]  # noqa: E731
    if not isinstance(af, dict):
        return F(f"the spec declares an affordability rule ({rule}) but the artifact has no affordability result")
    n, over, share = af.get("draws"), af.get("draws_over_budget"), af.get("share_over_budget")

    def count(x):
        return isinstance(x, int) and not isinstance(x, bool)
    if not count(n) or n <= 0 or not count(over) or not 0 <= over <= n or n != unc.get("draws"):
        return F(f"the spec declares an affordability rule ({rule}) but the artifact has no usable result "
                 f"(draws {n!r} of the artifact's {unc.get('draws')!r}, over budget {over!r})")
    if not (isinstance(share, (int, float)) and not isinstance(share, bool) and math.isfinite(share)
            and abs(share - over / n) <= 1e-12):
        return F(f"the artifact's share over budget {share!r} does not equal {over} of {n} draws")
    if af.get("rule") != rule:
        return F(f"the artifact judged the rule {af.get('rule')!r}, the signed spec declares {rule!r}")
    met = over / n <= float(rule["max_share_over_budget"]) + 1e-12
    if af.get("rule_met") is not met:
        return F(f"the artifact's verdict rule_met={af.get('rule_met')!r} does not follow from {over} of {n} draws "
                 f"over budget against at most {rule['max_share_over_budget']}")
    if not met:
        return F(f"the fixed plan exceeds the allocatable budget in {over} of {n} uncertainty draws (share "
                 f"{over / n:.3f}); the spec allows at most {rule['max_share_over_budget']}")
    return []


def reserve_findings(rule, point: dict, unc: dict) -> list[dict]:
    """F6 S1-1b (Codex r1 P2): with `enforce`, point.yaml must carry the reserve the plan was solved in, consistent with
    the artifacts: the published plan is the reserve's affordable plan; f x budget is the planned budget and the rest
    is held back; the reserve's draws and seed are the uncertainty step's, which re-optimised at the planned budget;
    its counts agree with the uncertainty step's affordability; and the affordable plan meets the rule."""
    if not (isinstance(rule, dict) and rule.get("enforce") is True):
        return []
    F = lambda c: [_f("analysis_contract_reserve", f"analysis/point.yaml: {c}", "reserve")]  # noqa: E731

    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    rv = (point or {}).get("reserve")
    if not isinstance(rv, dict):
        return F("the spec enforces the affordability rule but the point artifact has no reserve record")
    aff, af_u = rv.get("affordable") or {}, (unc or {}).get("affordability") or {}
    f, b, bp, held = rv.get("fraction"), rv.get("budget_allocatable"), rv.get("budget_planned"), rv.get("reserve_usd")
    if not all(num(x) for x in (f, b, bp, held)) or not 0 < f <= 1:
        return F(f"the reserve's fraction and budgets are not usable ({f!r}, {b!r}, {bp!r}, {held!r})")
    tol = 1e-9 * max(1.0, abs(b))
    out = []
    if aff.get("plan") != point.get("plan"):
        out += F("the published plan is not the reserve's affordable plan")
    if num(point.get("budget_allocatable")) and abs(point["budget_allocatable"] - b) > tol:
        out += F(f"the reserve's allocatable budget {b} is not the point's {point['budget_allocatable']}")
    if abs(bp - f * b) > tol or abs(held - (b - bp)) > tol:
        out += F(f"the reserve's arithmetic does not add up: {f} x {b} vs planned {bp}, held back {held}")
    if rv.get("rule") != rule:
        out += F(f"the reserve was searched under {rv.get('rule')!r}, the signed spec declares {rule!r}")
    if rv.get("draws") != unc.get("draws") or rv.get("seed") != unc.get("seed"):
        out += F(f"the reserve's draws ({rv.get('draws')!r}, seed {rv.get('seed')!r}) are not the uncertainty step's "
                 f"({unc.get('draws')!r}, seed {unc.get('seed')!r})")
    if not num(unc.get("budget_planned")) or abs(unc["budget_planned"] - bp) > tol:
        out += F(f"the uncertainty step re-optimised at {unc.get('budget_planned')!r}, not the planned budget {bp}")
    if (aff.get("draws_over_budget"), aff.get("share_over_budget")) != (af_u.get("draws_over_budget"),
                                                                        af_u.get("share_over_budget")):
        out += F(f"the reserve's count ({aff.get('draws_over_budget')!r} over) disagrees with the uncertainty step's "
                 f"affordability ({af_u.get('draws_over_budget')!r} over)")
    sh = aff.get("share_over_budget")
    if not num(sh) or sh > float(rule["max_share_over_budget"]) + 1e-12:
        out += F(f"the reserve's affordable plan is over budget in a share {sh!r} of draws; the spec allows at most "
                 f"{rule['max_share_over_budget']}")
    return out


def _reserve(run_dir: str, point, unc: dict) -> list[dict]:
    try:
        raw = yaml.safe_load(open(os.path.join(run_dir, SPEC))) or {}
        rule = ((raw.get("uncertainty") or {}) if isinstance(raw, dict) else {}).get("affordability")
    except (OSError, yaml.YAMLError) as e:
        return [_f("analysis_contract_reserve", f"{SPEC} unreadable for the affordability rule: {e}", "reserve")]
    return reserve_findings(rule, point or {}, unc or {})


def _burden(run_dir: str, data, signed: bool = False) -> list[dict]:
    """F6 S2-5: the signed spec's validation.burden, judged from the artifact's numbers (toolkit/analysis/burden.py);
    a group outside its tolerance is declarable, every other problem is not (fail closed)."""
    try:
        raw = yaml.safe_load(open(os.path.join(run_dir, SPEC))) or {}
    except (OSError, yaml.YAMLError) as e:
        return [_f("analysis_contract_burden", f"{SPEC} unreadable for validation.burden: {e}", "burden")]
    from toolkit.analysis import burden as BV
    if BV.section(raw) is None:
        return []
    try:              # the declared references, read again from the signed spec (Codex r1 P2: reconcile, not trust)
        from toolkit.spec import schema
        sp = schema.load(os.path.join(run_dir, SPEC))
        refs, bad = BV.load_references(sp)
    except Exception as e:  # noqa: BLE001 -- fail closed
        sp, refs, bad = None, None, [f"{type(e).__name__}: {e}"]
    out = [_f("analysis_contract_burden", f"validation.burden: {b}", "burden") for b in bad]
    if signed:          # Codex #165 r3: the sign-off binds the reference file's bytes; a replaced file is not signed
        try:
            from toolkit.spec import signoff
            st = signoff.status(run_dir)
        except Exception as e:  # noqa: BLE001 -- fail closed
            st = f"unreadable ({type(e).__name__}: {e})"
        if st != "signed":
            out.append(_f("analysis_contract_burden", f"the reference file or the spec changed since sign-off "
                          f"(status {st}): the analysis ran on a signed spec, the inputs now are not", "burden"))
    pv = (data or {}).get("provenance") if isinstance(data, dict) else None
    if data is not None and not (isinstance(pv, dict) and pv.get("independent") is True):
        out.append(_f("analysis_contract_burden", "analysis/burden_validation.yaml does not record a reference file "
                      "independent of the calibration inputs", "burden"))
    return out + BV.findings(raw, data, None if bad else refs) + (BV.block_findings(sp, data) if sp and not bad else [])


def _survey_changes(run_dir: str, data) -> list[dict]:
    try:
        raw = yaml.safe_load(open(os.path.join(run_dir, SPEC))) or {}
    except (OSError, yaml.YAMLError) as e:
        return [_f("analysis_contract_calibration", f"{SPEC} unreadable for the survey-change diagnostic: {e}")]
    from toolkit.analysis import joint_calibration as JC
    try:                 # Codex r3: the expected pairs, rebuilt from the spec and its targets, not from the record
        from toolkit.spec import schema
        exp, out = JC.expected_change_ids(schema.load(os.path.join(run_dir, SPEC))), []
    except Exception as e:  # noqa: BLE001 -- fail closed
        exp, out = None, [_f("analysis_contract_calibration", f"the expected survey-change pairs cannot be rebuilt from "
                                                              f"{SPEC}: {type(e).__name__}: {e}")]
    return out + JC.change_findings(raw, data, exp)


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
    cal_doc, unc_doc, est_doc, pt_doc, pv_doc, bv_doc = None, None, None, None, None, None
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
    base_k, base_r = EXPECTED_KINDS, EXPECTED_ROLES
    if man.get("deliverable_kind") == "estimates":      # no allocation units (toolkit/analysis/estimates.py)
        from toolkit.analysis.estimates import EXPECTED_KINDS as base_k, EXPECTED_ROLES as base_r
        cur_kind = _deliverable_kind(run_dir)
        if cur_kind != "estimates":
            out.append(_f("analysis_contract_stale", f"the manifest records an estimates run, the spec's deliverable "
                                                     f"kind is {cur_kind!r}"))
    need_k = set(base_k) | set(exp.get("kinds") or [])
    for k in sorted(need_k - kinds):
        out.append(_f("analysis_contract_incomplete", f"the completed run has no {k} artifact", k))
    for r in sorted(set(base_r) - roles):
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
    cur_ch = None if _names_engine(run_dir) else sha256(os.path.join(run_dir, ENGINE_CHOICE))
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
        if kind == "joint_calibration":       # F6 S2-3b: the change between survey rounds by group
            out += _survey_changes(run_dir, doc.get("data") or {})
        if kind == "calibration":
            out += _calibration(rel, doc.get("data") or {})
            cal_doc = (rel, doc.get("data") or {})
        if kind == "uncertainty":
            unc_doc = doc.get("data") or {}
        if kind == "point":
            pt_doc = doc.get("data") or {}
        if kind == "burden_validation":
            bv_doc = doc.get("data") or {}
        if kind == "plan_variants":
            pv_doc = doc.get("data") or {}
        if kind == "estimates":
            est_doc = (rel, doc.get("data") or {})
        if kind == "point" and ((doc.get("data") or {}).get("model_corrections") or {}).get("on_plan"):
            out.append(_f("analysis_contract_model_domain", f"{rel}: model corrections bear on the plan", rel))
    for rel, ent in sorted((man.get("deliverables") or {}).items()):
        p = os.path.join(run_dir, rel)
        if not os.path.exists(p):
            out.append(_f("analysis_contract_missing", f"deliverable {rel} is missing", rel))
        elif sha256(p) != ent.get("sha256"):
            out.append(_f("analysis_contract_tampered", f"deliverable {rel} ({ent.get('role')}) changed after the "
                                                        "harness wrote it", rel))
    if est_doc is not None:        # the automatic tests against the ones the calibration requires (Codex r2-3)
        exp_ids = (cal_doc[1].get("prior_range_tests") if cal_doc else None)
        out += estimates_test_findings(est_doc[0], est_doc[1], exp_ids if isinstance(exp_ids, list) else [])
    if cal_doc and cal_doc[1].get("method") == "posterior" and unc_doc is not None:
        out += _posterior_provenance(cal_doc[0], cal_doc[1], unc_doc)
    out += _affordability(run_dir, unc_doc or {})
    out += _reserve(run_dir, pt_doc, unc_doc or {})
    if pt_doc is not None:       # F6 S1-6 (Codex r1 P2-4): the spec, point.yaml and the plan_variants artifact agree
        out += _plan_variants(run_dir, pt_doc, pv_doc)
    out += _burden(run_dir, bv_doc, man.get("spec_signed") is True)
    out += _decision_tests(run_dir, man)
    out += _claims(run_dir, man)
    out += _figures(run_dir, man)
    return out


VERDICTS = ("yes", "no", "undetermined")


def estimates_test_findings(rel, data, expected=None) -> list[dict]:
    """Codex r1-7, r2-3: the automatic prior_range tests of an estimates run are required. `expected`: the test ids the
    calibration lists (calibration.yaml prior_range_tests); each must be present exactly once, run, with both declared
    ends evaluated (finite medians, a recognised verdict when the spec has a rule) and a boolean decision_changes. A
    missing, extra, refused or incomplete test is a finding, never a quiet pass."""
    import math
    out = []
    F = lambda c, k: out.append(_f("analysis_contract_decision_test", f"{rel}: {c}", k))  # noqa: E731
    tests = data.get("decision_tests") if isinstance(data, dict) else None
    if not isinstance(tests, list):
        F("no decision_tests list (the automatic prior_range tests)", rel)
        return out
    has_rule = isinstance(data.get("decision"), dict) and "verdict" in data["decision"]
    ids = [str((t or {}).get("id")) for t in tests]
    if expected is not None:
        for tid in sorted(set(expected) - set(ids)):
            F(f"automatic test {tid}, which the calibration requires, is missing", tid)
        for tid in sorted(set(ids) - set(expected)):
            F(f"automatic test {tid} is not one the calibration lists", tid)
    for tid in sorted({i for i in ids if ids.count(i) > 1}):
        F(f"automatic test {tid} is recorded more than once", tid)
    for t, tid in zip(tests, ids):
        if not isinstance(t, dict) or t.get("status") != "run":
            F(f"automatic test {tid} was not run (status {(t or {}).get('status')!r})", tid)
            continue
        ends = [e for e in t.get("ends") or [] if isinstance(e, dict)]
        decl = [e for e in ends if e.get("kind", "declared") == "declared"]
        if len(decl) != 2:
            F(f"automatic test {tid} has {len(decl)} declared ends evaluated (2 required)", tid)
        for e in ends:
            meds = (e.get("median") or {}).values()
            if "refused" in e or not meds or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in meds):
                F(f"automatic test {tid}: the end at {e.get('value')!r} has no finite result", tid)
            elif has_rule and e.get("verdict") not in VERDICTS:
                F(f"automatic test {tid}: the end at {e.get('value')!r} has no recognised verdict", tid)
        if has_rule and not isinstance(t.get("decision_changes"), bool):
            F(f"automatic test {tid} has no verdict", tid)
    return out


def _deliverable_kind(run_dir):
    try:
        return ((yaml.safe_load(open(os.path.join(run_dir, SPEC))) or {}).get("deliverable") or {}).get("kind")
    except (OSError, yaml.YAMLError, AttributeError):
        return None


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


def _plan_variants(run_dir: str, point: dict, pv) -> list[dict]:
    from toolkit.analysis import variants as V
    try:
        raw = yaml.safe_load(open(os.path.join(run_dir, SPEC))) or {}
    except (OSError, yaml.YAMLError) as e:
        return [_f("analysis_contract_plan_variants", f"{SPEC} unreadable: {e}")]
    return [_f("analysis_contract_plan_variants", m) for m in V.contract_findings(raw, point, pv)]


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
            elif doc["share_units_changed"] > doc["share_threshold"] and not doc["decision_changes"]:
                # F6 S1-5: regret or dollars moved can also change the plan (toolkit/analysis/moves.py)
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
        srcs = sc.get("source_hashes") if isinstance(sc, dict) else None
        if not isinstance(srcs, dict):
            out.append(_f("analysis_contract_figure", f"{rel}: provenance sidecar records no source mapping", rel))
            continue
        for src, h in srcs.items():
            # recorded relative to the run dir (toolkit/analysis/figures.py); outside the run dir fails closed
            p, why = resolve_source(run_dir, src)
            if p is None:
                out.append(_f("analysis_contract_figure", f"{rel}: {why}", rel))
            elif sha256(p) != h:
                out.append(_f("analysis_contract_figure", f"{rel}: source {os.path.relpath(p, os.path.realpath(run_dir))}"
                                                          " changed after the figure was drawn", rel))
        # the unsigned presentation files (labels, boundaries, crosswalk) the figure read, with their sha
        for why in presentation_findings(run_dir, sc):
            out.append(_f("analysis_contract_figure", f"{rel}: presentation input {why}", rel))
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

    def complete_run(d, cal=None, drop_kind=None, finalize=True, unc=None):
        """A minimal completed run: every expected artifact kind and deliverable role."""
        S = Store(d, sha256(os.path.join(d, SPEC)), True, {"name": "t", "version": "1", "card_sha256": "x"})
        open(os.path.join(d, "units.csv"), "w").write("id\n1\n")
        pay = {"engine": {"card": {}, "parameters": []}, "batch": {"evaluation": "direct"},
               "calibration": cal or {"method": "none", "units": {}, "diagnostics": {}, "identifiability": {}},
               "point": {"plan": {}, "averted": {}, "cost_allocated": 0, "mix": {}, "solver": {}},
               "uncertainty": unc or {"draws": 2, "covers": [], "excludes": [], "label": "", "fixed_plan": {}},
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
        # F6 S1-1 (Codex P1): the gate judges the signed spec's rule itself, from the artifact's counts; a declared rule
        # with no result, another rule in the artifact, or a verdict the counts do not support is a finding
        spec_text = open(os.path.join(d, SPEC)).read()
        base_u = {"draws": 4, "covers": [], "excludes": [], "label": "", "fixed_plan": {}}

        def aff(over, rule, met, draws=4):
            return {"draws": draws, "draws_over_budget": over, "share_over_budget": over / draws if draws else 0.0,
                    "budget_allocatable": 1.0, "rule": rule, "rule_met": met}
        r01 = {"max_share_over_budget": 0.1}
        for spec_rule, a_, want, label in (
                (r01, aff(3, r01, False), True, "rule not met"),
                (r01, aff(3, {"max_share_over_budget": 0.9}, True), True, "artifact carries a looser rule"),
                (r01, None, True, "declared rule, no affordability result"),
                (r01, aff(3, r01, True), True, "verdict the counts do not support"),
                (r01, aff(0, r01, True, draws=0), True, "declared rule judged on zero draws"),
                (r01, dict(aff(0, r01, True), share_over_budget=0.75), True, "share inconsistent with the counts"),
                (r01, dict(aff(0, r01, True), share_over_budget=None), True, "share missing"),
                (r01, dict(aff(0, r01, True), draws=True), True, "a boolean draw count"),
                (r01, dict(aff(0, r01, True), draws_over_budget=False), True, "a boolean over-budget count"),
                (r01, "x", True, "affordability not a mapping"),
                (r01, aff(0, r01, True), False, "rule met"),
                (None, aff(3, None, None), False, "no rule declared: reported, not judged"),
                (None, None, False, "no rule declared, older artifact without affordability")):
            open(os.path.join(d, SPEC), "w").write(spec_text + (yaml.safe_dump({"uncertainty": {"affordability": spec_rule}})
                                                                if spec_rule else ""))
            complete_run(d, unc=dict(base_u, **({"affordability": a_} if a_ is not None else {})))
            if label == "rule met":      # the artifact's total draws must equal the affordability count
                complete_run(d, unc=dict(base_u, draws=0, affordability=a_))
                f0 = [x for x in gate_findings(d) if x["kind"] == "analysis_contract_affordability"]
                ok(bool(f0), f"K13 affordability: counts on 4 draws in an artifact of 0 draws is a finding {f0[:1]}")
                complete_run(d, unc=dict(base_u, affordability=a_))
            f = [x for x in gate_findings(d) if x["kind"] == "analysis_contract_affordability"]
            ok(bool(f) == want, f"K13 affordability, {label}: finding {bool(f)} (want {want}) {f[:1]}")
        open(os.path.join(d, SPEC), "w").write(spec_text)
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
        # a spec that names its engine ignores engine_choice.json (toolkit/spec/schema.py): the harness still writes
        # the file at ENGINE_CHOICE, and the analysis records no choice hash. That is not stale (flu run 8 and
        # measles runs 6-7: a spurious HIGH analysis_contract_stale at every gate)
        open(os.path.join(d, SPEC), "w").write("engine:\n  ref: x:Y\n")
        complete_run(d)
        json.dump({"engine": {"ref": "x:Y"}, "why": "harness record"}, open(cp, "w"))
        ok(gate_findings(d) == [], f"K12b a spec that names its engine is not stale for the harness's engine_choice.json "
                                   f"it ignores: {gate_findings(d)}")
        open(os.path.join(d, SPEC), "w").write("engine: auto\n")
        complete_run(d)
        ok("analysis_contract_stale" in kinds(d), "K12c engine: auto with a choice the analysis did not record: stale")
    # figure sources are recorded relative to the run dir: a run whose outputs moved (VM /tmp paths) still checks,
    # and a source outside the run dir fails closed
    try:
        with tempfile.TemporaryDirectory() as parent:
            d = os.path.join(parent, "run_a")
            os.makedirs(os.path.join(d, "figures"))
            open(os.path.join(d, SPEC), "w").write("x: 1\n")
            S = complete_run(d, finalize=False)
            png = os.path.join(d, "figures", "f.png")
            open(png, "wb").write(b"png")
            src = "analysis/point.yaml"

            def sidecar(run, srcs):
                json.dump({"png_path": "figures/f.png", "paths_relative_to": "run_dir", "source_hashes": srcs,
                           "produced_by": "harness"}, open(os.path.join(run, "figures", "f.png.provenance.json"), "w"))
            sidecar(d, {src: sha256(os.path.join(d, src))})
            S.deliverable(png, "toolkit/analysis/figures.py", "figure")
            S.finalize("complete")
            figk = lambda r: [x["claim"] for x in gate_findings(r) if x["kind"] == "analysis_contract_figure"]  # noqa: E731
            ok(gate_findings(d) == [], f"K14a figure sources recorded relative to the run dir pass: {gate_findings(d)}")
            moved = os.path.join(parent, "run_moved")
            os.rename(d, moved)
            ok(gate_findings(moved) == [], f"K14b the run dir moved: its figures still check against their sources "
                                           f"{gate_findings(moved)}")
            keep = open(os.path.join(moved, src)).read()
            open(os.path.join(moved, src), "a").write("# edited\n")
            ok(any("changed after" in c for c in figk(moved)), "K14c a relative source edited after drawing: finding")
            open(os.path.join(moved, src), "w").write(keep)
            other = os.path.join(parent, "elsewhere.yaml")
            open(other, "w").write(keep)
            sidecar(moved, {other: sha256(other)})
            ok(any("outside the run dir" in c for c in figk(moved)),
               f"K14d an absolute source outside the run dir fails closed, even with a matching hash: {figk(moved)}")
            sidecar(moved, {"../elsewhere.yaml": sha256(other)})
            ok(any("outside the run dir" in c for c in figk(moved)),
               f"K14e a relative source that escapes the run dir fails closed: {figk(moved)}")
            sidecar(moved, {"/tmp/vm_job_x/run/analysis/point.yaml": sha256(os.path.join(moved, src))})
            ok(figk(moved) != [], "K14f an absolute VM path that no longer exists fails closed")
            sidecar(moved, {os.path.join(moved, src): sha256(os.path.join(moved, src))})
            ok(figk(moved) == [], f"K14g a legacy absolute source inside the run dir still passes: {figk(moved)}")
            sidecar(moved, ["not", "a", "mapping"])
            ok(figk(moved) != [], "K14h a sidecar whose sources are not a mapping fails closed")
    except Exception as e:  # noqa: BLE001
        ok(False, f"K14 figure sources relative to the run dir: {type(e).__name__}: {e}")
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
