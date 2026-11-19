#!/usr/bin/env python3
"""The Spec and evidence view (docs/design/review-workbench.md §5.3): the decision as signed, each value the signed
spec sets that scales an effect beside the evidence behind it, the evidence dossier, the sign-off history, and what
the sign-off sheet showed before signing.

    python3 tools/workbench/views/spec.py --self-test

`view(run)` returns {checks, facts, tables, charts, notes} and `images`, read from the verified signed copy of the spec
(checks.verified_spec); the spec on disk is named in a note when it differs. views/spec_read.py reads the run into
one model, views/spec_checks.py runs the seven checks on it, and this module lays the result out in the order the
owner reads it: the decision, the effects, the dossier (entries with no data first), the current signature and the
sign-off history, the pre-sign results. What the cases hold the view to, each from §3 or §5.3 (its second "Settled"
list wins over the first, and both over an earlier line), with what the specification leaves open marked "assumed":

 A1  facts: the 23 names of FACTS, no other. decision_maker; comparator (comparator.package); decision_date as
     written; window_start and window_end (objective.horizon); delivery_start and delivery_end (objective.delivery,
     null when the spec has none); envelope (constraints.envelope_usd); n_components; n_effect_inputs (the rows of
     tables.effects); n_effect_beyond_evidence and n_effect_no_followup (over the outcome-adjustment rows); the dossier
     counts; n_signoffs (sign-off records, archived and current); signed_by, signed_at, note_empty (the current
     record); n_presign_high. A fact the run does not record is null.
 A2  tables, with the columns of COLUMNS: decision, effects, dossier, signoffs (oldest first), presign. A table the
     run does not record is present with the value null. Assumed: two more tables, `signature` (the current
     signature alone, with what it changed, shown before the history, which the model orders oldest first) and
     `images` (the harness figures beside this view, §5.12).
 A3  effects (settled): one row per outcome adjustment (`where` outcome_adjustments) and one per engine parameter
     value the signed spec sets (`where` engine_parameters, id the parameter's name, component and reference null).
     window_years is the outcome window's length, for outcome adjustments only. Settled: an outcome adjustment has no
     key that cites a dossier entry and its source text is never matched to one, so follow_up_years and beyond are
     null for every outcome adjustment today. `verified` is the signed spec entry's own `verified` key
     (toolkit/spec/engine_parameters.py KEYS) where it has one, else the engine record's mark for that parameter;
     null for an outcome adjustment, whose keys are closed.
 A4  spec.effect_window (C1): `look` naming gap H1 for every outcome adjustment; `ok` only with none, and the sentence
     says so. spec.values_sourced (settled): `ok` only when the signed spec's own entry carries the mark fetched or
     matched; a value marked unverified, or with no mark in the spec, or with no source, is `look` with the count; a
     mark that is none of the producer's three words is `not_available` naming it. Assumed: an outcome adjustment,
     whose entry cannot carry the mark, is counted with the values that have none.
 A5  spec.dossier_coverage (C15): `look` for a no-data entry with no decision test, and for an observation with no
     checked mark. Settled: the mark is the observation's `checked` key (gap H2: no producer writes one); assumed: an
     observation counts as checked when the key is true or holds a date or text; n_observations_unchecked is null
     when no observation holds the key at all, and the check is then `look` saying the mark is not recorded.
     n_dossier_decided_by_owner and the column decided_by_owner are null: no field holds them.
 A6  spec.signoff_note: `look` for an empty note on the current signature; the sentence gives the number of spec
     sections that signature changed (top-level keys of the parsed spec against the sign-off before it), and a note
     says a change in comments alone is not seen. Settled: an archive folder with a sheet and no record is not a
     sign-off, is listed, and with a note present the check is `not_available` (the history has a gap).
 A7  spec.presign_sections: `look` when the sheet lacks a section the sign-off code writes for every spec
     (checks.SHEET_ALWAYS, held to the producers' source by fixtures.py X53), naming it and saying the sheet may
     predate it. spec.presign_high: `look` for a HIGH row, naming gap H14. Assumed: a presign row is a row of the
     spec critique table (severity its first cell, text its finding), a `- **HIGH**: ` line or a `- **REFUSED` line;
     its section is the heading above it without the `## ` marks.
 A8  spec.decision_date: the producer's rule (toolkit/spec/spec_critique.py _decision_date), with the day of the
     current signature as its `today` (settled). `look` when it raises anything, whatever the producer's severity.
 A9  tables.decision also holds the deliverable's own decision rule and windows where the kind has them (settled).
 A10 with no verified signed spec every check is `not_available` and says the signed copy could not be verified.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
from views import spec_read as R  # noqa: E402
from views.spec_checks import CHECKS, run_all  # noqa: E402

FACTS = ("decision_maker", "comparator", "decision_date", "window_start", "window_end", "delivery_start", "delivery_end",
         "envelope", "n_components", "n_effect_inputs", "n_effect_beyond_evidence", "n_effect_no_followup", "n_dossier",
         "n_dossier_observed", "n_dossier_no_data", "n_dossier_decided_by_owner", "n_observations",
         "n_observations_unchecked", "n_signoffs", "signed_by", "signed_at", "note_empty", "n_presign_high")
COLUMNS = {"decision": ["item", "value", "key"],
           "effects": ["id", "component", "value", "reference", "source", "follow_up_years", "window_years", "beyond", "where",
                       "verified"],
           "dossier": ["key", "kind", "model_value", "n_observations", "chosen", "low", "high", "decision_test", "no_data",
                       "decided_by_owner"],
           "signature": ["at", "by", "note", "n_sections_changed", "sections_changed"],
           "signoffs": ["at", "by", "note", "spec_hash", "sections_changed"],
           "presign": ["section", "severity", "text"],
           "images": ["figure", "path", "sha256", "title", "caption"]}
# The facts a table already holds are not repeated above it (the empty string); the counts get plain words.
FACT_LABELS = {
    "decision_maker": "", "comparator": "", "decision_date": "", "window_start": "", "window_end": "", "delivery_start": "",
    "delivery_end": "", "envelope": "", "signed_at": "",
    "n_components": "components the spec can allocate", "n_effect_inputs": "values the spec sets that scale an effect",
    "n_effect_beyond_evidence": "outcome adjustments whose recorded follow-up is shorter than the window (none can be counted: no follow-up is recorded)",
    "n_effect_no_followup": "outcome adjustments with no recorded follow-up",
    "n_dossier": "dossier entries", "n_dossier_observed": "dossier entries with observations",
    "n_dossier_no_data": "dossier entries that say no data", "n_dossier_decided_by_owner": "dossier entries decided by the owner (not recorded)",
    "n_observations": "observations in the dossier", "n_observations_unchecked": "observations not marked as opened",
    "n_signoffs": "sign-offs of this spec, archived and current", "signed_by": "current signature by",
    "note_empty": "current signature's note is empty", "n_presign_high": "rows the sign-off sheet marks HIGH"}
SIGNED, SHEET, SIGNOFF = R.SIGNED, R.SHEET, checks.SPEC_SIGNOFF
ARCHIVE = f"{checks.ARCHIVE_DIR}/{checks.ARCHIVE_PREFIX}*"


def _table(name, rows, about, **more):
    """A table of the result, or None when the run does not record it."""
    return None if rows is None else {"columns": list(COLUMNS[name]), "rows": [[r[c] for c in COLUMNS[name]] for r in rows], "about": about, **more}


def _dossier_rows(d):
    """One row per entry, those that rest on no data first: with no decision test, then with one; then the rest. Within
    each, an entry whose value is a whole mapping (one value per group, say) comes after those with one value, so the
    first rows read as a list."""
    rows = [{**e, "model_value": R.plain(e["model_value"]), "chosen": R.plain(e["chosen"]), "low": R.plain(e["low"]),
             "high": R.plain(e["high"]), "decided_by_owner": None} for e in d["entries"]]
    return sorted(rows, key=lambda e: (not e["no_data"], bool(e["decision_test"]), isinstance(e["chosen"], (dict, list))))


def _marks_said(m):
    """One sentence on whose marks the effects table shows for this run."""
    values = [r for r in m["effects"] or [] if r["where"] == "engine_parameters"]
    own = sum(r["own_mark"] is not None for r in values)
    if not values:
        return ""
    return (f" In this run {own} of the {len(values)} engine parameter values carry the signed spec's own mark"
            + ("." if own == len(values) else f"; for the other {len(values) - own} the mark shown is the engine record's, for the card's source."))


def _tables(m):
    ok, d, s, h = m["spec"]["verified"], m["dossier"], m["sheet"], m["history"]
    window = f"{m['window'][0]:g} to {m['window'][1]:g}" if m["window"][0] is not None else "not given"
    cur = m["current"]
    sheet_ok = ok and s is not None and s["spec_sha"] == m["spec"]["sha"]
    return {
        "decision": _table("decision", m["decision"], (
            f"The decision as signed, one row per item of the signed spec ({SIGNED}); key is the spec key the value is read "
            "from, and a value the spec does not hold is empty. window: the period over which the outcome is counted; "
            "delivery: the period in which the options are delivered and paid for; envelope: the budget the plan must fit; "
            "comparator: the package every option is compared with; components: what a package can hold; packages: how "
            "packages are formed from them (generate: every allowed combination); the last rows are the deliverable's own "
            "decision rule where its kind has one."), lead=True, wrap=["value"],
            labels={"item": "what", "value": "as signed", "key": "spec key"}),
        "effects": _table("effects", m["effects"] or None, (
            f"Every value the signed spec sets that scales an effect ({SIGNED}). where: outcome_adjustments (a fixed ratio "
            "applied to the outcome of each package that holds the component and not the reference, over the whole outcome "
            f"window, here {window}) or engine_parameters (a value the spec sets in place of the engine card's). "
            "follow_up_years: how long the evidence was followed, from a structured field only; it is empty for every row "
            "because no run records it as data. window_years: the length of the outcome window an adjustment is applied "
            "over. verified: the mark on the value's source, fetched (the source was opened), matched (checked against "
            f"the value) or unverified; the signed spec's own mark where it has one, else the engine record's ({R.ENGINE_REC}), "
            f"which describes the engine card's source and cannot vouch for the spec's.{_marks_said(m)} Needs you: each row "
            "the first two checks name."), lead=True, wrap=["source"],
            labels={"where": "set in", "verified": "mark on the source: the spec's own, else the engine record's",
                    "follow_up_years": "evidence followed (years)",
                    "window_years": "applied over (years)", "beyond": "beyond its evidence"}),
        "dossier": _table("dossier", None if d is None or d["bad"] else _dossier_rows(d), (
            f"One row per input of the evidence dossier the signed spec names ({d['file'] if d else 'none'}), entries that "
            "say no data first, those with no decision test before those with one. kind: the kind of input (the key's part "
            "before the colon); model_value: what the model uses, as recorded; n_observations: field or study observations "
            "listed for it; chosen, low, high: the value or range the dossier settles on; decision_test: the test that runs "
            "the plan under another value of this input; no_data: the entry says no measurement exists and borrows a rule. "
            "decided_by_owner is empty: no field records who decided. Needs you: every row with no data and no test."),
            lead=True, wrap=["model_value"],
            labels={"model_value": "what the model uses", "chosen": "value settled on", "low": "or range, from", "high": "to"}),
        "signature": _table("signature", None if not ok or cur is None or len(h["rows"]) < 2 else [{
            **cur, "note": cur["note"] if (cur["note"] or "").strip() else "(empty: what was accepted is not recorded)",
            "n_sections_changed": None if cur["sections_changed"] is None else len(cur["sections_changed"])}], (
            f"The current signature ({cur['file'] if cur else SIGNOFF}): who signed, when, the note that says what was "
            "accepted, and the sections of the spec (its top-level keys) that differ from the spec of the sign-off before "
            "it. An empty note leaves what was accepted unrecorded."), lead=True, wrap=["note"],
            labels={"n_sections_changed": "spec sections it changed", "sections_changed": "which sections"}),
        "signoffs": _table("signoffs", h["rows"] or None, (
            f"Every sign-off of this spec, oldest first: the archived records ({ARCHIVE}/{SIGNOFF}) and then the current one. "
            "spec_hash: the sha256 of the spec that was signed; sections_changed: the top-level keys of that spec that "
            "differ from the spec signed before it (empty for the first, and where a signed copy cannot be read with its "
            "recorded hash)."), lead=True, wrap=["note"], labels={"sections_changed": "against the sign-off before it"}),
        "presign": _table("presign", s["rows"] if sheet_ok else None, (
            f"What the sign-off sheet ({SHEET}) marked before signing: each row of its spec critique with the producer's "
            "severity (HIGH, MEDIUM, DECLARED: covered by a declared departure, INFO), and each line it marks HIGH or "
            "REFUSED, under the section of the sheet that holds it. The sheet is what the owner read; the sign-off record "
            "holds no hash of it."), lead=True, wrap=["text"]),
        "images": _table("images", m["images"] or None, (
            f"The harness figures that belong beside this view ({R.FIGS}: {', '.join(R.SPEC_FIGURES)}), each with the hash of the "
            "image as read.")),
    }


def _facts(m):
    f = dict.fromkeys(FACTS)
    h, d, s, cur = m["history"], m["dossier"], m["sheet"], m["current"]
    f["n_signoffs"] = len(h["rows"]) or None
    if not m["spec"]["verified"]:
        return f
    doc = m["spec"]["doc"]
    oa = [r for r in m["effects"] if r["where"] == "outcome_adjustments"]
    f.update(decision_maker=R.plain(doc.get("decision_maker")), comparator=R.plain(R.at(doc, "comparator.package")),
             decision_date=m["date"]["written"], window_start=m["window"][0], window_end=m["window"][1],
             delivery_start=m["delivery"][0], delivery_end=m["delivery"][1], envelope=R.plain(R.at(doc, "constraints.envelope_usd")),
             n_components=m["n_components"], n_effect_inputs=len(m["effects"]),
             n_effect_beyond_evidence=sum(r["beyond"] is True for r in oa), n_effect_no_followup=sum(r["follow_up_years"] is None for r in oa),
             signed_by=m["spec"]["signed_by"], signed_at=m["spec"]["signed_at"],
             note_empty=None if cur is None else not (cur["note"] or "").strip())
    if d is not None and not d["bad"]:
        es = d["entries"]
        marked = sum(e["marked"] for e in es)
        f.update(n_dossier=len(es), n_dossier_observed=sum(e["n_observations"] > 0 for e in es), n_dossier_no_data=sum(e["no_data"] for e in es),
                 n_observations=sum(e["n_observations"] for e in es),
                 n_observations_unchecked=sum(e["n_unchecked"] for e in es) if marked else None)
    if s is not None and s["spec_sha"] == m["spec"]["sha"] and not s["bad"]:
        f["n_presign_high"] = sum(r["severity"] == "HIGH" for r in s["rows"])
    return f


def _notes(m, tables):
    spec, h = m["spec"], m["history"]
    notes = {"reading": (
        "Read in this order: the decision as signed; the values the spec sets that scale an effect, each beside its source; "
        "the evidence dossier, entries with no data first; the current signature and the history of sign-offs; what the "
        "sign-off sheet showed before signing. An outcome adjustment is a fixed ratio the spec applies to the outcome over the "
        "whole outcome window. A decision test runs the plan again under another value of one input. The sign-off sheet is "
        "the page the owner reads before signing; a HIGH row on it is a finding the gate would raise after compute.")}
    if spec["verified"]:
        disk = {"same": f"The spec on disk ({R.SPEC}) is the same file.",
                "changed": f"The spec on disk ({R.SPEC}) differs from the signed copy; nothing here is read from it (the Steps view raises it).",
                "missing": f"The run holds no readable spec on disk ({R.SPEC}); nothing here is read from it."}[m["disk"]]
        notes["spec"] = (f"Everything here is read from the signed copy of the spec, {SIGNED} ({spec['sha'][:8]}), signed by "
                         f"{spec['signed_by']} at {str(spec['signed_at'])[:16]}; its sha256 is the one the sign-off record "
                         f"({spec['record']}) holds. {disk}")
    else:
        notes["spec"] = (f"No signed copy of the spec could be verified ({spec['why']}), so nothing of the spec is shown: the "
                         "decision, the effects, the dossier and the sheet are read only through a verified signed copy. The "
                         "sign-off records the run holds are still listed.")
    notes["not_recorded"] = (
        "Three things are shown as empty because no run file records them as data, and no text is read for them: how long "
        "the evidence behind an outcome adjustment was followed (gap H1: an adjustment has no such field and no key that "
        "cites a dossier entry), whether a dossier observation's source was opened (gap H2: the key `checked` on an "
        "observation, which no producer writes yet), and who decided a dossier entry.")
    notes["sections"] = (
        "sections_changed compares the parsed spec of each sign-off with the one signed before it, by top-level key. A change "
        "in comments alone is not seen, and a change inside a section is not described: the two signed copies are named in "
        "the table of sign-offs by their hashes."
        + (" Not sign-offs: " + "; ".join(f"{folder} ({why})" for folder, why in h["bare"]) + "." if h["bare"] else ""))
    if tables["effects"] is None and spec["verified"]:
        notes["effects"] = (m["oa_bad"] or m["values_bad"] or
                            "The signed spec sets no value that scales an effect: it has no outcome adjustment and no engine parameter value.")
    if tables["dossier"] is None and spec["verified"]:
        notes["dossier"] = (m["dossier_why"] or "; ".join(m["dossier"]["bad"][:3])) + "."
    if tables["presign"] is None and spec["verified"]:
        notes["presign"] = (f"{m['sheet_why']}." if m["sheet"] is None else
                            f"{SHEET} does not record the hash of the verified signed spec, so its rows are not shown as what the owner read.")
    if tables["signoffs"] is None:
        notes["signoffs"] = "The run holds no sign-off record for the spec."
    if tables["signature"] is None and spec["verified"]:
        notes["signature"] = ("This spec has one sign-off, the row of the table of sign-offs: there is none before it to compare with."
                              if m["current"] is not None else "The run holds no sign-off record for the spec.")
    if tables["images"] is None:
        notes["images"] = (f"The run has no readable figure index ({R.FIGS})." if m["images"] is None else
                           f"The figure index lists none of the figures that belong beside this view ({', '.join(R.SPEC_FIGURES)}) with an image that can be read.")
    return notes


def view(run):
    m = R.read(run)
    tables = _tables(m)
    images = [{"table": "images", "path": "path", "hash": "sha256", "title": "title", "caption": "caption", "after": "dossier"}] if tables["images"] else []
    return {"checks": run_all(m), "facts": _facts(m), "tables": tables, "charts": [], "notes": _notes(m, tables), "images": images,
            "fact_labels": dict(FACT_LABELS)}


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import json

    import yaml

    import workbench
    from lib.safe_io import write_nofollow
    from views import overview
    from views.fit_bench import Bench, check, close, fixture_builder, need, says
    fx = fixture_builder()
    b = Bench(lambda run: view(run))        # the module's `view` as it is when a case runs
    folder, look_at, case = b.folder, b.look_at, b.case
    hm, post = fx.METHODS
    est = dict(figures="estimates", kind="estimates")
    FACTS = ("decision_maker", "comparator", "decision_date", "window_start", "window_end", "delivery_start", "delivery_end",
             "envelope", "n_components", "n_effect_inputs", "n_effect_beyond_evidence", "n_effect_no_followup", "n_dossier",
             "n_dossier_observed", "n_dossier_no_data", "n_dossier_decided_by_owner", "n_observations",
             "n_observations_unchecked", "n_signoffs", "signed_by", "signed_at", "note_empty", "n_presign_high")
    COLUMNS = {"decision": ["item", "value", "key"],
               "effects": ["id", "component", "value", "reference", "source", "follow_up_years", "window_years", "beyond", "where",
                           "verified"],
               "dossier": ["key", "kind", "model_value", "n_observations", "chosen", "low", "high", "decision_test", "no_data",
                           "decided_by_owner"],
               "signoffs": ["at", "by", "note", "spec_hash", "sections_changed"],
               "presign": ["section", "severity", "text"]}
    CHECKS = {"effect_window": "C1", "values_sourced": None, "dossier_coverage": "C15", "signoff_note": None,
              "presign_sections": None, "presign_high": None, "decision_date": None}
    SECTIONS = tuple(h[3:] for h in fx.SHEET_ALWAYS)       # a row's section is the heading without its `## ` marks
    signed, signoff = fx.SPEC_FILES[1], fx.SIGNOFF

    def statuses(v):
        return {str(c.get("id")): c.get("status") for c in v["checks"]}

    def status(v, name):
        return check(v, name)["status"]

    def table(v, name):
        t, columns = v["tables"].get(name), COLUMNS[name]
        need(isinstance(t, dict) and t.get("columns") == columns, f"tables.{name} has columns {columns} (got {t.get('columns') if isinstance(t, dict) else t})")
        need(all(isinstance(r, list) and len(r) == len(columns) for r in t["rows"]), f"tables.{name} rows are lists in column order")
        return [dict(zip(columns, r)) for r in t["rows"]]

    def row(v, name, value):
        key = COLUMNS[name][0]
        found = [r for r in table(v, name) if r[key] == value]
        need(len(found) == 1, f"one row of tables.{name} with {key} {value!r} (found {len(found)}: {[r[key] for r in table(v, name)]})")
        return found[0]

    def has(r, **want):
        bad = {k: (r.get(k), w) for k, w in want.items() if not (close(r.get(k), w) if isinstance(w, float) else r.get(k) == w)}
        need(not bad, f"row {r.get(next(iter(r)))}: " + "; ".join(f"{k} is {g!r}, expected {w!r}" for k, (g, w) in bad.items()))

    def every(d):
        """Every check of every view for a run folder, by its full id (the overview gathers them)."""
        return {str(c["id"]): c["status"] for c in overview.view(workbench.Run(d))["checks"]}

    def doc(d, rel):
        with open(os.path.join(d, rel), encoding="utf-8") as f:
            return json.load(f) if rel.endswith(".json") else yaml.safe_load(f)

    def own(v):
        return {k.split(".")[-1]: s for k, s in statuses(v).items()}

    def rows_at(v):
        return table(v, "signoffs")

    # ------------------------------------------------------------------ the generic model
    def m_shape():
        for method in fx.METHODS:
            v = look_at(folder(method))
            need(tuple(sorted(v["facts"])) == tuple(sorted(FACTS)), f"{method}: exactly the facts of §5.3 (differs in {sorted(set(v['facts']) ^ set(FACTS))})")
            need(sorted(c["id"] for c in v["checks"]) == sorted(f"spec.{n}" for n in CHECKS), f"{method}: the seven checks, ids spec.<name> (got {[c.get('id') for c in v['checks']]})")
            for c in v["checks"]:
                name = c["id"].split(".")[-1]
                need(c.get("step") == "spec" and c.get("status") in workbench.checks.STATUSES and c.get("title"), f"{name}: step, status and title")
                need(isinstance(c.get("says"), str) and c["says"].strip() and "\n" not in c["says"], f"{name}: one plain sentence")
                ev = c.get("evidence")
                need(isinstance(ev, list) and ev and all(isinstance(e, dict) and e.get("file") and "field" in e for e in ev), f"{name}: names the file and field read ({ev})")
                need(c.get("case") == CHECKS[name], f"{name}: scorecard case {CHECKS[name]} (got {c.get('case')})")
            need(set(v["tables"]) >= set(COLUMNS), f"{method}: every table of the model is present (missing {sorted(set(COLUMNS) - set(v['tables']))})")
            for name in COLUMNS:
                table(v, name)
                need(v["tables"][name].get("about"), f"tables.{name} says what its rows are")
            need(v["notes"] and all(isinstance(t, str) and t.strip() for t in v["notes"].values()), "notes are plain text")
            need(any("signed" in t for t in v["notes"].values()), "a note says which copy of the spec was read (§3)")
            need(not workbench.chart_problems(v), f"every chart names a table and columns of the result ({workbench.chart_problems(v)})")

    def m_clean():
        for method in fx.METHODS:
            got = own(look_at(folder(method)))
            need(got == {n: "ok" for n in CHECKS}, f"{method}: on the clean run every check is ok ({ {k: s for k, s in got.items() if s != 'ok'} })")
            raised = {k: s for k, s in every(folder(method)).items() if s in ("look", "problem")}
            need(not raised, f"{method}: no view raises anything on the clean run ({raised})")

    def m_facts():
        for method in fx.METHODS:
            d = folder(method)
            f, spec, rec = look_at(d)["facts"], doc(d, signed), doc(d, signoff)
            want = {"decision_maker": spec["decision_maker"], "comparator": "A", "decision_date": "2026-01-01",
                    "window_start": 2026.0, "window_end": 2029.0, "delivery_start": 2026.0, "delivery_end": 2028.0,
                    "envelope": fx.ENVELOPE, "n_components": 3, "n_effect_inputs": 1, "n_effect_beyond_evidence": 0,
                    "n_effect_no_followup": 0, "n_dossier": 5, "n_dossier_observed": 2, "n_dossier_no_data": 3,
                    "n_dossier_decided_by_owner": None, "n_observations": 3, "n_observations_unchecked": 0, "n_signoffs": 3,
                    "signed_by": "owner", "signed_at": rec["signed_at"], "note_empty": False, "n_presign_high": 0}
            bad = {k: (f.get(k), w) for k, w in want.items() if not (close(f.get(k), w) if isinstance(w, float) else f.get(k) == w)}
            need(not bad, f"{method}: " + "; ".join(f"{k} is {g!r}, expected {w!r}" for k, (g, w) in bad.items()))

    def m_tables():
        for method in fx.METHODS:
            d = folder(method)
            v, spec = look_at(d), doc(d, signed)
            has(row(v, "decision", "decision_date"), value="2026-01-01", key="decision_date")
            has(row(v, "decision", "comparator"), value="A", key="comparator.package")
            need({"question", "decision_maker", "window", "delivery", "envelope"} <= {r["item"] for r in table(v, "decision")}, "the decision's items")
            need(all(r["key"] for r in table(v, "decision")), "each item names the spec key it came from")
            need([r["id"] for r in table(v, "effects")] == ["p2"], "the clean run holds no outcome adjustment (settled): one spec-set value")
            has(row(v, "effects", "p2"), component=None, value=3.1, reference=None, source="the owner's range", follow_up_years=None,
                window_years=None, beyond=None, where="engine_parameters", verified="matched")
            has(row(v, "dossier", "parameter:p1"), kind="parameter", n_observations=2, chosen=None, low=0.2, high=0.8,
                decision_test="DT3", no_data=False, decided_by_owner=None)
            has(row(v, "dossier", f"parameter:{fx.HELD}"), kind="parameter", n_observations=0, chosen=0.5, low=None, high=None,
                decision_test="DT7", no_data=True)
            has(row(v, "dossier", "unit_attribute:x1"), kind="unit_attribute", no_data=True, decision_test="DT9")
            need([r["no_data"] for r in table(v, "dossier")] == [True] * 3 + [False] * 2, "the dossier's entries with no data come first")
            sig = v["tables"]["signature"]
            need(sig["rows"] == [[rows_at(v)[-1]["at"], "owner", doc(d, signoff)["note"], 1, ["uncertainty"]]] and sig["lead"]
                 and "empty" in look_at(folder(method, "signoff_note_empty"))["tables"]["signature"]["rows"][0][2]
                 and list(v["tables"]).index("signature") < list(v["tables"]).index("signoffs"), "the current signature, with what it changed, before the history")
            need(tuple(SECTIONS) == workbench.checks.SHEET_ALWAYS, "the sections the view asks of a sheet are the producers' (fixtures.py X53)")
            need(v["images"] == [{"table": "images", "path": "path", "hash": "sha256", "title": "title", "caption": "caption", "after": "dossier"}]
                 and [r[0] for r in v["tables"]["images"]["rows"]] == ["data_coverage"] and not workbench.image_problems(v),
                 f"the harness figure that belongs beside this view (§5.12) is shown after the dossier ({v['tables']['images']})")
            rows = table(v, "signoffs")
            need([r["at"][:10] for r in rows] == [*fx.EARLIER, "2026-01-01"], f"sign-offs oldest first, archived then current ({[r['at'] for r in rows]})")
            need([r["sections_changed"] for r in rows] == [None, ["decision_maker"], ["uncertainty"]],
                 f"sections changed by top-level key against the sign-off before ({[r['sections_changed'] for r in rows]})")
            need(rows[-1]["spec_hash"] == doc(d, signoff)["sha256"]["decision_spec.yaml"] and all(r["by"] == "owner" and r["note"] for r in rows), "who, note and spec hash from each record")
            pre = table(v, "presign")
            need([(r["section"], r["severity"]) for r in pre] == [(SECTIONS[0], "MEDIUM")] * 2 + [(SECTIONS[0], "INFO")] * 2,
                 f"presign rows are the sheet's marked rows, each under its section ({[(r['section'], r['severity']) for r in pre]})")
            need("burden" in pre[0]["text"] and spec["comparator"]["package"] in pre[2]["text"], "each row's text is the finding")

    def e_kind():
        d = folder(post, **est)
        v, spec = look_at(d), doc(d, signed)
        rule = spec["deliverable"]["decision"]
        values = {r["item"]: r for r in table(v, "decision")}
        need(any(r["value"] == rule["threshold"] and r["key"] == "deliverable.decision.threshold" for r in values.values())
             and any(r["value"] == rule["probability"] and r["key"] == "deliverable.decision.probability" for r in values.values())
             and any(r["key"] == "deliverable.windows" for r in values.values()), "the decision table holds the deliverable's own rule and windows (settled)")
        f = v["facts"]
        need(f["window_start"] is None and f["comparator"] is None and f["envelope"] is None and f["n_components"] is None,
             "what this kind's spec does not hold is null")
        need([r["id"] for r in table(v, "effects")] == ["p2"] and f["n_effect_inputs"] == 1 and f["n_effect_no_followup"] == 0, "the spec-set value is still examined (settled)")
        need(status(v, "effect_window") == "ok" and "no outcome adjustment" in says(check(v, "effect_window")), "no outcome adjustment: ok, and said")
        need(not {k: s for k, s in own(v).items() if s in ("look", "problem")}, f"nothing is raised on this kind's clean run ({own(v)})")

    # ------------------------------------------------------------------ the settled rules
    def s_effects():
        v = look_at(folder(hm, "value_unverified"))
        need(row(v, "effects", "p2")["verified"] == "unverified" and status(v, "values_sourced") == "look" and "1" in says(check(v, "values_sourced")),
             "a spec-set value marked unverified is look, with the count")
        need(status(v, "effect_window") == "ok", "effect_window reads the rows applied over the outcome window only")
        v = look_at(folder(hm, "value_unknown_mark"))
        need(status(v, "values_sourced") == "not_available" and "seen" in says(check(v, "values_sourced")), "an unknown mark is not_available, naming it")
        v = look_at(folder(hm, "value_no_mark"))
        need(row(v, "effects", "p2")["verified"] == "matched" and status(v, "values_sourced") == "look" and "cannot vouch" in says(check(v, "values_sourced")),
             "with no mark in the signed spec the row shows the engine record's, which cannot vouch for it: look (settled)")

    def s_structured():
        v = look_at(folder(hm, "C1_outcome_adjustment"))
        has(row(v, "effects", "OA1"), component="C", value=0.8, reference="B", source=fx.OA["source"], follow_up_years=None,
            window_years=3.0, beyond=None, where="outcome_adjustments", verified=None)
        f = v["facts"]
        need((f["n_effect_inputs"], f["n_effect_no_followup"], f["n_effect_beyond_evidence"]) == (2, 1, 0),
             "an outcome adjustment has no follow-up, though a dossier observation with the same source text records five years: no source text is matched")
        need(status(v, "effect_window") == "look" and "h1" in says(check(v, "effect_window")) and status(v, "values_sourced") == "look", "look, naming gap H1")
        for name in ("C1_follow_up_prose_only", "C1_follow_up_short", "C1_source_not_in_dossier"):
            v = look_at(folder(hm, name))
            r = row(v, "effects", "OA1")
            need(r["follow_up_years"] is None and r["beyond"] is None and r["window_years"] == 3.0 and v["facts"]["n_effect_no_followup"] == 1
                 and v["facts"]["n_effect_beyond_evidence"] == 0, f"{name}: no follow-up from prose, from a dossier observation or from its absence")
            need(status(v, "effect_window") == "look" and "h1" in says(check(v, "effect_window")), f"{name}: look, naming gap H1")
        v = look_at(folder(hm, "C15_no_checked_marks"))
        need(v["facts"]["n_observations_unchecked"] is None and "h2" in says(check(v, "dossier_coverage")), "no checked field anywhere: null, and gap H2 named")
        v = look_at(folder(hm, "C15_observation_unchecked"))
        need(v["facts"]["n_observations_unchecked"] == 1, "one observation without its mark is counted")
        v = look_at(folder(hm, "C15_no_data_no_test"))
        need(fx.HELD in says(check(v, "dossier_coverage")) and row(v, "dossier", f"parameter:{fx.HELD}")["decision_test"] is None, "the entry with no data and no test is named")

    def s_date():
        for name, word in (("decision_date_after_delivery_start", "2026-09-01"), ("decision_date_unreadable", "soon")):
            v = look_at(folder(hm, name))
            need(status(v, "decision_date") == "look" and word in check(v, "decision_date")["says"], f"{name}: look, giving the date")
            need(v["facts"]["decision_date"] == word, "the fact is the date as written")
        need("spec_critique" in str(check(look_at(folder(hm)), "decision_date")["evidence"]) + check(look_at(folder(hm)), "decision_date")["says"]
             or "producer" in says(check(look_at(folder(hm)), "decision_date")), "the check says whose rule it applies")

    def s_sheet():
        v = look_at(folder(hm, "sheet_section_missing"))
        c = check(v, "presign_sections")
        need(c["status"] == "look" and "spec critique" in says(c) and "predate" in says(c), "the missing section is named, and the sheet may predate it")
        v = look_at(folder(hm, "presign_high_bullet"))
        need(v["facts"]["n_presign_high"] == 1 and "h14" in says(check(v, "presign_high")), "a HIGH line: counted, gap H14 named")
        need([r["section"] for r in table(v, "presign") if r["severity"] == "HIGH"] == [SECTIONS[5]], "the row sits under its section")
        v = look_at(folder(hm, "decision_date_after_delivery_start"))
        need([r["section"] for r in table(v, "presign") if r["severity"] == "HIGH"] == [SECTIONS[0]], "a HIGH row of the critique table is a row too")

    def s_note():
        v = look_at(folder(hm, "signoff_note_empty"))
        c = check(v, "signoff_note")
        need(c["status"] == "look" and v["facts"]["note_empty"] is True and "1 section" in says(c), f"an empty note, with the one section the signature changed ({c['says']})")
        v = look_at(folder(hm, "signoff_note_empty_many_sections"))
        need("6 sections" in says(check(v, "signoff_note")) and len(table(v, "signoffs")[-1]["sections_changed"]) == 6, "an empty note on a signature that changed six sections reads as such")
        need(any("comment" in t for t in look_at(folder(hm))["notes"].values()), "a note says a change in comments alone is not seen")

    def s_disk():
        d = folder(hm, "declared_only_on_disk")
        v = look_at(d)
        need(own(v) == {n: "ok" for n in CHECKS}, "the signed copy is read, whatever the spec on disk says")
        need(any("decision_spec.yaml" in t and "differ" in t for t in v["notes"].values()), "and a note names the spec on disk as differing (§3)")

    # ------------------------------------------------------------------ each planted edit, across every view
    def planted(method, name):
        def run():
            spec = fx.DEFECTS[name]
            before, after = every(folder(method)), every(folder(method, name))
            want = {f"spec.{k}": s for k, s in {spec["check"]: spec["status"], **spec.get("also", {})}.items()}
            want = {k: s for k, s in {**want, **spec.get("across", {})}.items() if before.get(k) != s}
            got = {k: s for k, s in after.items() if before.get(k) != s}
            need(set(before) == set(after) and got == want, f"moves {got}, expected {want}")
        return run

    def others_edits():
        for method in fx.METHODS:
            before = own(look_at(folder(method)))
            for name, spec in fx.DEFECTS.items():
                if spec.get("view") == "spec" or method not in spec["methods"] or "package_per_unit" not in spec.get("kinds", fx.KINDS[:1]) or "against" in spec:
                    continue
                want = {k[5:]: s for k, s in spec.get("across", {}).items() if k.startswith("spec.")}
                after = own(look_at(folder(method, name)))
                got = {k: s for k, s in after.items() if before.get(k) != s}
                need(got == want, f"{method}, {name}: moves {got}, expected {want}")

    # ------------------------------------------------------------------ fail closed
    def f_missing():
        d = os.path.join(b.tmp, "empty")
        os.makedirs(d)
        write_nofollow(d, "run_state.json", b"{}")
        need(own(look_at(d)) == {n: "not_available" for n in CHECKS}, "a run with none of the files: every check not_available")
        got = own(look_at(folder(hm, upto="launch")))
        need(got == {n: "not_available" for n in CHECKS}, f"before the sign-off there is no signed spec to read ({got})")

    def f_unverified():
        v = look_at(folder(hm, "spec_unverified"))
        need(own(v) == {n: "not_available" for n in CHECKS}, f"with no verified signed spec nothing is ok or look ({own(v)})")
        need(all("signed" in says(c) for c in v["checks"]), "and each check says the signed copy could not be verified")
        need(v["facts"]["n_effect_inputs"] is None and v["tables"]["effects"] is None and v["facts"]["decision_date"] is None, "nothing is shown from a spec that is not verified")

    def f_pieces():
        v = look_at(folder(hm, "sheet_missing"))
        need(v["facts"]["n_presign_high"] is None and v["tables"]["presign"] is None and fx.SHEET in check(v, "presign_sections")["says"] + str(check(v, "presign_sections")["evidence"]),
             "no sheet: the count is null, never zero, and the file is named")
        v = look_at(folder(hm, "dossier_unparsable"))
        need(v["facts"]["n_dossier"] is None and v["tables"]["dossier"] is None and v["notes"].get("dossier") and status(v, "effect_window") == "ok",
             "a dossier that cannot be parsed: no counts, no table, the reason in a note; the effects do not rest on it")
        v = look_at(folder(hm, "archive_no_record"))
        need(v["facts"]["n_signoffs"] == 3 and len(table(v, "signoffs")) == 3 and fx.EARLIER[1] + "b" in check(v, "signoff_note")["says"],
             "a folder with a sheet and no record is not a sign-off, and is named")

        def no_note(d):
            rec = doc(d, signoff)
            rec["note"] = ""
            write_nofollow(d, signoff, json.dumps(rec).encode())
        need(status(look_at(b.edited(hm, no_note, "archive_no_record")), "signoff_note") == "look", "an empty note is still raised beside such a folder")

    def f_words():
        words = fx.domain_words()
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            text = f.read().lower()
        need(len(words) > 50 and not [w for w in words if w in text], f"no domain word in this module ({[w for w in words if w in text][:5]})")
        for d in (folder(hm), folder(post, **est)):
            out = json.dumps(look_at(d)).lower()
            need(not [w for w in words if w in out], "nor in what the view returns for a fixture run: every name comes from the run")

    case("M1 the generic model: the five parts, the 23 facts, seven checks with sentence, evidence and case, five tables, valid charts", m_shape)
    case("M2 on both clean runs every check is ok and no view raises anything", m_clean)
    case("M3 facts on both clean runs, each from the file that holds it", m_facts)
    case("M4 tables: the decision, both kinds of effect input, the dossier, the sign-off history with sections changed, the sheet's rows", m_tables)
    case("E1 the estimates kind: its own decision rule and windows; a spec-set value with no outcome adjustment", e_kind)
    case("S1 settled: effects holds every spec-set value with `where` and `verified`; values_sourced is ok only on the spec's own mark; effect_window reads outcome adjustments", s_effects)
    case("S2 settled: only structured fields count (an outcome adjustment has no follow-up; checked mark; who decided); the checks name gaps H1 and H2", s_structured)
    case("S3 settled: decision_date follows the producer's rule", s_date)
    case("S4 settled: presign_sections names the section and says the sheet may predate it; presign_high names gap H14", s_sheet)
    case("S5 settled: signoff_note counts the sections the signature changed; comments are not seen, and said", s_note)
    case("S6 settled (§3): the signed copy is read and the spec on disk named when it differs", s_disk)
    n = 0
    for method in fx.METHODS:
        for name in fx.SPEC_DEFECTS:
            spec = fx.DEFECTS[name]
            n += 1
            also = "".join(f", {k} {s}" for k, s in {**spec.get("also", {}), **spec.get("across", {})}.items())
            case(f"P{n} {method}, {name}: {spec['check']} is {spec['status']}{also}, and no other check of any view changes", planted(method, name))
    case("Q1 the other views' edits move the Spec checks they name, and no other", others_edits)
    case("F1 fail closed: no files, and a run not yet signed", f_missing)
    case("F2 fail closed: with no verified signed spec every check is not_available and says so", f_unverified)
    case("F3 fail closed: no sheet, a dossier that is not YAML, an archive folder with no record", f_pieces)
    case("F4 no domain word in this module or in what it returns; names come from the run", f_words)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: spec.py --self-test")
