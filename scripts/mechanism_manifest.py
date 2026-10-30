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
      - id: <id>
        status: simplified               # the owner fixed the model's form
        assumption: "<what the model assumes instead>"
        specified_by: question           # or: brief (the signed decision brief)
        quote: "<the words of the question or brief that fix the form>"

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

When the owner's question or the signed brief itself fixes the model's form
(e.g. "a simple deterministic SIR model"), the simplification is the owner's
choice, not the modeler's: `specified_by` + a verbatim `quote` replace the
sufficiency test. The quote must appear in the run question or the signed
brief (whitespace- and case-insensitive), or the entry is invalid (HIGH). The
report must still state the limitation (MEDIUM, scope-declarable, if not).

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
  mechanism_scope_quote_unfound    HIGH  specified_by given, but the quote is not
                                         in the run question / signed brief
  mechanism_scope_not_stated       MEDIUM owner-specified simplification not stated
                                         in report.md (scope-declarable)
  mechanism_priors_unavailable     MEDIUM the run question cannot be read, so the
                                         required set is unknown
Never HIGH for leaving a feature out when its sufficiency test passed. None of
these are scope-declarable: a mechanism the run cannot model is `simplified`
with a test, or `fixed` with a source.

Phase 47 γ (D9): decision-layer features. The same file has a sibling section
for features of the decision layer: any rule, constraint, reserve, hedge, cap,
threshold or contingency added to the optimisation or the decision rule beyond
what the question or the signed brief specifies. E7 attempt 8 added a reserve
ladder, descoping rules, supply caps and a minimax hedge in rounds 3-5; its own
results showed the reserve's overrun tolerance never bound, and one rule read
information not available at its decision date. The rule is parsimony: show
the feature changes the recommendation, or take it out.

    decision_layer:                  # required once the run has an allocation;
      - id: price_reserve            #   `decision_layer: []` = none beyond the brief
        kind: decision_rule
        description: "<what the feature does>"
        in_recommended_plan: true    # is it applied in the recommended plan?
        sufficiency_test: models/sufficiency/price_reserve.yaml
        information_used:            # every input the rule reads, and when it is known
          - {input: "<input>", available: 2023-10}     # YYYY, YYYY-MM or YYYY-MM-DD
        decision_date: 2026-01       # optional: when this rule acts (default: the
                                     #   brief's `decision_date`, else metadata decision_year)
      - id: <id>
        kind: decision_rule
        description: "..."
        specified_by: brief          # or question: needs no test
        quote: "<the words that specify it>"
        information_used: []

The result file has the D1 format (`varied`: the recommendation with and
without the feature; `criterion`; `decision_changes`). A feature whose test
shows no change and that stays in the recommended plan must be removed, or
kept by a brief amendment that names it (`owner_amendment: AM-n`; the
amendment's change or reason names the feature id). Dates are compared at the
coarser of the two precisions: an input available in 2027 is later than a 2026
decision; one available in 2026 is not.

  decision_layer_undeclared        HIGH  the run has an allocation and the manifest
                                         has no `decision_layer:` list (MEDIUM at round 1)
  decision_feature_invalid         HIGH  malformed entry (kind, description, plan flag,
                                         information_used, dates, owner_amendment)
  decision_feature_untested        HIGH  neither specified_by nor a usable result file
                                         (MEDIUM at round 1)
  decision_feature_quote_unfound   HIGH  specified_by given, quote not in question/brief
  decision_feature_no_effect       HIGH  the test says the decision does not change, and
                                         the feature is still in the recommended plan
  decision_feature_future_information MEDIUM an input is available only after the
                                         decision date
  decision_layer_date_unknown      MEDIUM no decision date to check the inputs against
None of these are scope-declarable: a future input is fixed by re-tagging it or
by making the rule use what is known at the decision date. The gate cannot see
a feature the manifest does not list; the red-team and sufficiency critics
check the plan and decision_rule.md for features missing from `decision_layer:`.
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
_CODE_EXT = (".py", ".r", ".jl", ".ipynb")


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


def _read_doc(run_dir: str) -> tuple[dict | None, list[dict], bool]:
    """(parsed manifest, findings, exists). A manifest that holds only a
    `decision_layer:` list (no prior requires a mechanism) has no mechanisms."""
    path = os.path.join(run_dir, PATH)
    if not os.path.exists(path):
        return None, [], False
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, [_f("mechanism_manifest_invalid", "HIGH", f"`{PATH}` cannot be parsed: {e}")], True
    if isinstance(doc, dict) and "mechanisms" not in doc and "decision_layer" in doc:
        doc = dict(doc, mechanisms=[])
    if not isinstance(doc, dict) or not isinstance(doc.get("mechanisms"), list):
        return None, [_f("mechanism_manifest_invalid", "HIGH",
                         f"`{PATH}` must be a mapping with a `mechanisms:` list.")], True
    return doc, [], True


def _load_manifest(run_dir: str) -> tuple[dict | None, list[dict], bool]:
    """(entries by id, findings, exists)."""
    doc, out, exists = _read_doc(run_dir)
    if doc is None:
        return None, out, exists
    entries = {}
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


def _defines(text: str, sym: str, is_py: bool) -> bool:
    """`sym` (or the last part of a dotted name) is defined in the code: a
    def / class / assignment target, not a comment or a string (Codex review)."""
    name = sym.split(".")[-1]
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return False
    if is_py:
        import ast
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                return True
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else [])
            for t in targets:
                for n in ast.walk(t):
                    if (isinstance(n, ast.Name) and n.id == name) or (isinstance(n, ast.Attribute) and n.attr == name):
                        return True
        return False
    return re.search(r"^\s*(?:function\s+" + re.escape(name) + r"\b|" + re.escape(name)
                     + r"\s*(?:<-|=(?!=)))", text, re.M) is not None


