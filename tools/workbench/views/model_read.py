#!/usr/bin/env python3
"""What the Model view reads (docs/design/review-workbench.md §5.4), as one generic model: `read(run)`.

    python3 tools/workbench/views/model_read.py --self-test

It reads the run's own copies only, never the repository's engine card: analysis/engine.yaml (the card as the run used
it, the engine's parameters, the values the spec sets), engine_choice.json, the verified signed spec, the decision
tests, analysis/benchmarks_at_spec_values.yaml with its record, and analysis/figures.yaml for the harness figures
that belong beside this view. views/model.py runs the checks on what this returns and never opens a file itself.

The producers' words are mapped explicitly (§3); a word they do not write is `unknown`, never guessed at:
- benchmark statuses: toolkit/engines/benchmark.py STATUSES (in_band, out_of_band, not_assessable, band_mis_based);
  `in_band` on a row is `status == "in_band"` there, so a row where the two differ contradicts itself;
- a departure's status in the engine choice: toolkit/engines/benchmark_coverage.py signature() (signed, proposed,
  unsigned); only `signed` is a signature of the owner on an unchanged spec;
- a parameter's `verified`: toolkit/engines/contract.py (fetched, matched, unverified).

read(run) returns a mapping:
  engine      {file, why, name, version, schema, spec_sha}: `why` says why the engine record cannot be used (then the
              lists below that come from it are None); `spec_sha` is the spec hash its header records
  choice      {file, why, at, text, chosen, listed, reported, counted, departures}: `chosen` is the candidate the
              record chose (the eligible candidate whose ref is the record's engine ref); `listed` its
              benchmarks_not_in_band (None: the record has no such list), `reported` the ids it only reports,
              `counted` the benchmark ids its own departures name
  spec        checks.verified_spec(run): the one loader of the signed spec
  tests       checks.decision_tests(run): the one loader of decision tests; `tests_why` when there is none
  benchmarks  [row] in the record's order: the fourteen columns and, for the checks, `state` (the status word when
              it is one of the producer's, else `unknown`), `contradiction` (what in the row disagrees with its
              status), `edge` ((the nearer edge, its distance as a share of the band's width) for a row in band
              with a band, on the log scale where the band is above zero), `seeds_out` (the seed beyond an edge),
              `cover` (the departure that covers it, when one does), `why_not` (the producer's own reason a row is
              not in band)
  departures  [row]: the nine columns and `state` (ok, not_signed, not_counted, no_test, not_run, contradiction,
              unknown_test, unknown_status, no_tests_file), `near` (share within 25% of the test's own threshold)
              and `says`; one row per departure of the verified signed spec, then any the choice record alone holds
  parameters, spec_values, mechanisms, candidates, recomputed, images: rows as the tables show them
"""
from __future__ import annotations

import math
import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WB not in sys.path:
    sys.path.insert(0, _WB)
import checks  # noqa: E402

ENGINE_REC, CHOICE, DT, FIGS = "analysis/engine.yaml", "engine_choice.json", checks.DT, "analysis/figures.yaml"
BSV, BSV_REC = "analysis/benchmarks_at_spec_values.yaml", "analysis/benchmarks_at_spec_values.record.json"
SIGNED, SPEC = checks.SIGNED_COPY, checks.SPEC
SCHEMA = "analysis.engine/1"
BENCH_STATUSES = ("in_band", "out_of_band", "not_assessable", "band_mis_based")       # toolkit/engines/benchmark.py STATUSES
NOT_IN_BAND = BENCH_STATUSES[1:]
DEPARTURE_STATUSES = ("signed", "proposed", "unsigned")      # toolkit/engines/benchmark_coverage.py signature()
VERIFIED_WORDS = ("fetched", "matched", "unverified")        # toolkit/engines/contract.py ParamSpec.verified
NEAR = 0.25                  # §3 and §5.4: a near miss is within a quarter of the band's log width, or of a threshold
BOUND = 1e-6                 # §3: a bound is reached within 1e-6 of the range's width
MODEL_FIGURES = ("parameters", "design_coverage", "sensitivity")       # §5.12: the harness figures beside Model


def num(x):
    """x when it is a finite number, else None (a value the harness could not compute is not a number)."""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def g(x):
    return "none" if x is None else f"{x:.4g}" if isinstance(x, float) else str(x)


def _text(x, cap=2000):
    return " ".join(str(x).split())[:cap] if x not in (None, "") else None


