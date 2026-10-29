#!/usr/bin/env python3
"""Phase 42 γ (D1): mechanism manifest, parsimony first.

    python3 scripts/mechanism_manifest.py <run_dir> [--round N]
    python3 scripts/mechanism_manifest.py --self-test

E7 attempt 7's pre-mortem asked for a robustness check on a mechanism the
model collapsed; it was graded LOW and closed with prose. Attempt 8 had the
data for that mechanism and no code read it. A later test showed the
simplification was sufficient. A simplification is fine; an untested one is
not.

A prior in `.claude/orchestration/expert_priors.yaml` may carry
`requires_mechanism: [{id, description, sufficiency_test,
when_data_columns_any?}]`. When such a prior applies to the run
(`lib.expert_priors.run_priors`: its applies_when matches the question, or the
pre-mortem cites it as `expert_prior:<id>`), and, if `when_data_columns_any` is
given, a column of any data CSV the run holds (data/, data/raw/ included)
contains one of those strings, the run
must account for the mechanism. A prior whose disease area matches the
question but whose keywords do not (`lib.expert_priors.domain_matches`) also
requires a mechanism that has `when_data_columns_any`, when those columns are
in the data (the data shows the variation exists). Otherwise the run
must account for the mechanism in `models/mechanism_manifest.yaml`:

    mechanisms:
      - id: <mechanism id from the prior>
        status: modelled                 # represented in the model code
        code_ref: models/model.py::function_or_name
      - id: <id>
        status: fixed                    # held at a sourced value
        source: "<citation or Parameter Registry name>"
      - id: <id>
        status: simplified               # collapsed or left out
        assumption: "<what the model assumes instead>"
        sufficiency_test: models/sufficiency/<id>.yaml   # the result file

The result file (YAML or JSON) states the test and its verdict:

    varied: "<the assumption, and the plausible range it was varied over>"
    criterion: "<what counts as a decision change: the package for a stated
                share of units, or the headline beyond its interval>"
    decision_changes: false              # the verdict (true/false)
    # optional, checked when both are given:
    share_units_changed: 0.03
    share_threshold: 0.10

If the decision does not change, the simplification stands (the report should
cite the test). If it does, the mechanism must be added: the entry may not
stay `simplified`.

Findings:
  mechanism_manifest_invalid       HIGH  unreadable / malformed manifest or entry
  mechanism_unaccounted            HIGH  a required mechanism has no entry
                                         (MEDIUM at round 1, the drafting window)
  mechanism_ref_unresolved         HIGH  modelled code_ref / fixed source missing
  mechanism_sufficiency_missing    HIGH  simplified with no usable result file
                                         (MEDIUM at round 1)
  mechanism_simplification_rejected HIGH simplified although its test says the
                                         decision changes
  mechanism_test_not_cited         MEDIUM report.md does not cite the test
  mechanism_priors_unavailable     MEDIUM the run question cannot be read, so the
                                         required set is unknown
Never HIGH for leaving a feature out when its sufficiency test passed. None of
these are scope-declarable: a mechanism the run cannot model is `simplified`
with a test, or `fixed` with a source.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join("models", "mechanism_manifest.yaml")
STATUSES = ("modelled", "fixed", "simplified")


def _lib():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from lib import expert_priors
    return expert_priors


def _sibling(name: str):
    """scripts/<name>.py by path, under a private module name, so a run's own
    module of the same name (models/ on sys.path) is never picked up."""
    import importlib.util
    key = f"_ma_p42_{name}"
    mod = sys.modules.get(key)
    if mod is None:
        spec = importlib.util.spec_from_file_location(key, os.path.join(HERE, f"{name}.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[key] = mod
        try:
            spec.loader.exec_module(mod)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return mod


def _f(kind, sev, claim, stage="MODEL"):
    return {"kind": kind, "severity": sev, "stage": stage, "target_stage": "MODEL",
            "scope_declarable": False, "claim": claim}


def _inside(run_dir: str, rel: str) -> str | None:
    """Absolute path of `rel` when it is an existing file inside run_dir."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    root = os.path.realpath(run_dir)
    p = os.path.realpath(os.path.join(root, rel.strip()))
    if not (p == root or p.startswith(root + os.sep)) or not os.path.isfile(p):
        return None
    return p


