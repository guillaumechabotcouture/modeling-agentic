#!/usr/bin/env python3
"""The Model view's eight checks (docs/design/review-workbench.md §5.4), each a function of the generic model that
views/model_read.py returns; none opens a file. views/model.py lists the rules (A3 to A8 of its docstring).

    python3 tools/workbench/views/model_checks.py --self-test

Statuses follow §3: only a verified signed spec can excuse anything; a word a producer does not write is
`not_available` and names the word; a record that contradicts itself, or another file, is `problem`; something the
harness could not compute is never reported as passed; a file that cannot be read is `not_available` with the reason.
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402
from views import model_read as R  # noqa: E402
from views.model_read import g  # noqa: E402

CHECKS = {"benchmarks": "C4", "departures": None, "choice_text": None, "spec_values": None, "choice_bound": None,
          "seed_range": None, "parameters_sourced": None, "fitted_at_bound": None}       # name -> scorecard case
TITLES = {"benchmarks": "Each benchmark is in its band, or a signed departure covers it",
          "departures": "Each signed departure names a decision test that ran",
          "choice_text": "The engine choice and the engine record agree on which benchmarks are not in band",
          "spec_values": "Values the spec sets lie inside the engine card's range",
          "choice_bound": "The analysis files record the engine choice they used",
          "seed_range": "A benchmark in band stays in band for every seed",
          "parameters_sourced": "Each engine parameter has a checked source and a range",
          "fitted_at_bound": "No fitted parameter sits on the end of its fitted range"}
ENGINE, CHOICE, DT, SIGNED = R.ENGINE_REC, R.CHOICE, R.DT, R.SIGNED
SHOWN = 6        # ids named in a sentence before "and n more"; every row is in the check's table


def _r(name, status, says, evidence, rows=None):
    return checks.result(f"model.{name}", "model", TITLES[name], status, says, evidence, rows, CHECKS[name])


def _some(items, n=SHOWN):
    items = list(items)
    return ", ".join(items[:n]) + (f" and {len(items) - n} more" if len(items) > n else "")


def _no_record(m, name, field):
    return _r(name, "not_available", f"Not available: {m['engine']['why']}, so nothing can be said of the engine's {field}.",
              [(ENGINE, "data.card" if field == "benchmarks" else "data." + field)])


def _judge(m, r):
    """(status, kind, words) for one benchmark row (A4 of views/model.py). kind: unknown, contradiction, unplaced,
    near, clear, covered, not_gating or uncovered."""
    name = f"{r['id']} ({r['role'] or 'role not recorded'})"
    if r["state"] == "unknown":
        return "not_available", "unknown", f"{name} has the status {r['status']!r}, a word the producer does not write"
    if r["contradiction"]:
        return "problem", "contradiction", f"{name}: {r['contradiction']}"
    if r["state"] == "in_band":
        if r["edge"] is None:
            return "not_available", "unplaced", f"{name} is in band, with no band or no value recorded to place it by"
        edge, share = r["edge"]
        if share <= R.NEAR:
            return "look", "near", f"{r['id']} at {g(r['computed'])} (edge {g(edge)} of its band {g(r['band_low'])} to {g(r['band_high'])})"
        return "ok", "clear", ""
    what = f"{name} is {r['status']}: {r['why_not']}"
    c = r["cover"]
    if c is not None and c["state"] == "ok":
        return "look", "covered", f"{what}; its departure covers it: {c['says']}"
    if not r["gates"]:
        return "look", "not_gating", f"{what}; it does not gate"
    why = (f"its departure does not count ({c['says']})" if c is not None else
           "no departure in the verified signed spec names it" if m["spec"]["verified"] else
           f"no signed spec could be verified ({m['spec']['why']}), so no departure counts")
    return "problem", "uncovered", f"{what}; {why}"


SAID = (("uncovered", "Gating and not in band, with nothing signed that covers it"), ("contradiction", "A row that contradicts itself"),
        ("unknown", "Not judged"), ("unplaced", "Not judged"), ("covered", "Not in band under a signed departure whose test ran"),
        ("not_gating", "Not in band and not gating (the engine choice only reports it, or it covers no component)"),
        ("near", "In band within a quarter of the band's log width of an edge (near miss, workbench threshold)"))


def check_benchmarks(m):
    rows = m["benchmarks"]
    ev = [(ENGINE, "data.card.benchmarks[].status, band, computed, in_band"), (CHOICE, "candidates[].benchmarks_reported"),
          (SIGNED, "engine_departures"), (DT, "data.tests[].status, verdict")]
    if m["engine"]["why"]:
        return _no_record(m, "benchmarks", "benchmarks")
    if rows is None:
        return _r("benchmarks", "not_available", f"Not available: {ENGINE} holds no list of benchmarks (data.card.benchmarks), "
                  "which is not the same as an engine with none.", ev[:1])
    if not rows:
        return _r("benchmarks", "look", f"This engine ({m['engine']['name']}) records no benchmark: nothing ties it to a published "
                  "study or a reference result, so its effects rest on its parameters' sources alone.", ev[:1])
    judged = [(r, *_judge(m, r)) for r in rows]
    worst = checks.worst([s for _, s, _, _ in judged])
    n_in = sum(r["state"] == "in_band" for r in rows)
    head = f"{n_in} of {len(rows)} benchmarks are in band"
    if worst == "ok":
        return _r("benchmarks", "ok", f"{head}, each clear of both edges of its band.", ev)
    parts = []
    for kind, lead in SAID:
        texts = [text for _, _, k, text in judged if k == kind]
        if texts:
            sep = ", " if kind == "near" else "; "
            parts.append(f"{lead}: {sep.join(texts[:SHOWN])}" + (f"{sep}and {len(texts) - SHOWN} more in the rows behind this check" if len(texts) > SHOWN else ""))
    table = {"columns": ["id", "status", "gates", "judged", "why"],
             "rows": [[r["id"], r["status"], r["gates"], s, text] for r, s, _, text in judged if s != "ok"]}
    return _r("benchmarks", worst, f"{head}. " + ". ".join(parts) + ".", ev, table)


def check_departures(m):
    rows, spec = m["departures"], m["spec"]
    ev = [(SIGNED, "engine_departures"), (CHOICE, "engine_departures[].status"), (DT, "data.tests[].status, verdict, share_pct, threshold_pct")]
    if rows is None:
        return _r("departures", "not_available", f"Not available: no signed spec could be verified ({spec['why']}) and {CHOICE} "
                  "holds no list of departures, so what was departed from is not known.", ev)
    if not rows:
        return _r("departures", "ok" if spec["verified"] else "not_available",
                  "The signed spec declares no engine departure." if spec["verified"] else
                  f"Not available: no signed spec could be verified ({spec['why']}); {CHOICE} records no departure.", ev)
    status = {"ok": "look", "no_tests_file": "not_available", "unknown_status": "not_available", "unknown_test": "not_available"}
    each = [(r, status.get(r["state"], "problem")) for r in rows]
    worst = checks.worst([s for _, s in each])
    said = "; ".join(f"{r['kind']} {r['subject']}: {r['says']}" for r, _ in sorted(each, key=lambda x: checks.WORST_FIRST.index(x[1])))
    tail = {"look": " Each is declared and signed, and its test ran: whether the departure is acceptable is for you to judge.",
            "problem": " A departure whose test did not run, or that the verified signed spec does not hold, excuses nothing.",
            "not_available": ""}[worst]
    if not spec["verified"]:
        tail += f" No signed spec could be verified: {spec['why']}."
    table = {"columns": ["subject", "kind", "judged", "why"], "rows": [[r["subject"], r["kind"], s, r["says"]] for r, s in each]}
    return _r("departures", worst, f"{len(rows)} departure(s). {said}.{tail}", ev, table)


def check_choice_text(m):
    c, rows = m["choice"], m["benchmarks"]
    ev = [(CHOICE, "candidates[].benchmarks_not_in_band, benchmarks_reported, departures"), (ENGINE, "data.card.benchmarks[].status")]
    if c["why"]:
        return _r("choice_text", "not_available", f"Not available: {c['why']}.", ev[:1])
    if c["chosen"] is None:
        return _r("choice_text", "not_available", f"Not available: {CHOICE} holds no candidate with the reference of the engine it chose.", ev[:1])
    if m["engine"]["why"] or rows is None:
        return _r("choice_text", "not_available", f"Not available: {m['engine']['why'] or ENGINE + ' holds no list of benchmarks'}, "
                  "so the choice record's list has no table to be compared with.", ev)
    name, version = R._text(c["chosen"].get("name"), 200), R._text(c["chosen"].get("version"), 60)
    if (name, version) != (m["engine"]["name"], m["engine"]["version"]):
        return _r("choice_text", "problem", f"The choice record chose {name} {version}, and the engine record is of {m['engine']['name']} "
                  f"{m['engine']['version']}: the two files are about different engines.", ev)
    if c["listed"] is None:
        return _r("choice_text", "not_available", f"Not available: {CHOICE} holds no list of benchmarks not in band for the chosen "
                  "engine (a choice record of an older shape).", ev[:1])
    unknown = [r["id"] for r in rows if r["state"] == "unknown"]
    if unknown:
        return _r("choice_text", "not_available", f"Not available: {_some(unknown)} has a status word the producer does not write, "
                  "so the table cannot say which benchmarks are not in band.", ev)
    listed = {str(e.get("id")): e.get("status") for e in c["listed"]}
    table = {r["id"]: r["status"] for r in rows if r["gates"] and r["state"] in R.NOT_IN_BAND and r["id"] not in c["counted"]}
    only_list, only_table = sorted(set(listed) - set(table)), sorted(set(table) - set(listed))
    differ = sorted(i for i in set(listed) & set(table) if listed[i] != table[i])
    if only_list or only_table or differ:
        return _r("choice_text", "problem",
                  f"The choice record lists {_some(sorted(listed)) or 'no benchmark'} as not in band; the table gives "
                  f"{_some(sorted(table)) or 'none'} (gating, not in band, named by no departure the choice counted)."
                  + (f" Only in the table: {_some(only_table)}." if only_table else "")
                  + (f" Only in the choice record: {_some(only_list)}." if only_list else "")
                  + (f" With another status: {_some(differ)}." if differ else "")
                  + " The engine choice was made on a different reading of the benchmarks than this run's engine record gives.", ev)
    return _r("choice_text", "ok", f"The choice record's list of benchmarks not in band ({_some(sorted(listed)) or 'none'}) agrees with the table.", ev)


def check_spec_values(m):
    ev = [(ENGINE, "data.spec_values[].outside_card_range, outside, range, card_range"), (ENGINE, "spec_sha256"), (checks.SPEC_SIGNOFF, "sha256")]
    if m["engine"]["why"]:
        return _no_record(m, "spec_values", "spec_values")
    if not m["spec_values_ok"]:
        return _r("spec_values", "not_available", f"Not available: data.spec_values in {ENGINE} is not a mapping of parameters.", ev[:1])
    rows, spec = m["spec_values"], m["spec"]
    if not rows:
        return _r("spec_values", "ok", "The spec sets no engine parameter value.", ev[:1])
    wrong = [r for r in rows if r["wrong"]]
    flagged = [r for r in rows if r["flag"] is True and not r["wrong"]]
    # The record's own flag is the spec's (toolkit/spec/engine_parameters.py rows()): it is a signed declaration
    # only when the record was computed from the spec that is verified as signed.
    from_signed = spec["verified"] and m["engine"]["spec_sha"] == spec["sha"]

    def what(r):
        return (f"{r['name']}: its {r['outside']} ({g(r['value']) if r['outside'] == 'value' else g(r['spec_low']) + ' to ' + g(r['spec_high'])}) "
                f"lies outside the card's range {g(r['card_low'])} to {g(r['card_high'])}")
    if wrong:
        return _r("spec_values", "problem", f"{len(wrong)} of {len(rows)} spec-set value(s) have a record that contradicts itself: "
                  + "; ".join(f"{r['name']}: {r['wrong']}" for r in wrong[:SHOWN]) + ".", ev)
    if flagged and not from_signed:
        why = (f"no signed spec could be verified ({spec['why']})" if not spec["verified"] else
               f"the engine record was computed from another spec ({str(m['engine']['spec_sha'])[:8]}) than the signed copy ({spec['sha'][:8]})")
        return _r("spec_values", "problem", f"{len(flagged)} of {len(rows)} spec-set value(s) outside the card: "
                  + "; ".join(what(r) for r in flagged[:SHOWN]) + f". Nothing signed declares it: {why}.", ev)
    if flagged:
        return _r("spec_values", "look", f"{len(flagged)} of {len(rows)} spec-set value(s) outside the card, declared in the spec signed by "
                  f"{spec['signed_by']}: " + "; ".join(what(r) for r in flagged[:SHOWN]) + ". Whether the reason given holds is for you to judge.", ev)
    return _r("spec_values", "ok", f"{len(rows)} spec-set value(s), none outside the card's range.", ev)


def check_choice_bound(m):
    heads, spec = m["headers"], m["spec"]
    ev = [("analysis/*.yaml", "engine_choice_sha256"), (CHOICE, "sha256 of the file"), (SIGNED, "engine")]
    if not heads:
        return _r("choice_bound", "not_available", "Not available: no analysis file has a header with the key engine_choice_sha256.", ev[:1])
    held = [(rel, h, state) for rel, h, state in heads if h is not None]
    bad = [(rel, state) for rel, _, state in held if state != "same"]
    if bad:
        words = {"changed": "is not the file's hash now", "missing": f"has no {CHOICE} to compare with", "not_recorded": "is no sha256"}
        return _r("choice_bound", "problem", f"{len(bad)} of {len(heads)} analysis header(s) hold a hash for {CHOICE} that "
                  f"{words[bad[0][1]]} ({_some(rel for rel, _ in bad)}): the analysis did not use the engine choice now in the run folder.", ev)
    if held and len(held) < len(heads):
        return _r("choice_bound", "problem", f"{len(held)} of {len(heads)} analysis headers hold a hash for {CHOICE} and the others "
                  "hold null: the headers disagree on whether the engine was chosen.", ev)
    if held:
        return _r("choice_bound", "ok", f"All {len(held)} analysis headers hold a hash for {CHOICE}; against the file each is the same.", ev)
    if not spec["verified"]:
        return _r("choice_bound", "problem", f"The analysis headers hold no hash of the engine choice, and no signed spec could be "
                  f"verified ({spec['why']}), so nothing shows which engine the analysis was bound to.", ev)
    if m["engine_named"]:
        return _r("choice_bound", "ok", "The analysis headers hold no hash of the engine choice; the verified signed spec names "
                  "the engine, so no choice was left to be made.", ev)
    return _r("choice_bound", "look", "The analysis headers hold no hash of the engine choice (engine_choice_sha256 is null) while the "
              f"verified signed spec leaves the engine to be chosen: nothing ties the analysis to {CHOICE} (gap H10).", ev)


def check_seed_range(m):
    ev = [(ENGINE, "data.card.benchmarks[].seed_min, seed_max, band")]
    if m["engine"]["why"] or m["benchmarks"] is None:
        return _no_record(m, "seed_range", "benchmarks") if m["engine"]["why"] else _r(
            "seed_range", "not_available", f"Not available: {ENGINE} holds no list of benchmarks.", ev)
    out = [r for r in m["benchmarks"] if r["seeds_out"]]
    if not out:
        return _r("seed_range", "ok", "No benchmark in band has a seed range that crosses an edge of its band.", ev)
    return _r("seed_range", "look", f"{len(out)} benchmark(s) in band have a seed range that crosses an edge: " + "; ".join(
        f"{r['id']} (seeds {g(r['seed_min'])} to {g(r['seed_max'])} against band {g(r['band_low'])} to {g(r['band_high'])}: "
        f"{g(r['seeds_out'][0])} is beyond {g(r['seeds_out'][1])})" for r in out[:SHOWN])
        + (f"; and {len(out) - SHOWN} more" if len(out) > SHOWN else "") + ". With another seed these would be out of band.", ev,
        {"columns": ["id", "seed_min", "seed_max", "band_low", "band_high"],
         "rows": [[r["id"], r["seed_min"], r["seed_max"], r["band_low"], r["band_high"]] for r in out]})


def check_parameters_sourced(m):
    ev = [(ENGINE, "data.parameters[].verified, lo, hi")]
    rows = m["parameters"]
    if m["engine"]["why"] or rows is None:
        return _no_record(m, "parameters_sourced", "parameters") if m["engine"]["why"] else _r(
            "parameters_sourced", "not_available", f"Not available: {ENGINE} holds no list of parameters.", ev)
    odd = sorted({repr(r["verified"]) for r in rows if r["verified"] not in R.VERIFIED_WORDS})
    if odd:
        return _r("parameters_sourced", "not_available", f"Not available: the record marks parameters {', '.join(odd)}, which is not "
                  f"one of the words the engine contract writes ({', '.join(R.VERIFIED_WORDS)}).", ev)
    unverified = [r["name"] for r in rows if r["verified"] == "unverified"]
    no_range = [r["name"] for r in rows if r["low"] is None or r["high"] is None]
    named = list(dict.fromkeys(unverified + no_range))
    if not named:
        return _r("parameters_sourced", "ok", f"All {len(rows)} parameters have a range, and none is marked unverified.", ev)
    return _r("parameters_sourced", "look", f"{len(named)} of {len(rows)} parameters are marked unverified or have no range "
              f"({_some(named)}): {len(unverified)} unverified (the source was not checked against the value), {len(no_range)} with no "
              "range; a workbench check, not a threshold.", ev,
              {"columns": ["name", "verified", "has_range"], "rows": [[r["name"], r["verified"], r["low"] is not None and r["high"] is not None]
                                                                      for r in rows if r["name"] in named]})


def check_fitted_at_bound(m):
    ev = [(ENGINE, "data.card.fitted[].value, lo, hi")]
    if m["engine"]["why"]:
        return _no_record(m, "fitted_at_bound", "parameters")
    fitted = dict(m["fitted"] or {})
    for r in m["parameters"] or []:        # a fitted parameter the card's own list leaves out
        if r["kind"] == "fitted":
            fitted.setdefault(r["name"], (r["value"], r["low"], r["high"]))
    if not fitted:
        return _r("fitted_at_bound", "ok", "This engine has no fitted parameter.", ev)
    unknown = [n for n, (v, lo, hi) in fitted.items() if None in (v, lo, hi) or hi <= lo]
    at = [(n, v, lo, hi) for n, (v, lo, hi) in fitted.items() if n not in unknown and min(v - lo, hi - v) <= R.BOUND * (hi - lo)]
    if at:
        return _r("fitted_at_bound", "look", f"{len(at)} fitted parameter(s) on the bound of the fitted range: " + "; ".join(
            f"{n} ({g(v)} in {g(lo)} to {g(hi)})" for n, v, lo, hi in at) + ". A fit that ends on its bound may want to go further "
            "than the range lets it.", ev)
    if unknown:
        return _r("fitted_at_bound", "not_available", f"Not available: {_some(unknown)} has no value or no fitted range in the record.", ev)
    return _r("fitted_at_bound", "ok", f"No fitted parameter sits on the bound of its fitted range ({len(fitted)} fitted).", ev)


RULES = (check_benchmarks, check_departures, check_choice_text, check_spec_values, check_choice_bound, check_seed_range,
         check_parameters_sourced, check_fitted_at_bound)


def run_all(m):
    """The eight checks on one model, in order; a check that fails on the run's files says so and the rest run."""
    out = []
    for fn in RULES:
        try:
            out.append(fn(m))
        except Exception as e:      # a check must not take the page down; say which one failed and why
            out.append(_r(fn.__name__[6:], "not_available", f"This check failed on the run's files ({type(e).__name__}: {e}).", []))
    return out


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import workbench
    from views.fit_bench import Bench, fixture_builder, need, rewrite
    fx = fixture_builder()
    b = Bench(lambda run: run_all(R.read(run)))
    hm = "history_matching"

    def status(d):
        return {c["id"].split(".")[-1]: c for c in run_all(R.read(workbench.Run(d)))}

    def record(change):
        return rewrite(R.ENGINE_REC, lambda doc: change(doc["data"]))

    def shape():
        got = status(b.folder(hm))
        need(list(got) == list(CHECKS) and all(c["status"] == "ok" and c["step"] == "model" and c["title"] == TITLES[n] and c["evidence"]
                                                 and c["case"] == CHECKS[n] for n, c in got.items()),
             f"eight checks in order, each ok on the clean run with its title, case and evidence ({ {n: c['status'] for n, c in got.items()} })")

    def judged():
        m = R.read(workbench.Run(b.folder(hm, "C4_out_of_band_departure")))
        by = {r["id"]: _judge(m, r) for r in m["benchmarks"]}
        need(by["B2"][:2] == ("look", "covered") and "covers" in by["B2"][2] and by["B1"] == ("ok", "clear", ""), f"a covered row is for the reader, a clear one says nothing ({by['B2']})")
        m = R.read(workbench.Run(b.folder(hm, "C4_out_of_band_departure_not_run")))
        got = _judge(m, next(r for r in m["benchmarks"] if r["id"] == "B2"))
        need(got[:2] == ("problem", "uncovered") and "does not count" in got[2] and "did not run" in got[2], f"a departure whose test did not run covers nothing ({got})")
        m = R.read(workbench.Run(b.folder(hm, "C4_not_gating_out_of_band")))
        got = _judge(m, next(r for r in m["benchmarks"] if r["id"] == "B4"))
        need(got[:2] == ("look", "not_gating") and "does not gate" in got[2], f"a row that does not gate is for the reader ({got})")

    def rows_behind():
        c = status(b.folder(hm, "C4_out_of_band"))["benchmarks"]
        need(c["rows"]["columns"][0] == "id" and [r[0] for r in c["rows"]["rows"]] == ["B3"] and c["rows"]["rows"][0][3] == "problem",
             f"the rows behind the check are the rows it raises ({c['rows']})")
        c = status(b.folder(hm, "parameters_unverified"))["parameters_sourced"]
        need(sorted(r[0] for r in c["rows"]["rows"]) == ["b_add", fx.HELD], "and the same for the parameters with no checked source or no range")

    def odd_words():
        got = status(b.edited(hm, record(lambda data: data["parameters"][0].update(verified="checked"))))
        need(got["parameters_sourced"]["status"] == "not_available" and "'checked'" in got["parameters_sourced"]["says"],
             "a verified word the engine contract does not write is named, and nothing is counted")
        got = status(b.edited(hm, record(lambda data: data["card"]["fitted"]["f1"].update(lo=None))))
        need(got["fitted_at_bound"]["status"] == "not_available" and "f1" in got["fitted_at_bound"]["says"], "a fitted parameter with no range is not said to be clear of it")
        got = status(b.edited(hm, rewrite(R.ENGINE_REC, lambda doc: doc.update(spec_sha256="1" * 64)), "spec_value_outside_card"))
        need(got["spec_values"]["status"] == "problem" and "another spec" in got["spec_values"]["says"],
             "a flag in a record computed from another spec than the signed one is no signed declaration")

    def bound():
        def one_null(d):
            rewrite(fx.UNC, lambda doc: doc.update(engine_choice_sha256=None))(d)
        got = status(b.edited(hm, one_null))["choice_bound"]
        need(got["status"] == "problem" and "disagree" in got["says"], f"headers that disagree on whether the engine was chosen are a problem ({got['says']})")
        got = status(b.edited(hm, rewrite(fx.UNC, lambda doc: doc.update(engine_choice_sha256="abc"))))["choice_bound"]
        need(got["status"] == "problem" and "no sha256" in got["says"], "a recorded hash that is malformed is never a match")

    b.case("MC1 eight checks in order, each ok on the clean run, with title, scorecard case and evidence", shape)
    b.case("MC2 one benchmark row: covered, not covered because its test did not run, not gating", judged)
    b.case("MC3 a check that raises rows lists them behind it", rows_behind)
    b.case("MC4 a word the producer does not write, a fitted range that is missing, a record from another spec: never ok", odd_words)
    b.case("MC5 choice_bound: headers that disagree, or a malformed hash, are problems", bound)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: model_checks.py --self-test")