def _load_result(path: str):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    return json.loads(text) if path.endswith(".json") else yaml.safe_load(text)


def _num(x):
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def _norm(t: str) -> str:
    return " ".join(str(t).lower().split())


def _owner_texts(run_dir: str) -> dict:
    """{'question': text, 'brief': text} the owner wrote or signed (missing ones are '')."""
    out = {"question": "", "brief": ""}
    q, _, _ = _lib().run_question(run_dir)
    out["question"] = q or ""
    parts = []
    for rel in ("decision_brief.yaml", "decision_brief.md"):
        p = os.path.join(run_dir, rel)
        if os.path.isfile(p):
            try:
                with open(p, encoding="utf-8") as f:
                    parts.append(f.read())
            except (OSError, UnicodeDecodeError):
                pass
    out["brief"] = "\n".join(parts)
    return out


def _quote_found(run_dir: str, by: str, quote) -> bool:
    """The quote (3+ words) appears in the run's question or signed brief."""
    q = _norm(quote) if isinstance(quote, str) else ""
    return len(q.split()) >= 3 and q in _norm(_owner_texts(run_dir)[by])


def _test_result(run_dir: str, rel) -> tuple[dict | None, str | None]:
    """(result, problem) for a sufficiency-test result file named by `rel`."""
    p = _inside(run_dir, rel) if isinstance(rel, str) else None
    if p is None:
        return None, f"names no existing result file (`sufficiency_test: {rel!r}`)"
    try:
        res = _load_result(p)
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        return None, f"has a result file that cannot be parsed ({exc})"
    if not isinstance(res, dict):
        return None, "has a result file that is not a mapping"
    if not isinstance(res.get("decision_changes"), bool):
        return None, "has a result file without a `decision_changes: true|false` verdict"
    if not res.get("varied") or not (isinstance(res.get("criterion"), str) and res["criterion"].strip()):
        return None, "has a result file that does not state what was `varied` and the decision `criterion`"
    return res, None


def _result_conflicts(rel: str, res: dict, label: str, restated) -> list[dict]:
    """A result whose share numbers or restated verdict contradict its verdict."""
    out = []
    share, thr = _num(res.get("share_units_changed")), _num(res.get("share_threshold"))
    given = [k for k in ("share_units_changed", "share_threshold") if res.get(k) is not None]
    if given and (share is None or thr is None or not 0 <= share <= 1 or not 0 <= thr <= 1):
        out.append(_f("mechanism_manifest_invalid", "HIGH",
                      f"`{rel}`: share_units_changed and share_threshold must both be fractions in [0, 1] when "
                      f"either is given (got {res.get('share_units_changed')!r}, {res.get('share_threshold')!r})."))
    if share is not None and thr is not None and (share > thr) != res["decision_changes"]:
        out.append(_f("mechanism_manifest_invalid", "HIGH",
                      f"`{rel}`: share_units_changed {share} vs share_threshold {thr} contradicts "
                      f"decision_changes: {res['decision_changes']}."))
    if isinstance(restated, bool) and restated != res["decision_changes"]:
        out.append(_f("mechanism_manifest_invalid", "HIGH",
                      f"`{PATH}`: {label} restates decision_changes: {restated}, but its "
                      f"result file `{rel}` says {res['decision_changes']}."))
    return out


