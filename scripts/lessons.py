#!/usr/bin/env python3
"""
The lessons ledger: what expert review and run post-mortems taught us, what
we changed, and whether the error class came back (docs/lessons/README.md).

    python3 scripts/lessons.py validate                      # the ledger is well formed
    python3 scripts/lessons.py check <run_dir> [--json]      # writes lessons_check.json + .md
    python3 scripts/lessons.py record --lesson L012 --run R --result recurred [--by expert] [--note ..]
    python3 scripts/lessons.py record --from-check <run_dir>/lessons_check.json
    python3 scripts/lessons.py summary [--md]                # recurrence before/after each change
    python3 scripts/lessons.py --self-test

`check` runs at finalize (main.py). For each lesson that applies to the run's
question it reads the automatic signals the lesson declares:
  gate_kinds         finding kinds in validator_history.jsonl: in the last
                     round = recurred (shipped), only earlier = caught
  autopsy_why        substrings of run_autopsy failure reasons = recurred
  peak_mem_pct_over  peak memory in resources.jsonl above the number = recurred
  file_absent        a run-relative path missing at the end = recurred
A signal whose source is missing is `unknown`, never `absent` (fail closed).
Lessons without signals, and lessons with a `review` question, go on the
expert checklist (lessons_check.md) for that run.

Observations live in docs/lessons/observations.jsonl (append-only), so the
hand-edited ledger keeps its comments. `summary` splits each lesson's
observations at its change date and counts recurrences on each side.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(REPO, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(REPO, "scripts"))
from lib.safe_io import read_jsonl as _jsonl  # noqa: E402
LEDGER = os.path.join(REPO, "docs", "lessons", "ledger.yaml")
OBSERVATIONS = os.path.join(REPO, "docs", "lessons", "observations.jsonl")

SOURCE_KINDS = {"expert_review", "deep_review", "experiment", "run_failure", "owner_question"}
LAYERS = {"infrastructure", "harness", "agent_behaviour", "model_quality", "data", "domain_knowledge"}
STATUSES = {"open", "in_progress", "changed", "verified", "retired"}
TEST_HOWS = {"auto", "gate", "replay", "regression", "expert"}
DETECTORS = {"gate_kinds", "autopsy_why", "peak_mem_pct_over", "file_absent"}
RESULTS = {"recurred", "caught", "absent", "not_applicable"}
BY = {"auto", "expert", "maintainer", "replay"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?$")   # UTC; a bare date = start of day


def _d(v) -> str:
    return v.isoformat()[:19] if isinstance(v, date) else str(v)


def load_ledger(path: str = LEDGER) -> tuple[list[dict], list[str]]:
    """Lessons and validation errors. Any error means the ledger is not usable."""
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        return [], [f"ledger unreadable: {type(e).__name__}: {e}"]
    if not isinstance(doc, dict) or doc.get("version") != 1 or not isinstance(doc.get("lessons"), list):
        return [], ["ledger must be a mapping with version: 1 and a lessons list"]
    errs, seen = [], set()
    allowed = {"id", "title", "source", "layer", "applies_when", "finding", "change", "test"}
    for i, L in enumerate(doc["lessons"]):
        if not isinstance(L, dict):
            errs.append(f"lessons[{i}] is not a mapping")
            continue
        lid = L.get("id")
        tag = lid or f"lessons[{i}]"
        if not isinstance(lid, str) or not re.fullmatch(r"L\d{3}", lid):
            errs.append(f"{tag}: id must look like L001")
        elif lid in seen:
            errs.append(f"{tag}: duplicate id")
        seen.add(lid)
        for k in set(L) - allowed:
            errs.append(f"{tag}: unknown field {k!r}")
        for k in ("title", "finding"):
            if not isinstance(L.get(k), str) or not L[k].strip():
                errs.append(f"{tag}: {k} must be a non-empty string")
        src = L.get("source")
        if not isinstance(src, dict):
            errs.append(f"{tag}: source must be a mapping")
        else:
            if src.get("kind") not in SOURCE_KINDS:
                errs.append(f"{tag}: source.kind {src.get('kind')!r} not in {sorted(SOURCE_KINDS)}")
            if not _DATE.match(_d(src.get("date"))):
                errs.append(f"{tag}: source.date must be YYYY-MM-DD")
            for k in ("ref", "by"):
                if not isinstance(src.get(k), str) or not src[k].strip():
                    errs.append(f"{tag}: source.{k} must be a non-empty string")
        if L.get("layer") not in LAYERS:
            errs.append(f"{tag}: layer {L.get('layer')!r} not in {sorted(LAYERS)}")
        aw = L.get("applies_when")
        if not isinstance(aw, list) or not all(isinstance(w, str) and w.strip() for w in aw):
            errs.append(f"{tag}: applies_when must be a list of keywords ([] = every question)")
        ch = L.get("change")
        if not isinstance(ch, dict) or ch.get("status") not in STATUSES:
            errs.append(f"{tag}: change.status must be one of {sorted(STATUSES)}")
        else:
            if not isinstance(ch.get("where"), list) or not ch["where"]:
                errs.append(f"{tag}: change.where must list what was (or will be) changed")
            if not isinstance(ch.get("refs", []), list):
                errs.append(f"{tag}: change.refs must be a list")
            if ch["status"] in ("changed", "verified", "retired") and not _DATE.match(_d(ch.get("date"))):
                errs.append(f"{tag}: change.date (YYYY-MM-DD) is required once status is {ch['status']}")
        t = L.get("test")
        if not isinstance(t, dict):
            errs.append(f"{tag}: test must be a mapping")
            continue
        hows = t.get("how")
        if not isinstance(hows, list) or not hows or not set(hows) <= TEST_HOWS:
            errs.append(f"{tag}: test.how must be a non-empty subset of {sorted(TEST_HOWS)}")
        det = t.get("detect", {})
        if not isinstance(det, dict) or not set(det) <= DETECTORS:
            errs.append(f"{tag}: test.detect keys must be in {sorted(DETECTORS)}")
            det = {}
        for k in ("gate_kinds", "autopsy_why", "file_absent"):
            if k in det and (not isinstance(det[k], list) or not det[k]
                             or not all(isinstance(x, str) and x for x in det[k])):
                errs.append(f"{tag}: test.detect.{k} must be a non-empty list of strings")
        if "peak_mem_pct_over" in det and not isinstance(det["peak_mem_pct_over"], (int, float)):
            errs.append(f"{tag}: test.detect.peak_mem_pct_over must be a number")
        if not det and not t.get("review"):
            errs.append(f"{tag}: a lesson needs an automatic signal (test.detect) or an expert review question")
        if "review" in t and (not isinstance(t["review"], str) or not t["review"].strip()):
            errs.append(f"{tag}: test.review must be a non-empty string")
    return (doc["lessons"] if not errs else []), errs


def applies(lesson: dict, question: str | None) -> bool:
    words = lesson.get("applies_when") or []
    if not words:
        return True
    q = (question or "").lower()
    return any(w.lower() in q for w in words)


def _run_facts(run_dir: str) -> dict:
    """What the signals need, read once. None = source missing (-> unknown)."""
    sys.path.insert(0, os.path.join(REPO, "scripts"))
    import run_autopsy
    try:
        a = json.load(open(os.path.join(run_dir, "autopsy.json")))
    except (OSError, ValueError):
        try:
            a = run_autopsy.autopsy(run_dir)
        except Exception:  # noqa: BLE001 — an unreadable run is unknown, not absent
            a = None
    history = [h for h in _jsonl(os.path.join(run_dir, "validator_history.jsonl")) if isinstance(h, dict)]
    rounds = [{str(f.get("kind")) for f in (h.get("findings") or []) if isinstance(f, dict)} for h in history]
    try:
        meta = json.load(open(os.path.join(run_dir, "metadata.json")))
    except (OSError, ValueError):
        meta = {}
    return {"why": [str(f.get("why")) for f in (a or {}).get("failure", [])] if a else None,
            "peak_mem": ((a or {}).get("resources") or {}).get("peak_mem_pct"),
            "rounds": rounds or None, "question": meta.get("question"),
            "started": str(meta.get("started") or "")[:19] or None}


def _signal(kind: str, arg, facts: dict, run_dir: str) -> tuple[str, str]:
    if kind == "gate_kinds":
        rounds = facts["rounds"]
        if rounds is None:
            return "unknown", "no validator_history.jsonl"
        last = rounds[-1] & set(arg)
        if last:
            return "recurred", f"in the last gate round: {sorted(last)}"
        earlier = set().union(*rounds[:-1]) & set(arg) if len(rounds) > 1 else set()
        if earlier:
            return "caught", f"raised by the gate earlier, cleared by the end: {sorted(earlier)}"
        return "absent", "not raised in any gate round"
    if kind == "autopsy_why":
        if facts["why"] is None:
            return "unknown", "no autopsy"
        hits = [w for w in facts["why"] if any(s in w for s in arg)]
        return ("recurred", "; ".join(hits)) if hits else ("absent", "no matching autopsy reason")
    if kind == "peak_mem_pct_over":
        if facts["peak_mem"] is None:
            return "unknown", "no resource samples"
        p = float(facts["peak_mem"])
        return ("recurred" if p > arg else "absent"), f"peak memory {p}%"
    if kind == "file_absent":
        gone = [p for p in arg if not os.path.exists(os.path.join(run_dir, p))]
        return ("recurred", f"missing: {gone}") if gone else ("absent", "present")
    return "unknown", f"unknown signal {kind}"


_RANK = {"recurred": 3, "caught": 2, "unknown": 1, "absent": 0}


def check(run_dir: str, lessons: list[dict], question: str | None = None) -> dict:
    facts = _run_facts(run_dir)
    q = question if question is not None else facts["question"]
    out = {"run": os.path.basename(os.path.normpath(run_dir)), "question": q, "started": facts["started"],
           "checked_on": date.today().isoformat(), "lessons": []}
    for L in lessons:
        if not applies(L, q):
            out["lessons"].append({"id": L["id"], "result": "not_applicable", "signals": []})
            continue
        det = (L.get("test") or {}).get("detect") or {}
        sig = [{"signal": k, "result": r, "detail": d}
               for k in sorted(det) for r, d in [_signal(k, det[k], facts, run_dir)]]
        res = max((s["result"] for s in sig), key=_RANK.get) if sig else "review"
        row = {"id": L["id"], "title": L["title"], "status": L["change"]["status"], "result": res, "signals": sig}
        if L["test"].get("review"):
            row["review"] = L["test"]["review"]
        out["lessons"].append(row)
    out["recurred"] = [r["id"] for r in out["lessons"] if r["result"] == "recurred"]
    out["caught"] = [r["id"] for r in out["lessons"] if r["result"] == "caught"]
    out["unknown"] = [r["id"] for r in out["lessons"] if r["result"] == "unknown"]
    out["to_review"] = [r["id"] for r in out["lessons"] if r.get("review") and r["result"] != "not_applicable"]
    return out


def check_markdown(c: dict) -> str:
    L = [f"# Lessons check: {c['run']}", "",
         f"Automatic: {len(c['recurred'])} recurred, {len(c['caught'])} caught by a gate, "
         f"{len(c['unknown'])} unknown (signal missing). {len(c['to_review'])} lesson(s) need an expert answer.", ""]
    auto = [r for r in c["lessons"] if r.get("signals")]
    if auto:
        L += ["## Automatic signals", "", "| Lesson | Result | Detail |", "|---|---|---|"]
        L += [f"| {r['id']} {r['title']} | {r['result']} | "
              f"{'; '.join(s['detail'] for s in r['signals'] if s['result'] == r['result'])} |" for r in auto]
        L.append("")
    rev = [r for r in c["lessons"] if r.get("review") and r["result"] != "not_applicable"]
    if rev:
        L += ["## Expert checklist", "",
              "Answer each with recurred / absent / not_applicable, then "
              "`scripts/lessons.py record --lesson ID --run RUN --result R --by expert --note ...`.", ""]
        L += [f"- [ ] **{r['id']}** {r['title']}: {r['review']}" for r in rev]
    return "\n".join(L) + "\n"


def write_check(run_dir: str, lessons: list[dict]) -> dict:
    c = check(run_dir, lessons)
    json.dump(c, open(os.path.join(run_dir, "lessons_check.json"), "w"), indent=1)
    open(os.path.join(run_dir, "lessons_check.md"), "w").write(check_markdown(c))
    return c


def register_fields(run_dir: str, ledger: str = LEDGER) -> dict:
    """For finalize: write the run's lessons check and return the register-row
    fields. Never raises; a failure is returned as {"error": ...}."""
    try:
        lessons, errs = load_ledger(ledger)
        if errs:
            return {"error": f"ledger invalid: {errs[:3]}"}
        c = write_check(run_dir, lessons)
        return {k: c[k] for k in ("recurred", "caught", "unknown", "to_review")}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def load_observations(path: str = OBSERVATIONS) -> tuple[list[dict], list[str]]:
    obs, errs = [], []
    try:
        lines = open(path).read().splitlines()
    except FileNotFoundError:
        return [], []
    except OSError as e:
        return [], [f"observations unreadable: {e}"]
    for i, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except ValueError:
            errs.append(f"observations line {i}: not JSON")
            continue
        if (not isinstance(o, dict) or o.get("result") not in RESULTS or o.get("by") not in BY
                or not _DATE.match(str(o.get("date"))) or not o.get("lesson") or not o.get("run")):
            errs.append(f"observations line {i}: needs lesson, run, date, result in {sorted(RESULTS)}, by in {sorted(BY)}")
            continue
        obs.append(o)
    return obs, errs


def record(entries: list[dict], lessons: list[dict], path: str = OBSERVATIONS) -> list[str]:
    ids = {L["id"] for L in lessons}
    errs = []
    for e in entries:
        if e.get("lesson") not in ids:
            errs.append(f"unknown lesson {e.get('lesson')!r}")
        if e.get("result") not in RESULTS or e.get("by") not in BY or not _DATE.match(str(e.get("date"))):
            errs.append(f"bad observation {e}")
    if errs:
        return errs
    with open(path, "a") as f:
        for e in entries:
            f.write(json.dumps(e, sort_keys=True) + "\n")
    return []


def from_check(c: dict) -> list[dict]:
    """Automatic observations from a check: only definite results, never unknown or review.
    Dated by the run's start, so a run that predates a change counts before it."""
    day = c.get("started") or c.get("checked_on") or date.today().isoformat()
    return [{"lesson": r["id"], "run": c["run"], "date": day, "result": r["result"], "by": "auto",
             "note": "; ".join(s["detail"] for s in r["signals"] if s["result"] == r["result"])[:300]}
            for r in c["lessons"] if r["result"] in ("recurred", "caught", "absent")]


