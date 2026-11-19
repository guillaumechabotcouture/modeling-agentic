#!/usr/bin/env python3
"""The checks of the Spec and evidence view (docs/design/review-workbench.md §5.3), each on the generic model of
views/spec_read.py and never on a file.

    python3 tools/workbench/views/spec_checks.py --self-test

Statuses follow §3. With no verified signed copy of the spec every check is `not_available` and says so: nothing in
a spec that is not verified is shown or judged. A word a producer does not write and a malformed entry are
`not_available` naming it; a record that contradicts itself or another file is `problem`. What no file records as
data (a follow-up duration, a mark that a source was opened) is said to be not recorded, never read from text.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
from views import spec_read as R  # noqa: E402

CHECKS = {"effect_window": "C1", "values_sourced": None, "dossier_coverage": "C15", "signoff_note": None,
          "presign_sections": None, "presign_high": None, "decision_date": None}
TITLES = {"effect_window": "Effects applied over the outcome window, against how long their evidence was followed",
          "values_sourced": "The source behind each value the spec sets",
          "dossier_coverage": "Evidence dossier: entries with no data, and observations not marked as opened",
          "signoff_note": "What the current signature says it accepted",
          "presign_sections": "Sections of the sign-off sheet",
          "presign_high": "Rows the sign-off sheet marks HIGH",
          "decision_date": "The decision date against the delivery period"}
SIGNED, SHEET, SIGNOFF = R.SIGNED, R.SHEET, checks.SPEC_SIGNOFF
SHOWN = 6        # names given in a sentence before "and n more"; every row is in the view's table


def _r(name, status, says, evidence, rows=None):
    return checks.result(f"spec.{name}", "spec", TITLES[name], status, says, evidence, rows, CHECKS[name])


def _some(items, n=SHOWN):
    items = [str(i) for i in items]
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def _n(count, word, plural=None):
    return f"{count} {word if count == 1 else plural or word + 's'}"


def _unverified(m, name, evidence):
    return _r(name, "not_available", f"Not available: no signed copy of the spec could be verified ({m['spec']['why']}), and this "
              "check reads nothing else.", evidence)


def _years(pair):
    return f"{pair[0]:g} to {pair[1]:g}"


def check_effect_window(m):
    ev = [(SIGNED, "analysis.outcome_adjustments[]"), (SIGNED, "objective.horizon")]
    if not m["spec"]["verified"]:
        return _unverified(m, "effect_window", ev)
    if m["oa_bad"]:
        return _r("effect_window", "not_available", f"Not available: {m['oa_bad']}, so the effects applied over the outcome window cannot be listed.", ev)
    rows = [r for r in m["effects"] if r["where"] == "outcome_adjustments"]
    if not rows:
        return _r("effect_window", "ok", "The signed spec has no outcome adjustment: no fixed ratio is applied to the outcome over the "
                  "outcome window (the engine parameter values it sets are in the next check).", ev)
    bad = [f"{r['id']} ({r['bad']})" for r in rows if r["bad"]]
    if bad:
        return _r("effect_window", "not_available", f"Not available: {_n(len(bad), 'outcome adjustment')} of the signed spec cannot be read as "
                  f"the producer writes it: {_some(bad)}.", ev)
    window = (f"the whole outcome window of {rows[0]['window_years']:g} years ({_years(m['window'])})" if m["window"][0] is not None
              else "the outcome window, which the signed spec does not give as two numbers")
    table = {"columns": ["id", "component", "value", "reference", "window_years", "follow_up_years", "source"],
             "rows": [[r[c] for c in ("id", "component", "value", "reference", "window_years", "follow_up_years", "source")] for r in rows]}
    return _r("effect_window", "look", f"{_n(len(rows), 'outcome adjustment')} ({_some(r['id'] for r in rows)}) "
              f"{'is' if len(rows) == 1 else 'are'} applied as a fixed ratio over {window}. No field of the spec records how long the "
              "evidence behind an adjustment was followed, and an adjustment has no key that cites a dossier entry (gap H1), so the "
              "workbench cannot tell whether an effect is applied beyond its evidence. Needs you: read each source beside the window.", ev, table)


def check_values_sourced(m):
    ev = [(SIGNED, "engine_parameters.<name>.verified, source"), (SIGNED, "analysis.outcome_adjustments[].source"),
          (R.ENGINE_REC, "data.parameters[].verified")]
    if not m["spec"]["verified"]:
        return _unverified(m, "values_sourced", ev)
    if m["values_bad"] or m["oa_bad"]:
        return _r("values_sourced", "not_available", f"Not available: {m['values_bad'] or m['oa_bad']}, so the values the spec sets cannot be listed.", ev)
    rows = m["effects"]
    if not rows:
        return _r("values_sourced", "ok", "The signed spec sets no value that scales an effect: no engine parameter value and no outcome adjustment.", ev)
    bad = [f"{r['id']} ({r['bad']})" for r in rows if r["bad"]]
    if bad:
        return _r("values_sourced", "not_available", f"Not available: {_n(len(bad), 'entry', 'entries')} of the signed spec cannot be read as the "
                  f"producer writes it: {_some(bad)}.", ev)
    odd = sorted({repr(r[k]) for r in rows for k in ("own_mark", "engine_mark") if r[k] is not None and r[k] not in R.MARKS})
    if odd:
        named = [r["id"] for r in rows if any(r[k] is not None and r[k] not in R.MARKS for k in ("own_mark", "engine_mark"))]
        return _r("values_sourced", "not_available", f"Not available: the mark {', '.join(odd)} on {_some(named)} is not one of the words a source's "
                  f"mark takes ({', '.join(R.MARKS)}).", ev)
    no_source = [r["id"] for r in rows if not r["source"]]
    unverified = [r["id"] for r in rows if r["own_mark"] == "unverified"]
    values = [r for r in rows if r["where"] == "engine_parameters"]
    no_mark = [r["id"] for r in values if r["own_mark"] is None]
    adjust = [r["id"] for r in rows if r["where"] == "outcome_adjustments"]
    by_engine = {w: sum(1 for r in values if r["own_mark"] is None and r["engine_mark"] == w) for w in R.MARKS}
    named = list(dict.fromkeys(unverified + no_source + no_mark + adjust))
    table = {"columns": ["id", "where", "mark_in_spec", "mark_in_engine_record", "has_source"],
             "rows": [[r["id"], r["where"], r["own_mark"], r["engine_mark"], bool(r["source"])] for r in rows if r["id"] in named]}
    if not named:
        return _r("values_sourced", "ok", f"Each of the {_n(len(rows), 'value')} the signed spec sets has a source, and the spec's own entry marks "
                  f"that source {' or '.join(R.VOUCHED)}.", ev)
    parts = ([f"{len(unverified)} marked unverified in the spec ({_some(unverified)})"] if unverified else []) + (
        [f"{len(no_mark)} engine parameter value(s) with no mark in the spec ({_some(no_mark)}; the engine record marks its own source for "
         + ", ".join(f"{n} {w}" for w, n in by_engine.items() if n)
         + (f", and holds no mark for {len(no_mark) - sum(by_engine.values())}" if len(no_mark) > sum(by_engine.values()) else "")
         + ", which cannot vouch for the spec's)"] if no_mark else []) + (
        [f"{len(adjust)} outcome adjustment(s), whose entry has no key for a mark ({_some(adjust)})"] if adjust else []) + (
        [f"{len(no_source)} with no source ({_some(no_source)})"] if no_source else [])
    return _r("values_sourced", "look", f"{len(named)} of the {_n(len(rows), 'value')} the signed spec sets "
              f"{'has' if len(named) == 1 else 'have'} no source the spec itself marks as checked: {'; '.join(parts)}. Needs you: a value "
              "counts as sourced only when the signed spec's own entry carries the mark.", ev, table)


def check_dossier_coverage(m):
    d = m["dossier"]
    rel = d["file"] if d else "evidence_dossier.yaml"
    ev = [(SIGNED, "evidence.dossier"), (rel, "inputs.<key>.no_data, decision_test, observations[].checked"), (SIGNOFF, f"sha256[{rel}]")]
    if not m["spec"]["verified"]:
        return _unverified(m, "dossier_coverage", ev)
    if d is None:
        return _r("dossier_coverage", "not_available", f"Not available: {m['dossier_why']}.", ev)
    if d["bad"]:
        return _r("dossier_coverage", "not_available", f"Not available: {_some(d['bad'], 3)}; the dossier cannot be read as its producer writes it.", ev)
    es = d["entries"]
    n_obs, marked = sum(e["n_observations"] for e in es), sum(e["marked"] for e in es)
    seen = [e for e in es if e["n_observations"]]
    held = (f"{rel} holds {_n(len(es), 'entry', 'entries')}: {len(seen)} with observations ({n_obs} in all) and "
            f"{sum(e['no_data'] for e in es)} that say no data.")
    clash = [f"{e['key']} ({e['clash']})" for e in es if e["clash"]]
    if d["state"] in ("changed", "missing"):
        return _r("dossier_coverage", "problem", f"{rel} is not the file that was signed: its hash is {d['state']} against the one the sign-off "
                  f"record holds ({SIGNOFF}), so what follows was not accepted by the signature. {held}", ev)
    if clash:
        return _r("dossier_coverage", "problem", f"{_n(len(clash), 'entry', 'entries')} of {rel} contradict the producer's rule that an entry "
                  f"gives exactly one of observations and no_data: {_some(clash, 4)}.", ev)
    untested = [e["key"] for e in es if e["no_data"] and not e["decision_test"]]
    unchecked = [e["key"] for e in es if e["n_unchecked"]] if marked else []
    parts = []
    if untested:
        parts.append(f"{_n(len(untested), 'entry says', 'entries say')} no data and {'names' if len(untested) == 1 else 'name'} no decision test "
                     f"({_some(untested)})")
    if unchecked:
        parts.append(f"{sum(e['n_unchecked'] for e in es)} of {n_obs} observations carry no mark that the source was opened (`checked`), in "
                     f"{_some(unchecked)}")
    elif n_obs and not marked:
        parts.append(f"no observation carries a mark that its source was opened (the key `checked`; no producer writes it: gap H2), so whether "
                     f"the {n_obs} observations were checked against their sources is not recorded")
    if d["state"] == "not_recorded":
        parts.append(f"the sign-off record holds no hash for {rel}, so nothing binds this file to the signature")
    table = {"columns": ["key", "no_data", "decision_test", "n_observations", "observations_not_marked"],
             "rows": [[e["key"], e["no_data"], e["decision_test"], e["n_observations"], e["n_unchecked"] if marked else None]
                      for e in es if e["key"] in untested or e["key"] in unchecked]}
    if parts:
        return _r("dossier_coverage", "look", f"{held} {'; '.join(parts)[0].upper() + '; '.join(parts)[1:]}. Who decided an entry is not recorded "
                  "as data either. Needs you: the entries named are inputs whose value rests on a judgement no test varies.", ev,
                  table if table["rows"] else None)
    return _r("dossier_coverage", "ok", f"{held} Every entry with no data names a decision test"
              + (", and every observation carries the mark that its source was opened." if n_obs else "; there is no observation to mark."), ev)


def check_signoff_note(m):
    ev = [(SIGNOFF, "note, signed_by, signed_at"), (f"{checks.ARCHIVE_DIR}/{checks.ARCHIVE_PREFIX}*/{SIGNOFF}", "sha256, note"),
          (SIGNED, "top-level keys")]
    if not m["spec"]["verified"]:
        return _unverified(m, "signoff_note", ev)
    cur, h = m["current"], m["history"]
    if cur is None:
        return _r("signoff_note", "not_available", "Not available: the run holds no sign-off record for the spec.", ev)
    who = f"The current signature (by {cur['by']}, {cur['at'][:16]})"
    changed = cur["sections_changed"]
    what = (f"it changed {_n(len(changed), 'section')} of the spec against the sign-off before it"
            + (f" ({', '.join(changed)})" if changed else "") if changed is not None else f"the sections it changed are not counted: {cur['why']}")
    table = {"columns": ["at", "by", "note_empty", "sections_changed"],
             "rows": [[r["at"], r["by"], not (r["note"] or "").strip(), r["sections_changed"]] for r in h["rows"]]}
    gap = "; ".join(f"{folder}: {why}" for folder, why in h["bare"])
    earlier = sum(1 for r in h["rows"] if not r["current"] and not (r["note"] or "").strip())
    more = (f" {_n(earlier, 'earlier signature')} also {'has' if earlier == 1 else 'have'} an empty note." if earlier else "") + (
        f" Not a sign-off, and a gap in the history: {gap}." if gap else "")
    if not (cur["note"] or "").strip():
        return _r("signoff_note", "look", f"{who} has an empty note, so what was accepted is not recorded; {what}.{more} Needs you: say "
                  "what this signature accepted.", ev, table)
    if h["bare"]:
        return _r("signoff_note", "not_available", f"Not available for one sign-off: an archive folder holds no sign-off record ({gap}), so the "
                  f"history the count rests on has a gap. {who} has a note; {what}.", ev, table)
    return _r("signoff_note", "ok", f"{who} has a note; {what}.{more}", ev, table)


def _sheet_unusable(m, name, ev):
    """The answer of both sheet checks when the sheet cannot be used, or None."""
    if not m["spec"]["verified"]:
        return _unverified(m, name, ev)
    s = m["sheet"]
    if s is None:
        return _r(name, "not_available", f"Not available: {m['sheet_why']} ({SHEET} is what the owner read before signing).", ev)
    if s["spec_sha"] is None:
        return _r(name, "not_available", f"Not available: {SHEET} does not record which spec it was written from (its last line holds no "
                  "valid spec hash), so it cannot be tied to the signed spec.", ev)
    if s["spec_sha"] != m["spec"]["sha"]:
        return _r(name, "problem", f"{SHEET} was written from another spec ({s['spec_sha'][:8]}) than the verified signed copy "
                  f"({m['spec']['sha'][:8]}): it is not what the owner read before signing this spec.", ev)
    return None


def check_presign_sections(m):
    ev = [(SHEET, "## headings"), (SHEET, "signoff-sheet-sources spec_sha256")]
    bad = _sheet_unusable(m, "presign_sections", ev)
    if bad:
        return bad
    have = m["sheet"]["sections"]
    lacking = [h for h in checks.SHEET_ALWAYS if h not in have]
    table = {"columns": ["section", "on_the_sheet", "written_for_every_spec"],
             "rows": [[h, h in have, True] for h in checks.SHEET_ALWAYS] + [[h, True, False] for h in have if h not in checks.SHEET_ALWAYS]}
    if lacking:
        return _r("presign_sections", "look", f"The sign-off sheet lacks {len(lacking)} of the {len(checks.SHEET_ALWAYS)} sections the current "
                  f"sign-off code writes for every spec: {'; '.join(lacking)}. The sheet may predate a section; what such a section would "
                  "have shown was not in front of the owner at signing.", ev, table)
    return _r("presign_sections", "ok", f"The sign-off sheet holds all {len(checks.SHEET_ALWAYS)} sections the current sign-off code writes for "
              f"every spec, and {len(have) - len(checks.SHEET_ALWAYS)} it writes for a spec that has the thing.", ev, table)


def check_presign_high(m):
    ev = [(SHEET, "rows of the spec critique; lines marked HIGH or REFUSED"), (SIGNOFF, "sha256 (holds no hash of the sheet)")]
    bad = _sheet_unusable(m, "presign_high", ev)
    if bad:
        return bad
    s = m["sheet"]
    if s["bad"]:
        return _r("presign_high", "not_available", f"Not available: {_some(s['bad'], 3)}; the sheet's rows cannot be counted.", ev)
    rows = s["rows"]
    table = {"columns": ["section", "severity", "text"], "rows": [[r["section"], r["severity"], r["text"]] for r in rows if r["severity"] in ("HIGH", "REFUSED")]}
    refused, high = [r for r in rows if r["severity"] == "REFUSED"], [r for r in rows if r["severity"] == "HIGH"]
    counts = ", ".join(f"{sum(r['severity'] == w for r in rows)} {w}" for w in ("HIGH", "MEDIUM", "DECLARED", "INFO", "REFUSED")
                       if any(r["severity"] == w for r in rows)) or "none"
    if refused:
        return _r("presign_high", "problem", f"The sign-off sheet holds {_n(len(refused), 'line')} saying the sign-off would be refused, and "
                  f"the spec is signed (first: {refused[0]['text'][:300]}).", ev, table)
    if high:
        return _r("presign_high", "look", f"The sign-off sheet marks {_n(len(high), 'row')} HIGH (under: {_some(sorted({str(r['section']) for r in high}), 3)}; "
                  f"rows marked on the sheet: {counts}). The sign-off record holds no hash of the sheet (gap H14), so nothing shows that this "
                  "is the sheet the owner read, and a HIGH row cannot be shown as accepted by signing. Needs you: each row is in the "
                  "table of pre-sign results.", ev, table)
    return _r("presign_high", "ok", f"The sign-off sheet marks no row HIGH (rows marked on the sheet: {counts}).", ev)


def check_decision_date(m):
    ev = [(SIGNED, "decision_date, objective.delivery, objective.horizon"), (SIGNOFF, "signed_at"),
          ("toolkit/spec/spec_critique.py", "_decision_date (the producer's rule, reproduced)")]
    if not m["spec"]["verified"]:
        return _unverified(m, "decision_date", ev)
    d = m["date"]
    if d["findings"] is None:
        return _r("decision_date", "not_available", f"Not available: {d['why']}.", ev)
    rule = "the producer's rule for the decision date (toolkit/spec/spec_critique.py), applied on the day of the current signature"
    if d["findings"]:
        severity, text = d["findings"][0]
        return _r("decision_date", "look", f"By {rule}, {d['today'].isoformat()}: {text} (the producer's severity: {severity}).", ev)
    start = m["delivery"][0] if m["delivery"][0] is not None else m["window"][0]
    return _r("decision_date", "ok", f"The decision is dated {d['written']}; "
              + (f"the period it is compared with starts in {start:g}" if start is not None and start >= R.CALENDAR_MIN
                 else "the spec gives no delivery period or window in calendar years to compare it with")
              + f". Nothing is raised by {rule}, {d['today'].isoformat()}.", ev)


RULES = (check_effect_window, check_values_sourced, check_dossier_coverage, check_signoff_note, check_presign_sections,
         check_presign_high, check_decision_date)


def run_all(m):
    out = []
    for fn in RULES:
        name = fn.__name__[len("check_"):]
        try:
            out.append(fn(m))
        except Exception as e:      # a check must not take the page down; it says which one failed and why
            out.append(_r(name, "not_available", f"This check failed on the run's files ({type(e).__name__}: {e}).", [(SIGNED, "")]))
    return out


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import json

    import workbench
    from lib.safe_io import read_nofollow, write_nofollow
    from views.fit_bench import Bench, fixture_builder, need
    fx = fixture_builder()
    b = Bench(lambda run: {"checks": run_all(R.read(run)), "facts": {}, "tables": {}, "charts": [], "notes": {}})
    hm = fx.METHODS[0]

    def got(d):
        return {c["id"].split(".")[-1]: c for c in run_all(R.read(workbench.Run(d)))}

    def retext(rel, change):
        def edit(d):
            write_nofollow(d, rel, change(read_nofollow(d, rel)[0].decode("utf-8")).encode("utf-8"))
        return edit

    def shape():
        cs = run_all(R.read(workbench.Run(b.folder(hm))))
        need([c["id"] for c in cs] == [f"spec.{n}" for n in CHECKS] and all(c["status"] == "ok" for c in cs),
             f"seven checks, in order, each ok on the clean run ({[(c['id'], c['status']) for c in cs if c['status'] != 'ok']})")
        need(all(c["case"] == CHECKS[c["id"][5:]] and c["evidence"] and "\n" not in c["says"] for c in cs), "each with its case, evidence and one sentence")
        broken = run_all({"spec": {"verified": True}})
        need(all(c["status"] == "not_available" and "failed" in c["says"] for c in broken), "a check that fails says so and is not_available")

    def sheet():
        refused = got(b.edited(hm, retext(fx.SHEET, lambda t: t.replace("\n## Evidence dossier", "\n- **REFUSED**: p2 was chosen with a "
                                                                        "held-out benchmark.\n\n## Evidence dossier", 1))))
        need(refused["presign_high"]["status"] == "problem" and "refused" in refused["presign_high"]["says"], "a REFUSED line on the sheet of a signed spec is a problem")
        odd = got(b.edited(hm, retext(fx.SHEET, lambda t: t.replace("| MEDIUM | 3 |", "| SEVERE | 3 |", 1))))
        need(odd["presign_high"]["status"] == "not_available" and "SEVERE" in odd["presign_high"]["says"], "a severity the producer does not write is named")
        short = got(b.edited(hm, retext(fx.SHEET, lambda t: t.replace("| MEDIUM | 3 | baseline_burden |", "| MEDIUM | 3 |", 1))))
        need(short["presign_high"]["status"] == "not_available" and "cells" in short["presign_high"]["says"], "a malformed row of the critique is named")
        other = got(b.edited(hm, retext(fx.SHEET, lambda t: t.replace('"spec_sha256": "', '"spec_sha256": "0000', 1))))
        need(other["presign_high"]["status"] == "not_available" and other["presign_sections"]["status"] == "not_available",
             "a sheet whose recorded spec hash is no valid hash is tied to no spec")
        swapped = got(b.edited(hm, retext(fx.SHEET, lambda t: t.replace(t.split('"spec_sha256": "')[1][:64], "a" * 64))))
        need(swapped["presign_high"]["status"] == "problem" and swapped["presign_sections"]["status"] == "problem" and "another spec" in swapped["presign_high"]["says"],
             "a sheet written from another spec is a problem for both sheet checks")
        cut = got(b.edited(hm, retext(fx.SHEET, lambda t: t[:t.index(fx.SHEET_SOURCES)])))
        need(cut["presign_sections"]["status"] == "not_available" and "which spec" in cut["presign_sections"]["says"], "a sheet with no sources line is tied to no spec")
        word = got(b.edited(hm, retext(fx.SHEET, lambda t: t.replace("\n## Evidence dossier", "\nThe range is HIGH in one study.\n- **p9** = 2 (HIGH)\n\n## Evidence dossier", 1))))
        need(word["presign_high"]["status"] == "ok", "the word HIGH where it marks nothing raises nothing")

    def dossier():
        def entry(change):
            def edit(text):
                import yaml
                doc = yaml.safe_load(text)
                change(doc["inputs"])
                return yaml.safe_dump(doc, sort_keys=False)
            return retext(fx.DOSSIER, edit)
        moved = got(b.edited(hm, retext(fx.DOSSIER, lambda t: t + "\n")))
        need(moved["dossier_coverage"]["status"] == "problem" and "not the file that was signed" in moved["dossier_coverage"]["says"],
             "a dossier changed after the sign-off is a problem")

        def resign(d):       # the record follows the dossier, so only the entry's own fault is left
            import hashlib
            rec = json.loads(read_nofollow(d, fx.SIGNOFF)[0])
            rec["sha256"][fx.DOSSIER] = hashlib.sha256(read_nofollow(d, fx.DOSSIER)[0]).hexdigest()
            write_nofollow(d, fx.SIGNOFF, json.dumps(rec).encode())

        def with_entry(change):
            return got(b.edited(hm, lambda d: (entry(change)(d), resign(d))))
        both = with_entry(lambda ins: ins["parameter:p1"].update(no_data={"reason": "r", "borrowing_rule": "b"}))
        need(both["dossier_coverage"]["status"] == "problem" and "parameter:p1" in both["dossier_coverage"]["says"], "an entry with observations and no_data contradicts itself")
        neither = with_entry(lambda ins: ins["unit_attribute:x1"].pop("no_data"))
        need(neither["dossier_coverage"]["status"] == "problem" and "neither" in neither["dossier_coverage"]["says"], "an entry with neither is a problem")
        scalar = with_entry(lambda ins: ins.update({"parameter:p1": "see the appendix"}))
        need(scalar["dossier_coverage"]["status"] == "not_available" and "parameter:p1" in scalar["dossier_coverage"]["says"], "an entry that is no mapping is named")
        false = with_entry(lambda ins: ins["parameter:p1"]["observations"][0].update(checked=False))
        need(false["dossier_coverage"]["status"] == "look" and "1 of 3" in false["dossier_coverage"]["says"], "a mark recorded as false is no mark")

        def unbound(d):
            rec = json.loads(read_nofollow(d, fx.SIGNOFF)[0])
            del rec["sha256"][fx.DOSSIER]
            write_nofollow(d, fx.SIGNOFF, json.dumps(rec).encode())
        free = got(b.edited(hm, unbound))
        need(free["dossier_coverage"]["status"] == "look" and "binds" in free["dossier_coverage"]["says"], "a dossier the sign-off record holds no hash for is not ok")

    def values():
        c = got(b.folder(hm, "value_no_mark"))["values_sourced"]
        need(c["status"] == "look" and "cannot vouch" in c["says"] and "1 matched" in c["says"], f"no mark in the spec is look, whatever the engine record says ({c['says']})")
        c = got(b.folder(hm, "C1_outcome_adjustment"))
        need(c["values_sourced"]["status"] == "look" and "no key for a mark" in c["values_sourced"]["says"] and c["effect_window"]["status"] == "look",
             "an outcome adjustment carries no mark: look in both checks")

    def record():
        def signed_at(value):
            def edit(d):
                rec = json.loads(read_nofollow(d, fx.SIGNOFF)[0])
                rec["signed_at"] = value
                write_nofollow(d, fx.SIGNOFF, json.dumps(rec).encode())
            return edit
        c = got(b.edited(hm, signed_at("yesterday")))["decision_date"]
        need(c["status"] == "not_available" and "signed_at" in c["says"], "with no day of signature the producer's rule cannot be applied")
        c = got(b.edited(hm, signed_at("2026-03-01T00:00:00+00:00")))["decision_date"]
        need(c["status"] == "look" and "2026-03-01" in c["says"], "signed after the delivery period started: the rule raises it")

    b.case("SC1 seven checks, each ok on the clean run, each with case, evidence and one sentence; a failing check says so", shape)
    b.case("SC2 the sheet: a REFUSED line, an unknown severity, a malformed row, another spec's sheet, no sources line, the bare word", sheet)
    b.case("SC3 the dossier: changed after signing, an entry that contradicts itself, a malformed entry, a false mark, an unbound file", dossier)
    b.case("SC4 values: no mark in the spec is look whatever a derived file says; an outcome adjustment has no mark", values)
    b.case("SC5 the decision date: the day of the signature is needed, and is the rule's today", record)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: spec_checks.py --self-test")