def required_mechanisms(run_dir: str, priors=None) -> tuple[list[tuple[str, object]], str | None]:
    """[(prior_id, Mechanism)] the run must account for, and an error string."""
    ep = _lib()
    applied, err = ep.run_priors(run_dir, priors)
    applied_ids = {p.id for p in applied}
    # A prior whose disease area matches the question but whose keywords do
    # not (a generic budget question that never names the intervention) still
    # requires a mechanism whose trigger columns are in the run's data: the
    # data, not the wording, shows the variation exists.
    domain_only = [p for p in ep.run_domain_priors(run_dir, priors) if p.id not in applied_ids]
    cols: list[str] | None = None
    out = []
    for p in applied + domain_only:
        by_domain = p.id not in applied_ids
        for m in p.requires_mechanism:
            if by_domain and not m.when_data_columns_any:
                continue
            if m.when_data_columns_any:
                if cols is None:
                    # Any data the run holds, raw downloads included (E7 attempt 7
                    # had the trigger data only under data/raw/). An unreadable
                    # processed file cannot rule the trigger out; raw ones are
                    # source downloads in any format and are skipped.
                    headers, errs = _sibling("data_use").processed_headers(run_dir, include_raw=True)
                    cols = [c.lower() for cs in headers.values() for c in cs]
                    raw = os.path.join("data", "raw") + os.sep
                    if any(not r.startswith(raw) for r in errs):
                        cols.append("\0unreadable")
                hit = any(s.lower() in c for s in m.when_data_columns_any for c in cols)
                if not hit and (by_domain or "\0unreadable" not in cols):
                    continue
            out.append((p.id, m))
    return out, err