def summarise(lessons: list[dict], obs: list[dict]) -> list[dict]:
    rows = []
    for L in lessons:
        ch = L["change"]
        cut = _d(ch.get("date")) if ch.get("date") else None
        mine = [o for o in obs if o["lesson"] == L["id"] and o["result"] != "not_applicable"]
        before = [o for o in mine if cut is None or str(o["date"]) < cut]
        after = [o for o in mine if cut is not None and str(o["date"]) >= cut]

        def n(xs, r):
            return sum(1 for o in xs if o["result"] == r)
        runs_after = sorted({o["run"] for o in after})
        if after and n(after, "recurred"):
            note = "recurred after the change: reopen"
        elif (ch["status"] == "changed" and len(runs_after) >= 2 and not n(after, "recurred")
              and not (L["test"].get("review") and not any(o["by"] != "auto" and o["result"] == "absent" for o in after))):
            note = "absent or caught in >=2 runs since the change: candidate for verified"
        elif ch["status"] == "changed" and runs_after and not n(after, "recurred") and L["test"].get("review"):
            note = "no automatic recurrence since the change; needs an expert check"
        elif ch["status"] in ("open", "in_progress") and n(before, "recurred"):
            note = "still recurring; no change shipped"
        elif ch["status"] == "changed" and not after:
            note = "no run observed since the change"
        else:
            note = ""
        rows.append({"id": L["id"], "title": L["title"], "layer": L["layer"], "status": ch["status"],
                     "change_date": cut, "before": {"runs": len({o['run'] for o in before}),
                                                    "recurred": n(before, "recurred"), "caught": n(before, "caught")},
                     "after": {"runs": len(runs_after), "recurred": n(after, "recurred"), "caught": n(after, "caught")},
                     "note": note})
    return rows


