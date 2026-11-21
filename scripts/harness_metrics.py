#!/usr/bin/env python3
"""Harness autonomy metrics: per run chain, did it complete, how many owner interventions, how long it waited.

The definitions are the harness benchmark protocol's (section 2): a chain is a first run directory plus every
relaunch made from it; "completes" is decided by the chain's last directory; the nine intervention types I1-I9;
owner-wait split into owner response and operator latency, censored at the cut-off. Read-only: it reads
metadata.json, run_state.json, the sign-off records, findings.jsonl and whether report.md / report_withheld.md
exist, through lib.safe_io (no link followed). It refuses any path under `_truth` or `evals_private`.

Every field says how it was derived: `record` (a harness record states it), `inferred` (reconstructed from
other records, for example a pause from a transition followed by a resume), or `unavailable`. A run directory
that cannot be read is a row with status `unreadable`, never a skipped row.

Times: history and journal times are UTC with an offset; `metadata.started` and `invocations[].at` are naive
local time, converted with `metadata.utc_offset_s`. With no offset a naive time is not computable, and so is
any wait that needs it.

    python3 scripts/harness_metrics.py <run_dir>... [--chain a,b ...] [--cutoff ISO] [--ledger L] [--json out.json]
    python3 scripts/harness_metrics.py --self-test

Importable (PR 4B run_harvest / feedback_store): `load_run`, `chains`, `summarise`, `rows`, `wave_summary`.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import safe_io  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REFUSED_PARTS = ("_truth", "evals_private")
CENSOR_H = 24.0
COMPLETES = ("completed", "completed_scope_declared")
NOT_ACCEPTED = ("completed_gate_not_accepted", "completed_scope_declined", "completed_no_report")
SIGNED_BY_OWNER = "spec signed by owner"
SCOPE_ANSWERED = "the owner answered the scope declarations"
PAUSE_STATES = {"SIGN": "awaiting_signoff", "OWNER_DECISION": "awaiting_scope_signoff",
                "SPEC": "awaiting_spec_amendment"}
REC, INF, NA = "record", "inferred", "unavailable"


def F(value, source, note=None) -> dict:
    """One derived field."""
    return {"value": value, "from": source, **({"note": note} if note else {})}


# ------------------------------------------------------------------ reading
def _read(run_dir: str, rel: str):
    """(object, error): (None, None) when the file is absent; an error string when it exists and cannot be read."""
    try:
        data, _ = safe_io.read_nofollow(run_dir, rel)
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"{rel}: {type(e).__name__}"
    try:
        if rel.endswith(".jsonl"):
            return [json.loads(x) for x in data.decode().splitlines() if x.strip()], None
        return json.loads(data.decode()), None
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"{rel}: {type(e).__name__}"


def _exists(run_dir: str, rel: str) -> bool:
    return os.path.lexists(os.path.join(run_dir, rel))


def load_run(run_dir: str) -> dict:
    """Everything the metric reads from one run directory. `errors` non-empty means the row is unreadable."""
    run = {"dir": run_dir, "name": os.path.basename(os.path.normpath(run_dir)), "errors": []}
    parts = os.path.abspath(run_dir).split(os.sep)
    if any(p in REFUSED_PARTS for p in parts):
        run["errors"].append("refused: the path is under " + "/".join(REFUSED_PARTS))
        return run
    if not os.path.isdir(run_dir):
        run["errors"].append("not a directory")
        return run
    for key, rel in (("meta", "metadata.json"), ("st", "run_state.json"), ("signoff", "decision_spec.signoff.json"),
                     ("scope", "scope_signoff.json"), ("scope_req", "scope_signoff_request.json"),
                     ("geometry", "data/geometry_signoff.json"), ("findings", "findings.jsonl")):
        obj, err = _read(run_dir, rel)
        if err:
            run["errors"].append(err)
        run[key] = obj
    for key in ("meta", "st"):
        if not isinstance(run.get(key), dict):
            run["errors"].append(f"{key}: missing or not a mapping")
    run["report"] = _exists(run_dir, "report.md")
    run["withheld"] = _exists(run_dir, "report_withheld.md")
    return run


def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x) -> list:
    return x if isinstance(x, list) else []


# ------------------------------------------------------------------ time
def utc(s, offset_s=None):
    """A UTC datetime: an aware time is converted; a naive one needs the run's utc_offset_s, else None."""
    if not isinstance(s, str) or not s:
        return None
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        return t.astimezone(timezone.utc)
    if not isinstance(offset_s, (int, float)) or isinstance(offset_s, bool):
        return None
    return (t - timedelta(seconds=offset_s)).replace(tzinfo=timezone.utc)