def _check_scope(run_dir: str, mid: str, e: dict, report: str | None) -> list[dict] | None:
    """Owner-specified simplification: None when the entry does not claim one."""
    by = str(e.get("specified_by", "")).strip().lower()
    if not by:
        return None
    out = []
    quote = e.get("quote")
    if by not in ("question", "brief"):
        return [_f("mechanism_manifest_invalid", "HIGH",
                   f"`{PATH}`: mechanism `{mid}` has specified_by {e.get('specified_by')!r}; use `question` or `brief`.")]
    if not isinstance(e.get("assumption"), str) or not e["assumption"].strip():
        out.append(_f("mechanism_manifest_invalid", "HIGH",
                      f"`{PATH}`: mechanism `{mid}` is `simplified` but states no `assumption:`."))
    q = _norm(quote) if isinstance(quote, str) else ""
    if not _quote_found(run_dir, by, quote):
        out.append(_f("mechanism_scope_quote_unfound", "HIGH",
                      f"`{PATH}`: mechanism `{mid}` is simplified as specified by the {by}, but its `quote` "
                      f"({str(quote)[:120]!r}) is not found verbatim (3+ words) in the run's {by}. Quote the words "
                      "that fix the model's form, or give a sufficiency test instead."))
    elif report is not None and mid not in report and q not in _norm(report):
        out.append({**_f("mechanism_scope_not_stated", "MEDIUM",
                         f"report.md does not state the owner-specified simplification of `{mid}` "
                         f"({e.get('assumption', '')[:120]}). State it as a limitation of the requested model.", "WRITE"),
                    "target_stage": "WRITE", "scope_declarable": True})
    return out