def summary_markdown(rows: list[dict]) -> str:
    L = ["| Lesson | Layer | Status | Before change: runs / recurred / caught | After: runs / recurred / caught | Note |",
         "|---|---|---|---|---|---|"]
    for r in rows:
        b, a = r["before"], r["after"]
        L.append(f"| {r['id']} {r['title']} | {r['layer']} | {r['status']}"
                 f"{' ' + r['change_date'] if r['change_date'] else ''} | {b['runs']} / {b['recurred']} / {b['caught']} | "
                 f"{a['runs']} / {a['recurred']} / {a['caught']} | {r['note']} |")
    return "\n".join(L) + "\n"


def _self_test() -> int:
    import tempfile
    fails = []
    lessons, errs = load_ledger()
    if errs or len(lessons) < 10:
        fails.append(f"LS1 the checked-in ledger validates: {errs[:5]}")
    good = {"id": "L001", "title": "t", "finding": "f", "layer": "harness", "applies_when": [],
            "source": {"kind": "run_failure", "ref": "r", "date": "2026-10-01", "by": "maintainers"},
            "change": {"status": "changed", "where": ["x"], "refs": [], "date": "2026-10-10"},
            "test": {"how": ["auto"], "detect": {"gate_kinds": ["k_bad"], "autopsy_why": ["killed"],
                                                 "peak_mem_pct_over": 95, "file_absent": ["report.md"]}}}
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "ledger.yaml")

        def errs_for(lesson_list, version=1):
            yaml.safe_dump({"version": version, "lessons": lesson_list}, open(p, "w"))
            return load_ledger(p)[1]
        if errs_for([good]):
            fails.append(f"LS2 a well-formed lesson validates: {errs_for([good])}")
        bad = [dict(good, layer="vibes"), dict(good, change=dict(good["change"], date=None)),
               dict(good, test={"how": ["expert"]}), dict(good, test={"how": ["auto"], "detect": {"grep": ["x"]}}),
               dict(good, extra=1), dict(good, id="X1")]
        for i, b in enumerate(bad):
            if not errs_for([b]):
                fails.append(f"LS3.{i} a malformed lesson is rejected (fail closed): {b}")
        if not errs_for([good, good]) or not errs_for([good], version=2):
            fails.append("LS3b duplicate ids and an unknown version are rejected")
        open(p, "w").write("lessons: [")
        if load_ledger(p)[0] or not load_ledger(p)[1]:
            fails.append("LS3c an unparseable ledger returns no lessons and an error")

        # a run where the gate caught k_bad in round 1 and cleared it; memory peaked at 99%
        run = os.path.join(d, "run1")
        os.makedirs(run)
        hist = [{"round": 1, "findings": [{"kind": "k_bad", "severity": "HIGH"}]}, {"round": 2, "findings": []}]
        open(os.path.join(run, "validator_history.jsonl"), "w").write("\n".join(json.dumps(h) for h in hist))
        json.dump({"failure": [{"layer": "infrastructure", "why": "memory reached 99.9%"}],
                   "resources": {"peak_mem_pct": 99.9}}, open(os.path.join(run, "autopsy.json"), "w"))
        json.dump({"question": "Where should campaigns run?", "started": "2026-10-10T05:53:05.123456"},
                  open(os.path.join(run, "metadata.json"), "w"))
        open(os.path.join(run, "report.md"), "w").write("r")
        L1 = dict(good, test={"how": ["gate"], "detect": {"gate_kinds": ["k_bad"]}})
        L2 = dict(good, id="L002", test={"how": ["auto"], "detect": {"peak_mem_pct_over": 95, "file_absent": ["report.md"]}})
        L3 = dict(good, id="L003", applies_when=["tuberculosis"], test={"how": ["expert"], "review": "q?"})
        L4 = dict(good, id="L004", test={"how": ["expert"], "review": "is the headline plausible?"})
        L5 = dict(good, id="L005", test={"how": ["auto"], "detect": {"autopsy_why": ["SIGKILL"]}})
        c = write_check(run, [L1, L2, L3, L4, L5])
        res = {r["id"]: r["result"] for r in c["lessons"]}
        if res != {"L001": "caught", "L002": "recurred", "L003": "not_applicable", "L004": "review", "L005": "absent"}:
            fails.append(f"LS4 signals: caught / recurred (worst signal wins) / not applicable / review / absent: {res}")
        if c["to_review"] != ["L004"] or "L004" not in open(os.path.join(run, "lessons_check.md")).read():
            fails.append(f"LS5 the expert checklist lists only applicable review lessons: {c['to_review']}")
        hist.append({"round": 3, "findings": [{"kind": "k_bad"}]})
        open(os.path.join(run, "validator_history.jsonl"), "w").write("\n".join(json.dumps(h) for h in hist))
        if check(run, [L1])["lessons"][0]["result"] != "recurred":
            fails.append("LS6 a kind still raised in the last gate round is recurred (shipped), not caught")
        empty = os.path.join(d, "empty")
        os.makedirs(empty)
        r_empty = {r["id"]: r["result"] for r in check(empty, [L1, L2])["lessons"]}
        if r_empty != {"L001": "unknown", "L002": "recurred"}:
            fails.append(f"LS7 missing sources are unknown, never absent; a missing report is recurred: {r_empty}")

        obs_path = os.path.join(d, "obs.jsonl")
        auto = from_check(c)
        if {o["lesson"] for o in auto} != {"L001", "L002", "L005"} or any(o["by"] != "auto" for o in auto):
            fails.append(f"LS8 only definite automatic results are recorded: {auto}")
        same_day = dict(L1, change=dict(L1["change"], date="2026-10-10T20:23"))
        if summarise([same_day], auto)[0]["before"]["runs"] != 1 or auto[0]["date"] != "2026-10-10T05:53:05":
            fails.append(f"LS8b observations are dated by the run's start: a run before a same-day change counts before it: {auto[:1]}")
        if not record([{"lesson": "L999", "run": "r", "date": "2026-10-11", "result": "absent", "by": "auto"}],
                      [L1], obs_path):
            fails.append("LS9 recording against an unknown lesson is refused")
        rows = [{"lesson": "L001", "run": "a", "date": "2026-10-05", "result": "recurred", "by": "expert"},
                {"lesson": "L001", "run": "b", "date": "2026-10-10", "result": "caught", "by": "auto"},
                {"lesson": "L001", "run": "c", "date": "2026-10-13", "result": "absent", "by": "auto"},
                {"lesson": "L002", "run": "b", "date": "2026-10-12", "result": "recurred", "by": "auto"},
                {"lesson": "L004", "run": "a", "date": "2026-10-05", "result": "recurred", "by": "expert"}]
        if record(rows, [L1, L2, L4], obs_path):
            fails.append("LS9b valid observations are recorded")
        open(obs_path, "a").write("not json\n")
        obs, oerrs = load_observations(obs_path)
        if len(obs) != 5 or len(oerrs) != 1:
            fails.append(f"LS10 a corrupt observation line is reported, the rest kept: {len(obs)} {oerrs}")
        L4o = dict(L4, change={"status": "open", "where": ["x"], "refs": []})
        s = {r["id"]: r for r in summarise([L1, L2, L4o], obs)}
        if (s["L001"]["before"]["recurred"], s["L001"]["after"]["runs"], s["L001"]["after"]["caught"]) != (1, 2, 1) \
                or "candidate for verified" not in s["L001"]["note"]:
            fails.append(f"LS11 split at the change date (a run on that day counts as after); caught-but-not-shipped counts toward verified: {s['L001']}")
        L1r = dict(L1, test=dict(L1["test"], review="q?"))
        if "needs an expert check" not in summarise([L1r], obs)[0]["note"]:
            fails.append("LS11b a lesson with a review question is not verified by automatic absence alone")
        if "reopen" not in s["L002"]["note"] or "still recurring" not in s["L004"]["note"]:
            fails.append(f"LS12 recurrence after a change says reopen; open lessons say still recurring: "
                         f"{s['L002']['note']!r} {s['L004']['note']!r}")
        rf = register_fields(run, ledger=p)            # p now holds an unparseable ledger
        if "error" not in rf or os.path.exists(os.path.join(empty, "lessons_check.json")):
            fails.append(f"LS15 finalize gets an error, not an exception, from a bad ledger: {rf}")
        yaml.safe_dump({"version": 1, "lessons": [L1, L4]}, open(p, "w"))
        rf = register_fields(run, ledger=p)
        if rf != {"recurred": ["L001"], "caught": [], "unknown": [], "to_review": ["L004"]}:
            fails.append(f"LS16 finalize gets the register fields: {rf}")
        if "| L001 t |" not in summary_markdown(list(s.values())):
            fails.append("LS13 summary renders")
    obs, oerrs = load_observations()
    if oerrs:
        fails.append(f"LS14 the checked-in observations are well formed: {oerrs[:3]}")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="The lessons ledger")
    ap.add_argument("--self-test", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("validate")
    c = sub.add_parser("check")
    c.add_argument("run_dir")
    c.add_argument("--question")
    c.add_argument("--json", action="store_true")
    r = sub.add_parser("record")
    r.add_argument("--lesson")
    r.add_argument("--run")
    r.add_argument("--result", choices=sorted(RESULTS))
    r.add_argument("--by", choices=sorted(BY), default="expert")
    r.add_argument("--note", default="")
    r.add_argument("--date", default=date.today().isoformat(), help="the run's start (UTC), YYYY-MM-DD[THH:MM]")
    r.add_argument("--from-check")
    s = sub.add_parser("summary")
    s.add_argument("--md", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    lessons, errs = load_ledger()
    if errs:
        print("LEDGER INVALID:", *errs, sep="\n  - ", file=sys.stderr)
        return 1
    if a.cmd == "validate":
        obs, oerrs = load_observations()
        for e in oerrs:
            print(f"  - {e}", file=sys.stderr)
        print(f"OK: {len(lessons)} lessons, {len(obs)} observations" if not oerrs else "OBSERVATIONS INVALID")
        return 1 if oerrs else 0
    if a.cmd == "check":
        res = check(a.run_dir, lessons, a.question) if a.question else write_check(a.run_dir, lessons)
        print(json.dumps(res, indent=1) if a.json else check_markdown(res))
        return 0
    if a.cmd == "record":
        if a.from_check:
            entries = from_check(json.load(open(a.from_check)))
        elif a.lesson and a.run and a.result:
            entries = [{"lesson": a.lesson, "run": a.run, "date": a.date, "result": a.result, "by": a.by,
                        "note": a.note}]
        else:
            ap.error("record needs --from-check, or --lesson, --run and --result")
        errs = record(entries, lessons)
        print(*(errs or [f"recorded {len(entries)} observation(s)"]), sep="\n")
        return 1 if errs else 0
    if a.cmd == "summary":
        obs, oerrs = load_observations()
        if oerrs:
            print("OBSERVATIONS INVALID:", *oerrs, sep="\n  - ", file=sys.stderr)
            return 1
        rows = summarise(lessons, obs)
        print(summary_markdown(rows) if a.md else json.dumps(rows, indent=1))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