def _iso(t):
    return t.isoformat(timespec="seconds") if t else None


def _h(a, b):
    return round((b - a).total_seconds() / 3600.0, 4) if a and b else None


def _invocations(run) -> tuple[list, bool]:
    """(UTC times of every invocation, every resume's time computable). The launch itself ends no pause."""
    meta = _d(run.get("meta"))
    off = meta.get("utc_offset_s")
    ts = [utc(_d(i).get("at"), off) for i in _l(meta.get("invocations"))]
    return ts, all(ts[1:])


def started(run):
    meta = _d(run.get("meta"))
    return utc(meta.get("started"), meta.get("utc_offset_s"))


# ------------------------------------------------------------------ completion
def completion(run) -> dict:
    """The protocol's class for one (last) run directory: completes / not_accepted / not_completed."""
    if run["errors"]:
        return {"status": F("unreadable", REC, "; ".join(run["errors"])), "completes": F(False, REC),
                "class": "not_completed"}
    st, meta = run["st"], run["meta"]
    status = st.get("status") or (f"paused:{st['paused']}" if st.get("paused") else None) or meta.get("status")
    note = None
    if meta.get("status") and st.get("status") and meta["status"] != st["status"]:
        note = f"metadata.json says {meta['status']}"
    if not status:
        return {"status": F("no_status", REC, "no terminal status or pause recorded"), "completes": F(False, REC),
                "class": "not_completed"}
    ok = status in COMPLETES and run["report"] and not run["withheld"]
    cls = "completes" if ok else ("not_accepted" if status in NOT_ACCEPTED else "not_completed")
    why = None if ok or status not in COMPLETES else ("report withheld" if run["withheld"] else "no report.md")
    return {"status": F(status, REC, note), "completes": F(ok, REC, why), "class": cls}


# ------------------------------------------------------------------ pauses
def _sign_journal_done(st, t0, t1):
    """The latest completed lead sign_spec journal time in [t0, t1): the lead SIGN paused only after it."""
    best = None
    for k, j in _d(st.get("journal")).items():
        if "SIGN#" in k and k.endswith(":sign_spec"):
            t = utc(_d(j).get("completed"))
            if t and t0 and t >= t0 and (t1 is None or t < t1) and (best is None or t > best):
                best = t
    return best


def pauses(run) -> list[dict]:
    """Every pause of one run: explicit `{"paused": reason}` history entries (record), and pauses inferred from a
    transition into SIGN / OWNER_DECISION followed by a resume before the transition out. A pause still in force
    (run_state.paused) has resume None. Times are UTC datetimes or None (not computable)."""
    st = _d(run.get("st"))
    hist = [h for h in _l(st.get("history")) if isinstance(h, dict)]
    inv, _ = _invocations(run)
    resumes = sorted(t for t in inv[1:] if t)
    out = []
    explicit = [(utc(h.get("at")), h) for h in hist if h.get("paused")]
    for t, h in explicit:
        nxt = next((r for r in resumes if t and r > t), None)
        out.append({"reason": h["paused"], "state": h.get("state"), "at": t, "resume": nxt, "from": REC})
    for i, h in enumerate(hist):
        to = h.get("to")
        if to not in ("SIGN", "OWNER_DECISION"):
            continue
        t_in = utc(h.get("at"))
        t_out = next((utc(x.get("at")) for x in hist[i + 1:] if x.get("from") == to), None)
        if t_in is None or any(t and t >= t_in and (t_out is None or t < t_out) for t, _ in explicit):
            continue
        start = t_in
        for r in resumes:
            if r > t_in and (t_out is None or r < t_out):
                at = _sign_journal_done(st, start, r) if to == "SIGN" else None
                out.append({"reason": PAUSE_STATES[to], "state": to, "at": max(start, at) if at else start,
                            "resume": r, "from": INF})
                start = r
    if st.get("paused") and not any(p["resume"] is None for p in out):
        s = st.get("state")
        cands = [t for t in [utc(h.get("at")) for h in hist if h.get("to") == s] + resumes if t]
        t_in = max(cands) if cands else None
        if s == "SIGN" and t_in:
            j = _sign_journal_done(st, t_in, None)
            t_in = max(t_in, j) if j else t_in
        out.append({"reason": st["paused"], "state": s, "at": t_in, "resume": None, "from": INF})
    return sorted(out, key=lambda p: p["at"] or datetime.max.replace(tzinfo=timezone.utc))