def _load_manifest(run_dir: str) -> tuple[dict | None, list[dict], bool]:
    """(entries by id, findings, exists)."""
    path = os.path.join(run_dir, PATH)
    if not os.path.exists(path):
        return None, [], False
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, [_f("mechanism_manifest_invalid", "HIGH", f"`{PATH}` cannot be parsed: {e}")], True
    if not isinstance(doc, dict) or not isinstance(doc.get("mechanisms"), list):
        return None, [_f("mechanism_manifest_invalid", "HIGH",
                         f"`{PATH}` must be a mapping with a `mechanisms:` list.")], True
    entries, out = {}, []
    for i, e in enumerate(doc["mechanisms"]):
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"].strip():
            out.append(_f("mechanism_manifest_invalid", "HIGH", f"`{PATH}` mechanisms[{i}] has no `id:`."))
            continue
        mid = e["id"].strip()
        if mid in entries:
            out.append(_f("mechanism_manifest_invalid", "HIGH", f"`{PATH}`: mechanism `{mid}` is listed twice."))
            continue
        st = str(e.get("status", "")).strip().lower()
        if st not in STATUSES:
            out.append(_f("mechanism_manifest_invalid", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` has status {e.get('status')!r}; "
                          f"it must be one of {', '.join(STATUSES)}."))
            continue
        entries[mid] = dict(e, status=st)
    return entries, out, True


def _load_result(path: str):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return json.loads(text) if path.endswith(".json") else yaml.safe_load(text)


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _check_entry(run_dir: str, mid: str, e: dict, late: str, report: str | None) -> list[dict]:
    out = []
    st = e["status"]
    if st == "modelled":
        ref = e.get("code_ref")
        fname, _, sym = (ref or "").partition("::")
        p = _inside(run_dir, fname)
        found = False
        if p is not None:
            try:
                with open(p, encoding="utf-8") as f:
                    text = f.read()
                found = not sym.strip() or re.search(r"(?<![A-Za-z0-9_])" + re.escape(sym.strip())
                                                     + r"(?![A-Za-z0-9_])", text) is not None
            except (OSError, UnicodeDecodeError):
                found = False
        if not found:
            out.append(_f("mechanism_ref_unresolved", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` is `modelled` but `code_ref: {ref!r}` does not name an "
                          "existing run file (and, after `::`, a name in it)."))
    elif st == "fixed":
        if not isinstance(e.get("source"), str) or len(e["source"].strip()) < 3:
            out.append(_f("mechanism_ref_unresolved", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` is `fixed` but gives no `source:` for the fixed value."))
    else:
        if not isinstance(e.get("assumption"), str) or not e["assumption"].strip():
            out.append(_f("mechanism_manifest_invalid", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` is `simplified` but states no `assumption:`."))
        rel = e.get("sufficiency_test")
        p = _inside(run_dir, rel) if isinstance(rel, str) else None
        problem, res = None, None
        if p is None:
            problem = (f"names no existing result file (`sufficiency_test: {rel!r}`)")
        else:
            try:
                res = _load_result(p)
            except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
                problem = f"has a result file that cannot be parsed ({exc})"
            if problem is None:
                if not isinstance(res, dict):
                    problem = "has a result file that is not a mapping"
                elif not isinstance(res.get("decision_changes"), bool):
                    problem = "has a result file without a `decision_changes: true|false` verdict"
                elif not res.get("varied") or not (isinstance(res.get("criterion"), str) and res["criterion"].strip()):
                    problem = "has a result file that does not state what was `varied` and the decision `criterion`"
        if problem:
            out.append(_f("mechanism_sufficiency_missing", late,
                          f"`{PATH}`: mechanism `{mid}` is `simplified` but {problem}. Vary the assumption over its "
                          "plausible range and record whether the decision changes (the package for a stated share "
                          "of units, or the headline beyond its uncertainty interval)."))
            return out
        share, thr = _num(res.get("share_units_changed")), _num(res.get("share_threshold"))
        if share is not None and thr is not None and (share > thr) != res["decision_changes"]:
            out.append(_f("mechanism_manifest_invalid", "HIGH",
                          f"`{rel}`: share_units_changed {share} vs share_threshold {thr} contradicts "
                          f"decision_changes: {res['decision_changes']}."))
        if isinstance(e.get("decision_changes"), bool) and e["decision_changes"] != res["decision_changes"]:
            out.append(_f("mechanism_manifest_invalid", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` restates decision_changes: {e['decision_changes']}, but its "
                          f"result file `{rel}` says {res['decision_changes']}."))
        if res["decision_changes"]:
            out.append(_f("mechanism_simplification_rejected", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` is `simplified`, but its sufficiency test (`{rel}`) finds that "
                          "the decision changes. Add the mechanism to the model (status `modelled`) in the next round."))
        elif report is not None and rel.strip() not in report and os.path.basename(rel.strip()) not in report \
                and mid not in report:
            out.append({**_f("mechanism_test_not_cited", "MEDIUM",
                             f"report.md does not cite the sufficiency test for simplified mechanism `{mid}` "
                             f"(`{rel}`). State the simplification and cite the test that supports it.", "WRITE"),
                        "target_stage": "WRITE", "scope_declarable": True})
    return out


def gate_findings(run_dir: str, round_n: int | None = None, priors=None) -> list[dict]:
    late = "MEDIUM" if round_n is not None and round_n < 2 else "HIGH"
    out: list[dict] = []
    required, err = required_mechanisms(run_dir, priors)
    if err:
        out.append(_f("mechanism_priors_unavailable", "MEDIUM",
                      f"The run question cannot be read ({err}), so the mechanisms the matched priors require are "
                      "unknown; only priors the pre-mortem cites were checked."))
    entries, invalid, exists = _load_manifest(run_dir)
    out += invalid
    if entries is None and exists:
        return out              # malformed: entries unknown, already HIGH
    entries = entries or {}
    report = None
    rp = os.path.join(run_dir, "report.md")
    if os.path.isfile(rp):
        try:
            with open(rp, encoding="utf-8") as f:
                report = f.read()
        except (OSError, UnicodeDecodeError):
            report = None
    for pid, m in required:
        if m.id not in entries:
            out.append(_f("mechanism_unaccounted", late,
                          f"Prior `{pid}` requires mechanism `{m.id}` ({m.description[:200]}), and `{PATH}` has no "
                          f"entry for it. Record it as `modelled` (code_ref), `fixed` (source), or `simplified` "
                          f"(assumption + sufficiency-test result). Sufficiency test: {m.sufficiency_test[:300]}"))
    for mid, e in entries.items():
        out += _check_entry(run_dir, mid, e, late, report)
    return out


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    fails = []
    ep = _lib()

    def ok(c, m):
        if not c:
            fails.append(m)

    def w(d, rel, text):
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text if isinstance(text, str) else yaml.safe_dump(text))

    prior = ep._from_dict({
        "id": "widget_subgroups", "kind": "methodological_standard", "severity_if_unaddressed": "HIGH",
        "claim": "Widget response differs by subgroup.", "rationale": "r",
        "applies_when": {"keywords_any": ["widget"], "domain_any": []},
        "requires_mechanism": [{"id": "subgroup_mix", "description": "subgroup mix per unit",
                                "sufficiency_test": "vary the mix over its observed range",
                                "when_data_columns_any": ["mix_"]}]})
    P = [prior]
    kinds = lambda v: sorted(x["kind"] for x in v)
    with tempfile.TemporaryDirectory() as d:
        w(d, "metadata.json", json.dumps({"question": "Allocate the widget budget across units"}))
        w(d, "data/processed/units.csv", "unit,pop\nA,1\n")
        ok(gate_findings(d, 2, P) == [], f"MM0 trigger column absent → nothing required, got {gate_findings(d, 2, P)}")
        w(d, "data/raw/src.csv", "unit,mix_raw\nA,0.4\n")
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_unaccounted"], "MM0b trigger data under data/raw/ counts")
        os.remove(os.path.join(d, "data", "raw", "src.csv"))
        w(d, "data/processed/units.csv", "unit,pop,mix_a\nA,1,0.4\n")
        v = gate_findings(d, 2, P)
        ok(kinds(v) == ["mechanism_unaccounted"] and v[0]["severity"] == "HIGH",
           f"MM1 required mechanism with no manifest → HIGH, got {v}")
        ok(gate_findings(d, 1, P)[0]["severity"] == "MEDIUM", "MM1b round 1 is the drafting window (MEDIUM)")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "simplified", "assumption": "one mix"}]})
        v = gate_findings(d, 2, P)
        ok(kinds(v) == ["mechanism_sufficiency_missing"] and v[0]["severity"] == "HIGH",
           f"MM2 simplified with no result file → HIGH, got {v}")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "simplified", "assumption": "one mix",
                                    "sufficiency_test": "models/suff.yaml"}]})
        w(d, "models/suff.yaml", {"varied": "mix 0.1-0.9", "criterion": "package changes in >10% of units"})
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_sufficiency_missing"],
           "MM3 a result without a decision_changes verdict is not a result")
        w(d, "models/suff.yaml", {"varied": "mix 0.1-0.9", "criterion": "package changes in >10% of units",
                                  "decision_changes": False, "share_units_changed": 0.03, "share_threshold": 0.1})
        ok(gate_findings(d, 2, P) == [], f"MM4 simplified + passed test → no finding, got {gate_findings(d, 2, P)}")
        w(d, "report.md", "# Report\nNo mention.\n")
        v = gate_findings(d, 2, P)
        ok(kinds(v) == ["mechanism_test_not_cited"] and v[0]["severity"] == "MEDIUM",
           f"MM5 the report must cite the test (MEDIUM only), got {v}")
        w(d, "report.md", "# Report\nThe mix is simplified; see models/suff.yaml.\n")
        ok(gate_findings(d, 2, P) == [], "MM5b a citing report clears it")
        w(d, "models/suff.yaml", {"varied": "mix", "criterion": "c", "decision_changes": True,
                                  "share_units_changed": 0.3, "share_threshold": 0.1})
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_simplification_rejected"],
           "MM6 a failed test means the mechanism must be added")
        w(d, "models/suff.yaml", {"varied": "mix", "criterion": "c", "decision_changes": False,
                                  "share_units_changed": 0.3, "share_threshold": 0.1})
        ok("mechanism_manifest_invalid" in kinds(gate_findings(d, 2, P)), "MM7 a verdict contradicting its numbers")
        w(d, "models/model.py", "def mix_effect(u):\n    return u\n")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "modelled",
                                    "code_ref": "models/model.py::mix_effect"}]})
        ok(gate_findings(d, 2, P) == [], "MM8 modelled with a resolvable code_ref")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "modelled",
                                    "code_ref": "models/model.py::nope"}]})
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_ref_unresolved"], "MM9 unresolvable code_ref")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "modelled", "code_ref": "../../etc/passwd"}]})
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_ref_unresolved"], "MM9b a path outside the run")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "fixed", "source": "Registry: mix_share"}]})
        ok(gate_findings(d, 2, P) == [], "MM10 fixed with a source")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "ignored"}]})
        ok("mechanism_manifest_invalid" in kinds(gate_findings(d, 2, P)), "MM11 unknown status fails closed")
        w(d, PATH, "mechanisms: [")
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_manifest_invalid"], "MM12 unparseable manifest fails closed")
        os.remove(os.path.join(d, PATH))
        w(d, "metadata.json", json.dumps({"question": "Allocate the gadget budget"}))
        ok(gate_findings(d, 2, P) == [], "MM13 a non-matching question requires nothing")
        dom = ep._from_dict({
            "id": "widgetitis_mix", "kind": "methodological_standard", "severity_if_unaddressed": "HIGH",
            "claim": "c", "rationale": "r", "applies_when": {"keywords_any": ["nets"], "domain_any": ["widgetitis"]},
            "requires_mechanism": [{"id": "mix_by_group", "description": "d", "sufficiency_test": "t",
                                    "when_data_columns_any": ["mix_"]},
                                   {"id": "no_trigger", "description": "d", "sufficiency_test": "t"}]})
        w(d, "metadata.json", json.dumps({"question": "Allocate the widgetitis budget"}))
        v = gate_findings(d, 2, [dom])
        ok(kinds(v) == ["mechanism_unaccounted"] and "mix_by_group" in v[0]["claim"],
           f"MM13b domain match + trigger columns requires the triggered mechanism only, got {v}")
        w(d, "data/processed/units.csv", "unit,pop\nA,1\n")
        ok(gate_findings(d, 2, [dom]) == [], "MM13c domain match without trigger columns requires nothing")
        w(d, "data/processed/units.csv", "unit,pop,mix_a\nA,1,0.4\n")
        w(d, "metadata.json", json.dumps({"question": "Allocate the gadget budget"}))
        w(d, "pre_mortem.yaml", "concerns:\n  - id: PM-001\n    evidence_files: ['expert_prior:widget_subgroups']\n")
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_unaccounted"], "MM14 a prior the pre-mortem cites applies")
        w(d, "metadata.json", "{broken")
        ok("mechanism_priors_unavailable" in kinds(gate_findings(d, 2, P)), "MM15 unreadable question fails closed")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--round", type=int, default=None)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    req, err = required_mechanisms(a.run_dir)
    print(f"required mechanisms: {', '.join(f'{p}:{m.id}' for p, m in req) or 'none'}")
    v = gate_findings(a.run_dir, a.round)
    for x in v:
        print(f"[{x['severity']}] {x['kind']}: {x['claim'][:300]}")
    return 1 if any(x["severity"] == "HIGH" for x in v) else 0


if __name__ == "__main__":
    sys.exit(main())