def _pair(x):
    """(low, high) of a two-number list, else (None, None)."""
    if isinstance(x, (list, tuple)) and len(x) == 2 and num(x[0]) is not None and num(x[1]) is not None:
        return x[0], x[1]
    return None, None


def _dicts(x):
    return [e for e in x if isinstance(e, dict)] if isinstance(x, list) else []


def _engine(run):
    """(the engine record's `data`, {file, why, name, version, schema})."""
    out = {"file": ENGINE_REC, "why": None, "name": None, "version": None, "schema": None, "spec_sha": None}
    doc, why = run.large(ENGINE_REC)
    if doc is None:
        return None, {**out, "why": why}
    out.update(schema=doc.get("schema"), spec_sha=checks.digest(doc.get("spec_sha256")))
    if doc.get("schema") != SCHEMA:        # §3a: an unknown schema is named, never guessed at
        return None, {**out, "why": f"{ENGINE_REC} has the schema {doc.get('schema')!r}, which this view does not know ({SCHEMA})"}
    data = doc.get("data")
    card = data.get("card") if isinstance(data, dict) else None
    if not isinstance(card, dict):
        return None, {**out, "why": f"{ENGINE_REC} holds no engine card (data.card)"}
    out.update(name=_text(card.get("name"), 200), version=_text(card.get("version"), 60))
    return data, out


def _choice(run):
    out = {"file": CHOICE, "why": None, "at": None, "text": None, "chosen": None, "listed": None, "reported": set(),
           "counted": set(), "departures": None, "candidates": None}
    doc, why = run.json(CHOICE)
    if doc is None:
        return {**out, "why": why}
    cands = doc.get("candidates")
    if not isinstance(cands, list):
        return {**out, "why": f"{CHOICE} holds no list of candidates"}
    ref = doc["engine"].get("ref") if isinstance(doc.get("engine"), dict) else None
    same = [c for c in _dicts(cands) if ref is not None and c.get("ref") == ref]
    chosen = next((c for c in same if c.get("eligible") is True), same[0] if same else None)
    out.update(at=_text(doc.get("at"), 40), text=_text(doc.get("why"), 1200), chosen=chosen, candidates=cands,
               departures=_dicts(doc["engine_departures"]) if isinstance(doc.get("engine_departures"), list) else None)
    if chosen is not None:
        if isinstance(chosen.get("benchmarks_not_in_band"), list):
            out["listed"] = _dicts(chosen["benchmarks_not_in_band"])
        out["reported"] = {str(r.get("id")) for r in _dicts(chosen.get("benchmarks_reported"))}
        out["counted"] = {str(d.get("benchmark")) for d in _dicts(chosen.get("departures")) if d.get("benchmark")}
    return out


def _identity(d):
    """A departure is {engine, benchmark | component, reason, decision_test} (benchmark_coverage.py): its identity is
    the engine, which of the two it names, the name and the test."""
    kind = "component" if d.get("component") else "benchmark"
    return _text(d.get("engine"), 200), kind, _text(d.get(kind), 200), _text(d.get("decision_test"), 200)


