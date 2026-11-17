#!/usr/bin/env python3
"""Spec-level critique before SIGN: the decision design principles checked on the spec alone (owner, 2026-11-17).

    python3 toolkit/spec/spec_critique.py <run_dir> [--today YYYY-MM-DD]     # prints the findings, exit 1 on HIGH
    python3 toolkit/spec/spec_critique.py --self-test

A run's gate raised these as HIGH findings only after paid compute, although each needs only the spec and the
evidence dossier. They are computed here, deterministically, and shown on the sign-off sheet
(toolkit/spec/signoff.py sheet), so the owner sees them before signing. They do not refuse a signature: the owner
decides, and the spec records the decision.

Checks (principle numbers from .claude/orchestration/decision_design_principles.md):
  headline_comparator (2)  the comparator is a no-action package (no components): the headline would compare the
                           recommended plan with no action, which is context only.
  decision_date (2)        the decision date is past, or the delivery window starts before today: actions already
                           delivered would be optimised as if still open.
  baseline_burden (3)      no validation.burden anchor row (role anchor) sets the absolute burden: the engine's own
                           burden would rank the units. A burden_scale decision test lowers it to MEDIUM.
  decision_input_ranges (6) a dossier decision input that is a parameter held at a point value (HIGH); an
                           engine_parameters value with a range that no decision test varies (HIGH), or a point value
                           nothing varies (MEDIUM); an allocated component's cost item with no uncertainty range and
                           no price test (MEDIUM).

A finding is declared when the spec's `principle_departures` names its check with a reason:

    principle_departures:
      - {principle: 2, check: headline_comparator, reason: "the harness cannot yet take a per-unit funded plan as the
         comparator; the funded plan is the fixed_plan variant gc7_as_funded, reported beside the headline"}

A declared finding stays on the sheet as declared (the report repeats it). A malformed entry is itself a HIGH finding.

Owner judgment (not computed, listed on the sheet): whether a non-empty comparator package is the plan as currently
funded, and whether an anchor's estimand matches the objective's outcome, population and window. No LLM task runs
here. Nothing here names a disease, a place or an intervention.
"""
from __future__ import annotations

import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CHECKS = {"headline_comparator": 2, "decision_date": 2, "baseline_burden": 3, "decision_input_ranges": 6}
SECTION = "## Spec critique (decision design principles, before signing)"
PAST_DAYS = 90             # an ISO decision date more than this before today is past
DELIVERED_YEARS = 0.25     # a delivery window starting more than this before today has delivered actions


def _year_frac(d: datetime.date) -> float:
    start = datetime.date(d.year, 1, 1)
    return d.year + (d - start).days / 365.25