def _check_entry(run_dir: str, mid: str, e: dict, late: str, report: str | None) -> list[dict]:
    out = []
    st = e["status"]
    if st == "simplified":
        scoped = _check_scope(run_dir, mid, e, report)
        if scoped is not None:
            return scoped
    if st == "modelled":
        ref = e.get("code_ref")
        fname, _, sym = (ref if isinstance(ref, str) else "").partition("::")
        p = _inside(run_dir, fname)
        found = False
        # Code, and a named function / class / variable in it (prose or data
        # files do not show a mechanism is modelled; Codex review).
        if p is not None and not (p.lower().endswith(_CODE_EXT) and sym.strip()):
            p = None
        if p is not None:
            try:
                with open(p, encoding="utf-8") as f:
                    text = f.read()
                found = _defines(text, sym.strip(), p.lower().endswith(".py"))
            except (OSError, UnicodeDecodeError):
                found = False
        if not found:
            out.append(_f("mechanism_ref_unresolved", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` is `modelled` but `code_ref: {ref!r}` does not name an "
                          "existing code file of the run and, after `::`, a function or name defined in it "
                          "(`models/<file>.py::<name>`)."))
    elif st == "fixed":
        if not isinstance(e.get("source"), str) or len(e["source"].strip()) < 3:
            out.append(_f("mechanism_ref_unresolved", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` is `fixed` but gives no `source:` for the fixed value."))
    else:
        if not isinstance(e.get("assumption"), str) or not e["assumption"].strip():
            out.append(_f("mechanism_manifest_invalid", "HIGH",
                          f"`{PATH}`: mechanism `{mid}` is `simplified` but states no `assumption:`."))
        rel = e.get("sufficiency_test")
        res, problem = _test_result(run_dir, rel)
        if problem:
            out.append(_f("mechanism_sufficiency_missing", late,
                          f"`{PATH}`: mechanism `{mid}` is `simplified` but {problem}. Vary the assumption over its "
                          "plausible range and record whether the decision changes (the package for a stated share "
                          "of units, or the headline beyond its uncertainty interval)."))
            return out
        out += _result_conflicts(rel, res, f"mechanism `{mid}`", e.get("decision_changes"))
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


# --------------------------------------------------------------------------- D9: decision layer

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$")
_LAYER_HOWTO = (
    "Each entry: `id`, `kind: decision_rule`, `description`, `in_recommended_plan: true|false`, "
    "`sufficiency_test: models/sufficiency/<id>.yaml` (a result file with `varied`: the recommendation with and "
    "without the feature, `criterion`, `decision_changes: true|false`), and `information_used: [{input: ..., "
    "available: YYYY-MM}]`. A feature the question or signed brief specifies takes `specified_by: question|brief` "
    "and a verbatim `quote:` instead of the test.")


def _date(v) -> tuple[int, ...] | None:
    """(Y,), (Y, M) or (Y, M, D) from YYYY, YYYY-MM, YYYY-MM-DD or a YAML date."""
    import datetime as dt
    if isinstance(v, bool):
        return None
    if isinstance(v, dt.date):
        return (v.year, v.month, v.day)
    if isinstance(v, int):
        return (v,) if 1900 <= v <= 2200 else None
    m = _DATE_RE.match(v.strip()) if isinstance(v, str) else None
    if not m:
        return None
    parts = tuple(int(x) for x in m.groups() if x is not None)
    if (len(parts) > 1 and not 1 <= parts[1] <= 12) or (len(parts) > 2 and not 1 <= parts[2] <= 31):
        return None
    return parts


def _fmt(d: tuple) -> str:
    return "-".join([str(d[0])] + [f"{x:02d}" for x in d[1:]])


def _later(a: tuple, b: tuple) -> bool:
    """`a` is after `b`, compared at the coarser of the two precisions."""
    n = min(len(a), len(b))
    return a[:n] > b[:n]


def run_decision_date(run_dir: str) -> tuple[tuple | None, str]:
    """(date, source) of the decision: decision_brief.yaml `decision_date`, else the
    run's decision year (metadata.json). (None, reason) when neither is known."""
    p = os.path.join(run_dir, "decision_brief.yaml")
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                doc = yaml.safe_load(f)
        except (OSError, UnicodeDecodeError, yaml.YAMLError):
            doc = None
        if isinstance(doc, dict) and doc.get("decision_date") is not None:
            d = _date(doc["decision_date"])
            if d is None:
                return None, f"decision_brief.yaml decision_date {doc['decision_date']!r} is not YYYY[-MM[-DD]]"
            return d, "decision_brief.yaml decision_date"
    _, year, err = _lib().run_question(run_dir)
    if year:
        return (int(year),), "the run's decision year (metadata.json)"
    return None, err or "decision_brief.yaml has no decision_date and metadata.json no decision_year"


def _amendment_names(run_dir: str, aid, fid: str) -> bool:
    """brief_amendments.yaml has a valid amendment `aid` whose text names `fid`."""
    if not isinstance(aid, str) or not aid.strip():
        return False
    ok, _errors = _sibling("decision_brief")._amendments(run_dir)
    for am in ok:
        if str(am.get("id", "")).strip() == aid.strip():
            return fid.lower() in f"{am.get('change', '')} {am.get('reason', '')}".lower()
    return False


def decision_layer_findings(run_dir: str, round_n: int | None = None, allocation: bool | None = None) -> list[dict]:
    """D9: every decision-layer feature beyond the question and brief changes the
    recommendation, and reads only what is known at its decision date."""
    late = "MEDIUM" if round_n is not None and round_n < 2 else "HIGH"
    doc, bad, _exists = _read_doc(run_dir)
    if bad:
        return []              # reported by gate_findings as mechanism_manifest_invalid
    if doc is None or "decision_layer" not in doc:
        if allocation is None:
            allocation = _sibling("alloc_toolkit_use").allocation_present(run_dir)
        if not allocation:
            return []
        return [_f("decision_layer_undeclared", late,
                   f"This run produces an allocation, and `{PATH}` has no `decision_layer:` list. Under "
                   "`decision_layer:`, list every rule, constraint, reserve, hedge, cap, threshold or contingency "
                   "that the optimisation or the decision rule adds beyond the question and the signed brief. "
                   + _LAYER_HOWTO + " Write `decision_layer: []` if the plan adds nothing beyond them.")]
    layer = doc["decision_layer"]
    if not isinstance(layer, list):
        return [_f("decision_feature_invalid", "HIGH", f"`{PATH}`: `decision_layer:` must be a list. " + _LAYER_HOWTO)]
    out, seen = [], set()
    run_date, date_src = None, None
    for i, e in enumerate(layer):
        if not isinstance(e, dict) or not isinstance(e.get("id"), str) or not e["id"].strip():
            out.append(_f("decision_feature_invalid", "HIGH", f"`{PATH}` decision_layer[{i}] has no `id:`."))
            continue
        fid = e["id"].strip()
        if fid in seen:
            out.append(_f("decision_feature_invalid", "HIGH", f"`{PATH}`: decision feature `{fid}` is listed twice."))
            continue
        seen.add(fid)
        by = str(e.get("specified_by") or "").strip().lower()
        need = []
        if str(e.get("kind", "")).strip() != "decision_rule":
            need.append("`kind: decision_rule`")
        if not (isinstance(e.get("description"), str) and e["description"].strip()):
            need.append("a `description`")
        if by and by not in ("question", "brief"):
            need.append("`specified_by: question` or `brief`")
        if not by and not isinstance(e.get("in_recommended_plan"), bool):
            need.append("`in_recommended_plan: true|false`")
        own_date = _date(e["decision_date"]) if e.get("decision_date") is not None else None
        if e.get("decision_date") is not None and own_date is None:
            need.append("a `decision_date` as YYYY, YYYY-MM or YYYY-MM-DD")
        info, dated = e.get("information_used"), []
        if not isinstance(info, list):
            need.append("an `information_used:` list (`[]` if the rule reads no input)")
        else:
            for it in info:
                d = _date(it.get("available")) if isinstance(it, dict) else None
                if d is None or not (isinstance(it.get("input"), str) and it["input"].strip()):
                    need.append(f"`input` and `available: YYYY[-MM[-DD]]` on information_used item {it!r:.80}")
                else:
                    dated.append((it["input"].strip(), d))
        if need:
            out.append(_f("decision_feature_invalid", "HIGH",
                          f"`{PATH}`: decision feature `{fid}` needs " + "; ".join(need) + ". " + _LAYER_HOWTO))
        if dated:
            when = own_date
            if when is None:
                if date_src is None:
                    run_date, date_src = run_decision_date(run_dir)
                    if run_date is None:
                        out.append(_f("decision_layer_date_unknown", "MEDIUM",
                                      f"No decision date to check the decision features' information against "
                                      f"({date_src}). Add `decision_date: YYYY-MM` to decision_brief.yaml, or a "
                                      f"`decision_date` to each feature in `{PATH}`."))
                when = run_date
            late_inputs = [f"{n} (available {_fmt(d)})" for n, d in dated
                           if when is not None and _later(d, when)]
            if late_inputs:
                at = _fmt(when)
                out.append(_f("decision_feature_future_information", "MEDIUM",
                              f"`{PATH}`: decision feature `{fid}` reads information available only after its decision "
                              f"date {at}: {'; '.join(late_inputs)[:400]}. Make the rule use what is known at {at} (a "
                              "forecast or the last observed value), or, if it acts at a later decision point, give "
                              "the feature that `decision_date`."))
        if by in ("question", "brief"):
            if not _quote_found(run_dir, by, e.get("quote")):
                out.append(_f("decision_feature_quote_unfound", "HIGH",
                              f"`{PATH}`: decision feature `{fid}` is specified by the {by}, but its `quote` "
                              f"({str(e.get('quote'))[:120]!r}) is not found verbatim (3+ words) in the run's {by}. "
                              "Quote the words that specify the feature, or give a sufficiency test instead."))
            continue
        rel = e.get("sufficiency_test")
        res, problem = _test_result(run_dir, rel)
        if problem:
            out.append(_f("decision_feature_untested", late,
                          f"`{PATH}`: decision feature `{fid}` is not specified by the question or brief, and it "
                          f"{problem}. Solve the decision with and without the feature and record, in that file, "
                          "`varied` (with vs without), the `criterion` for a decision change (the package for a "
                          "stated share of units, or the headline beyond its uncertainty interval) and "
                          "`decision_changes: true|false`."))
            continue
        out += _result_conflicts(rel, res, f"decision feature `{fid}`", e.get("decision_changes"))
        if not res["decision_changes"] and e.get("in_recommended_plan") is True \
                and not _amendment_names(run_dir, e.get("owner_amendment"), fid):
            am = e.get("owner_amendment")
            out.append(_f("decision_feature_no_effect", "HIGH",
                          f"`{PATH}`: decision feature `{fid}` does not change the recommendation (`{rel}`: "
                          "decision_changes: false), and it is still in the recommended plan. Remove it from the "
                          "plan and the decision rule (then set `in_recommended_plan: false`), or keep it through "
                          "a brief amendment in brief_amendments.yaml whose change or reason names "
                          f"`{fid}`, cited as `owner_amendment: AM-n`"
                          + (f" (`owner_amendment: {am!r}` is not a valid amendment naming `{fid}`)." if am else ".")))
    return out


def gate_findings(run_dir: str, round_n: int | None = None, priors=None,
                  allocation: bool | None = None) -> list[dict]:
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
    return out + decision_layer_findings(run_dir, round_n, allocation)


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
        w(d, "models/suff.yaml", {"varied": "mix", "criterion": "c", "decision_changes": False,
                                  "share_units_changed": 0.8, "share_threshold": "0.1"})
        ok("mechanism_manifest_invalid" in kinds(gate_findings(d, 2, P)), "MM7b a non-numeric threshold fails closed")
        w(d, "models/suff.yaml", {"varied": "mix", "criterion": "c", "decision_changes": False,
                                  "share_units_changed": 0.8, "share_threshold": 10})
        ok("mechanism_manifest_invalid" in kinds(gate_findings(d, 2, P)), "MM7c a percentage threshold is invalid")
        w(d, "models/model.py", "def mix_effect(u):\n    return u\n")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "modelled",
                                    "code_ref": "models/model.py::mix_effect"}]})
        ok(gate_findings(d, 2, P) == [], "MM8 modelled with a resolvable code_ref")
        w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "modelled",
                                    "code_ref": "models/model.py::nope"}]})
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_ref_unresolved"], "MM9 unresolvable code_ref")
        w(d, "models/stub.py", "# TODO: implement mix_effect\nX = 'mix_effect'\n")
        for bad in ("report.md", "models/model.py", "models/suff.yaml::varied", "models/stub.py::mix_effect"):
            w(d, PATH, {"mechanisms": [{"id": "subgroup_mix", "status": "modelled", "code_ref": bad}]})
            ok(kinds(gate_findings(d, 2, P)) == ["mechanism_ref_unresolved"],
               f"MM9c code_ref {bad!r} (prose, no name, or not code) does not show the mechanism is modelled")
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
    # Owner-specified simplification: the question (or signed brief) fixes the model's form.
    with tempfile.TemporaryDirectory() as d:
        w(d, "metadata.json", json.dumps({"question": "Using a simple deterministic widget model, allocate the "
                                                      "widget budget across units"}))
        w(d, "data/processed/units.csv", "unit,pop,mix_a\nA,1,0.4\n")
        ent = {"id": "subgroup_mix", "status": "simplified", "assumption": "one mix for all units",
               "specified_by": "question", "quote": "a  Simple deterministic WIDGET model"}
        w(d, PATH, {"mechanisms": [ent]})
        ok(gate_findings(d, 2, P) == [], f"MM16 a simplification the question specifies needs no test: "
                                         f"{gate_findings(d, 2, P)}")
        w(d, PATH, {"mechanisms": [dict(ent, quote="an agent-based widget model")]})
        v = gate_findings(d, 2, P)
        ok(kinds(v) == ["mechanism_scope_quote_unfound"] and v[0]["severity"] == "HIGH",
           f"MM16b a quote not in the question is HIGH: {v}")
        w(d, PATH, {"mechanisms": [dict(ent, quote="widget model")]})
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_scope_quote_unfound"], "MM16c a quote of under 3 words is not enough")
        w(d, PATH, {"mechanisms": [dict(ent, specified_by="modeler")]})
        ok(kinds(gate_findings(d, 2, P)) == ["mechanism_manifest_invalid"], "MM16d specified_by must be question or brief")
        w(d, PATH, {"mechanisms": [ent]})
        w(d, "report.md", "# Report\nResults.\n")
        v = gate_findings(d, 2, P)
        ok(kinds(v) == ["mechanism_scope_not_stated"] and v[0]["scope_declarable"] and v[0]["severity"] == "MEDIUM",
           f"MM16e the report must state the owner-specified limitation (MEDIUM, declarable): {v}")
        w(d, "report.md", "# Report\nAs requested, a simple deterministic widget model: subgroup_mix is one mix.\n")
        ok(gate_findings(d, 2, P) == [], "MM16f stated in the report: nothing")
        os.remove(os.path.join(d, "report.md"))
        w(d, "decision_brief.yaml", yaml.safe_dump({"decision": "Use one pooled subgroup mix for every unit."}))
        w(d, PATH, {"mechanisms": [dict(ent, specified_by="brief", quote="one pooled subgroup mix")]})
        ok(gate_findings(d, 2, P) == [], "MM16g the signed brief can specify the simplification")
    # Phase 47 γ (D9): decision-layer features.
    with tempfile.TemporaryDirectory() as d:
        w(d, "metadata.json", json.dumps({"question": "Allocate the gadget budget across units with a budget cap",
                                          "decision_year": 2024}))
        w(d, "decision_brief.yaml", yaml.safe_dump({"constraints": [{"id": "C1", "text": "Total cost at most the "
                                                                     "stated envelope in every year."}]}))
        ok(gate_findings(d, 2, []) == [], "DL0 no allocation and no manifest: nothing required")
        w(d, "models/allocation_plan.csv", "unit,package\nA,x\n")
        v = gate_findings(d, 2, [])
        ok(kinds(v) == ["decision_layer_undeclared"] and v[0]["severity"] == "HIGH" and not v[0]["scope_declarable"]
           and "decision_layer: []" in v[0]["claim"], f"DL1 an allocation with no decision_layer list → HIGH: {v}")
        ok(gate_findings(d, 1, [])[0]["severity"] == "MEDIUM", "DL1b round 1 is the drafting window")
        w(d, PATH, {"mechanisms": [], "decision_layer": []})
        ok(gate_findings(d, 2, []) == [], "DL2 `decision_layer: []` declares nothing beyond the brief")
        w(d, PATH, {"decision_layer": []})
        ok(gate_findings(d, 2, []) == [], "DL2b a manifest with only decision_layer (no mechanisms) is valid")
        feat = {"id": "price_reserve", "kind": "decision_rule", "description": "hold back a contingency reserve",
                "in_recommended_plan": True, "sufficiency_test": "models/sufficiency/price_reserve.yaml",
                "information_used": [{"input": "unit price list", "available": "2023-10"}]}
        w(d, PATH, {"decision_layer": [{k: v for k, v in feat.items() if k != "sufficiency_test"}]})
        v = gate_findings(d, 2, [])
        ok(kinds(v) == ["decision_feature_untested"] and v[0]["severity"] == "HIGH",
           f"DL3 a declared feature with no test result → HIGH: {v}")
        w(d, PATH, {"decision_layer": [feat]})
        w(d, "models/sufficiency/price_reserve.yaml", {"varied": "the plan with and without the reserve",
                                                       "criterion": "package changes in >5% of units",
                                                       "decision_changes": False, "share_units_changed": 0.0,
                                                       "share_threshold": 0.05})
        v = gate_findings(d, 2, [])
        ok(kinds(v) == ["decision_feature_no_effect"] and v[0]["severity"] == "HIGH" and not v[0]["scope_declarable"],
           f"DL4 a feature whose test shows no change, still in the plan → HIGH: {v}")
        w(d, PATH, {"decision_layer": [dict(feat, in_recommended_plan=False)]})
        ok(gate_findings(d, 2, []) == [], "DL4b removed from the plan: the entry stays as the record, no finding")
        w(d, "brief_amendments.yaml", {"amendments": [{"id": "AM-1", "item": "C1", "change": "keep price_reserve "
                                       "in the plan", "reason": "owner wants the reserve although it changes nothing"}]})
        w(d, PATH, {"decision_layer": [dict(feat, owner_amendment="AM-1")]})
        ok(gate_findings(d, 2, []) == [], "DL4c kept by a brief amendment that names it")
        w(d, PATH, {"decision_layer": [dict(feat, owner_amendment="AM-2")]})
        ok(kinds(gate_findings(d, 2, [])) == ["decision_feature_no_effect"], "DL4d an unknown amendment does not keep it")
        w(d, "models/sufficiency/price_reserve.yaml", {"varied": "with and without", "criterion": "c",
                                                       "decision_changes": True})
        w(d, PATH, {"decision_layer": [feat]})
        ok(gate_findings(d, 2, []) == [], f"DL5 a feature that changes the decision: none {gate_findings(d, 2, [])}")
        w(d, PATH, {"decision_layer": [dict(feat, decision_changes=False)]})
        ok(kinds(gate_findings(d, 2, [])) == ["mechanism_manifest_invalid"], "DL5b a restated verdict must match")
        brief_feat = {"id": "annual_cap", "kind": "decision_rule", "description": "cost cap per year",
                      "specified_by": "brief", "quote": "at most the stated envelope in every year",
                      "information_used": []}
        w(d, PATH, {"decision_layer": [brief_feat]})
        ok(gate_findings(d, 2, []) == [], "DL6 a brief-specified feature needs no test")
        w(d, PATH, {"decision_layer": [dict(brief_feat, quote="a cap the brief never mentions")]})
        ok(kinds(gate_findings(d, 2, [])) == ["decision_feature_quote_unfound"], "DL6b its quote must be in the brief")
        w(d, PATH, {"decision_layer": [dict(feat, information_used=[{"input": "realised year-3 costs",
                                                                       "available": 2027}])]})
        v = gate_findings(d, 2, [])
        ok(kinds(v) == ["decision_feature_future_information"] and v[0]["severity"] == "MEDIUM"
           and not v[0]["scope_declarable"], f"DL7 post-decision information → MEDIUM: {v}")
        w(d, PATH, {"decision_layer": [dict(feat, decision_date="2027-06", information_used=[
            {"input": "realised year-3 costs", "available": 2027}])]})
        ok(gate_findings(d, 2, []) == [], "DL7b same year at the coarser precision is not later")
        w(d, PATH, {"decision_layer": [dict(feat, information_used=[{"input": "survey", "available": 2024}])]})
        ok(gate_findings(d, 2, []) == [], "DL7c available in the decision year is not later")
        w(d, "decision_brief.yaml", yaml.safe_dump({"decision_date": "2023-06"}))
        ok(kinds(gate_findings(d, 2, [])) == ["decision_feature_future_information"],
           "DL7d the brief's decision_date takes precedence over the decision year")
        w(d, "decision_brief.yaml", "{}\n")
        w(d, "metadata.json", json.dumps({"question": "Allocate the gadget budget"}))
        ok("decision_layer_date_unknown" in kinds(gate_findings(d, 2, [])), "DL7e no decision date: MEDIUM")
        for bad in ({"decision_layer": {"price_reserve": feat}},
                    {"decision_layer": [dict(feat, kind="mechanism")]},
                    {"decision_layer": [{k: v for k, v in feat.items() if k != "information_used"}]},
                    {"decision_layer": [dict(feat, information_used=[{"input": "prices", "available": "soon"}])]},
                    {"decision_layer": [{k: v for k, v in feat.items() if k != "in_recommended_plan"}]},
                    {"decision_layer": [feat, feat]}):
            w(d, PATH, bad)
            ok("decision_feature_invalid" in kinds(gate_findings(d, 2, [])), f"DL8 malformed entry fails closed: {bad}")
        w(d, PATH, "decision_layer: [")
        ok(kinds(gate_findings(d, 2, [])) == ["mechanism_manifest_invalid"], "DL9 unparseable manifest fails closed")
        w(d, PATH, {"decision_layer": [feat]})
        w(d, "models/sufficiency/price_reserve.yaml", "varied: [")
        ok("decision_feature_untested" in kinds(gate_findings(d, 2, [])), "DL9b unparseable result fails closed")
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