def _departures(spec, choice, tests, tests_why):
    """One row per departure of the verified signed spec, then any the choice record marks signed that the signed spec
    does not hold. None when neither file gives a list."""
    recorded = {}
    for d in choice["departures"] or []:
        recorded.setdefault(_identity(d), d)
    rows, seen = [], set()

    def row(d, signed):
        engine, kind, subject, test = _identity(d)
        rec = recorded.get((engine, kind, subject, test))
        word = rec.get("status") if rec is not None else None
        t = (tests or {}).get(test) if test else None
        share, limit = (t or {}).get("share_pct"), (t or {}).get("threshold_pct")
        r = {"subject": subject, "kind": kind, "reason": _text(d.get("reason")), "status": word if isinstance(word, str) else None,
             "signed": signed, "test": test, "verdict": (t or {}).get("verdict") if isinstance((t or {}).get("verdict"), str) else None,
             "share_pct": share, "share_threshold_pct": limit, "engine": engine, "near": False}
        if not signed:
            r.update(state="not_signed", says=f"{CHOICE} gives it the status {word!r}, and the verified signed spec does not hold it")
        elif choice["why"] is not None or choice["departures"] is None:
            r.update(state="unknown_status", says=f"{CHOICE} records no status for it ({choice['why'] or 'it holds no engine_departures list'})")
        elif rec is None:
            r.update(state="not_counted", says=f"{CHOICE} does not record it, so nothing shows the engine choice counted it")
        elif word not in DEPARTURE_STATUSES:
            r.update(state="unknown_status", says=f"{CHOICE} gives it the status {word!r}, a word the engine choice does not write")
        elif word != "signed":
            r.update(state="not_counted", says=f"{CHOICE} gives it the status {word} (not the owner's signature on an unchanged spec), so it does not count")
        elif not test:
            r.update(state="no_test", says="it names no decision test")
        elif tests is None:
            r.update(state="no_tests_file", says=f"its test {test} cannot be read ({tests_why})")
        elif t is None:
            r.update(state="not_run", says=f"{DT} holds no test {test}")
        elif t["state"] == "ran":
            r["near"] = bool(t["verdict"] == "NO_CHANGE" and num(share) is not None and num(limit) is not None
                             and limit > 0 and share >= (1 - NEAR) * limit)
            at = f" at {g(share)}% of units against its {g(limit)}% threshold" if num(share) is not None and num(limit) is not None else ""
            r.update(state="ok", says=f"its test {test} ran with verdict {t['verdict']}{at}"
                                      + (f", within {NEAR:.0%} of the threshold (near miss)" if r["near"] else ""))
        elif t["state"] == "not_run":
            r.update(state="not_run", says=f"its test {test} did not run (status {t['status']}, verdict {t['verdict']})")
        elif t["state"] == "contradiction":
            r.update(state="contradiction", says=f"its test {test} has status {t['status']} with verdict {t['verdict']}, a pair the "
                                                 "analysis does not write, so it is not a test that ran")
        else:
            r.update(state="unknown_test", says=f"its test {test} has status {t['status']!r} and verdict {t['verdict']!r}, "
                                                "words the analysis does not write")
        return r
    if spec["verified"]:
        for d in _dicts(spec["doc"].get("engine_departures")):
            seen.add(_identity(d))
            rows.append(row(d, True))
    elif choice["departures"] is None:
        return None
    rows += [row(d, False) for key, d in recorded.items() if key not in seen]
    return rows


def _benchmarks(data, engine, choice, departures):
    rows = data["card"].get("benchmarks")
    if not isinstance(rows, list):
        return None
    cover = {r["subject"]: r for r in departures or [] if r["kind"] == "benchmark" and r["signed"] and r["engine"] == engine["name"]}
    out = []
    for b in _dicts(rows):
        bid, word = _text(b.get("id"), 200), b.get("status")
        lo, hi = _pair(b.get("band"))
        value, smin, smax = num(b.get("computed")), num(b.get("seed_min")), num(b.get("seed_max"))
        state = word if isinstance(word, str) and word in BENCH_STATUSES else "unknown"
        dep = cover.get(bid)
        covers = b.get("covers")
        r = {"id": bid, "metric": _text(b.get("metric")), "role": _text(b.get("role"), 60), "target": num(b.get("target")),
             "band_low": lo, "band_high": hi, "computed": value, "seed_min": smin, "seed_max": smax,
             "status": word if isinstance(word, str) else None,
             "gates": not (bid in choice["reported"] or ("covers" in b and isinstance(covers, (dict, list)) and not covers)),
             "source": _text(b.get("source")), "departure_test": dep["test"] if dep else None,
             "departure_verdict": dep["verdict"] if dep else None,
             "state": state, "contradiction": None, "edge": None, "seeds_out": None, "cover": dep, "why_not": None}
        if state != "unknown" and "in_band" in b and b["in_band"] is not (state == "in_band"):
            r["contradiction"] = f"its in_band is {b['in_band']!r} beside the status {word}"
        elif state == "in_band" and lo is not None and value is not None and not lo <= value <= hi:
            r["contradiction"] = f"its status is in_band while its value {g(value)} is outside its band {g(lo)} to {g(hi)}"
        elif state == "in_band" and lo is not None and value is not None and hi > lo:
            log = lo > 0 and value > 0
            width = math.log(hi / lo) if log else hi - lo
            below, above = (math.log(value / lo), math.log(hi / value)) if log else (value - lo, hi - value)
            r["edge"] = (lo, below / width) if below <= above else (hi, above / width)
            if smin is not None and smin < lo:
                r["seeds_out"] = (smin, lo)
            elif smax is not None and smax > hi:
                r["seeds_out"] = (smax, hi)
        if state == "band_mis_based":
            reach = _pair(b.get("reach_in_fitted_range"))
            r["why_not"] = f"the band cannot be reached ({_text(b.get('mis_based_reason'), 80) or 'no reason recorded'}" + (
                f"; the fitted range reaches {g(reach[0])} to {g(reach[1])}" if reach[0] is not None else "") + ")"
        elif state == "not_assessable":
            r["why_not"] = f"it could not be assessed ({_text(b.get('not_assessable_because'), 160) or 'no reason recorded'})"
        elif state == "out_of_band":
            r["why_not"] = (f"computed {g(value)} against its band {g(lo)} to {g(hi)}" if lo is not None else
                            f"computed {g(value)}; it is judged by rules, with no band")
        out.append(r)
    return out


