#!/usr/bin/env python3
"""The Model view (docs/design/review-workbench.md §5.4): the engine a run chose, its benchmarks against their bands,
the departures the spec signed, and the engine's parameters beside the values the spec sets.

    python3 tools/workbench/views/model.py --self-test

views/model_read.py reads the files into one generic model (the engine record analysis/engine.yaml, never the
repository's engine card; engine_choice.json; the verified signed spec; the decision tests; the benchmarks recomputed
at the spec's values; the figure index). This module runs the eight checks on that model and returns {checks, facts,
tables, charts, notes, images}. Reading order for the owner: the benchmark table and its charts, then departures, the
engine choice and its candidates, the values the spec sets against the card, every parameter, and the mechanisms.

The rules, each from §3 or §5.4 (its "Settled" lists win over an earlier line), with what §5.4 leaves open marked
"assumed":

 A1  facts: the fifteen names of §5.4 (FACTS below); check ids are model.<name> for the eight checks of CHECKS. A
     count is null when the file it comes from cannot be read, never zero.
 A2  tables.parameters has the ten columns of the generic model followed by value_used, spec_low and spec_high
     (assumed: appended in that order). For a parameter the spec does not set, owner_value, card_value,
     outside_card, spec_low and spec_high are null and value_used is the card's value (assumed). A table
     spec_values (assumed name) repeats the values the spec sets beside the card's value and range, so they can be
     read first.
 A3  `gates` is false when the chosen candidate lists the benchmark under benchmarks_reported, or when the benchmark's
     row holds a `covers` that is empty; true otherwise, also when there is no choice record (fail closed).
 A4  model.benchmarks: `problem` for a gating benchmark that is not in band (out_of_band, not_assessable,
     band_mis_based) with no departure that counts, and for a row whose status contradicts its own value, band or
     in_band flag; `look` for one not gating or covered, one in band within a quarter of the band's log width of an
     edge, and an engine record with no benchmark; `not_available` for an unknown status word and for a row in band
     with no band to place it in. A departure counts only when the verified signed spec holds it for this engine,
     the engine choice marks it `signed` and its decision test ran (§3). Assumed: "a band that cannot be reached is
     look" (§5.4) is read with the gating rule before it, so a gating one with no departure is still `problem`.
 A5  model.departures (settled): `look` for any signed departure whose test ran, with its verdict and share, and a
     near miss said where a NO_CHANGE share is within 25% of the test's own threshold; `problem` when the test did
     not run, the departure names no test, the choice record marks signed a departure the verified signed spec does
     not hold, or the engine choice did not count it (status other than signed); `not_available` with no decision
     tests file or a status word the producers do not write; `ok` when the verified signed spec declares none.
 A6  model.choice_text reads no free text: it compares the ids in the chosen candidate's benchmarks_not_in_band with
     the table's rows that gate, are not in band and are named by no departure of that candidate. A choice record of
     an older shape, with no such list, is `not_available`. Assumed: a choice record whose chosen engine has another
     name or version than the engine record is a `problem` too (two files that disagree, §3).
 A7  model.spec_values: `look` when the record's flag is true and the record was computed from the verified signed
     spec (its header's spec hash), so the flag is that spec's own; `problem` when the flag contradicts the record's
     own numbers, or is true with no verified signed spec behind the record (assumed).
 A8  model.choice_bound: every analysis header that holds a hash of the engine choice is compared with the file:
     all the same is `ok`; one that differs, is malformed or has no file to compare is `problem`. All null: `ok`
     when the verified signed spec names the engine, `look` (gap H10) when it leaves the engine to be chosen,
     `problem` with no verified signed spec.
 A9  the ranges chart's `scale` is "log" when some band spans more than a factor of one hundred (settled). Assumed:
     a second chart, interval_points on one log axis, shows each band with the value where it is, since the ranges
     chart draws a row inside its own band.
 A10 `images` is a list beside checks, facts, tables, charts and notes: each entry {table, path, hash, title,
     caption} names a table and its columns. The view shows the harness figures that belong beside it (§5.12:
     parameters, design_coverage, sensitivity) through a table `images` (figure, path, sha256, title, caption).
 A11 notes: `engine` (which record was read), `spec` (which copy of the spec, and who signed it), `choice` (the
     record's own reason), `reading` (the terms) and, for an engine record with no benchmark, `benchmarks`.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
from views import model_read as R  # noqa: E402
from views.model_checks import CHECKS, run_all  # noqa: E402

FACTS = ("engine_name", "engine_version", "n_candidates", "n_eligible", "n_benchmarks", "n_in_band", "n_out_of_band",
         "n_unreachable", "n_near_edge", "n_not_gating", "n_not_assessable", "n_departures", "n_owner_values",
         "n_owner_values_outside_card", "choice_bound")
COLUMNS = {
    "candidates": ["name", "version", "eligible", "n_reasons", "first_reason"],
    "benchmarks": ["id", "metric", "role", "target", "band_low", "band_high", "computed", "seed_min", "seed_max", "status",
                   "gates", "source", "departure_test", "departure_verdict"],
    "departures": ["subject", "kind", "reason", "status", "signed", "test", "verdict", "share_pct", "share_threshold_pct"],
    "parameters": ["name", "value", "low", "high", "units", "kind", "source", "owner_value", "card_value", "outside_card",
                   "value_used", "spec_low", "spec_high"],
    "mechanisms": ["text"],
    "benchmarks_at_spec_values": ["id", "computed", "status", "spec_hash", "test"],
    "images": ["figure", "path", "sha256", "title", "caption"],
}
SPEC_VALUES = ["name", "units", "card_value", "card_low", "card_high", "value", "spec_low", "spec_high", "outside",
               "uncertainty", "source", "reason"]
IMAGE_KEYS = ("table", "path", "hash", "title", "caption")
ENGINE, CHOICE, DT, SIGNED = R.ENGINE_REC, R.CHOICE, R.DT, R.SIGNED


def _table(name, rows, about, columns=None, empty=None, **more):
    """A table of the result, or None when the run does not record it. With no rows it says so in one sentence
    (`empty`) and is not shown first: there is nothing in it for the reader."""
    cols = columns or COLUMNS[name]
    if rows is None:
        return None
    if not rows and empty:
        return {"columns": list(cols), "rows": [], "about": empty}
    return {"columns": list(cols), "rows": [[r[c] for c in cols] for r in rows], "about": about, **more}


def _tables(m):
    e = m["engine"]
    return {
        "benchmarks": _table("benchmarks", m["benchmarks"], (
            f"One row per benchmark of the engine as this run used it ({ENGINE} data.card.benchmarks): a published result the "
            "engine is run against. role: fit (a fitted parameter was tuned to it) or held_out (used in no fit). target: the "
            "published value; band: the range the engine's value must fall in; computed: the engine's value (the mean over "
            "seeds); seed_min and seed_max: the lowest and highest single-seed value. status: the producer's word (in_band, "
            "out_of_band, not_assessable, or band_mis_based: the band cannot be reached). gates: yes when the engine is "
            f"eligible only if the benchmark is in band or a signed departure covers it; no when the engine choice only reports "
            f"it or it covers no component ({CHOICE}). departure_test and departure_verdict: the decision test of the signed "
            f"departure that names it, and that test's verdict ({SIGNED}, {DT}). Needs you: every row not in band, and every "
            "row the first check names."),
            empty=f"This engine ({e['name']}) records no benchmark ({ENGINE} data.card.benchmarks is an empty list).",
            lead=True, wrap=["metric", "source"]),
        "departures": _table("departures", m["departures"], (
            f"One row per engine departure of the verified signed spec ({SIGNED} engine_departures): a benchmark not in band, or a "
            "component no benchmark covers, that the owner accepted with a decision test that varies the effect. status: the "
            f"engine choice's word for it ({CHOICE}); signed: the verified signed spec holds it; share_pct: the share of units "
            f"whose package changes in its test, against share_threshold_pct, above which the plan is said to change ({DT})."),
            empty=(f"The verified signed spec declares no engine departure ({SIGNED} engine_departures)." if m["spec"]["verified"] else
                   f"No signed spec could be verified, and {CHOICE} records no departure."),
            lead=True, wrap=["reason"], labels={"share_pct": "% of units that change package", "share_threshold_pct": "% above which the plan changes"}),
        "benchmarks_at_spec_values": _table("benchmarks_at_spec_values", m["recomputed"], (
            f"Benchmarks recomputed with the values the spec sets ({R.BSV}), where a decision test reads one. spec_hash: the "
            f"spec the file was computed for, from its record ({R.BSV_REC}); test: the signed spec's decision test that names the "
            "benchmark. The note on this table says whether that spec is the one signed.")),
        "candidates": _table("candidates", m["candidates"], (
            f"One row per engine the engine choice considered ({CHOICE} candidates), in its order. eligible: it can answer the "
            "signed spec; n_reasons and first_reason: why not (why_not). A candidate with no readable name is shown by its "
            "code reference."), wrap=["first_reason"]),
        "spec_values": _table("spec_values", m["spec_values"], (
            f"One row per engine parameter the signed spec sets ({ENGINE} data.spec_values): the card's value and range beside the "
            "spec's value and range. outside: which of value and range lies outside the card's range, where the record flags "
            "it. Needs you where a row is outside: the spec's reason is in the row."), SPEC_VALUES, wrap=["source", "reason"]),
        "parameters": _table("parameters", m["parameters"], (
            f"One row per parameter of the engine as this run used it ({ENGINE} data.parameters), in the record's order. value, "
            "low, high: the card's; owner_value, spec_low, spec_high: what the signed spec sets instead (null where it sets "
            "none); card_value: the card value a spec-set value replaces; value_used: the spec's value where it sets one, else "
            "the card's. kind: fixed, fitted (to benchmarks), calibrated (per run) or scenario."), wrap=["source"]),
        "mechanisms": _table("mechanisms", None if m["mechanisms"] is None else [{"text": t} for t in m["mechanisms"]],
                             f"What the engine models, in its card's own words ({ENGINE} data.card.mechanisms)"
                             + (f", for {e['name']} {e['version']}." if e["name"] else "."), wrap=["text"]),
        "images": _table("images", m["images"], (
            f"The harness figures that belong beside this view ({R.FIGS}: {', '.join(R.MODEL_FIGURES)}), each with the hash of the "
            "image as read.")),
    }


def _charts(m, tables):
    out, rows = [], m["benchmarks"] or []
    banded = [r for r in rows if r["band_low"] is not None and r["band_high"] is not None]
    if banded:
        wide = any(r["band_low"] > 0 and r["band_high"] / r["band_low"] > 100 for r in banded)
        left = len(rows) - len(banded)
        out.append({"kind": "ranges", "table": "benchmarks", "x": "id", "y": "computed", "outer_low": "band_low", "outer_high": "band_high",
                    "low": "seed_min", "high": "seed_max", **({"scale": "log"} if wide else {}),
                    "title": "Each benchmark's band, with the engine's value and its range over seeds",
                    "labels": {"band_low": "band", "seed_min": "range over seeds", "computed": "computed value (mean over seeds)"},
                    "how_to_read": "One row per benchmark, each on its own band. The bar is the range of single-seed values and the "
                                   "mark the computed value. A value or seed range outside the band is drawn at the band's edge: the "
                                   "numbers at the right and the next chart show how far out it is."
                                   + (f" {left} benchmark(s) with no band or no seed values are not drawn." if left else "")})
        positive = all(v is None or v > 0 for r in banded for v in (r["band_low"], r["computed"], r["target"]))
        out.append({"kind": "interval_points", "table": "benchmarks", "x": "id", "y": "target", "low": "band_low", "high": "band_high",
                    "model": "computed", "group": "status", **({"scale": "log"} if positive else {}),
                    "title": "Published value and band against the engine's value, by status",
                    "labels": {"target": "published value, with its band", "computed": "the engine's computed value"},
                    "how_to_read": "One row per benchmark, gathered by the producer's status, all on one axis"
                                   + (" (log scale)" if positive else "") + ". The line is the band and the diamond the engine's "
                                   "value: a diamond off the line is out of band, one near the line's end is a near miss."})
    if tables["spec_values"] and any(r["card_low"] is not None and r["spec_low"] is not None for r in m["spec_values"]):
        out.append({"kind": "ranges", "table": "spec_values", "x": "name", "y": "value", "outer_low": "card_low", "outer_high": "card_high",
                    "low": "spec_low", "high": "spec_high", "title": "Values the spec sets: the spec's range inside the card's range",
                    "labels": {"card_low": "the card's range", "spec_low": "the spec's range", "value": "the spec's value"},
                    "how_to_read": "One row per parameter the spec sets with a range, each on the card's own range. A bar that "
                                   "reaches an end of its row may go beyond it: the table gives the numbers. Parameters the spec "
                                   "sets with no range, or the card gives none for, are in the table only. The harness figure "
                                   "named parameters, under Images, draws every parameter."})
    return out


def _notes(m):
    e, c, spec = m["engine"], m["choice"], m["spec"]
    notes = {"engine": (f"Read from {ENGINE}: the engine card as this run used it ({e['name']} {e['version']}), never the repository's "
                        "card, which moves on after a run." if not e["why"] else f"The engine record could not be used: {e['why']}.")}
    notes["spec"] = (f"The signed copy of the spec, {SIGNED} ({str(spec['sha'])[:8]}), signed by {spec['signed_by']} at "
                     f"{str(spec['signed_at'])[:16]} (sign-off record {spec['record']}). Departures and declared values count from it alone."
                     if spec["verified"] else f"No signed spec could be verified: {spec['why']}. Nothing in a spec excuses a benchmark or a value here.")
    if c["why"]:
        notes["choice"] = f"The engine choice could not be read: {c['why']}."
    else:
        name = (c["chosen"] or {}).get("name") or "an engine no candidate row matches"
        notes["choice"] = (f"{CHOICE}{' (' + c['at'][:16] + ')' if c['at'] else ''} chose {name}"
                           + (f". Its own reason: {c['text']}" if c["text"] else ". It records no reason.")
                           + (" The verified signed spec names the engine, so the choice confirms it." if m["engine_named"] else ""))
    if m["benchmarks"] == []:
        notes["benchmarks"] = (f"This engine ({e['name']}) records no benchmark, so there is no benchmark table, no band chart and no "
                               "departure from a benchmark to show.")
    if m["recomputed_says"]:
        notes["benchmarks_at_spec_values"] = m["recomputed_says"]
    notes["reading"] = ("Benchmark: a published result the engine is run against. Band: the range its value must fall in. Seed range: "
                        "the spread of that value over the random seeds the benchmark was run with. Gating: a benchmark that makes "
                        "the engine ineligible unless it is in band or a signed departure covers it. Departure: the owner's signed "
                        "acceptance of a benchmark not in band, or of a component with no benchmark, with a decision test that runs "
                        "the plan across the doubt. Near miss: within a quarter of the band's log width of an edge, or within 25% of "
                        "a test's threshold (workbench thresholds).")
    return notes


def view(run):
    m = R.read(run)
    out = run_all(m)
    rows, c, spec = m["benchmarks"], m["candidates"], m["spec"]

    def count(fn):
        return None if rows is None else sum(1 for r in rows if fn(r))
    values = m["spec_values"] if m["spec_values_ok"] else None
    heads = [state for _, held, state in m["headers"] if held is not None]
    facts = {"engine_name": m["engine"]["name"], "engine_version": m["engine"]["version"],
             "n_candidates": None if c is None else len(c), "n_eligible": None if c is None else sum(r["eligible"] for r in c),
             "n_benchmarks": None if rows is None else len(rows), "n_in_band": count(lambda r: r["state"] == "in_band"),
             "n_out_of_band": count(lambda r: r["state"] == "out_of_band"), "n_unreachable": count(lambda r: r["state"] == "band_mis_based"),
             "n_near_edge": count(lambda r: r["state"] == "in_band" and not r["contradiction"] and r["edge"] is not None and r["edge"][1] <= R.NEAR),
             "n_not_gating": count(lambda r: not r["gates"]), "n_not_assessable": count(lambda r: r["state"] == "not_assessable"),
             "n_departures": sum(r["signed"] for r in m["departures"]) if spec["verified"] and m["departures"] is not None else None,
             "n_owner_values": None if values is None else len(values),
             "n_owner_values_outside_card": None if values is None else sum(r["flag"] is True for r in values),
             "choice_bound": (bool(heads) and all(s == "same" for s in heads) and len(heads) == len(m["headers"])) if m["headers"] else None}
    tables = _tables(m)
    images = ([{"table": "images", "path": "path", "hash": "sha256", "title": "title", "caption": "caption"}] if tables["images"] else [])
    return {"checks": out, "facts": {k: facts[k] for k in FACTS}, "tables": tables, "charts": _charts(m, tables),
            "notes": _notes(m), "images": images}


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import hashlib
    import json
    import math
    import re

    import yaml

    import workbench
    from views.fit_bench import Bench, check, close, fixture_builder, need, rewrite, says, statuses
    fx = fixture_builder()
    b = Bench(lambda run: view(run))        # the module's `view` as it is when a case runs
    folder, look_at, case, edited = b.folder, b.look_at, b.case, b.edited
    hm, post = "history_matching", "posterior"
    dec = "declared"        # §9: the clean run holds nothing declared; this variant holds one of each declaration
    engine_file, choice_file, tests_file = fx.ENGINE_REC, fx.CHOICE, fx.DT
    signed_file, signoff_file = "decision_brief.signed/decision_spec.yaml", "decision_spec.signoff.json"
    record_checks = ("benchmarks", "choice_text", "spec_values", "seed_range", "parameters_sourced", "fitted_at_bound")

    # ---- readers of a view result
    def table(v, name):
        t = v["tables"].get(name)
        need(isinstance(t, dict) and t.get("columns") == COLUMNS[name] and isinstance(t.get("rows"), list),
             f"tables.{name} is {{columns, rows, about}} with columns {COLUMNS[name]} (got {t.get('columns') if isinstance(t, dict) else t})")
        need(all(isinstance(r, list) and len(r) == len(COLUMNS[name]) for r in t["rows"]), f"tables.{name} rows are lists in column order")
        return [dict(zip(COLUMNS[name], r)) for r in t["rows"]]

    def row(v, name, key, value):
        found = [r for r in table(v, name) if r[key] == value]
        need(len(found) == 1, f"one row of tables.{name} with {key} {value!r} (found {len(found)})")
        return found[0]

    def same(got, want):
        return close(got, want) if isinstance(want, float) else got == want and type(got) is type(want)

    def holds(got, want, what):
        off = {k: (got.get(k), w) for k, w in want.items() if not same(got.get(k), w)}
        need(not off, f"{what}: {off} (got, wanted)")

    def sha(d, rel):
        with open(os.path.join(d, rel), "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    # ---- edits of a copy of a built run
    def both(*edits):
        return lambda d: [e(d) for e in edits]

    def rejson(rel, change):
        def edit(d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                doc = json.load(f)
            change(doc)
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=1)
        return edit

    def remove(rel):
        return lambda d: os.remove(os.path.join(d, rel))

    def record(change):
        """An edit of analysis/engine.yaml: change(its data)."""
        return rewrite(engine_file, lambda doc: change(doc["data"]))

    def bench(bid, **change):
        return record(lambda data: next(r for r in data["card"]["benchmarks"] if r["id"] == bid).update(change))

    def chosen(change):
        return rejson(choice_file, lambda doc: change(doc["candidates"][0]))

    def test(tid, **change):
        return rewrite(tests_file, lambda doc: next(t for t in doc["data"]["tests"] if t["test_id"] == tid).update(change))

    unverified = {"the sign-off record is missing": remove(signoff_file),
                  "the sign-off record holds another hash for the spec": rejson(signoff_file, lambda so: so["sha256"].update({"decision_spec.yaml": "0" * 64})),
                  "the signed copy is missing": remove(signed_file)}

    # ------------------------------------------------------------------------------- the generic model (§5.4)
    def m_shape():
        for method in (hm, post):
            v = look_at(folder(method))
            need(sorted(v["facts"]) == sorted(FACTS), f"facts are exactly {sorted(FACTS)} (got {sorted(v['facts'])})")
            need([c.get("id") for c in v["checks"]] == [f"model.{n}" for n in CHECKS], f"the eight checks, in this order (got {[c.get('id') for c in v['checks']]})")
            need(all(c.get("step") == "model" and c.get("case") == CHECKS[c["id"][6:]] and str(c.get("says") or "").strip() and c.get("evidence")
                     for c in v["checks"]), "each check names its view, its scorecard case, one sentence and the file and field it read")
            need(statuses(v) == {n: "ok" for n in CHECKS}, f"on the clean run every check is ok (got {statuses(v)})")
            need(set(v["tables"]) >= set(COLUMNS) and all(isinstance(t.get("about"), str) and t["about"].strip() for t in v["tables"].values() if t),
                 f"every table of the model is present, each with what its rows are (got {sorted(v['tables'])})")

    def m_facts():
        v = look_at(folder(hm))
        holds(v["facts"], {"engine_name": "toy-engine", "engine_version": "0.1", "n_candidates": 3, "n_eligible": 1,
                           "n_benchmarks": 4, "n_in_band": 4, "n_out_of_band": 0, "n_unreachable": 0, "n_near_edge": 0,
                           "n_not_gating": 1, "n_not_assessable": 0, "n_departures": 0, "n_owner_values": 1,
                           "n_owner_values_outside_card": 0, "choice_bound": True}, "facts of the clean run, which holds nothing declared (§9)")
        v = look_at(folder(hm, "C4_out_of_band_departure"))
        holds(v["facts"], {"n_in_band": 3, "n_out_of_band": 1, "n_departures": 1}, "a benchmark out of band under a signed departure")
        holds(look_at(folder(hm, dec))["facts"], {"n_departures": 2}, "one departure for a benchmark and one for a component")
        v = look_at(folder(hm, "C4_unreachable"))
        holds(v["facts"], {"n_in_band": 3, "n_out_of_band": 0, "n_unreachable": 1, "n_not_gating": 1}, "a band that cannot be reached")
        v = look_at(folder(hm, "C4_near_edge"))
        holds(v["facts"], {"n_in_band": 4, "n_near_edge": 1}, "a value near its band's edge is still in band")
        v = look_at(folder(hm, "spec_value_outside_card"))
        holds(v["facts"], {"n_owner_values": 1, "n_owner_values_outside_card": 1}, "a spec-set value outside the card")
        v = look_at(folder(hm, "choice_unbound_open"))
        holds(v["facts"], {"choice_bound": False}, "no hash of the engine choice in the headers")

    def m_candidates():
        v = look_at(folder(hm))
        got = table(v, "candidates")
        need(len(got) == 3, f"one row per candidate (got {len(got)})")
        holds(got[0], {"name": "toy-engine", "version": "0.1", "eligible": True, "n_reasons": 0, "first_reason": None}, "the chosen engine")
        holds(got[1], {"name": "other-engine", "version": "1", "eligible": False, "n_reasons": 2, "first_reason": "no component B"}, "a candidate that does not fit")
        holds(got[2], {"name": "toolkit.engines.third.engine:ThirdEngine", "version": None, "eligible": False, "n_reasons": 1},
              "a candidate with no readable name is shown by its reference")
        need(str(got[2]["first_reason"]).startswith("card unreadable"), "and its first reason is the record's")

    def m_benchmarks():
        d = folder(hm)
        v = look_at(d)
        got = table(v, "benchmarks")
        need([r["id"] for r in got] == ["B1", "B2", "B3", "B4"], f"one row per benchmark of the run's own engine record, in its order (got {[r['id'] for r in got]})")
        holds(got[0], {"metric": "component A against none, outcome ratio at 12 months", "role": "fit", "target": 0.6, "band_low": 0.4,
                       "band_high": 0.9, "computed": 0.6, "seed_min": 0.576, "seed_max": 0.624, "status": "in_band", "gates": True,
                       "source": "fixture study one, table 2", "departure_test": None, "departure_verdict": None}, "B1")
        holds(got[1], {"role": "held_out", "status": "in_band", "gates": True, "departure_test": None, "departure_verdict": None},
              "B2, which no departure names on the clean run (§9)")
        holds(row(look_at(folder(hm, "C4_out_of_band_departure")), "benchmarks", "id", "B2"),
              {"role": "held_out", "status": "out_of_band", "computed": 0.78, "gates": True, "departure_test": "DT4", "departure_verdict": "NO_CHANGE"},
              "B2 out of band, which a signed departure names")
        holds(got[3], {"band_low": 2.0, "band_high": 72.0, "computed": 12.0, "gates": False}, "B4, which the engine choice only reports")
        v = look_at(folder(hm, "C4_out_of_band"))
        holds(row(v, "benchmarks", "id", "B3"), {"status": "out_of_band", "computed": 1.12, "gates": True, "departure_test": None}, "B3 out of band")

    def m_departures():
        need(table(look_at(folder(hm)), "departures") == [], "the clean run declares no departure: the table has no rows (§9)")
        v = look_at(folder(hm, dec))
        got = table(v, "departures")
        need(len(got) == 2, f"one row per departure of the signed spec (got {len(got)})")
        holds(row(v, "departures", "subject", "B2"), {
            "kind": "benchmark", "reason": "B2 is held out; where the engine sits outside its band, DT4 runs the plan across the band",
            "status": "signed", "signed": True, "test": "DT4", "verdict": "NO_CHANGE", "share_pct": 0.0, "share_threshold_pct": 10.0}, "B2")
        holds(row(v, "departures", "subject", "B"), {"kind": "component", "status": "signed", "signed": True, "test": "DT5", "verdict": "NO_CHANGE"}, "B")
        v = look_at(folder(hm, "C4_out_of_band_departure_unsigned"))
        need(table(v, "departures") == [], "a departure that is only in the spec on disk is no row")

    def m_parameters():
        v = look_at(folder(hm))
        need([r["name"] for r in table(v, "parameters")] == ["p1", "p2", "p3", "f1", "b_add", "c1"], "one row per parameter of the engine record")
        holds(row(v, "parameters", "name", "p2"), {
            "value": 3.0, "low": 1.0, "high": 6.0, "units": "ratio", "kind": "fitted", "source": "fitted once to benchmarks B1 and B4 (fixture card)",
            "owner_value": 3.1, "card_value": 3.0, "outside_card": False, "value_used": 3.1, "spec_low": 1.0, "spec_high": 5.5},
            "a parameter the spec sets (settled: value_used is the spec's value)")
        holds(row(v, "parameters", "name", "p1"), {
            "value": 0.5, "low": 0.2, "high": 0.8, "kind": "calibrated", "owner_value": None, "card_value": None, "outside_card": None,
            "value_used": 0.5, "spec_low": None, "spec_high": None}, "a parameter the spec does not set (assumed: A2)")
        v = look_at(folder(hm, "spec_value_outside_card"))
        holds(row(v, "parameters", "name", "p2"), {"high": 5.0, "outside_card": True, "spec_high": 5.5}, "outside the card")
        need([r["text"] for r in table(look_at(folder(hm)), "mechanisms")] == list(fx.MECHANISMS), "mechanisms are the record's card's")

    def m_recomputed():
        v = look_at(folder(hm))
        need("benchmarks_at_spec_values" in v["tables"] and v["tables"]["benchmarks_at_spec_values"] is None,
             "a table the run does not record is null")
        d = folder(hm, "spec_benchmarks_recomputed")
        got = table(look_at(d), "benchmarks_at_spec_values")
        need(len(got) == 1, "one row per benchmark recomputed at the spec's values")
        holds(got[0], {"id": "B2", "computed": 0.51, "status": "in_band", "spec_hash": sha(d, signed_file), "test": "DT4"},
              "the recomputed benchmark, the spec hash its record holds, and the decision test that reads it")

    def m_chart():
        v = look_at(folder(hm))
        need(workbench.chart_problems(v) == [], f"every chart is a declaration the page can draw ({workbench.chart_problems(v)})")
        ranges = [c for c in v["charts"] if c.get("kind") == "ranges" and c.get("table") == "benchmarks"]
        need(len(ranges) == 1, f"one ranges chart over tables.benchmarks (found {len(ranges)})")
        holds(ranges[0], {"x": "id", "y": "computed", "outer_low": "band_low", "outer_high": "band_high", "low": "seed_min",
                          "high": "seed_max"}, "each band with the computed value and the seed range")
        need(str(ranges[0].get("title") or "").strip() and str(ranges[0].get("how_to_read") or "").strip(), "with a title and how to read it")
        need(ranges[0].get("scale") != "log", "no log scale when no band spans more than a factor of one hundred (the widest here spans 36)")
        for band, want in (([2.0, 190.0], False), ([2.0, 210.0], True)):
            wide = look_at(edited(hm, bench("B4", band=band)))
            chart = next(c for c in wide["charts"] if c.get("kind") == "ranges" and c.get("table") == "benchmarks")
            need((chart.get("scale") == "log") is want, f"a band of {band}: log scale is {want} (settled: more than a factor of one hundred)")

    def images(v):
        got = v.get("images")
        need(isinstance(got, list) and all(isinstance(i, dict) and sorted(i) == sorted(IMAGE_KEYS) for i in got),
             f"images is a list of {{table, path, hash, title, caption}} (got {got!r})")
        for i in got:
            t = v["tables"].get(i["table"])
            need(isinstance(t, dict) and all(i[k] in t["columns"] for k in IMAGE_KEYS[1:]), f"an image entry names a table and its columns ({i})")
        return got

    def m_images():
        d = folder(hm)
        v = look_at(d)
        need(any(i == {"table": "images", "path": "path", "hash": "sha256", "title": "title", "caption": "caption"} for i in images(v)),
             "the view declares its harness figures over tables.images (assumed: A10)")
        holds(row(v, "images", "figure", "parameters"), {"path": "figures/parameters.png", "sha256": sha(d, "figures/parameters.png")},
              "the harness figure of the engine's parameters, with the hash of the image as read")
        v = look_at(folder(post, figures="estimates"))
        need(images(v) is not None and table(v, "images") == [], "a figure set with no figure for this view: no row, and no failure")

    def m_notes():
        v = look_at(folder(hm))
        need(all(isinstance(t, str) and t.strip() for t in v["notes"].values()), "every note is a sentence")
        need(engine_file in v["notes"].get("engine", "") and signed_file in v["notes"].get("spec", "") and "owner" in v["notes"]["spec"],
             "notes.engine names the record read; notes.spec names the signed copy and who signed it (assumed: A11)")
        need("lead" in look_at(folder(hm, "signed_by_lead"))["notes"]["spec"], "the signer is read from the sign-off record")

    # ------------------------------------------------------------------------------- the settled rules (§5.4)
    def s_gates():
        base = folder(hm)
        need(row(look_at(base), "benchmarks", "id", "B4")["gates"] is False, "a benchmark the run lists as reported only does not gate")
        v = look_at(edited(hm, bench("B3", covers={})))
        need(row(v, "benchmarks", "id", "B3")["gates"] is False, "a benchmark whose covers is recorded and empty does not gate (assumed: A3)")
        v = look_at(edited(hm, bench("B3", covers={"A": [1]})))
        need(row(v, "benchmarks", "id", "B3")["gates"] is True, "one whose covers names a component gates")
        v = look_at(edited(hm, chosen(lambda c: c.pop("benchmarks_reported"))))
        need(all(r["gates"] is True for r in table(v, "benchmarks")), "with no reported list recorded, every benchmark gates (fail closed)")

    def s_benchmarks():
        gating = edited(hm, chosen(lambda c: c.update(benchmarks_reported=[])), defect="C4_unreachable")
        v = look_at(gating)
        need(statuses(v)["benchmarks"] == "problem" and v["facts"]["n_unreachable"] == 1, "a gating benchmark whose band cannot be reached, with no departure, is a problem")
        v = look_at(edited(hm, bench("B4", mis_based_reason=fx.MIS_BASED[0]), defect="C4_unreachable"))
        need(v["facts"]["n_unreachable"] == 1 and statuses(v)["benchmarks"] == "look", "n_unreachable counts the row whatever the reason")
        v = look_at(edited(hm, bench("B3", status="not_assessable", in_band=False, computed=None, not_assessable_because="published values not yet transcribed: fixture")))
        need(v["facts"]["n_not_assessable"] == 1 and v["facts"]["n_in_band"] == 3 and statuses(v)["benchmarks"] == "problem",
             "a gating benchmark that could not be assessed is not in band: a problem, never reported as passed")
        c = check(look_at(folder(hm, "C4_out_of_band_departure")), "benchmarks")
        need("b2" in says(c) and "dt4" in says(c) and "no change" in says(c), f"a covered benchmark's sentence gives the departure and its test's verdict ({says(c)})")
        need("b3" in says(check(look_at(folder(hm, "C4_out_of_band")), "benchmarks")), "the sentence names the benchmark that is out of band")

    def s_near_edge():
        low, high = 0.4, 0.9
        width = math.log(high / low)
        for share, want in ((0.24, "look"), (0.26, "ok")):
            for value in (high * math.exp(-share * width), low * math.exp(share * width)):
                v = look_at(edited(hm, bench("B1", computed=value, per_seed=[value] * 4, seed_min=value, seed_max=value)))
                need(statuses(v)["benchmarks"] == want and v["facts"]["n_near_edge"] == (want == "look"),
                     f"a value {share} of the band's log width from an edge is {want} (got {statuses(v)['benchmarks']})")
        c = check(look_at(folder(hm, "C4_near_edge")), "benchmarks")
        need("b1" in says(c) and "0.84" in says(c) and "0.9" in says(c), f"a near miss is shown with both numbers ({says(c)})")

    def s_none():
        v = look_at(folder(hm, "no_benchmarks"))
        need(table(v, "benchmarks") == [] and v["facts"]["n_benchmarks"] == 0, "a table the run records with no rows has rows: []")
        need("toy-engine" in v["notes"].get("benchmarks", "") and "no benchmark" in v["notes"]["benchmarks"],
             "the view says this engine (by name) records no benchmark")

    def s_choice_text():
        v = look_at(edited(hm, rejson(choice_file, lambda doc: doc.update(why="every benchmark is out of band")), defect="choice_unbound_named"))
        need(statuses(v)["choice_text"] == "ok", "the check reads no free text: the choice's `why` does not move it")
        need(statuses(look_at(folder(hm, "C4_out_of_band_departure")))["choice_text"] == "ok",
             "a benchmark a counted departure covers is not expected in the list (assumed: A6)")
        need(statuses(look_at(folder(hm, "C4_not_gating_out_of_band")))["choice_text"] == "ok", "nor is one the choice only reports")
        v = look_at(edited(hm, chosen(lambda c: c.pop("benchmarks_not_in_band"))))
        need(statuses(v)["choice_text"] == "not_available", "a choice record with no such list (an older record) is not_available")
        c = check(look_at(folder(hm, "choice_list_disagrees")), "choice_text")
        need("b1" in says(c), f"the sentence names the benchmark the list and the table disagree on ({says(c)})")

    def s_choice_bound():
        c = check(look_at(folder(hm, "choice_unbound_open")), "choice_bound")
        need("h10" in says(c), f"a null hash with the engine left open is gap H10 ({says(c)})")
        v = look_at(edited(hm, remove(choice_file)))
        need(statuses(v)["choice_bound"] in ("problem", "not_available"), "a hash in the headers with no file to compare is never a match")
        for name, edit in unverified.items():
            v = look_at(edited(hm, edit, defect="choice_unbound_named"))
            need(statuses(v)["choice_bound"] in ("problem", "not_available"), f"{name}: a null hash is excused only by a verified signed spec")

    def s_spec_values():
        c = check(look_at(folder(hm, "spec_value_outside_card")), "spec_values")
        need("p2" in says(c) and "range" in says(c), f"the sentence names the parameter and which of value and range is outside ({says(c)})")
        v = look_at(edited(hm, record(lambda data: data["spec_values"]["p2"].update(value=6.5, outside="value", outside_card_range=True))))
        need(statuses(v)["spec_values"] == "look" and "value" in says(check(v, "spec_values")), "a value outside the card, flagged, is look and said to be the value")
        v = look_at(edited(hm, record(lambda data: data.pop("spec_values"))))
        need(statuses(v)["spec_values"] == "ok" and v["facts"]["n_owner_values"] == 0, "an engine record with no spec_values: the spec sets none (the harness writes the key only when it does)")
        for name, edit in unverified.items():
            v = look_at(edited(hm, edit, defect="spec_value_outside_card"))
            need(statuses(v)["spec_values"] == "problem", f"{name}: a flagged value is a problem (assumed: A7)")

    def s_added():
        c = check(look_at(folder(hm, "seed_range_crosses_edge")), "seed_range")
        need("b1" in says(c) and "0.93" in says(c), f"seed_range names the benchmark and the seed beyond the edge ({says(c)})")
        v = look_at(edited(hm, bench("B3", per_seed=[0.95, 1.05, 1.15, 1.2], seed_min=0.95, seed_max=1.2), defect="C4_out_of_band"))
        need(statuses(v)["seed_range"] == "ok", "seed_range is about benchmarks in band only")
        c = check(look_at(folder(hm, "parameters_unverified")), "parameters_sourced")
        need(re.search(r"\b2\b", says(c)) and re.search(r"\b6\b", says(c)), f"parameters_sourced gives the count, of how many ({says(c)})")
        c = check(look_at(folder(hm, "fitted_on_bound")), "fitted_at_bound")
        need("f1" in says(c), f"fitted_at_bound names the parameter ({says(c)})")
        for gap, want in ((0.5e-6, "look"), (2e-6, "ok")):
            def near(data, gap=gap):
                data["card"]["fitted"]["f1"]["value"] = 0.6 - gap * 0.3
                next(p for p in data["parameters"] if p["name"] == "f1")["value"] = 0.6 - gap * 0.3
            need(statuses(look_at(edited(hm, record(near))))["fitted_at_bound"] == want,
                 f"a fitted value {gap} of the range's width from its end is {want} (§3: reached within 1e-6)")

    def s_departures():
        need(statuses(look_at(folder(hm)))["departures"] == "ok", "a run whose signed spec declares no departure is ok")
        base = "C4_out_of_band_departure"
        for share, near in ((7.6, True), (7.4, False)):
            c = check(look_at(edited(hm, test("DT4", share_pct=share), base)), "departures")
            need(c["status"] == "look" and ("near miss" in says(c)) is near,
                 f"a signed departure whose test ran is look (settled); a share of {share}% against the 10% threshold is "
                 f"{'a' if near else 'no'} near miss (within 25%) ({c['status']}: {says(c)})")
        c = check(look_at(folder(hm, "departure_near_miss")), "departures")
        need("9" in says(c) and "10" in says(c) and "dt4" in says(c), f"the near miss is shown with both numbers ({says(c)})")
        v = look_at(edited(hm, test("DT4", verdict="CHANGES_PLAN", share_pct=25.0), base))
        need(statuses(v)["departures"] == "look" and "changes plan" in says(check(v, "departures")), "a departure whose test changes the plan is look, with the verdict")
        holds(row(look_at(folder(hm, "departure_test_not_run")), "departures", "subject", "B"), {"verdict": "NOT_RUN", "share_pct": None}, "a test that did not run")
        v = look_at(folder(hm, "signed_by_lead"))
        need(statuses(v)["departures"] == "problem" and statuses(v)["benchmarks"] == "problem" and "unsigned" in says(check(v, "departures")),
             "a departure the engine choice marks unsigned (a lead's signature) does not count, and covers nothing")

    def planted(method, defect):
        def fn():
            spec = fx.DEFECTS[defect]
            need(spec.get("view") == "model" and spec["check"] in CHECKS and spec["case"] == CHECKS[spec["check"]], "the fixture records this edit for a Model check")
            clean, v = statuses(look_at(folder(method))), look_at(folder(method, defect))
            want = {**clean, spec["check"]: spec["status"], **spec.get("also", {})}
            got = {k: (s if k not in spec.get("unpinned", ()) else want[k]) for k, s in statuses(v).items()}
            need(got == want, f"only {spec['check']} (and {spec.get('also', {})}) moves: {({k: s for k, s in got.items() if want.get(k) != s})}")
            need(all(check(v, n)["says"].strip() for n in CHECKS), "each check says why")
        return fn

    # ------------------------------------------------------------------------------- fail closed (§3, §9)
    def x_no_record():
        for what, d in (("no engine record", edited(hm, remove(engine_file))), ("a run stopped at the sign-off", folder(hm, upto="sign-off"))):
            v = look_at(d)
            got = statuses(v)
            need(all(got[n] == "not_available" for n in record_checks), f"{what}: the checks that read it are not_available (got {got})")
            need(not {"look", "problem"} & set(got.values()) or what == "no engine record", f"{what}: nothing is raised")
            need(all(v["tables"][n] is None for n in ("benchmarks", "parameters", "mechanisms")) and v["facts"]["n_benchmarks"] is None,
                 f"{what}: its tables are null and its counts are null, never zero")
            need("engine.yaml" in check(v, "benchmarks")["says"], "the sentence names the file")

    def x_no_choice():
        v = look_at(edited(hm, remove(choice_file)))
        need(v["tables"]["candidates"] is None and v["facts"]["n_candidates"] is None and statuses(v)["choice_text"] == "not_available",
             "no engine choice record: no candidate table, and the list cannot be compared")
        v = look_at(edited(hm, lambda d: open(os.path.join(d, choice_file), "w").write("{not json")))
        need(statuses(v)["choice_text"] == "not_available" and choice_file in check(v, "choice_text")["says"], "a choice record that cannot be parsed is named")

    def x_unverified():
        for name, edit in unverified.items():
            v = look_at(edited(hm, edit, defect="C4_out_of_band_departure"))
            need(statuses(v)["benchmarks"] == "problem", f"{name}: a departure excuses nothing (got {statuses(v)['benchmarks']})")
            need(v["tables"]["departures"] is None or all(r["signed"] is False for r in table(v, "departures")),
                 f"{name}: no departure is shown as signed")
            need(statuses(v)["departures"] not in ("ok", "look"), f"{name}: the departures check does not pass")

    def x_tests():
        v = look_at(edited(hm, remove(tests_file), defect="C4_out_of_band_departure"))
        need(statuses(v)["departures"] == "not_available" and row(v, "departures", "subject", "B2")["verdict"] is None,
             "no decision tests file: the departures' tests cannot be read")
        need(statuses(v)["benchmarks"] == "problem", "and a benchmark out of band is not excused by a test that cannot be shown to have run")
        need(statuses(look_at(folder(hm, "decision_tests_missing")))["departures"] == "ok", "a run with no departure needs no test")
        for words in ({"status": "run", "verdict": "NOT_RUN"}, {"status": "not_run", "verdict": "NO_CHANGE"}, {"status": "done"}):
            v = look_at(edited(hm, test("DT4", **words), defect="C4_out_of_band_departure"))
            need(statuses(v)["departures"] not in ("ok", "look") and statuses(v)["benchmarks"] == "problem",
                 f"{words}: a status and verdict the producer never writes together is not a test that ran ({statuses(v)})")

    def x_contradiction():
        def add(doc):
            entry = {"engine": "toy-engine", "benchmark": "B3", "reason": "r", "decision_test": "DT1", "status": "signed",
                     "benchmark_status": "out_of_band", "role": "held_out", "components": ["B"]}
            doc["engine_departures"].append(entry)
            doc["candidates"][0]["departures"].append(dict(entry))
        v = look_at(edited(hm, both(rejson(choice_file, add), bench("B3", computed=1.12, in_band=False, status="out_of_band"))))
        need(statuses(v)["departures"] == "problem" and statuses(v)["benchmarks"] == "problem",
             "a departure the choice record marks signed and the signed spec does not hold is a contradiction, and covers nothing")
        v = look_at(edited(hm, bench("B1", in_band=False)))
        need(statuses(v)["benchmarks"] == "problem", "a row whose in_band flag disagrees with its status contradicts itself")
        v = look_at(edited(hm, record(lambda data: data["card"].pop("benchmarks"))))
        need(statuses(v)["benchmarks"] == "not_available" and v["tables"]["benchmarks"] is None, "an engine record with no benchmark list at all is not an empty list")

    def x_reads_only():
        d = folder(hm, "C4_out_of_band")
        before = {os.path.join(at, f): sha(at, f) for at, _, files in os.walk(d) for f in files}
        look_at(d)
        need(before == {os.path.join(at, f): sha(at, f) for at, _, files in os.walk(d) for f in files}, "the view only reads")

    def w_source():
        with open(os.path.abspath(__file__), encoding="utf-8") as f:
            text = f.read().lower()
        need(not [w for w in fx.domain_words() if w in text], "no domain word in this file (§3a)")

    def w_words():
        text = json.dumps(look_at(folder(hm, "C4_out_of_band")), default=str).lower()
        need(not [w for w in fx.domain_words() if w in text], "no domain word in the view's result")
        a, c = look_at(folder(hm)), look_at(folder(post))
        need(yaml.safe_dump(a["tables"]["benchmarks"]) == yaml.safe_dump(c["tables"]["benchmarks"]) and statuses(a) == statuses(c),
             "the model does not depend on the calibration method")

    case("M1 the generic model: the fifteen facts, the eight checks with ids model.<name>, every table present; all ok on the clean run", m_shape)
    case("M2 facts hold the run's counts, and follow a planted edit", m_facts)
    case("M3 candidates: name, version, eligible, n_reasons, first_reason; a candidate with no readable name is shown by its reference", m_candidates)
    case("M4 benchmarks: the fourteen columns, read from the run's own engine record, with the departure's test and verdict", m_benchmarks)
    case("M5 departures: subject, kind, reason, status, signed, test, verdict, share and threshold, from the signed copy only", m_departures)
    case("M6 parameters: the card's value and range beside the spec's value, value_used, spec_low and spec_high; mechanisms (assumed: A2)", m_parameters)
    case("M7 benchmarks_at_spec_values: null when the run has none; id, computed, status, spec_hash, test when it has", m_recomputed)
    case("M8 one ranges chart of band, computed value and seed range, on a log scale where a band spans more than a factor of one hundred (settled)", m_chart)
    case("M9 images: the harness figure of the parameters is declared over a table of path and hash (assumed: A10)", m_images)
    case("M10 notes name the engine record, the spec copy read and who signed it (assumed: A11)", m_notes)
    case("S1 gates: false when reported only or covers recorded and empty, else true (assumed: A3)", s_gates)
    case("S2 model.benchmarks: unreachable and not-assessable rows are counted and never passed; sentences name benchmark, departure, verdict", s_benchmarks)
    case("S3 a value within a quarter of the band's log width of an edge is a near miss, shown with both numbers", s_near_edge)
    case("S4 an engine record that lists no benchmark: rows [], look, and the view says so by the engine's name", s_none)
    case("S5 model.choice_text reads no free text and compares the choice's own not-in-band list with the table (assumed: A6)", s_choice_text)
    case("S6 model.choice_bound: gap H10 named; a hash with no file, or a null hash with no verified signed spec, never passes", s_choice_bound)
    case("S7 model.spec_values uses the record's own flag and says which of value and range is outside (assumed: A7)", s_spec_values)
    case("S8 the added checks: seed_range, parameters_sourced (with its count), fitted_at_bound (reached within 1e-6 of the range)", s_added)
    case("S9 model.departures: look for a signed departure whose test ran (settled), a near miss against the test's own threshold said; unsigned or not run is a problem", s_departures)
    n = 0
    for method, defects in ((hm, fx.MODEL_DEFECTS), (post, ("C4_out_of_band", "C4_out_of_band_departure", "choice_unbound_open", "spec_value_outside_card"))):
        for defect in defects:
            n += 1
            spec = fx.DEFECTS[defect]
            also = "".join(f", {k} {s}" for k, s in spec.get("also", {}).items())
            case(f"P{n} {method}, {defect}: {spec['check']} is {spec['status']}{also}, and no other check changes", planted(method, defect))
    case("X1 no engine record, or a run stopped at the sign-off: not_available with the file's name, tables and counts null", x_no_record)
    case("X2 no engine choice record, or one that cannot be parsed: no candidates, and the list check is not_available", x_no_choice)
    case("X3 no verified signed spec, three ways: a departure excuses nothing and is not shown as signed", x_unverified)
    case("X4 no decision tests file, or a test record the producer never writes: no departure's test counts as run", x_tests)
    case("X5 records that contradict themselves or each other are problems; a record with no benchmark list is not an empty one", x_contradiction)
    case("X6 the view only reads: no file of the run changes", x_reads_only)
    case("W1 no domain word in this file (§3a)", w_source)
    case("W2 no domain word in the view's result; the model is the same for another calibration method (§3a)", w_words)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: model.py --self-test")