def _answer(run, p, end):
    """When the owner answered pause p (UTC), from the sign-off or scope records; None when no record does."""
    t0 = p["at"]
    if t0 is None:
        return None

    def inside(t):
        return t and t >= t0 and (end is None or t <= end)
    if p["reason"] == "awaiting_signoff":
        so = _d(run.get("signoff"))
        t = utc(so.get("signed_at") or so.get("at"))
        return t if (so.get("signed_by") or so.get("by")) == "owner" and inside(t) else None
    if p["reason"] == "awaiting_scope_signoff":
        ts = [utc(_d(a).get("at")) for a in _d(_d(run.get("scope")).get("answers")).values()
              if _d(a).get("by") == "owner"]
        ts = [t for t in ts if inside(t)] or [t for t in [utc(_d(run.get("scope_req")).get("answered_at"))] if inside(t)]
        return max(ts) if ts else None
    return None


def owner_wait(run_list, cutoff) -> dict:
    """Owner-wait over a chain (ordered run dicts): Σ(resume − pause), split into owner response (answer − pause)
    and operator latency (resume − answer), each pause censored at `cutoff`."""
    items, computable = [], all(_invocations(r)[1] for r in run_list)
    for k, run in enumerate(run_list):
        child = run_list[k + 1] if k + 1 < len(run_list) else None
        for p in pauses(run):
            end = p["resume"] or (started(child) if child else None)
            ans = _answer(run, p, end)
            if p["at"] is None or (end is None and child is not None):
                computable = False
                items.append({**p, "wait_h": None})
                continue
            censored = end is None or end > cutoff
            e = min(end, cutoff) if end else cutoff
            wait = max(0.0, _h(p["at"], e))
            if ans and ans <= e:
                resp = max(0.0, _h(p["at"], ans))
            elif ans is None and not censored and p["reason"] != "awaiting_budget":
                resp = None                     # left, but no record says when the owner answered
            else:
                resp = wait                     # still waiting on the owner, or the resume is the answer (budget)
            items.append({"reason": p["reason"], "state": p["state"], "from": p["from"], "run": run["name"],
                          "pause_at": _iso(p["at"]), "answer_at": _iso(ans), "resume_at": _iso(end),
                          "censored": censored, "wait_h": wait, "owner_response_h": resp,
                          "operator_latency_h": None if resp is None else round(wait - resp, 4)})
    src = INF if any(i.get("from") == INF for i in items) else REC
    if not computable:
        return {"total_h": F(None, NA, "a naive time with no utc_offset_s, or a pause with no time"), "pauses": items}
    tot = round(sum(i["wait_h"] for i in items), 4)
    resp = [i["owner_response_h"] for i in items]
    return {"total_h": F(tot, src), "censored": any(i["censored"] for i in items),
            "owner_response_h": F(None if None in resp else round(sum(resp), 4), src if None not in resp else NA),
            "operator_latency_h": F(None if None in resp else round(tot - sum(resp), 4),
                                    src if None not in resp else NA), "pauses": items}