def _parameters(data):
    """(rows of the parameters table, rows of the spec-set values, the record's spec_values or why it is unusable)."""
    pars = data.get("parameters")
    sv = data.get("spec_values", {})
    sv_ok = isinstance(sv, dict) and all(isinstance(v, dict) for v in sv.values())
    by = sv if sv_ok else {}
    rows = None
    if isinstance(pars, list):
        rows = []
        for p in _dicts(pars):
            s = by.get(p.get("name"))
            slo, shi = _pair(s.get("range")) if s else (None, None)
            rows.append({"name": _text(p.get("name"), 200), "value": num(p.get("value")), "low": num(p.get("lo")), "high": num(p.get("hi")),
                         "units": _text(p.get("units"), 80), "kind": _text(p.get("kind"), 40), "source": _text(p.get("source")),
                         "owner_value": num(s.get("value")) if s else None, "card_value": num(s.get("card_value")) if s else None,
                         "outside_card": (s.get("outside_card_range") if isinstance(s.get("outside_card_range"), bool) else None) if s else None,
                         "value_used": num(s.get("value")) if s and num(s.get("value")) is not None else num(p.get("value")),
                         "spec_low": slo, "spec_high": shi, "verified": p.get("verified")})
    values = []
    for name, s in by.items():
        clo, chi = _pair(s.get("card_range"))
        slo, shi = _pair(s.get("range"))
        value, flag, word = num(s.get("value")), s.get("outside_card_range"), s.get("outside")
        if clo is None:
            found = None
        elif value is not None and not clo <= value <= chi:
            found = "value"
        else:
            found = "range" if slo is not None and (slo < clo or shi > chi) else None
        wrong = None       # toolkit/spec/engine_parameters.py: the flag is the spec's; `outside` is computed from the numbers
        if not isinstance(flag, bool):
            wrong = f"its outside_card_range is {flag!r}, neither true nor false"
        elif found != (word if word in ("value", "range") else None) or (word is not None and word not in ("value", "range")):
            wrong = f"its outside is {word!r} while its numbers give {found!r}"
        elif flag is not (found is not None):
            wrong = (f"its outside_card_range is {str(flag).lower()} while its {found or 'value and range'} "
                     f"{'lies outside' if found else 'lie inside'} the card's range" if clo is not None else
                     "its outside_card_range is true while the record gives no card range")
        values.append({"name": str(name), "units": _text(s.get("units"), 80), "card_value": num(s.get("card_value")), "card_low": clo,
                       "card_high": chi, "value": value, "spec_low": slo, "spec_high": shi, "outside": found if flag is True else None,
                       "uncertainty": _text(s.get("uncertainty"), 300), "source": _text(s.get("source")), "reason": _text(s.get("reason")),
                       "flag": flag, "wrong": wrong})
    return rows, values, (sv if sv_ok else None)