def _date_of(v):
    """(date, is_year_only) for a decision date given as a year or an ISO date; None when unreadable."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return datetime.date(int(v), 1, 1), True
    if isinstance(v, datetime.datetime):
        return v.date(), False
    if isinstance(v, datetime.date):
        return v, False
    s = str(v or "").strip()
    try:
        return (datetime.date(int(s), 1, 1), True) if s.isdigit() else (datetime.date.fromisoformat(s[:10]), False)
    except ValueError:
        return None


def _f(sev, check, text, fix, judgment=False):
    return {"check": check, "principle": CHECKS[check], "severity": sev, "text": text, "fix": fix,
            "declared": None, "judgment": judgment}


def _packages(raw) -> dict:
    pk = (raw.get("options") or {}).get("packages") or {}
    return {k: v for k, v in pk.items() if k not in ("generate", "within_envelope")} if isinstance(pk, dict) else {}


def _comparator(raw) -> list:
    pkg = str((raw.get("comparator") or {}).get("package"))
    pk = _packages(raw)
    gen = isinstance((raw.get("options") or {}).get("packages"), dict) and raw["options"]["packages"].get("generate")
    variants = [v.get("id") for v in ((raw.get("analysis") or {}).get("plan_variants") or {}).get("variants") or []
                if isinstance(v, dict) and v.get("type") == "fixed_plan"]
    if (pkg in pk and not pk[pkg]) or (gen and pkg == "none" and pkg not in pk):
        held = (f" The funded plan is the fixed_plan variant {', '.join(map(str, variants))}, which the harness "
                "reports only against the unconstrained optimum, never as the headline." if variants else
                " No fixed_plan variant gives the funded plan.")
        return [_f("HIGH", "headline_comparator",
                   f"The comparator `{pkg}` has no components, so the headline compares the recommended plan with no "
                   f"action, which principle 2 makes context only.{held}",
                   "Set comparator.package to the funded plan when it is one package in every unit; otherwise declare "
                   "the funded plan as a fixed_plan variant and declare this check in principle_departures (the "
                   "harness cannot yet take a per-unit plan as the comparator)")]
    return [_f("INFO", "headline_comparator", f"The comparator is package `{pkg}`.",
               "Confirm it is the plan as currently funded in every unit", judgment=True)]


def _decision_date(raw, today) -> list:
    got = _date_of(raw.get("decision_date"))
    if got is None:
        return [_f("HIGH", "decision_date", f"decision_date {raw.get('decision_date')!r} is not a year or an ISO date.",
                   "Give the date the decision is made")]
    d, year_only = got
    obj = raw.get("objective") or {}
    win = obj.get("delivery") or obj.get("horizon") or []
    past = d.year < today.year if year_only else (today - d).days > PAST_DAYS
    try:
        start = float(win[0])
    except (TypeError, ValueError, IndexError):
        start = None
    now = _year_frac(today)
    out = []
    if start is not None and start < now - DELIVERED_YEARS:
        out.append(_f("HIGH", "decision_date",
                      f"The delivery window starts in {start:g}, before today ({today.isoformat()}); the decision is "
                      f"dated {raw.get('decision_date')}. Actions delivered since then would be optimised as if open.",
                      "Date the decision today, set what was delivered as fixed inputs (constraints.locks, the funded "
                      "plan's delivered part) and optimise only what remains; or declare this check"))
    elif past:
        out.append(_f("MEDIUM", "decision_date",
                      f"The decision is dated {raw.get('decision_date')}, before today ({today.isoformat()}).",
                      "Date the decision when it is made (reprogramming from the decision date)"))
    return out


def _burden(raw, run_dir) -> list:
    tests = [t for t in (raw.get("analysis") or {}).get("decision_tests") or [] if isinstance(t, dict)]
    scale = [str(t.get("id")) for t in tests if t.get("type") == "burden_scale"]
    sec = (raw.get("validation") or {}).get("burden")
    anchors, why = 0, "no validation.burden section"
    if isinstance(sec, dict) and sec.get("csv"):
        try:
            from toolkit.analysis import burden as BV
            from toolkit.spec import schema
            sp = schema.Spec(raw, run_dir)
            refs, _bad = BV.load_references(sp, BV.read_reference(run_dir, BV.config(sp)["csv"]))
            anchors = len(BV.anchor_rows(refs))
            why = "validation.burden has no anchor row (role anchor): its references only validate"
        except Exception as e:  # noqa: BLE001 -- fail closed: unreadable references anchor nothing
            why = f"validation.burden references could not be read ({type(e).__name__}: {e})"
    if anchors:
        return [_f("INFO", "baseline_burden", f"{anchors} anchor row(s) set the absolute burden from external "
                   "estimates; the engine supplies the relative reductions.",
                   "Confirm each anchor's outcome, population and window match the objective's", judgment=True)]
    if scale:
        return [_f("MEDIUM", "baseline_burden", f"The engine's own burden ranks the units ({why}); decision test(s) "
                   f"{', '.join(scale)} scale it to external estimates, as a test, not in the plan.",
                   "Anchor the burden (validation.burden anchor rows) or declare this check")]
    return [_f("HIGH", "baseline_burden", f"The engine's own burden ranks the units ({why}); principle 3 takes "
               "baseline burden from data-driven external estimates.",
               "Add anchor rows to validation.burden, or a burden_scale decision test, or declare this check")]


def _varied(raw, run_dir) -> tuple[set, set]:
    """(parameters, unit attributes) that a decision test or the uncertainty draws vary."""
    an, unc = raw.get("analysis") or {}, raw.get("uncertainty") or {}
    tests = [t for t in an.get("decision_tests") or [] if isinstance(t, dict)]
    ps = {t.get("parameter") for t in tests if t.get("type") == "parameter_value"
          and len(set(map(str, t.get("values") or []))) >= 2}
    ps |= set(unc.get("priors") or {})              # a drawn prior gets its generated prior_range_<p> test
    # an engine_parameters range is drawn (schema: range needs dist, engine_parameters.py)
    ps |= {p for p, v in (raw.get("engine_parameters") or {}).items() if isinstance(v, dict) and v.get("range")}
    try:
        from toolkit.spec import priors as PRI, schema
        ps |= set(PRI.drawn(schema.Spec(raw, run_dir)))      # engine-card priors, drawn without being listed
    except Exception:  # noqa: BLE001 -- without the engine its card priors are unknown: counted as not drawn
        pass
    cols = {str((t.get("unit_attribute") or {}).get("column")) for t in tests if t.get("type") == "unit_attribute"}
    ea = (raw.get("decision_units") or {}).get("engine_attributes") or {}
    return ps, {a for a, v in ea.items() if isinstance(v, dict) and str(v.get("column")) in cols}


def _ranges(raw, run_dir) -> list:
    an = raw.get("analysis") or {}
    tests = [t for t in an.get("decision_tests") or [] if isinstance(t, dict)]
    unc = raw.get("uncertainty") or {}
    vp, va = _varied(raw, run_dir)
    out, nodata, held, excluded, inputs = [], [], [], [], {}
    excl_p = {str(x.get("parameter")): str(x.get("reason") or "").strip() for x in unc.get("excluded") or []
              if isinstance(x, dict) and x.get("parameter") and str(x.get("reason") or "").strip()}
    ev = (raw.get("evidence") or {}).get("dossier")
    if ev and run_dir:
        import yaml
        try:
            with open(os.path.join(run_dir, ev), encoding="utf-8") as fh:
                inputs = (yaml.safe_load(fh) or {}).get("inputs") or {}
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            inputs = {}                                # the dossier precondition reports an unreadable dossier
    inputs = inputs if isinstance(inputs, dict) else {}
    for k, e in sorted(inputs.items()):
        kind, _, name = str(k).partition(":")
        ch = e.get("chosen") if isinstance(e, dict) else None
        if kind not in ("parameter", "unit_attribute") or not isinstance(ch, dict) or "range" in ch:
            continue                                   # a card table takes a value: no test type varies one
        if kind == "parameter" and name in excl_p:
            excluded.append(f"`{k}`: {excl_p[name]}")
        elif name not in (vp if kind == "parameter" else va):
            (nodata if "no_data" in e else held).append(f"`{k}` = {ch.get('value')!r}")
    for p, v in sorted((raw.get("engine_parameters") or {}).items()):
        if isinstance(v, dict) and p not in vp and f"parameter:{p}" not in inputs:
            held.append(f"`engine_parameters.{p}` = {v.get('value')!r}")
    if nodata:
        out.append(_f("HIGH", "decision_input_ranges", f"{len(nodata)} decision input(s) the evidence dossier records "
                      f"with no data are held at a point value that no decision test or draw varies: "
                      f"{'; '.join(nodata)}.",
                      "Give each a range and a decision test that varies it, or declare this check"))
    if excluded:
        out.append(_f("INFO", "decision_input_ranges", f"Held at a point, excluded from the draws with a reason "
                      f"(uncertainty.excluded): {'; '.join(excluded)}.",
                      "Confirm the reason shows the input cannot move the decision", judgment=True))
    if held:
        out.append(_f("MEDIUM", "decision_input_ranges", f"{len(held)} input(s) with evidence are held at a point "
                      f"value that no decision test or draw varies: {'; '.join(held)}.",
                      "Give a range and a decision test to each that can move the decision", judgment=True))
    srcs = [s for s in unc.get("sources") or [] if isinstance(s, dict) and s.get("kind") == "costs"]
    costed = set().union(*[set((s.get("items") or {})) for s in srcs]) if srcs else set()
    costed |= {i for t in tests if t.get("type") == "cost_items" for i in (t.get("multipliers") or {})}
    excl = any(isinstance(x, dict) and x.get("kind") == "costs" for x in unc.get("excluded") or [])
    allocated = {c for cs in _packages(raw).values() for c in (cs or [])} or set(
        ((raw.get("options") or {}).get("components") or {}))
    loose = sorted(str(it.get("id")) for it in (raw.get("options") or {}).get("cost_items") or []
                   if isinstance(it, dict) and it.get("component") in allocated and it.get("id") not in costed)
    if loose and not excl:
        out.append(_f("MEDIUM", "decision_input_ranges", f"Cost item(s) {', '.join(loose)} of allocated components "
                      "have no uncertainty range and no price test.",
                      "Add them to the costs uncertainty source with a multiplier range, or to a cost_items test"))
    return out


def _departures(raw) -> tuple[dict, list]:
    got, bad = {}, []
    deps = raw.get("principle_departures")
    if deps is None:
        return got, bad
    if not isinstance(deps, list):
        return got, [_f("HIGH", "headline_comparator", "principle_departures is not a list.", "Give a list of "
                        "{principle, check, reason}")]
    for i, d in enumerate(deps):
        ok = (isinstance(d, dict) and d.get("check") in CHECKS and d.get("principle") == CHECKS[d.get("check")]
              and isinstance(d.get("reason"), str) and d["reason"].strip())
        if ok:
            got[d["check"]] = d["reason"].strip()
        else:
            chk = d.get("check") if isinstance(d, dict) and d.get("check") in CHECKS else "headline_comparator"
            bad.append(_f("HIGH", chk, f"principle_departures[{i}] is malformed: {d!r}",
                          f"Give {{principle, check, reason}}, check one of {', '.join(CHECKS)} with its principle "
                          "number and a non-empty reason"))
    return got, bad


def findings(raw: dict, run_dir: str | None = None, today: datetime.date | None = None) -> list[dict]:
    """Every check's findings on the spec `raw` ({check, principle, severity, text, fix, declared, judgment}); a
    finding whose check the spec declares carries the reason in `declared` and severity DECLARED."""
    return []           # α: tests first; the checks land in β
    today = today or datetime.date.today()
    if not isinstance(raw, dict):
        return [_f("HIGH", "headline_comparator", "the spec is not a mapping", "Fix the spec")]
    out = []
    for name, fn in (("headline_comparator", lambda: _comparator(raw)),
                     ("decision_date", lambda: _decision_date(raw, today)),
                     ("baseline_burden", lambda: _burden(raw, run_dir)),
                     ("decision_input_ranges", lambda: _ranges(raw, run_dir))):
        try:
            out += fn()
        except Exception as e:  # noqa: BLE001 -- fail closed: a check that cannot run is a finding
            out.append(_f("HIGH", name, f"the check could not run: {type(e).__name__}: {e}", "Fix the spec section"))
    deps, bad = _departures(raw)
    for f in out:
        if f["check"] in deps and f["severity"] in ("HIGH", "MEDIUM"):
            f["declared"], f["severity"] = deps[f["check"]], "DECLARED"
    return out + bad


def sheet_lines(raw, run_dir, today=None) -> list[str]:
    """The sign-off sheet's section: each finding, with its principle, severity and fix, before signing."""
    fs = findings(raw, run_dir, today)
    open_ = [f for f in fs if f["severity"] in ("HIGH", "MEDIUM")]
    out = [SECTION, "", f"{sum(f['severity'] == 'HIGH' for f in fs)} HIGH, "
           f"{sum(f['severity'] == 'MEDIUM' for f in fs)} MEDIUM, {sum(f['severity'] == 'DECLARED' for f in fs)} "
           "declared. These are the findings the gate would raise after compute; signing with one open accepts it.", "",
           "| Severity | Principle | Check | Finding | What resolves it |", "|---|---|---|---|---|"]
    order = {"HIGH": 0, "MEDIUM": 1, "DECLARED": 2, "INFO": 3}
    cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")      # noqa: E731
    for f in sorted(fs, key=lambda f: order.get(f["severity"], 9)):
        what = f"declared: {f['declared']}" if f["declared"] else f["fix"]
        if f["judgment"]:
            what = f"owner judgment: {what}"
        out.append(f"| {f['severity']} | {f['principle']} | {f['check']} | {cell(f['text'])} | {cell(what)} |")
    return out + ([""] if open_ or fs else ["None.", ""])