# ------------------------------------------------------------------ interventions
def interventions(run_list, ledger=None, linked=False) -> dict:
    """I1-I9 for a chain. `linked`: every relaunch link is a parent_run record (else the operator's --chain)."""
    hist = [h for r in run_list for h in _l(_d(r.get("st")).get("history")) if isinstance(h, dict)]
    ps = [p for r in run_list for p in pauses(r)]
    left = [p for k, r in enumerate(run_list) for p in pauses(r) if p["resume"] or k + 1 < len(run_list)]
    i1 = F(len(left), INF if any(p["from"] == INF for p in left) else REC)
    if any(not isinstance(_d(r.get("meta")).get("invocations"), list) for r in run_list):
        i5 = F(None, NA, "a run records no invocations")
    else:
        i5 = F(sum(len(r["meta"]["invocations"]) - 1 for r in run_list), REC)
    i6 = F(len(run_list) - 1, REC if linked or len(run_list) == 1 else INF,
           None if linked or len(run_list) == 1 else "chain from --chain")
    signs = sum(1 for h in hist if h.get("why") == SIGNED_BY_OWNER)
    if not signs:
        signs = sum(1 for r in run_list if (_d(r.get("signoff")).get("signed_by") or _d(r.get("signoff")).get("by"))
                    == "owner")
    i3 = sum(1 for r in run_list for a in _d(_d(r.get("geometry")).get("approvals")).values()
             if _d(a).get("approved_by") == "owner")
    items = sum(1 for r in run_list for a in _d(_d(r.get("scope")).get("answers")).values() if _d(a).get("by") == "owner")
    i4 = F(sum(1 for h in hist if h.get("why") == SCOPE_ANSWERED), REC, f"{items} item(s) answered")
    raises = sum(1 for h in hist if "budget_raised" in h)
    edits = [p for h in hist for p in _l(h.get("hand_edits"))]
    spec_changed = sum(1 for r in run_list for f in _l(r.get("findings"))
                       if _d(f).get("kind") == "spec_changed_after_signoff")
    hashed = all(_exists(r["dir"], "run_files.sha256.json") for r in run_list)
    i8 = F(len(edits) + spec_changed, REC if hashed else INF,
           None if hashed else "no run-files hash record: only spec changes after sign-off are seen")
    led = [_d(_d(ledger).get(r["name"])) for r in run_list]
    i9 = (F(sum(int(x.get("i9", 0)) for x in led), REC, "operator ledger") if ledger and any(led)
          else F(None, NA, "outside the run directory: the operator ledger"))
    head = [x["value"] for x in (i1, i5, i6, i8)]
    dec = (signs, i3, i4["value"], raises)
    return {"I1_pauses": i1, "I2_spec_signatures": F(signs, REC), "I3_geometry_approvals": F(i3, REC),
            "I4_scope_answers": i4, "I5_resumes": i5, "I6_relaunches": i6, "I7_budget_raises": F(raises, REC),
            "I8_hand_edits": i8, "I9_operator_actions": i9, "pending_pauses": len(ps) - len(left),
            "headline": F(None if None in head else sum(head), NA if None in head else
                          (INF if INF in (i1["from"], i6["from"], i8["from"]) else REC)),
            "owner_decisions": F(sum(dec), REC)}


# ------------------------------------------------------------------ chains and rows
def chains(dirs, explicit=None) -> list[list[str]]:
    """Group run dirs into chains: the operator's `explicit` lists first, then metadata.parent_run links."""
    explicit = [list(c) for c in explicit or []]
    used = {os.path.normpath(d) for c in explicit for d in c}
    rest = [d for d in dirs if os.path.normpath(d) not in used]
    by_name = {os.path.basename(os.path.normpath(d)): d for d in rest}
    parent = {}
    for d in rest:
        obj, _ = _read(d, "metadata.json") if os.path.isdir(d) else (None, None)
        p = _d(_d(obj).get("parent_run")).get("run_dir")
        if p in by_name and by_name[p] != d:
            parent[d] = by_name[p]
    kids = {}
    for c, p in parent.items():
        kids.setdefault(p, []).append(c)
    out = []
    for d in rest:
        if d in parent:
            continue
        c, cur = [d], d
        while len(kids.get(cur, [])) == 1 and kids[cur][0] not in c:
            cur = kids[cur][0]
            c.append(cur)
        out.append(c)
    return explicit + out


def question_key(question) -> str | None:
    """The eval question this run answers, matched on the public evals/<q>/question.txt; None for any other."""
    q = " ".join(str(question or "").split())
    for k in sorted(os.listdir(os.path.join(REPO, "evals"))) if os.path.isdir(os.path.join(REPO, "evals")) else []:
        try:
            t = " ".join(open(os.path.join(REPO, "evals", k, "question.txt")).read().split())
        except OSError:
            continue
        if q and t and (q == t or q.startswith(t) or t.startswith(q)):
            return k
    return None