def _recomputed(run, spec):
    """(rows or None, a sentence about the file or None): benchmarks recomputed at the spec's values, when the run
    has them. The file has no artifact header; its record file holds its hash and the hash of the spec it was for."""
    raw, why = run.read(BSV, 1)
    if raw is None and str(why).endswith("is not in the run folder"):
        return None, None
    doc, why = run.large(BSV)
    if doc is None or not isinstance(doc.get("benchmarks"), list):
        return None, f"{why or BSV + ' holds no list of benchmarks'}, so the benchmarks recomputed at the spec's values are not shown."
    rec, rec_why = run.json(BSV_REC)
    held = checks.digest((rec or {}).get("spec_sha256"))
    names = {}
    for t in _dicts((((spec["doc"] or {}).get("analysis") or {}).get("decision_tests")) if isinstance((spec["doc"] or {}).get("analysis"), dict) else None):
        if t.get("benchmark") is not None and t.get("id") is not None:
            names.setdefault(str(t["benchmark"]), str(t["id"]))
    rows = [{"id": _text(b.get("id"), 200), "computed": num(b.get("computed")), "status": b.get("status") if isinstance(b.get("status"), str) else None,
             "spec_hash": held, "test": names.get(str(b.get("id")))} for b in _dicts(doc["benchmarks"])]
    state = checks.hash_state((rec or {}).get("sha256"), run.sha(BSV))
    said = f"{BSV} holds {len(rows)} benchmark(s) recomputed at the values the spec sets. "
    if rec is None:
        said += f"Its record cannot be read ({rec_why}), so nothing says which spec it was computed for."
    else:
        said += (f"Its record ({BSV_REC}) holds a hash for the file that is {state.replace('_', ' ')} against the file now, and "
                 + ("no valid hash of a spec." if held is None else
                    "the hash of the verified signed spec." if spec["verified"] and held == spec["sha"] else
                    f"the hash of another spec ({held[:8]}) than the signed copy ({str(spec['sha'])[:8]}): "
                    "these values were computed for a spec that is not the one signed."))
    return rows, said


def _images(run):
    """Rows of the harness figures that belong beside this view (§5.12), each with the hash of the image as read, or
    None when the run has no figure index."""
    doc, _ = run.large(FIGS)
    figs = doc["data"].get("figures") if doc is not None and isinstance(doc.get("data"), dict) else None
    if not isinstance(figs, list):
        return None
    rows = []
    for e in _dicts(figs):
        name, rel = e.get("figure") or e.get("id"), e.get("path")
        if name in MODEL_FIGURES and isinstance(rel, str):
            sha = run.sha(rel)
            if sha is not None:
                rows.append({"figure": name, "path": rel, "sha256": sha, "title": _text(e.get("title"), 400), "caption": _text(e.get("caption"))})
    return rows


def read(run):
    data, engine = _engine(run)
    choice, spec = _choice(run), checks.verified_spec(run)
    tests, tests_why = checks.decision_tests(run)
    m = {"engine": engine, "choice": choice, "spec": spec, "tests": tests, "tests_why": tests_why,
         "benchmarks": None, "parameters": None, "spec_values": None, "spec_values_ok": None, "mechanisms": None, "fitted": None,
         "candidates": None, "headers": _headers(run)}
    m["departures"] = _departures(spec, choice, tests, tests_why)
    if data is not None:
        m["benchmarks"] = _benchmarks(data, engine, choice, m["departures"])
        m["parameters"], m["spec_values"], sv = _parameters(data)
        m["spec_values_ok"] = sv is not None
        mech = data["card"].get("mechanisms")
        m["mechanisms"] = [_text(x) for x in mech if _text(x)] if isinstance(mech, list) else None
        fitted = data["card"].get("fitted")
        m["fitted"] = ({str(k): (num(v.get("value")), num(v.get("lo")), num(v.get("hi"))) for k, v in fitted.items() if isinstance(v, dict)}
                       if isinstance(fitted, dict) else None)
    if choice["candidates"] is not None:
        m["candidates"] = [{"name": _text(c.get("name"), 200) or _text(c.get("ref"), 200) or "(no name and no reference)",
                            "version": _text(c.get("version"), 60), "eligible": c.get("eligible") is True,
                            "n_reasons": len(c["why_not"]) if isinstance(c.get("why_not"), list) else 0,
                            "first_reason": _text(c["why_not"][0], 600) if isinstance(c.get("why_not"), list) and c["why_not"] else None}
                           for c in _dicts(choice["candidates"])]
    m["recomputed"], m["recomputed_says"] = _recomputed(run, spec)
    m["images"] = _images(run)
    engine_set = (spec["doc"] or {}).get("engine")
    m["engine_named"] = (None if not spec["verified"] else
                         bool(engine_set) and engine_set != "auto" and not (isinstance(engine_set, dict) and engine_set.get("auto") is True))
    return m