def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        c or fails.append(m)

    T = datetime.date(2026, 11, 17)

    def spec(**kw):
        r = {"decision_date": 2026, "objective": {"horizon": [2027.0, 2030.0]},
             "options": {"components": {"c1": {}, "c2": {}}, "packages": {"none": [], "c1": ["c1"], "c2": ["c2"]},
                         "cost_items": [{"id": "k1", "component": "c1"}, {"id": "k2", "component": "c2"}]},
             "comparator": {"package": "c1"},
             "uncertainty": {"sources": [{"kind": "costs", "items": {"k1": [0.8, 1.2], "k2": [0.8, 1.2]}}]},
             "analysis": {"decision_tests": [{"id": "BS", "type": "burden_scale"}]},
             "validation": {}}
        for k, v in kw.items():
            r[k] = v
        return r

    def sev(fs, check):
        return [f["severity"] for f in fs if f["check"] == check]

    ok(sev(findings(spec(), today=T), "headline_comparator") == ["INFO"], "SC1 a non-empty comparator is owner judgment")
    fs = findings(spec(comparator={"package": "none"}), today=T)
    ok(sev(fs, "headline_comparator") == ["HIGH"], f"SC2 a no-action comparator is HIGH: {fs}")
    r = spec(comparator={"package": "none"})
    r["options"]["packages"] = {"generate": True}
    ok(sev(findings(r, today=T), "headline_comparator") == ["HIGH"], "SC3 a generated `none` comparator is no action")
    r["analysis"]["plan_variants"] = {"variants": [{"id": "funded", "type": "fixed_plan", "column": "p"}]}
    t = [f["text"] for f in findings(r, today=T) if f["check"] == "headline_comparator"][0]
    ok("funded" in t and "never as the headline" in t, f"SC4 the fixed_plan variant is named: {t}")
    r["principle_departures"] = [{"principle": 2, "check": "headline_comparator", "reason": "per-unit plan"}]
    fs = findings(r, today=T)
    ok(sev(fs, "headline_comparator") == ["DECLARED"], f"SC5 a declared departure is shown as declared: {fs}")
    r["principle_departures"] = [{"principle": 3, "check": "headline_comparator", "reason": "x"}]
    ok(sev(findings(r, today=T), "headline_comparator") == ["HIGH", "HIGH"], "SC6 a wrong principle number is refused")
    r["principle_departures"] = [{"principle": 2, "check": "headline_comparator", "reason": " "}]
    ok(sev(findings(r, today=T), "headline_comparator") == ["HIGH", "HIGH"], "SC7 a blank reason is refused")

    ok(sev(findings(spec(decision_date=2024, objective={"delivery": [2024.0, 2027.0], "horizon": [2024, 2029]}),
                    today=T), "decision_date") == ["HIGH"], "SC8 a delivery window begun before today is HIGH")
    ok(sev(findings(spec(decision_date=2025), today=T), "decision_date") == ["MEDIUM"],
       "SC9 a past decision year with a future window is MEDIUM")
    ok(sev(findings(spec(decision_date="2026-10-01"), today=T), "decision_date") == [],
       "SC10 an ISO date within the tolerance is not past")
    ok(sev(findings(spec(decision_date="2026-01-01"), today=T), "decision_date") == ["MEDIUM"],
       "SC11 an ISO date long before today is past")
    ok(sev(findings(spec(decision_date="soon"), today=T), "decision_date") == ["HIGH"], "SC12 an unreadable date")
    ok(sev(findings(spec(decision_date=True), today=T), "decision_date") == ["HIGH"], "SC12b a boolean date")

    ok(sev(findings(spec(), today=T), "baseline_burden") == ["MEDIUM"], "SC13 a burden_scale test alone is MEDIUM")
    ok(sev(findings(spec(analysis={"decision_tests": []}), today=T), "baseline_burden") == ["HIGH"],
       "SC14 no anchor and no test is HIGH")
    ok(sev(findings(spec(analysis={}, validation={"burden": {"csv": "missing.csv"}}), run_dir="/nonexistent",
                    today=T), "baseline_burden") == ["HIGH"], "SC15 unreadable references anchor nothing (fail closed)")

    ok(sev(findings(spec(), today=T), "decision_input_ranges") == [], "SC16 every cost item has a range")
    r = spec(engine_parameters={"p": {"value": 1.0}, "q": {"value": 2.0, "range": [1, 3], "dist": "uniform"},
                                "s": {"value": 1}},
             analysis={"decision_tests": [{"id": "D", "type": "parameter_value", "parameter": "s", "values": [1, 2]}]})
    fs = [f for f in findings(r, today=T) if f["check"] == "decision_input_ranges"]
    ok([f["severity"] for f in fs] == ["MEDIUM"] and "engine_parameters.p" in fs[0]["text"]
       and "engine_parameters.s" not in fs[0]["text"] and "engine_parameters.q" not in fs[0]["text"],
       f"SC17 a point value nothing varies is MEDIUM; a drawn range and a tested parameter are clean: {fs}")
    r = spec()
    r["uncertainty"] = {"sources": [{"kind": "costs", "items": {"k1": [0.8, 1.2]}}]}
    fs = [f for f in findings(r, today=T) if f["check"] == "decision_input_ranges"]
    ok(len(fs) == 1 and "k2" in fs[0]["text"], f"SC18 a cost item with no range: {fs}")
    r["analysis"]["decision_tests"].append({"id": "P", "type": "cost_items", "multipliers": {"k2": 1.3}})
    ok(sev(findings(r, today=T), "decision_input_ranges") == [], "SC19 a price test covers the cost item")
    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "ev.yaml"), "w") as fh:
            fh.write("inputs:\n  'parameter:a': {no_data: {reason: r}, chosen: {value: 0.5}}\n"
                     "  'parameter:b': {chosen: {range: [0, 1]}}\n  'parameter:c': {observations: [1], chosen: {value: 2}}\n"
                     "  'parameter:t': {no_data: {reason: r}, chosen: {value: 1}}\n"
                     "  'unit_attribute:z': {no_data: {reason: r}, chosen: {value: x}}\n"
                     "  'unit_attribute:y': {no_data: {reason: r}, chosen: {value: 1}}\n"
                     "  'card_table:k': {no_data: {reason: r}, chosen: {value: x}}\n"
                     "  'parameter:x': {no_data: {reason: r}, chosen: {value: 3}}\n")
        r = spec(evidence={"dossier": "ev.yaml"}, decision_units={"engine_attributes": {"y": {"column": "ycol"}}})
        r["uncertainty"]["excluded"] = [{"parameter": "x", "reason": "cannot change the plan: priced out"}]
        r["analysis"]["decision_tests"] += [{"id": "T", "type": "parameter_value", "parameter": "t", "values": [1, 2]},
                                            {"id": "U", "type": "unit_attribute", "unit_attribute": {"column": "ycol"}}]
        fs = {f["severity"]: f["text"] for f in findings(r, run_dir=d, today=T)
              if f["check"] == "decision_input_ranges"}
        ok(set(fs) == {"HIGH", "MEDIUM", "INFO"} and "parameter:a" in fs["HIGH"] and "unit_attribute:z" in fs["HIGH"]
           and "parameter:c" in fs["MEDIUM"] and not any(x in fs["HIGH"] + fs["MEDIUM"] for x in
                                                         ("parameter:b", "parameter:t", "unit_attribute:y", "card_"))
           and "parameter:x" not in fs["HIGH"] and "priced out" in fs["INFO"],
           f"SC20 a no-data input held at a point is HIGH, an observed one MEDIUM, an excluded one INFO; ranges, "
           f"tested inputs and card tables are clean: {fs}")
        md = "\n".join(sheet_lines(spec(comparator={"package": "none"}, evidence={"dossier": "ev.yaml"}), d, T))
        good = SECTION in md and "| HIGH | 2 | headline_comparator |" in md and "parameter:a" in md
        ok(good, "SC21 the sheet section lists the findings" + ("" if good else f":\n{md}"))
    ok(sev(findings({"decision_date": 2026}, today=T), "baseline_burden") == ["HIGH"],
       "SC22 a sparse spec still gets every check (fail closed)")
    print(f"{'FAIL' if fails else 'OK'}: " + (f"{len(fails)} case(s) failed" if fails else
                                               "all self-test cases passed."))
    return 1 if fails else 0


def main(argv=None) -> int:
    import argparse
    import yaml
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--today")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir is required")
    with open(os.path.join(a.run_dir, "decision_spec.yaml"), encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    today = datetime.date.fromisoformat(a.today) if a.today else None
    print("\n".join(sheet_lines(raw, a.run_dir, today)))
    return 1 if any(f["severity"] == "HIGH" for f in findings(raw, a.run_dir, today)) else 0


if __name__ == "__main__":
    sys.exit(main())