def summarise(chain_dirs, cutoff=None, ledger=None, linked=None) -> dict:
    """One row for one chain (ordered run dirs, first launch first)."""
    runs = [load_run(d) for d in chain_dirs]
    last, first = runs[-1], runs[0]
    if linked is None:
        linked = all(_d(_d(b.get("meta")).get("parent_run")).get("run_dir") == a["name"]
                     for a, b in zip(runs, runs[1:]))
    row = {"chain": [r["name"] for r in runs], "label": last["name"], **completion(last)}
    if any(r["errors"] for r in runs):
        row["status"] = F("unreadable", REC, "; ".join(f"{r['name']}: {e}" for r in runs for e in r["errors"]))
        row["completes"], row["class"] = F(False, REC), "not_completed"
        return row
    t0 = started(first)
    now = datetime.now(timezone.utc)
    cut = utc(cutoff) if cutoff else (t0 + timedelta(hours=CENSOR_H) if t0 else None)
    cut = min(cut, now) if cut else None
    meta, st = last["meta"], last["st"]
    commits = {c for r in runs for c in [r["meta"].get("commit")] +
               [_d(i).get("commit") for i in _l(r["meta"].get("invocations"))] if c}
    row.update({"question": F(question_key(first["meta"].get("question")), INF),
                "commit": F(meta.get("commit"), REC if meta.get("commit") else NA,
                            "commits differ across invocations" if len(commits) > 1 else None),
                "stop_state": F(st.get("state"), REC), "rounds": F(st.get("round"), REC),
                "model_usd": F(round(sum(float(r["meta"].get("model_spend_usd", r["st"].get("spent_usd")) or 0)
                                         for r in runs), 4), REC),
                "models": F(meta.get("models"), REC if meta.get("models") else NA),
                "sdk_version": F(meta.get("sdk_version"), REC if meta.get("sdk_version") else NA),
                "interventions": interventions(runs, ledger, linked)})
    if cut is None:
        row["owner_wait"] = {"total_h": F(None, NA, "no computable launch time or cut-off"), "pauses": []}
    else:
        row["owner_wait"] = owner_wait(runs, cut)
    row["cutoff"] = _iso(cut)
    end = utc(meta.get("finished")) or max([utc(h.get("at")) for h in _l(st.get("history")) if isinstance(h, dict)
                                            and utc(h.get("at"))] or [None], key=lambda t: t or now)
    if st.get("paused"):
        end = cut                     # a pause in force at the cut-off: the run's time runs to the cut-off
    w = row["owner_wait"]["total_h"]["value"]
    wall = _h(t0, min(end, cut) if end and cut else end)
    row["autonomous_wall_h"] = (F(round(wall - w, 4), row["owner_wait"]["total_h"]["from"]) if wall is not None
                                and w is not None else F(None, NA, "start, end or wait not computable"))
    return row


def rows(dirs, explicit=None, cutoff=None, ledger=None) -> list[dict]:
    return [summarise(c, cutoff, ledger, linked=False if c in (explicit or []) else None)
            for c in chains(dirs, explicit)]


def wave_summary(rs) -> dict:
    n = len(rs)
    heads = [r.get("interventions", {}).get("headline", {}).get("value") for r in rs]
    waits = [r.get("owner_wait", {}).get("total_h", {}).get("value") for r in rs]
    return {"chains": n, "completed": sum(1 for r in rs if r["completes"]["value"]),
            "scope_declared": sum(1 for r in rs if r["completes"]["value"]
                                  and r["status"]["value"] == "completed_scope_declared"),
            "median_interventions": statistics.median([h for h in heads if h is not None]) if any(
                h is not None for h in heads) else None,
            "interventions_unavailable": sum(1 for h in heads if h is None),
            "total_owner_wait_h": round(sum(w for w in waits if w is not None), 4),
            "owner_wait_unavailable": sum(1 for w in waits if w is None)}


def _cell(f, fmt=str):
    if not isinstance(f, dict):
        return str(f)
    if f.get("from") == NA:
        return "n/a"
    v = f.get("value")
    return (fmt(v) if v is not None else "-") + ("~" if f.get("from") == INF else "")