def _headers(run):
    """[(analysis file, the hash of the engine choice its header holds, that hash against engine_choice.json)] for
    every analysis file whose header has the key. The harness writes the hash only when the spec leaves the engine to
    the engine choice (toolkit/analysis/artifacts.py), and null otherwise."""
    now, out = run.sha(CHOICE), []
    for name in sorted(run.names("analysis")):
        if not name.endswith(".yaml"):
            continue
        head, _ = run.envelope(f"analysis/{name}")
        if isinstance(head, dict) and "engine_choice_sha256" in head:
            held = head["engine_choice_sha256"]
            out.append((f"analysis/{name}", held, None if held is None else checks.hash_state(held, now)))
    return out


# ---------------------------------------------------------------------------------------------- self-test

def _self_test():
    import json

    import workbench
    from views.fit_bench import Bench, fixture_builder, need, rewrite
    fx = fixture_builder()
    b = Bench(read)
    hm = "history_matching"

    def look(d):
        return read(workbench.Run(d))

    def by(rows, key="id"):
        return {r[key]: r for r in rows}

    def rejson(rel, change):
        def edit(d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                doc = json.load(f)
            change(doc)
            with open(os.path.join(d, rel), "w", encoding="utf-8") as f:
                json.dump(doc, f)
        return edit

    def record(change):
        return rewrite(ENGINE_REC, lambda doc: change(doc["data"]))

    def words():
        import ast
        src = {}
        for rel in ("toolkit/engines/benchmark.py", "toolkit/engines/benchmark_coverage.py", "toolkit/engines/contract.py"):
            with open(os.path.join(os.path.dirname(os.path.dirname(_WB)), rel), encoding="utf-8") as f:
                src[rel] = f.read()
        got = next(ast.literal_eval(n.value) for n in ast.parse(src["toolkit/engines/benchmark.py"]).body
                   if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "STATUSES" for t in n.targets))
        need(tuple(got) == BENCH_STATUSES, f"the benchmark status words are the producer's ({got})")
        need(all(f'"{w}"' in src["toolkit/engines/benchmark_coverage.py"] for w in DEPARTURE_STATUSES),
             "the departure status words are the ones signature() returns")
        need(" | ".join(VERIFIED_WORDS) in src["toolkit/engines/contract.py"], "the verified words are the engine contract's")
        need(BSV in open(os.path.join(os.path.dirname(os.path.dirname(_WB)), "toolkit/spec/engine_parameters.py"), encoding="utf-8").read(),
             "the recomputed benchmarks are read from the file the producer writes")

    def clean():
        m = look(b.folder(hm))
        need(m["engine"]["why"] is None and (m["engine"]["name"], m["engine"]["version"]) == ("toy-engine", "0.1"), f"the engine record ({m['engine']})")
        need([r["state"] for r in m["benchmarks"]] == ["in_band"] * 4 and all(r["contradiction"] is None and r["cover"] is None for r in m["benchmarks"]),
             "four benchmarks in band, none covered and none contradicting itself")
        need(all(r["edge"] and r["edge"][1] > NEAR and r["seeds_out"] is None for r in m["benchmarks"]), "each clear of both edges, seeds too")
        need(m["departures"] == [] and m["spec"]["verified"] and m["engine_named"] is False, "no departure; the signed spec is verified and leaves the engine open")
        need(m["choice"]["listed"] == [] and m["choice"]["reported"] == {"B4"} and m["choice"]["chosen"]["name"] == "toy-engine", "the chosen candidate's lists")
        need(len(m["headers"]) == len(fx.artifacts(hm)) and all(state == "same" for _, _, state in m["headers"]), f"every header's hash of the choice ({m['headers']})")
        need([r["name"] for r in m["spec_values"]] == ["p2"] and m["spec_values"][0]["wrong"] is None and m["spec_values"][0]["outside"] is None, "one spec-set value, inside the card")

    def departures():
        m = look(b.folder(hm, "C4_out_of_band_departure"))
        d = by(m["departures"], "subject")["B2"]
        need(d["state"] == "ok" and d["signed"] and d["status"] == "signed" and not d["near"] and "NO_CHANGE" in d["says"], f"a signed departure whose test ran ({d})")
        need(by(m["benchmarks"])["B2"]["cover"] is d and by(m["benchmarks"])["B2"]["departure_test"] == "DT4", "covers its benchmark")
        m = look(b.edited(hm, rejson(CHOICE, lambda doc: [e.update(engine="other-engine") for e in doc["engine_departures"]]), "C4_out_of_band_departure"))
        need([r["state"] for r in m["departures"]] == ["not_counted", "not_signed"], f"a record for another engine is not this departure's ({[r['state'] for r in m['departures']]})")
        m = look(b.edited(hm, rejson(CHOICE, lambda doc: doc["engine_departures"][0].update(status="agreed")), "C4_out_of_band_departure"))
        need(m["departures"][0]["state"] == "unknown_status" and "'agreed'" in m["departures"][0]["says"], "a status word the engine choice does not write is named")
        m = look(b.folder(hm, "signed_by_lead"))
        need({r["state"] for r in m["departures"]} == {"not_counted"} and all("unsigned" in r["says"] for r in m["departures"]),
             "a departure the engine choice marks unsigned (a lead's signature) does not count")
        m = look(b.edited(hm, rejson(CHOICE, lambda doc: doc.pop("engine_departures")), "departure_component"))
        need(m["departures"][0]["state"] == "unknown_status", "a choice record with no departures list says nothing of a signed departure")
        m = look(b.edited(hm, rewrite(SIGNED, lambda doc: doc["engine_departures"][0].update(engine="other-engine")), "C4_out_of_band_departure"))
        need(m["departures"] is not None and all(not r["signed"] for r in m["departures"]) and by(m["benchmarks"])["B2"]["cover"] is None,
             "a signed copy edited after the sign-off is not verified: nothing is signed and nothing is covered")

    def rows():
        m = look(b.edited(hm, record(lambda data: data["card"]["benchmarks"][0].update(band=[-1.0, 1.0], computed=0.9, per_seed=[], seed_min=None, seed_max=None))))
        r = m["benchmarks"][0]
        need(r["edge"][0] == 1.0 and abs(r["edge"][1] - 0.05) < 1e-9, f"a band that reaches zero or below is placed on the plain scale ({r['edge']})")
        m = look(b.edited(hm, record(lambda data: data["card"]["benchmarks"][0].update(computed=float("nan")))))
        need(m["benchmarks"][0]["computed"] is None and m["benchmarks"][0]["edge"] is None, "a value that is not a number is no value")
        m = look(b.edited(hm, record(lambda data: data["spec_values"]["p2"].update(outside="value"))))
        need(m["spec_values"][0]["wrong"], "an `outside` word the numbers do not give contradicts the record")
        m = look(b.edited(hm, record(lambda data: data.update(spec_values=["p2"]))))
        need(m["spec_values_ok"] is False and m["spec_values"] == [], "spec_values that is no mapping is not read as none set")
        m = look(b.edited(hm, rewrite(ENGINE_REC, lambda doc: doc.update(schema="analysis.engine/9"))))
        need(m["benchmarks"] is None and "analysis.engine/9" in m["engine"]["why"], "an unknown schema is named and not read")

    def recomputed():
        d = b.folder(hm, "spec_benchmarks_recomputed")
        m = look(d)
        need(len(m["recomputed"]) == 1 and m["recomputed"][0]["test"] == "DT4" and "verified signed spec" in m["recomputed_says"]
             and "same" in m["recomputed_says"], f"the record ties the file to the signed spec ({m['recomputed_says']})")
        m = look(b.edited(hm, rejson(BSV_REC, lambda doc: doc.update(spec_sha256="1" * 64)), "spec_benchmarks_recomputed"))
        need("another spec" in m["recomputed_says"], "a record made for another spec says so")
        m = look(b.edited(hm, lambda folder: os.remove(os.path.join(folder, BSV_REC)), "spec_benchmarks_recomputed"))
        need(m["recomputed"][0]["spec_hash"] is None and "cannot be read" in m["recomputed_says"], "with no record, no spec hash is shown")
        need(look(b.folder(hm))["recomputed"] is None and look(b.folder(hm))["recomputed_says"] is None, "a run with no such file says nothing")

    b.case("MR1 the status words are read from the producers' code", words)
    b.case("MR2 the clean run: four benchmarks in band, no departure, the choice's lists, every header bound", clean)
    b.case("MR3 a departure counts only as the verified signed spec's entry that the engine choice marks signed, with a test that ran", departures)
    b.case("MR4 rows: a band at or below zero, a value that is no number, a spec value whose words disagree, an unknown schema", rows)
    b.case("MR5 benchmarks recomputed at the spec's values: the record's spec hash against the signed copy", recomputed)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: model_read.py --self-test")
