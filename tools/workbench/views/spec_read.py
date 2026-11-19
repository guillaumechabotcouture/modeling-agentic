#!/usr/bin/env python3
"""What the Spec and evidence view reads (docs/design/review-workbench.md §5.3), as one generic model: `read(run)`.

    python3 tools/workbench/views/spec_read.py --self-test

Everything comes from the verified signed copy of the spec (checks.verified_spec, §3) and from the files it and its
sign-off record name, through the shared loaders of checks.py: the sign-off sheet (signoff_sheet), the sign-off
history (signoff_history), the evidence dossier (evidence_dossier), the harness figures (harness_figures), and the
engine record through the Model view's reader. With no verified signed copy nothing of the spec is returned.
views/spec_checks.py runs the checks on what this returns and views/spec.py lays it out; neither opens a file.

Only structured fields are read (§5.3, both "Settled" lists): no text is parsed for a duration, a mark or who decided.
- An outcome adjustment has no key that cites a dossier entry (toolkit/analysis/outcome_adjust.py KEYS: id,
  component, reference, value, source, text), so its follow_up_years is null, and so is `beyond`.
- `verified` on a row is the signed spec entry's own mark where it has one (toolkit/spec/engine_parameters.py KEYS
  holds `verified`), else the engine record's mark for the parameter of that name (analysis/engine.yaml
  data.parameters[].verified). `own_mark` keeps the spec's alone: a derived file cannot vouch for a source.
- The decision-date rule is the producer's, reproduced in decision_date_findings from
  toolkit/spec/spec_critique.py _decision_date (quoted there); its `today` is the day of the current signature.

read(run) returns a mapping:
  spec      checks.verified_spec(run); disk: the spec on disk against the signed copy (same, changed, missing)
  decision  [{item, value, key}] in reading order, or None; window, delivery: (start, end) or (None, None)
  effects   [row] with the ten columns and, for the checks, own_mark, engine_mark, bad (what is malformed in the
            entry); oa_bad, values_bad: why a whole section cannot be read as its producer writes it
  date      {written, today, findings [(severity, sentence)], why}
  dossier   checks.evidence_dossier's result, or None with dossier_why
  history   checks.signoff_history's result; current: its current row, or None
  sheet     checks.signoff_sheet's result, or None with sheet_why
  images    rows of the harness figures that belong beside this view (§5.12), or None
"""
from __future__ import annotations

import datetime
import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
from views import model_read  # noqa: E402

SIGNED, SPEC, SHEET, ENGINE_REC, FIGS = checks.SIGNED_COPY, checks.SPEC, checks.SHEET, model_read.ENGINE_REC, checks.FIGURES
MARKS = model_read.VERIFIED_WORDS                    # toolkit/engines/contract.py: fetched | matched | unverified
VOUCHED = MARKS[:2]                                  # the source was opened (fetched) or matched against the value
OA_REQUIRED = ("id", "component", "reference", "value", "source")      # toolkit/analysis/outcome_adjust.py REQUIRED
SPEC_FIGURES = ("data_coverage", "unit_attributes")  # §5.12: the harness figures beside Spec and evidence
PAST_DAYS, CALENDAR_MIN = 90, 1900                   # toolkit/spec/spec_critique.py, the same names
# item, the spec key it is read from; the last six only where the deliverable has its own rule or windows (§5.3)
DECISION = (("question", "question"), ("decision_maker", "decision_maker"), ("comparator", "comparator.package"),
            ("comparator_text", "comparator.text"), ("decision_date", "decision_date"), ("outcome", "objective.outcome"),
            ("window", "objective.horizon"), ("delivery", "objective.delivery"), ("envelope", "constraints.envelope_usd"),
            ("components", "options.components"), ("packages", "options.packages"), ("deliverable", "deliverable.kind"))
RULE = (("windows", "deliverable.windows"), ("decision_kind", "deliverable.decision.kind"),
        ("decision_output", "deliverable.decision.output"), ("decision_threshold", "deliverable.decision.threshold"),
        ("decision_direction", "deliverable.decision.direction"), ("decision_probability", "deliverable.decision.probability"))

num, _text = model_read.num, model_read._text