def table(rs) -> str:
    cols = ["label", "question", "status", "done", "stop", "interv", "decisions", "wait_h", "auto_h", "model_$",
            "rounds"]
    lines = []
    for r in rs:
        iv, ow = r.get("interventions", {}), r.get("owner_wait", {})
        wait = _cell(ow.get("total_h", F(None, NA))) + ("*" if ow.get("censored") else "")
        lines.append([r["label"][:40], _cell(r.get("question", F(None, NA))), _cell(r["status"]),
                      "Y" if r["completes"]["value"] else "N", _cell(r.get("stop_state", F(None, NA))),
                      _cell(iv.get("headline", F(None, NA))), _cell(iv.get("owner_decisions", F(None, NA))), wait,
                      _cell(r.get("autonomous_wall_h", F(None, NA))), _cell(r.get("model_usd", F(None, NA))),
                      _cell(r.get("rounds", F(None, NA)))])
    w = [max(len(c), *(len(x[i]) for x in lines)) if lines else len(c) for i, c in enumerate(cols)]
    fmt = "  ".join("{:<%d}" % n for n in w)
    s = wave_summary(rs)
    return "\n".join([fmt.format(*cols)] + [fmt.format(*x) for x in lines] + [
        "", "~ inferred, n/a unavailable, * censored at the cut-off",
        f"completed {s['completed']}/{s['chains']} (scope declared {s['scope_declared']}); median interventions "
        f"{s['median_interventions']}; total owner-wait {s['total_owner_wait_h']} h"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dirs", nargs="*")
    ap.add_argument("--chain", action="append", default=[], help="comma-separated run dirs of one chain, in order")
    ap.add_argument("--cutoff", help="ISO time with offset (default: first launch + 24 h)")
    ap.add_argument("--ledger", help="JSON operator ledger: {run dir name: {i9: n}}")
    ap.add_argument("--json", help="write the rows and the wave summary here")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    explicit = [[x for x in c.split(",") if x] for c in a.chain]
    dirs = list(a.run_dirs) + [d for c in explicit for d in c]
    if not dirs:
        ap.error("no run directory")
    ledger = json.load(open(a.ledger)) if a.ledger else None
    rs = rows(dirs, explicit, a.cutoff, ledger)
    print(table(rs))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"rows": rs, "summary": wave_summary(rs)}, f, indent=1, default=str)
    return 0