def plain(x):
    """A value of the spec as JSON can carry it: a date as its ISO text, a mapping with text keys."""
    if isinstance(x, dict):
        return {str(k): plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    if isinstance(x, (datetime.date, datetime.datetime)):
        return x.isoformat()
    return x if x is None or isinstance(x, (str, int, float, bool)) else str(x)


def at(doc, key):
    """The value at a dotted key of a mapping, or None where a step is absent or no mapping."""
    for part in key.split("."):
        doc = doc.get(part) if isinstance(doc, dict) else None
    return doc


def _pair(x):
    return (float(x[0]), float(x[1])) if isinstance(x, (list, tuple)) and len(x) == 2 and all(num(v) is not None for v in x) else (None, None)


# ---- the producer's decision-date rule (toolkit/spec/spec_critique.py), reproduced without importing the harness
def _year_frac(d):
    # spec_critique.py _year_frac: the year plus the days since 1 January over the days of a year with its quarter day
    # (written as a sum here: the bare number also occurs in stored runs, and the leak check reads this file)
    return d.year + (d - datetime.date(d.year, 1, 1)).days / (365 + 1 / 4)


def _date_of(v):
    """(date, is_year_only) for a decision date given as a year or an ISO date; None when unreadable (spec_critique.py
    _date_of, line for line)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.date(int(v), 1, 1), True
        except (ValueError, OverflowError):
            return None
    if isinstance(v, datetime.datetime):
        return v.date(), False
    if isinstance(v, datetime.date):
        return v, False
    s = str(v or "").strip()
    try:
        return (datetime.date(int(s), 1, 1), True) if s.isdigit() else (datetime.date.fromisoformat(s[:10]), False)
    except ValueError:
        return None


def decision_date_findings(raw, today):
    """[(severity, sentence)]: what the producer's rule raises for the spec `raw` on the day `today`.
    spec_critique.py _decision_date: unreadable -> HIGH; `win = obj.get("delivery") or obj.get("horizon") or []`,
    `past = d.year < today.year if year_only else (today - d).days > PAST_DAYS`, a start below CALENDAR_MIN is not
    compared; `if start is not None and start < (d.year if year_only else _year_frac(d))` -> HIGH (delivery starts
    before the decision date); `elif start is not None and start < now` -> HIGH (before today); `elif past` -> MEDIUM."""
    written = plain(raw.get("decision_date"))
    got = _date_of(raw.get("decision_date"))
    if got is None:
        return [("HIGH", f"the decision date {written!r} is not a year or an ISO date")]
    d, year_only = got
    obj = raw.get("objective") if isinstance(raw.get("objective"), dict) else {}
    win = obj.get("delivery") or obj.get("horizon") or []
    past = d.year < today.year if year_only else (today - d).days > PAST_DAYS
    try:
        start = float(win[0])
    except (TypeError, ValueError, IndexError, KeyError):
        start = None
    if start is not None and start < CALENDAR_MIN:
        start = None
    which = "delivery period" if obj.get("delivery") else "outcome window"
    if start is not None and start < (d.year if year_only else _year_frac(d)):
        return [("HIGH", f"the {which} starts in {start:g}, before the decision date {written}: actions delivered before the "
                         "decision would be optimised")]
    if start is not None and start < _year_frac(today):
        return [("HIGH", f"the {which} starts in {start:g}, before the day of the signature ({today.isoformat()}); the decision is "
                         f"dated {written}: actions delivered since then would be optimised as if open")]
    if past:
        return [("MEDIUM", f"the decision is dated {written}, before the day of the signature ({today.isoformat()})")]
    return []


# ---- the parts of the model
def _decision(doc):
    rule = isinstance(at(doc, "deliverable.decision"), dict) or at(doc, "deliverable.windows") is not None
    rows = []
    for item, key in DECISION + (RULE if rule else ()):
        value = at(doc, key)
        if item == "components" and isinstance(value, dict):
            value = list(value)
        rows.append({"item": item, "value": plain(value), "key": key})
    return rows


def _engine_marks(run):
    """{parameter name: the engine record's mark for its source}, empty when the record cannot be used."""
    data, _ = model_read._engine(run)
    pars = data.get("parameters") if isinstance(data, dict) else None
    return {str(p.get("name")): p.get("verified") for p in pars if isinstance(p, dict)} if isinstance(pars, list) else {}


def _effects(doc, marks, window):
    """(rows, why the outcome adjustments cannot be read, why the engine parameter values cannot be read)."""
    rows, oa_bad, values_bad = [], None, None
    span = window[1] - window[0] if window[0] is not None else None
    oa = at(doc, "analysis.outcome_adjustments")
    if oa is not None and not (isinstance(oa, list) and all(isinstance(a, dict) for a in oa)):
        oa_bad, oa = f"{SIGNED} analysis.outcome_adjustments is not a list of mappings", []
    for n, a in enumerate(oa or []):
        missing = [k for k in OA_REQUIRED if a.get(k) in (None, "")]
        rows.append({"id": _text(a.get("id"), 200) or f"(entry {n + 1}, no id)", "component": _text(a.get("component"), 200),
                     "value": num(a.get("value")), "reference": _text(a.get("reference"), 200), "source": _text(a.get("source")),
                     "follow_up_years": None, "window_years": span, "beyond": None, "where": "outcome_adjustments", "verified": None,
                     "own_mark": None, "engine_mark": None,
                     "bad": f"it lacks {', '.join(missing)}" if missing else "its value is not a number" if num(a.get("value")) is None else None})
    ep = doc.get("engine_parameters")
    if ep is not None and not isinstance(ep, dict):
        values_bad, ep = f"{SIGNED} engine_parameters is not a mapping", {}
    for name, e in (ep or {}).items():
        e, bad = (e, None) if isinstance(e, dict) else ({}, "its entry is not a mapping")
        own = e.get("verified")
        engine = marks.get(str(name))
        rows.append({"id": str(name), "component": None, "value": num(e.get("value")), "reference": None, "source": _text(e.get("source")),
                     "follow_up_years": None, "window_years": None, "beyond": None, "where": "engine_parameters",
                     "verified": plain(own) if own is not None else plain(engine), "own_mark": own, "engine_mark": engine,
                     "bad": bad or ("its value is not a number" if num(e.get("value")) is None else None)})
    return rows, oa_bad, values_bad


def _today(record_at):
    try:
        return datetime.date.fromisoformat(str(record_at)[:10])
    except ValueError:
        return None


def _images(run):
    figs = checks.harness_figures(run)
    return None if figs is None else [f for name, f in figs.items() if name in SPEC_FIGURES]


def read(run):
    spec = checks.verified_spec(run)
    m = {"spec": spec, "disk": None, "decision": None, "window": (None, None), "delivery": (None, None), "effects": None,
         "oa_bad": None, "values_bad": None, "date": None, "kind": None, "n_components": None}
    if spec["verified"]:
        doc = spec["doc"]
        m["disk"] = checks.hash_state(spec["sha"], run.sha(SPEC))
        m["decision"] = _decision(doc)
        m["window"], m["delivery"] = _pair(at(doc, "objective.horizon")), _pair(at(doc, "objective.delivery"))
        m["effects"], m["oa_bad"], m["values_bad"] = _effects(doc, _engine_marks(run), m["window"])
        m["kind"] = at(doc, "deliverable.kind") if isinstance(at(doc, "deliverable.kind"), str) else None
        comps = at(doc, "options.components")
        m["n_components"] = len(comps) if isinstance(comps, (dict, list)) else None
        today = _today(spec["signed_at"])
        m["date"] = {"written": plain(doc.get("decision_date")), "today": today,
                     "findings": decision_date_findings(doc, today) if today is not None else None,
                     "why": None if today is not None else f"the sign-off record's signed_at ({spec['signed_at']!r}) is not a date, "
                     "and the producer's rule needs the day of the signature"}
    m["dossier"], m["dossier_why"] = checks.evidence_dossier(run, spec)
    m["history"], _ = checks.signoff_history(run, spec)
    m["current"] = next((r for r in m["history"]["rows"] if r["current"]), None)
    m["sheet"], m["sheet_why"] = checks.signoff_sheet(run)
    m["images"] = _images(run)
    return m


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import workbench
    from views.fit_bench import Bench, fixture_builder, need
    fx = fixture_builder()
    b = Bench(lambda run: {"checks": [], "facts": {}, "tables": {}, "charts": [], "notes": {}})
    hm, post = fx.METHODS
    D = datetime.date

    def look(d):
        return read(workbench.Run(d))

    def rule():
        base = {"decision_date": "2026-01-01", "objective": {"horizon": [2026.0, 2029.0], "delivery": [2026.0, 2028.0]}}

        def got(today, **change):
            raw = {**base, **change}
            return [s for s, _ in decision_date_findings(raw, today)]
        need(got(D(2026, 1, 1)) == [] and got(D(2025, 12, 1)) == [], "delivery starts on the decision date, signed on or before it: nothing")
        need(got(D(2026, 1, 1), decision_date="2026-09-01") == ["HIGH"], "delivery starts before the decision date: HIGH")
        need(got(D(2026, 3, 1)) == ["HIGH"], "delivery starts before the day of the signature: HIGH")
        need(got(D(2026, 1, 1), decision_date="soon") == ["HIGH"] and got(D(2026, 1, 1), decision_date=True) == ["HIGH"], "unreadable: HIGH")
        late = {"objective": {"horizon": [2027.0, 2029.0]}}
        need(got(D(2026, 6, 1), **late) == ["MEDIUM"] and got(D(2026, 3, 31), **late) == [], "a date more than 90 days past, with a later start: MEDIUM")
        need(got(D(2027, 1, 1), decision_date=2026, **late) == ["MEDIUM"] and got(D(2026, 12, 31), decision_date=2026, **late) == [],
             "a year alone is past only in a later year")
        need(got(D(2026, 1, 1), decision_date=2026, objective={"horizon": [2025.5, 2029.0]}) == ["HIGH"], "with no delivery period the window's start is compared")
        need(got(D(2026, 1, 1), objective={"horizon": [0.0, 5.0]}) == [] and got(D(2026, 1, 1), objective=None) == [],
             "a start in model years, or no window, is not compared")
        need(got(D(2026, 1, 1), decision_date=D(2026, 9, 1)) == ["HIGH"], "a date the YAML reader already parsed is read as a date")

    def clean():
        for method in fx.METHODS:
            m = look(b.folder(method))
            need(m["spec"]["verified"] and m["disk"] == "same" and m["window"] == (2026.0, 2029.0) and m["delivery"] == (2026.0, 2028.0), f"{method}: the window and the delivery period")
            need([r["id"] for r in m["effects"]] == ["p2"] and m["effects"][0]["own_mark"] == "matched" and m["effects"][0]["engine_mark"] == "matched",
                 f"{method}: the clean run holds one spec-set value and no outcome adjustment ({[r['id'] for r in m['effects']]})")
            need(m["date"]["findings"] == [] and m["date"]["today"] == D(2026, 1, 1), f"{method}: the producer's rule on the day of the signature raises nothing")
            need([r["item"] for r in m["decision"]] == [i for i, _ in DECISION] and m["n_components"] == 3, f"{method}: the decision's items, in reading order")
            need(len(m["history"]["rows"]) == 3 and m["current"]["sections_changed"] == ["uncertainty"] and not m["history"]["bare"], f"{method}: the history")
            need(m["dossier"]["state"] == "same" and len(m["dossier"]["entries"]) == 5 and m["sheet"]["spec_sha"] == m["spec"]["sha"], f"{method}: the dossier and the sheet")
            need(m["images"] is not None and {r["figure"] for r in m["images"]} <= set(SPEC_FIGURES), f"{method}: the figures beside the view")

    def adjustments():
        m = look(b.folder(hm, "C1_follow_up_short"))
        oa = [r for r in m["effects"] if r["where"] == "outcome_adjustments"]
        need(len(oa) == 1 and oa[0]["follow_up_years"] is None and oa[0]["beyond"] is None and oa[0]["window_years"] == 3.0 and oa[0]["verified"] is None,
             "an outcome adjustment is never matched to a dossier observation by its source text: no follow-up, though one is recorded there")
        cited = [o for e in m["dossier"]["entries"] for o in e["observations"] if o.get("source") == oa[0]["source"]]
        need(len(cited) == 1 and cited[0][fx.FOLLOW_UP] == 1.0, "the observation with the same source text records a follow-up of one year")
        m = look(b.folder(hm, "value_no_mark"))
        r = next(r for r in m["effects"] if r["id"] == "p2")
        need(r["own_mark"] is None and r["engine_mark"] == "matched" and r["verified"] == "matched", "with no mark in the spec the row shows the engine record's, kept apart")

    def kind():
        m = look(b.folder(post, figures="estimates", kind="estimates"))
        items = {r["item"]: r for r in m["decision"]}
        need(items["decision_threshold"]["value"] == 1.0 and items["windows"]["key"] == "deliverable.windows" and items["comparator"]["value"] is None
             and m["window"] == (None, None) and m["n_components"] is None, "the estimates kind: its own rule and windows; no comparator, window or components")
        need(m["dossier"] is None and "names no evidence dossier" in m["dossier_why"], "and no dossier is named")

    def closed():
        m = look(b.folder(hm, "spec_unverified"))
        need(m["decision"] is None and m["effects"] is None and m["date"] is None and m["dossier"] is None and "verified" in m["dossier_why"],
             "with no verified signed copy nothing of the spec is returned")
        need(len(m["history"]["rows"]) == 3 and m["current"]["sections_changed"] is None and "verified" in m["current"]["why"],
             "the sign-off records are still listed, and the current one's sections are not counted")
        d = os.path.join(b.tmp, "none")
        os.makedirs(d)
        m = look(d)
        need(m["decision"] is None and m["history"]["rows"] == [] and m["sheet"] is None and m["images"] is None, "an empty folder: nothing, and no error")
        need(plain({1: D(2026, 1, 2), "a": (1, None)}) == {"1": "2026-01-02", "a": [1, None]} and at({"a": {"b": 1}}, "a.b.c") is None, "plain values")

    b.case("SR1 the producer's decision-date rule, case by case (toolkit/spec/spec_critique.py _decision_date)", rule)
    b.case("SR2 the clean runs: decision, window, one spec-set value, history, dossier, sheet, figures", clean)
    b.case("SR3 an outcome adjustment has no follow-up whatever the dossier records under the same source text; marks are kept apart", adjustments)
    b.case("SR4 the estimates kind", kind)
    b.case("SR5 fail closed: an unverified signed copy, an empty folder", closed)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: spec_read.py --self-test")