# ------------------------------------------------------------------ self-test (synthetic run dirs only)
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    T0 = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)

    def at(h):
        return (T0 + timedelta(hours=h)).isoformat(timespec="seconds")

    def local(h, off=3600):          # naive local time, as main.py writes it
        return (T0 + timedelta(hours=h, seconds=off or 0)).replace(tzinfo=None).isoformat(timespec="seconds")

    def mk(root, name, st=None, meta=None, files=None, off=3600, inv=(0,)):
        d = os.path.join(root, name)
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        m = {"started": local(0, off), "commit": "abc1234", "status": (st or {}).get("status"),
             "invocations": [{"at": local(h, off), "commit": "abc1234"} for h in inv]}
        if off is not None:
            m["utc_offset_s"] = off
        m.update(meta or {})
        files = dict(files or {})
        files.setdefault("metadata.json", json.dumps(m))
        if st is not None:
            files.setdefault("run_state.json", json.dumps({"state": "FINAL", "round": 1, "history": [], **st}))
        for rel, txt in files.items():
            open(os.path.join(d, rel), "w").write(txt)
        return d

    with tempfile.TemporaryDirectory() as root:
        fin = {"history": [{"at": at(1), "from": "GATE", "to": "FINAL", "why": "accepted"}]}
        d = mk(root, "r_ok", {**fin, "status": "completed"}, {"finished": at(1)}, {"report.md": "x"})
        r = summarise([d])
        ok(r["completes"] == F(True, REC) and r["interventions"]["headline"]["value"] == 0
           and r["owner_wait"]["total_h"]["value"] == 0 and r["autonomous_wall_h"]["value"] == 1.0,
           f"HM1 a completed run with report.md completes, 0 interventions, 1 h autonomous ({r['autonomous_wall_h']})")
        d = mk(root, "r_wh", {**fin, "status": "completed"}, None, {"report_withheld.md": "x"})
        ok(summarise([d])["completes"]["value"] is False, "HM2 a withheld report does not complete")
        d = mk(root, "r_sd", {**fin, "status": "completed_scope_declared"}, None, {"report.md": "x"})
        r = summarise([d])
        ok(r["completes"]["value"] and wave_summary([r])["scope_declared"] == 1, "HM3 scope declared completes, apart")
        d = mk(root, "r_none", {"state": "ANALYZE"})
        ok(summarise([d])["status"]["value"] == "no_status" and not summarise([d])["completes"]["value"],
           "HM4a a run that stopped with no status recorded is not completed (fail closed)")
        d = mk(root, "r_bad", None, None, {"run_state.json": "{not json"})
        r = rows([d, os.path.join(root, "missing")])
        ok(len(r) == 2 and all(x["status"]["value"] == "unreadable" and not x["completes"]["value"] for x in r),
           "HM4 an unparseable run_state.json and a missing dir are unreadable rows, not skipped")
        os.makedirs(os.path.join(root, "_truth"))
        d = mk(os.path.join(root, "_truth"), "r_t", {**fin, "status": "completed"}, None, {"report.md": "x"})
        ok("refused" in summarise([d])["status"].get("note", ""), "HM5 a path under _truth is refused, unread")
        # HM6 explicit awaiting_budget pause, raised and resumed 2 h later
        h = [{"at": at(1), "paused": "awaiting_budget", "state": "CRITIQUE"},
             {"at": at(3), "budget_raised": {"from": 5, "to": 9}}, {"at": at(4), "from": "GATE", "to": "FINAL"}]
        d = mk(root, "r_bud", {"history": h, "status": "completed"}, None, {"report.md": "x"}, inv=(0, 3))
        r = summarise([d])
        iv, ow = r["interventions"], r["owner_wait"]
        ok(iv["I1_pauses"] == F(1, REC) and iv["I5_resumes"]["value"] == 1 and iv["I7_budget_raises"]["value"] == 1
           and iv["headline"]["value"] == 2 and ow["total_h"] == F(2.0, REC) and ow["owner_response_h"]["value"] == 2.0,
           f"HM6 an explicit budget pause: I1 record, I5, I7, wait 2 h all owner response ({iv['headline']}, {ow['total_h']})")
        # HM7 inferred owner SIGN pause; owner signs at +1 h; resume at +3 h (naive local, offset 1 h)
        h = [{"at": at(0.5), "from": "SPEC", "to": "SIGN"}, {"at": at(3.2), "from": "SIGN", "to": "EVIDENCE",
                                                               "why": SIGNED_BY_OWNER}]
        so = json.dumps({"signed_by": "owner", "signed_at": at(1.5)})
        d = mk(root, "r_sign", {"history": h, "status": "completed"}, None,
               {"report.md": "x", "decision_spec.signoff.json": so}, inv=(0, 3))
        r = summarise([d])
        iv, ow = r["interventions"], r["owner_wait"]
        ok(iv["I1_pauses"] == F(1, INF) and iv["I2_spec_signatures"]["value"] == 1 and iv["headline"]["from"] == INF
           and ow["total_h"]["value"] == 2.5 and ow["owner_response_h"]["value"] == 1.0
           and ow["operator_latency_h"]["value"] == 1.5,
           f"HM7 a SIGN pause is inferred; naive local times are converted (wait {ow['total_h']}, "
           f"response {ow['owner_response_h']}, latency {ow['operator_latency_h']})")
        # HM8 the time-zone trap: no utc_offset_s, naive invocation times
        d = mk(root, "r_tz", {"history": h, "status": "completed"}, None, {"report.md": "x"}, off=None, inv=(0, 3))
        r = summarise([d], cutoff=at(30))
        ok(r["owner_wait"]["total_h"]["from"] == NA and r["autonomous_wall_h"]["from"] == NA,
           f"HM8 naive times with no offset: wait not computable ({r['owner_wait']['total_h']})")
        # HM9 a pending lead SIGN pause: starts at the sign_spec journal time; censored at launch + 24 h
        st = {"state": "SIGN", "paused": "awaiting_signoff", "status": None,
              "history": [{"at": at(1), "from": "SPEC", "to": "SIGN"}],
              "journal": {"SIGN#1:sign_spec": {"status": "completed", "completed": at(2)}}}
        d = mk(root, "r_pend", st)
        r = summarise([d])
        iv, ow = r["interventions"], r["owner_wait"]
        ok(r["status"]["value"] == "paused:awaiting_signoff" and not r["completes"]["value"]
           and iv["I1_pauses"]["value"] == 0 and iv["pending_pauses"] == 1 and ow["censored"]
           and ow["total_h"]["value"] == 22.0 and ow["pauses"][0]["pause_at"] == at(2)
           and r["autonomous_wall_h"]["value"] == 2.0,
           f"HM9 a pending pause is censored at 24 h, not an intervention ({ow['total_h']}, {iv['I1_pauses']}, "
           f"autonomous {r['autonomous_wall_h']})")
        r = summarise([d], cutoff=at(5))
        ok(r["owner_wait"]["total_h"]["value"] == 3.0, "HM10 an explicit cut-off censors there")
        # HM11 a relaunch chain through parent_run, and the same chain from --chain
        st = {"state": "SPEC", "paused": "awaiting_spec_amendment", "status": None,
              "history": [{"at": at(1), "from": "GATE", "to": "SPEC"}]}
        p = mk(root, "r_par", st)
        c = mk(root, "r_child", {**fin, "status": "completed"},
               {"parent_run": {"run_dir": "r_par"}, "started": local(4)}, {"report.md": "x"})
        r = rows([p, c])
        ok(len(r) == 1 and r[0]["chain"] == ["r_par", "r_child"] and r[0]["interventions"]["I6_relaunches"] == F(1, REC)
           and r[0]["interventions"]["I1_pauses"]["value"] == 1 and r[0]["owner_wait"]["total_h"]["value"] == 3.0
           and r[0]["completes"]["value"], f"HM11 parent_run links a relaunch: I6 record, the pause ends at the "
           f"child's start ({r[0]['interventions']['I6_relaunches']}, {r[0]['owner_wait']['total_h']})")
        c2 = mk(root, "r_child2", {**fin, "status": "completed"}, {"started": local(4)}, {"report.md": "x"})
        r = rows([], explicit=[[p, c2]])
        ok(r[0]["interventions"]["I6_relaunches"]["from"] == INF, "HM12 an operator --chain makes I6 inferred")
        # HM13 an OWNER_DECISION pause answered through the scope records
        h = [{"at": at(1), "from": "GATE", "to": "OWNER_DECISION"},
             {"at": at(6), "from": "OWNER_DECISION", "to": "GATE", "why": SCOPE_ANSWERED}]
        sc = json.dumps({"answers": {"f1": {"by": "owner", "at": at(2)}, "f2": {"by": "owner", "at": at(4)}}})
        d = mk(root, "r_scope", {"history": h, "status": "completed"}, None,
               {"report.md": "x", "scope_signoff.json": sc}, inv=(0, 5))
        r = summarise([d])
        iv, ow = r["interventions"], r["owner_wait"]
        ok(iv["I4_scope_answers"]["value"] == 1 and "2 item" in iv["I4_scope_answers"]["note"]
           and ow["owner_response_h"]["value"] == 3.0 and ow["operator_latency_h"]["value"] == 1.0,
           f"HM13 a scope answer: I4 1 (2 items), response to the last answer ({ow['owner_response_h']})")
        # HM14 an explicit pause entry suppresses the inference (no double count)
        h = [{"at": at(1), "from": "SPEC", "to": "SIGN"}, {"at": at(1), "paused": "awaiting_signoff", "state": "SIGN"},
             {"at": at(3), "from": "SIGN", "to": "EVIDENCE", "why": SIGNED_BY_OWNER}]
        d = mk(root, "r_both", {"history": h, "status": "completed"}, None, {"report.md": "x"}, inv=(0, 2))
        r = summarise([d])
        ok(r["interventions"]["I1_pauses"] == F(1, REC) and len(r["owner_wait"]["pauses"]) == 1,
           f"HM14 an explicit pause entry is the record; no inferred copy ({r['interventions']['I1_pauses']})")
        # HM15 geometry approvals, a ledger, the JSON and the table
        g = json.dumps({"approvals": {"G1": {"approved_by": "owner"}, "G2": {"approved_by": "lead"}}})
        d = mk(root, "r_geo", {**fin, "status": "run_failed"}, None, {"data/geometry_signoff.json": g})
        r = rows([d], ledger={"r_geo": {"i9": 2}})
        iv = r[0]["interventions"]
        out = os.path.join(root, "o.json")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            main([d, "--json", out])
        js = json.load(open(out))
        ok(iv["I3_geometry_approvals"]["value"] == 1 and iv["I9_operator_actions"] == F(2, REC, "operator ledger")
           and r[0]["class"] == "not_completed" and js["summary"]["chains"] == 1
           and all("from" in v for v in js["rows"][0]["interventions"].values() if isinstance(v, dict))
           and "r_geo" in table(r),
           "HM15 geometry approvals by the owner, the operator ledger, JSON fields carry their derivation")
        d = mk(root, "r_raw", {**fin, "status": "completed"}, {"models": {"deep": "m"}}, {"report.md": "x"})
        r = summarise([d])
        ok(r["models"] == F({"deep": "m"}, REC) and r["sdk_version"]["from"] == NA,
           "HM16 model ids are read from metadata when recorded, else unavailable")
        ok(utc(local(0)[:-3] + "xx") is None and utc("2020-01-01T01:00:00", 3600) == T0
           and utc("2020-01-01T00:00:00+00:00") == T0, "HM17 utc(): naive local with offset, aware, garbage")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
