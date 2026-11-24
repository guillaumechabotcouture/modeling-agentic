#!/usr/bin/env python3
"""Harness autonomy metrics: per run chain, did it complete, how many owner interventions, how long it waited.

The definitions are the harness benchmark protocol's (section 2): a chain is a first run directory plus every
relaunch made from it; "completes" is decided by the chain's last directory; the nine intervention types I1-I9;
owner-wait split into owner response and operator latency, censored at the cut-off. Everything is counted on the
chain as it stood at the cut-off: by default the strict arm's (24 h after the first launch, or the first time every
chain is FINAL or paused); pass --cutoff for an assisted arm (a harvest at run exit passes "now"). Read-only: it reads
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

Importable (PR 4B run_harvest / feedback_store): `load_run`, `as_of`, `chains`, `summarise`, `rows`, `wave_summary`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
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
PAUSE_STATES = {"SIGN": "awaiting_signoff", "OWNER_DECISION": "awaiting_scope_signoff", "SPEC": "awaiting_spec_amendment"}
REC, INF, NA = "record", "inferred", "unavailable"


def F(value, source, note=None) -> dict:      # one derived field
    return {"value": value, "from": source, **({"note": note} if note else {})}


# ------------------------------------------------------------------ reading
def _read(run_dir: str, rel: str):
    """(object, error): (None, None) when the file is absent; an error string when it exists and cannot be read."""
    try:
        data, _ = safe_io.read_nofollow(run_dir, rel)
    except FileNotFoundError:
        return None, None
    except Exception as e:  # noqa: BLE001 -- a link refused (ValueError) or any other failure: unreadable
        return None, f"{rel}: {type(e).__name__}"
    try:
        if rel.endswith(".jsonl"):
            return [json.loads(x) for x in data.decode().splitlines() if x.strip()], None
        return json.loads(data.decode()), None
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"{rel}: {type(e).__name__}"


def _sha(run_dir, rel):
    try:
        return hashlib.sha256(safe_io.read_nofollow(run_dir, rel)[0]).hexdigest()
    except Exception:  # noqa: BLE001
        return None


def _regular(run_dir: str, rel: str) -> bool:
    """A regular file (not a link, a directory or a dangling name): what a delivered report must be."""
    try:
        return stat.S_ISREG(os.lstat(os.path.join(run_dir, rel)).st_mode)
    except OSError:
        return False


def _edited_inputs(run_dir: str, prov) -> list[str]:
    """Staged inputs whose bytes no longer match data/inputs_provenance.json (a hand edit, or a removal)."""
    return [dest for dest, e in sorted(_d(_d(prov).get("inputs")).items())
            if _sha(run_dir, dest) != _d(e).get("sha256")]


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
                     ("geometry", "data/geometry_signoff.json"), ("findings", "findings.jsonl"),
                     ("prov", "data/inputs_provenance.json")):
        obj, err = _read(run_dir, rel)
        if err:
            run["errors"].append(err)
        run[key] = obj
    for key in ("meta", "st"):
        if not isinstance(run.get(key), dict):
            run["errors"].append(f"{key}: missing or not a mapping")
    run["report"] = _regular(run_dir, "report.md")
    run["withheld"] = os.path.lexists(os.path.join(run_dir, "report_withheld.md"))
    run["edited_inputs"] = _edited_inputs(run_dir, run.get("prov"))
    return run


_d = lambda x: x if isinstance(x, dict) else {}       # noqa: E731
_l = lambda x: x if isinstance(x, list) else []       # noqa: E731
_iso = lambda t: t.isoformat(timespec="seconds") if t else None                   # noqa: E731
started = lambda run: utc(_d(run.get("meta")).get("started"), _d(run.get("meta")).get("utc_offset_s"))  # noqa: E731
_h = lambda a, b: round((b - a).total_seconds() / 3600.0, 4) if a and b else None  # noqa: E731


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


def _invocations(run) -> tuple[list, bool]:
    """(UTC times of every invocation, every resume's time computable). The launch itself ends no pause."""
    meta = _d(run.get("meta"))
    off = meta.get("utc_offset_s")
    ts = [utc(_d(i).get("at"), off) for i in _l(meta.get("invocations"))]
    return ts, all(ts[1:])


# ------------------------------------------------------------------ completion
def completion(run) -> dict:
    """The protocol's class for one (last) run directory, as of the cut-off (as_of): completes / not_accepted /
    not_completed."""
    def no(status, note):
        return {"status": F(status, REC, note), "completes": F(False, REC), "class": "not_completed"}
    if run["errors"]:
        return no("unreadable", "; ".join(run["errors"]))
    st, meta = run["st"], run["meta"]
    status = st.get("status") or (f"paused:{st['paused']}" if st.get("paused") else None)
    if not status:
        return no("no_status", "no terminal status or pause recorded by the cut-off")
    if st.get("status") and meta.get("status") != st["status"]:
        return no("status_mismatch", f"run_state.json {st['status']}, metadata.json {meta.get('status')}")
    # Every withhold path moves report.md away (stage_machine._quarantine_report), so a regular report.md is the
    # delivered one; a report_withheld.md beside it is a repaired draft's leftover (baseline hb2 §5 fix 8).
    ok = bool(status in COMPLETES and run["report"])
    cls = "completes" if ok else ("not_accepted" if status in NOT_ACCEPTED else "not_completed")
    why = ("a stale report_withheld.md remains beside the delivered report.md" if ok and run["withheld"]
           else None if ok or status not in COMPLETES else "report withheld" if run["withheld"]
           else "no regular report.md")
    return {"status": F(status, REC), "completes": F(ok, REC, why), "class": cls}


# ------------------------------------------------------------------ pauses
def _sign_journal_done(st, t0, t1):
    """The latest completed lead sign_spec journal time in [t0, t1]: the lead SIGN paused only after it."""
    ts = [utc(_d(j).get("completed")) for k, j in _d(st.get("journal")).items()
          if "SIGN#" in k and k.endswith(":sign_spec")]
    ts = [t for t in ts if t and t0 and t >= t0 and (t1 is None or t <= t1)]
    return max(ts) if ts else None


def pauses(run) -> list[dict]:
    """Every pause of one run. Explicit `{"paused": reason}` history entries are the record; each is ended by the
    first later resume (an invocation after the first) not already ending another. Without one, a pause is inferred
    from a transition into SIGN or OWNER_DECISION (or into SPEC after the spec was signed: the amendment pause)
    followed by a resume at or before the transition out; each further resume in that visit is one more pause. A
    pause still in force (run_state.paused) has resume None. Times are UTC or None."""
    st = _d(run.get("st"))
    hist = [h for h in _l(st.get("history")) if isinstance(h, dict)]
    resumes = sorted(t for t in _invocations(run)[0][1:] if t)
    out, used = [], set()
    for h in sorted((h for h in hist if h.get("paused")), key=lambda h: utc(h.get("at")) or datetime.max.replace(
            tzinfo=timezone.utc)):
        t = utc(h.get("at"))
        nxt = next((r for r in resumes if t and r > t and r not in used), None)   # not the resume it began in
        used.add(nxt)
        out.append({"reason": h["paused"], "state": h.get("state"), "at": t, "resume": nxt, "from": REC})
    signed = next((utc(h.get("at")) for h in hist if h.get("from") == "SIGN" and h.get("to") == "EVIDENCE"), None)
    for i, h in enumerate(hist):
        to, t_in = h.get("to"), utc(h.get("at"))
        if to not in PAUSE_STATES or t_in is None or (to == "SPEC" and not (signed and t_in >= signed)):
            continue
        t_out = next((utc(x.get("at")) for x in hist[i + 1:] if x.get("from") == to), None)
        rec = [p["at"] for p in out if p["from"] == REC and p["reason"] == PAUSE_STATES[to] and p["at"]
               and p["at"] >= t_in and (t_out is None or p["at"] <= t_out)]
        lim = min(rec) if rec else None                    # from here on the visit's pauses are recorded
        start = t_in
        for r in resumes:
            if r in used:
                start = max(start, r)                      # the end of a recorded pause: the next one starts after
            elif r > t_in and (t_out is None or r <= t_out) and (lim is None or r <= lim):   # whole seconds
                j = _sign_journal_done(st, start, r) if to == "SIGN" else None
                out.append({"reason": PAUSE_STATES[to], "state": to, "at": max(start, j) if j else start,
                            "resume": r, "from": INF})
                used.add(r)
                start = r
    if st.get("paused") and not any(p["resume"] is None for p in out):
        s = st.get("state")
        cands = [t for t in [utc(h.get("at")) for h in hist if h.get("to") == s] + resumes if t]
        t_in = max(cands) if cands else None
        j = _sign_journal_done(st, t_in, None) if s == "SIGN" and t_in else None
        out.append({"reason": st["paused"], "state": s, "at": max(t_in, j) if j else t_in, "resume": None,
                    "from": INF})
    return sorted(out, key=lambda p: p["at"] or datetime.max.replace(tzinfo=timezone.utc))


def _answer(run, p, end):
    """When the owner answered pause p (UTC): the owner's spec sign-off or geometry approval for a SIGN pause, the
    owner's scope answers (else the request's answered_at) for a scope pause; None when no record says."""
    t0 = p["at"]

    def inside(t):
        return t and t0 and t >= t0 and (end is None or t <= end)
    ts = []
    if p["reason"] == "awaiting_signoff":
        so = _d(run.get("signoff"))
        if (so.get("signed_by") or so.get("by")) == "owner":
            ts.append(utc(so.get("signed_at") or so.get("at")))
        ts += [utc(_d(a).get("at")) for a in _d(_d(run.get("geometry")).get("approvals")).values()
               if _d(a).get("approved_by") == "owner"]
    elif p["reason"] == "awaiting_scope_signoff":
        ts = [utc(_d(a).get("at")) for a in _d(_d(run.get("scope")).get("answers")).values()
              if _d(a).get("by") == "owner"]
        if not any(inside(t) for t in ts):
            ts = [utc(_d(run.get("scope_req")).get("answered_at"))]
    ts = [t for t in ts if inside(t)]
    return max(ts) if ts else None


def _ends(run_list):
    """Each run's pauses with the time each one ended: its resume, or the next run's start for a relaunch."""
    out = []
    for k, run in enumerate(run_list):
        child = run_list[k + 1] if k + 1 < len(run_list) else None
        for p in pauses(run):
            relaunch = child is not None and p["resume"] is None
            out.append((run, p, started(child) if relaunch else p["resume"], relaunch))
    return out


def _finished(run):
    """When the run reached FINAL: metadata.finished, else its history's transition into FINAL."""
    hist = _l(_d(run.get("st")).get("history"))
    return utc(_d(run.get("meta")).get("finished")) or next(
        (utc(h.get("at")) for h in reversed(hist) if _d(h).get("to") == "FINAL"), None)


def as_of(run_list, cut):
    """The chain as it stood at `cut`: runs launched later, and history entries, invocations and owner records
    dated later, are dropped; a run not FINAL by then has no status; a pause in force then is run_state.paused.
    Every count and wait is computed on this view, so nothing after the cut-off counts."""
    if cut is None:
        return run_list
    out = []
    for k, r in enumerate(run_list):
        if k and started(r) and started(r) > cut:
            break
        st, meta = dict(r["st"]), dict(r["meta"])
        off = meta.get("utc_offset_s")
        keep = (lambda t: t is None or t <= cut)
        st["history"] = [h for h in _l(st.get("history")) if keep(utc(_d(h).get("at")))]
        if isinstance(meta.get("invocations"), list):     # a run that records none keeps none: I5 unavailable
            meta["invocations"] = [i for i in meta["invocations"] if keep(utc(_d(i).get("at"), off))]
        fin, live = _finished(r), [x for x in _ends(run_list[k:k + 2]) if x[0] is r]
        last = next((h.get("to") for h in reversed(st["history"]) if h.get("to")), None)
        pend = next((p for p in pauses(r) if p["resume"] is None), None)
        if fin and fin > cut:                              # still working at the cut-off
            st.update(status=None, paused=None, state=last)
            meta["status"] = None
        elif not fin and pend and pend["at"] and pend["at"] > cut:
            st.update(paused=None, state=last)            # its last pause came after the cut-off
        for _, p, e, _ in live:
            if (p["at"] is None or p["at"] <= cut) and (e is None or e > cut):     # no time: kept (fail closed)
                st.update(paused=p["reason"], status=None, state=p["state"] or st.get("state"))
        so = _d(r.get("signoff"))
        so = so if keep(utc(so.get("signed_at") or so.get("at"))) else {}
        sc = {"answers": {f: a for f, a in _d(_d(r.get("scope")).get("answers")).items()
                          if keep(utc(_d(a).get("at")))}}
        geo = {"approvals": {g: a for g, a in _d(_d(r.get("geometry")).get("approvals")).items()
                             if keep(utc(_d(a).get("at")))}}
        req = _d(r.get("scope_req"))
        req = req if keep(utc(req.get("answered_at"))) else {}
        out.append(dict(r, st=st, meta=meta, signoff=so, scope=sc, geometry=geo, scope_req=req))
    return out


def owner_wait(run_list, cutoff) -> dict:
    """Owner-wait over a chain (ordered run dicts, as_of the cut-off): Σ(resume − pause), split into owner response
    (answer − pause) and operator latency (resume − answer), each pause censored at `cutoff`. For a budget pause the
    resume (with the higher cap) is the owner's answer: all of its wait is owner response."""
    items, computable = [], all(_invocations(r)[1] for r in run_list)
    for run, p, end, relaunch in _ends(run_list):
        if p["at"] is None or (relaunch and end is None):
            computable = False
            continue
        ans = _answer(run, p, end)
        censored = end is None or end > cutoff
        e = min(end, cutoff) if end else cutoff
        wait = max(0.0, _h(p["at"], e))
        if ans and ans <= e:
            resp = max(0.0, _h(p["at"], ans))
        elif ans is None and not censored and p["reason"] != "awaiting_budget":
            resp = None                                    # left, but no record says when the owner answered
        else:
            resp = wait                                    # still waiting on the owner, or a budget pause
        items.append({"reason": p["reason"], "state": p["state"], "from": p["from"], "run": run["name"],
                      "pause_at": _iso(p["at"]), "answer_at": _iso(ans), "resume_at": _iso(end),
                      "censored": censored, "wait_h": wait, "owner_response_h": resp,
                      "operator_latency_h": None if resp is None else round(wait - resp, 4)})
    src = INF if any(i["from"] == INF for i in items) else REC
    if not computable:
        return {"total_h": F(None, NA, "a naive time with no utc_offset_s, or a pause with no time"),
                "censored": False, "pauses": items}
    tot = round(sum(i["wait_h"] for i in items), 4)
    resp = [i["owner_response_h"] for i in items]
    split = None not in resp
    return {"total_h": F(tot, src), "censored": any(i["censored"] for i in items),
            "owner_response_h": F(round(sum(resp), 4) if split else None, src if split else NA),
            "operator_latency_h": F(round(tot - sum(resp), 4) if split else None, src if split else NA),
            "pauses": items}


# ------------------------------------------------------------------ interventions
def _owner_decisions(run) -> tuple[int, int, int, int]:
    """(I2 spec signatures, I3 geometry approvals, I4 scope answers, answered items) by the owner in one run."""
    hist = [h for h in _l(_d(run.get("st")).get("history")) if isinstance(h, dict)]
    so = _d(run.get("signoff"))
    i2 = sum(1 for h in hist if h.get("why") == SIGNED_BY_OWNER) or int((so.get("signed_by") or so.get("by")) == "owner")
    i3 = sum(1 for a in _d(_d(run.get("geometry")).get("approvals")).values() if _d(a).get("approved_by") == "owner")
    ans = [_d(a) for a in _d(_d(run.get("scope")).get("answers")).values() if _d(a).get("by") == "owner"]
    i4 = max(sum(1 for h in hist if h.get("why") == SCOPE_ANSWERED), int(bool(ans)))
    return i2, i3, i4, len(ans)


def interventions(run_list, ledger=None, linked=False) -> dict:
    """I1-I9 for a chain (as_of the cut-off). `linked`: every relaunch link is a parent_run record (else it came
    from the operator's --chain). A pause still in force is not an intervention (nobody acted on it): it is
    `pending_pauses`, and its wait is censored."""
    hist = [h for r in run_list for h in _l(_d(r.get("st")).get("history")) if isinstance(h, dict)]
    ends = _ends(run_list)
    left = [p for _, p, e, _ in ends if e is not None]
    if not all(_invocations(r)[1] for r in run_list):
        i1 = F(None, NA, "a resume time is naive with no utc_offset_s")
    else:
        i1 = F(len(left), INF if any(p["from"] == INF for p in left) else REC)
    if any(not isinstance(_d(r.get("meta")).get("invocations"), list) for r in run_list):
        i5 = F(None, NA, "a run records no invocations")
    else:
        i5 = F(sum(max(0, len(r["meta"]["invocations"]) - 1) for r in run_list), REC)
    if any(started(r) is None for r in run_list[1:]):
        i6 = F(None, NA, "a relaunch has no computable start time")
    else:
        i6 = F(len(run_list) - 1, REC if linked or len(run_list) == 1 else INF,
               None if linked or len(run_list) == 1 else "chain from --chain")
    dec = [sum(x) for x in zip(*[_owner_decisions(r) for r in run_list])]
    raises = sum(1 for h in hist if "budget_raised" in h)
    if all(os.path.lexists(os.path.join(r["dir"], "run_files.sha256.json")) for r in run_list):
        i8 = F(sum(len(set(_l(h.get("hand_edits")))) for h in hist), REC)
    else:
        spec = sum(1 for r in run_list if any(_d(f).get("kind") == "spec_changed_after_signoff"
                                              for f in _l(r.get("findings"))))
        i8 = F(spec + sum(len(r["edited_inputs"]) for r in run_list), INF,
               "no run-files hash record: only the spec after sign-off and the staged inputs are checked")
    led = [_d(_d(ledger).get(r["name"])) for r in run_list]
    i9 = (F(sum(int(x.get("i9", 0)) for x in led), REC, "operator ledger") if ledger and any(led)
          else F(None, NA, "outside the run directory: the operator ledger"))
    head = [x["value"] for x in (i1, i5, i6, i8)]
    return {"I1_pauses": i1, "I2_spec_signatures": F(dec[0], REC), "I3_geometry_approvals": F(dec[1], REC),
            "I4_scope_answers": F(dec[2], REC, f"{dec[3]} item(s) answered"), "I5_resumes": i5,
            "I6_relaunches": i6, "I7_budget_raises": F(raises, REC), "I8_hand_edits": i8,
            "I9_operator_actions": i9, "pending_pauses": sum(1 for _, _, e, _ in ends if e is None),
            "headline": F(None if None in head else sum(head), NA if None in head else
                          (INF if INF in (i1["from"], i6["from"], i8["from"]) else REC)),
            "owner_decisions": F(dec[0] + dec[1] + dec[2] + raises, REC)}


# ------------------------------------------------------------------ chains and rows
def chains(dirs, explicit=None) -> list[list[str]]:
    """Group run dirs into chains: the operator's `explicit` lists first, then metadata.parent_run links. A link
    holds only when the parent's name is unique among the dirs given and, when recorded, its run_state.json sha256
    matches; a parent with several children ends its chain there (each child starts its own). Every dir given is in
    exactly one chain."""
    seen, ex, rest = set(), [], []
    for c in explicit or []:
        c = [d for d in c if os.path.normpath(d) not in seen and not seen.add(os.path.normpath(d))]
        ex += [c] if c else []
    for d in dirs:
        if os.path.normpath(d) not in seen:
            seen.add(os.path.normpath(d))
            rest.append(d)
    names = {}
    for d in rest:
        names.setdefault(os.path.basename(os.path.normpath(d)), []).append(d)
    parent = {}
    for d in rest:
        obj, _ = _read(d, "metadata.json") if os.path.isdir(d) else (None, None)
        pr = _d(_d(obj).get("parent_run"))
        cand = names.get(pr.get("run_dir"), [])
        if len(cand) == 1 and cand[0] != d and ("run_state_sha256" not in pr     # a hash recorded but null: no link
                                                or pr["run_state_sha256"] is not None
                                                and pr["run_state_sha256"] == _sha(cand[0], "run_state.json")):
            parent[d] = cand[0]
    kids = {}
    for c, p in parent.items():
        kids.setdefault(p, []).append(c)
    out, placed = [], set()
    for d in rest:
        if d in parent and len(kids[parent[d]]) == 1:
            continue                                       # followed from its parent
        c, cur = [d], d
        while len(kids.get(cur, [])) == 1 and kids[cur][0] not in c:
            cur = kids[cur][0]
            c.append(cur)
        out.append(c)
        placed.update(c)
    out += [[d] for d in rest if d not in placed]          # a parent cycle: each dir alone, never dropped
    return ex + out


def question_key(question) -> str | None:
    """The eval question this run answers, matched on the public evals/<q>/question.txt; None for any other."""
    q, ev = " ".join(str(question or "").split()), os.path.join(REPO, "evals")
    for k in sorted(os.listdir(ev)) if os.path.isdir(ev) and q else []:
        t = " ".join(open(os.path.join(ev, k, "question.txt")).read().split()) if os.path.isfile(
            os.path.join(ev, k, "question.txt")) else ""
        if t and (q == t or q.startswith(t) or t.startswith(q)):
            return k
    return None


def _cut(cutoff):
    return cutoff if isinstance(cutoff, datetime) else utc(cutoff) if cutoff else None


def wave_cutoff(chain_list):
    """The protocol's strict-arm cut-off: 24 h after the first launch, or the first time every chain is FINAL or
    paused, whichever comes first. An unreadable chain never settles. None when no launch time is computable."""
    loaded = [[load_run(d) for d in c] for c in chain_list]
    t0 = [started(c[0]) for c in loaded if not c[0]["errors"] and started(c[0])]
    if not t0:
        return None
    cap = min(t0) + timedelta(hours=CENSOR_H)
    if any(r["errors"] for c in loaded for r in c):
        return cap
    cands = sorted({t for c in loaded for t in [_finished(r) for r in c] + [p["at"] for _, p, _, _ in _ends(c)]
                    if t and t <= cap})
    for t in cands:
        if all((lambda v: v[-1]["st"].get("status") or v[-1]["st"].get("paused"))(as_of(c, t)) for c in loaded):
            return t
    return cap


def summarise(chain_dirs, cutoff=None, ledger=None, linked=None) -> dict:
    """One row for one chain (ordered run dirs, first launch first), as it stood at `cutoff` (ISO or datetime;
    default: wave_cutoff of this chain alone)."""
    full = [load_run(d) for d in chain_dirs]
    if any(r["errors"] for r in full):
        return {"chain": [r["name"] for r in full], "label": full[-1]["name"], "class": "not_completed",
                "status": F("unreadable", REC, "; ".join(f"{r['name']}: {e}" for r in full for e in r["errors"])),
                "completes": F(False, REC)}
    if linked is None:
        linked = all(_d(b["meta"].get("parent_run")).get("run_dir") == a["name"] for a, b in zip(full, full[1:]))
    now = datetime.now(timezone.utc)
    cut = _cut(cutoff) if cutoff else wave_cutoff([chain_dirs])
    cut = min(cut, now) if cut else None
    runs = as_of(full, cut)
    last, first = runs[-1], runs[0]
    row = {"chain": [r["name"] for r in full], "label": full[-1]["name"], **completion(last)}
    t0 = started(first)
    meta, st = last["meta"], last["st"]
    commits = {c for r in runs for c in [r["meta"].get("commit")] +
               [_d(i).get("commit") for i in _l(r["meta"].get("invocations"))] if c}
    row.update({"question": F(question_key(first["meta"].get("question")), INF),
                "commit": F(meta.get("commit"), REC if meta.get("commit") else NA,
                            "commits differ across invocations" if len(commits) > 1 else None),
                "stop_state": F(st.get("state"), REC), "rounds": F(st.get("round"), REC),
                "model_usd": F(round(sum(float(r["meta"].get("model_spend_usd", r["st"].get("spent_usd")) or 0)
                                         for r in full), 4), REC),
                "models": F(meta.get("models"), REC if meta.get("models") else NA),
                "sdk_version": F(meta.get("sdk_version"), REC if meta.get("sdk_version") else NA),
                "interventions": interventions(runs, ledger, linked), "cutoff": _iso(cut)})
    if cut is None:
        row["owner_wait"] = {"total_h": F(None, NA, "no computable launch time or cut-off"), "censored": False,
                             "pauses": []}
    else:
        row["owner_wait"] = owner_wait(runs, cut)
    end = _finished(last) if st.get("status") else cut           # paused or still working: to the cut-off
    if t0 and cut and t0 > cut:
        row["cutoff_note"] = "launched after the cut-off: pass one wave's runs, or --cutoff"
    w = row["owner_wait"]["total_h"]["value"]
    wall = _h(t0, min(end, cut) if end and cut else end)
    row["autonomous_wall_h"] = (F(round(max(0.0, wall) - w, 4), row["owner_wait"]["total_h"]["from"])
                                if wall is not None and w is not None else F(None, NA, "start, end or wait unknown"))
    return row


def rows(dirs, explicit=None, cutoff=None, ledger=None) -> list[dict]:
    """One row per chain. With no `cutoff`, the wave's strict-arm cut-off (wave_cutoff) applies to every chain."""
    cs = chains(dirs, explicit)
    cut = _cut(cutoff) if cutoff else wave_cutoff(cs)
    return [summarise(c, cut, ledger, linked=False if c in (explicit or []) else None) for c in cs]



def wave_summary(rs) -> dict:
    """Per wave: completed k/n (scope declared apart), median headline interventions, total owner-wait."""
    heads = [r.get("interventions", {}).get("headline", {}).get("value") for r in rs]
    waits = [r.get("owner_wait", {}).get("total_h", {}).get("value") for r in rs]
    hs, done = [h for h in heads if h is not None], [r for r in rs if r["completes"]["value"]]
    return {"chains": len(rs), "completed": len(done), "median_interventions": statistics.median(hs) if hs else None,
            "scope_declared": sum(1 for r in done if r["status"]["value"] == "completed_scope_declared"),
            "interventions_unavailable": len(heads) - len(hs), "owner_wait_unavailable": waits.count(None),
            "total_owner_wait_h": round(sum(w for w in waits if w is not None), 4)}


def _cell(f):
    if not isinstance(f, dict) or f.get("from") == NA:
        return "n/a"
    return ("-" if f.get("value") is None else str(f["value"])) + ("~" if f.get("from") == INF else "")


def table(rs) -> str:
    cols = ["label", "question", "status", "done", "stop", "interv", "decisions", "wait_h", "auto_h", "model_$",
            "rounds"]
    lines = []
    for r in rs:
        iv, ow = r.get("interventions", {}), r.get("owner_wait", {})
        g = [r.get(k) for k in ("question", "status", "stop_state")] + [iv.get("headline"), iv.get("owner_decisions"),
                                                                       ow.get("total_h")]
        g = [_cell(x) for x in g] + [_cell(r.get(k)) for k in ("autonomous_wall_h", "model_usd", "rounds")]
        g[5] += "*" if ow.get("censored") else ""
        lines.append([r["label"][:40]] + g[:2] + ["Y" if r["completes"]["value"] else "N"] + g[2:])
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
    if not a.run_dirs and not explicit:
        ap.error("no run directory")
    rs = rows(list(a.run_dirs), explicit, a.cutoff, json.load(open(a.ledger)) if a.ledger else None)
    print(table(rs))
    if a.json:
        safe_io.publish(a.json, json.dumps({"rows": rs, "summary": wave_summary(rs)}, indent=1, default=str),
                        os.path.dirname(os.path.abspath(a.json)))
    return 0


# ------------------------------------------------------------------ self-test (synthetic run dirs only)
def _self_test() -> int:
    import contextlib
    import io
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        fails.extend([] if c else [m])
    T0 = datetime(2020, 1, 1, 0, 0, tzinfo=timezone.utc)

    def at(h):
        return (T0 + timedelta(hours=h)).isoformat(timespec="seconds")

    def local(h, off=3600):          # naive local time, as main.py writes it
        return (T0 + timedelta(hours=h, seconds=off or 0)).replace(tzinfo=None).isoformat(timespec="seconds")

    def mk(name, st=None, meta=None, files=None, off=3600, inv=(0,), under=""):
        d = os.path.join(root, under, name)
        os.makedirs(os.path.join(d, "data"), exist_ok=True)
        m = {"started": local(0, off), "commit": "abc1234", "status": (st or {}).get("status"),
             "invocations": [{"at": local(h, off), "commit": "abc1234"} for h in inv],
             **({"utc_offset_s": off} if off is not None else {}), **(meta or {})}
        files = {"metadata.json": json.dumps(m), **({"run_state.json": json.dumps(
            {"state": "FINAL", "round": 1, "history": [], **st})} if st is not None else {}), **(files or {})}
        for rel, txt in files.items():
            open(os.path.join(d, rel), "w").write(txt)
        return d

    def iv(r, k):
        return r["interventions"][k]["value"]

    def wait(r, k="total_h"):
        return r["owner_wait"][k]["value"]
    A, R = at(30), {"report.md": "x"}          # A: an assisted-arm cut-off, after every owner action below
    with tempfile.TemporaryDirectory() as root:
        fin = {"history": [{"at": at(1), "from": "GATE", "to": "FINAL", "why": "accepted"}]}
        done = {**fin, "status": "completed"}
        r = summarise([mk("r_ok", done, {"finished": at(1)}, R)])
        ok(r["completes"] == F(True, REC) and iv(r, "headline") == 0 and wait(r) == 0
           and r["autonomous_wall_h"]["value"] == 1.0, "HM1 a completed run with report.md: 0 interventions, 1 h")
        ok(not summarise([mk("r_wh", done, None, {"report_withheld.md": "x"})])["completes"]["value"],
           "HM2 a withheld report does not complete")
        # HM2b (baseline hb2 §5 fix 8): a language repair left the rejected draft in report_withheld.md and the
        # repaired report.md was delivered. Every withhold path moves report.md away, so a regular report.md at FINAL
        # is the delivered one: the stale file does not make the run "not done".
        r = summarise([mk("r_wh_stale", {**fin, "status": "completed_scope_declared"}, None,
                          {"report.md": "x", "report_withheld.md": "old"})])
        ok(r["completes"]["value"] and "stale" in str(r["completes"].get("note")),
           f"HM2b a delivered report.md completes although a stale report_withheld.md remains ({r['completes']})")
        r = summarise([mk("r_sd", {**fin, "status": "completed_scope_declared"}, None, R)])
        ok(r["completes"]["value"] and wave_summary([r])["scope_declared"] == 1, "HM3 scope declared completes, apart")
        r = summarise([mk("r_none", {"state": "ANALYZE"})])
        ok(r["status"]["value"] == "no_status" and not r["completes"]["value"], "HM4a no status recorded: not done")
        r = rows([mk("r_bad", None, None, {"run_state.json": "{not json"}), os.path.join(root, "missing")])
        ok(len(r) == 2 and all(x["status"]["value"] == "unreadable" and not x["completes"]["value"] for x in r),
           "HM4 an unparseable run_state.json and a missing dir are unreadable rows, not skipped")
        ok("refused" in summarise([mk("r_t", done, None, R, under="_truth")])["status"].get("note", ""),
           "HM5 a path under _truth is refused, unread")
        h = [{"at": at(1), "paused": "awaiting_budget", "state": "CRITIQUE"},
             {"at": at(3), "budget_raised": {"from": 5, "to": 9}}, {"at": at(4), "from": "GATE", "to": "FINAL"}]
        d = mk("r_bud", {"history": h, "status": "completed"}, None, R, inv=(0, 3))
        r0, r = summarise([d]), summarise([d], A)
        ok(r0["cutoff"] == at(1) and not r0["completes"]["value"] and iv(r0, "headline") == 0,
           "HM30 strict arm by default: the wave stops at the first pause, nothing after counts")
        ok(r["interventions"]["I1_pauses"] == F(1, REC) and iv(r, "I5_resumes") == 1 and iv(r, "I7_budget_raises") == 1
           and iv(r, "headline") == 2 and r["owner_wait"]["total_h"] == F(2.0, REC)
           and wait(r, "owner_response_h") == 2.0, "HM6 an explicit budget pause: I1 record, I5, I7, 2 h owner response")
        h = [{"at": at(0.5), "from": "SPEC", "to": "SIGN"},
             {"at": at(3.2), "from": "SIGN", "to": "EVIDENCE", "why": SIGNED_BY_OWNER}]
        so = json.dumps({"signed_by": "owner", "signed_at": at(1.5)})
        r = summarise([mk("r_sign", {"history": h, "status": "completed"}, None,
                          {**R, "decision_spec.signoff.json": so}, inv=(0, 3))], A)
        ok(r["interventions"]["I1_pauses"] == F(1, INF) and iv(r, "I2_spec_signatures") == 1
           and r["interventions"]["headline"]["from"] == INF and wait(r) == 2.5 and wait(r, "owner_response_h") == 1.0
           and wait(r, "operator_latency_h") == 1.5, "HM7 an inferred SIGN pause; naive local times converted; split")
        r = summarise([mk("r_tz", {"history": h, "status": "completed"}, None, R, off=None, inv=(0, 3))], A)
        ok(r["owner_wait"]["total_h"]["from"] == NA and r["autonomous_wall_h"]["from"] == NA,
           "HM8 naive times with no offset: wait not computable")
        d = mk("r_pend", {"state": "SIGN", "paused": "awaiting_signoff", "status": None,
                          "history": [{"at": at(1), "from": "SPEC", "to": "SIGN"}],
                          "journal": {"SIGN#1:sign_spec": {"status": "completed", "completed": at(2)}}})
        r = rows([d, mk("r_running", {"state": "ANALYZE"})])[0]
        ok(r["status"]["value"] == "paused:awaiting_signoff" and not r["completes"]["value"]
           and iv(r, "I1_pauses") == 0 and r["interventions"]["pending_pauses"] == 1 and r["owner_wait"]["censored"]
           and wait(r) == 22.0 and r["owner_wait"]["pauses"][0]["pause_at"] == at(2)
           and r["autonomous_wall_h"]["value"] == 2.0, "HM9 a pending lead SIGN pause while another run runs: from "
           "the sign_spec time, censored at launch + 24 h, not an intervention")
        r = summarise([d])
        ok(r["cutoff"] == at(2) and wait(r) == 0.0 and r["owner_wait"]["censored"],
           "HM9b every run paused or FINAL: the cut-off is when the last one settled")
        ok(wait(summarise([d], cutoff=at(5))) == 3.0, "HM10 an explicit cut-off censors there")
        p = mk("r_par", {"state": "SPEC", "paused": "awaiting_spec_amendment", "status": None,
                         "history": [{"at": at(1), "from": "GATE", "to": "SPEC"}]})
        c = mk("r_child", done, {"parent_run": {"run_dir": "r_par"}, "started": local(4)}, R)
        r = rows([p, c], cutoff=A)
        ok(len(r) == 1 and r[0]["chain"] == ["r_par", "r_child"] and r[0]["interventions"]["I6_relaunches"] == F(1, REC)
           and iv(r[0], "I1_pauses") == 1 and wait(r[0]) == 3.0 and r[0]["completes"]["value"],
           "HM11 parent_run links a relaunch: I6 record, the pause ends at the child's start")
        r = rows([], explicit=[[p, mk("r_child2", done, {"started": local(4)}, R)]], cutoff=A)
        ok(r[0]["interventions"]["I6_relaunches"]["from"] == INF, "HM12 an operator --chain makes I6 inferred")
        h = [{"at": at(1), "from": "GATE", "to": "OWNER_DECISION"},
             {"at": at(6), "from": "OWNER_DECISION", "to": "GATE", "why": SCOPE_ANSWERED}]
        sc = json.dumps({"answers": {"f1": {"by": "owner", "at": at(2)}, "f2": {"by": "owner", "at": at(4)}}})
        r = summarise([mk("r_scope", {"history": h, "status": "completed"}, None, {**R, "scope_signoff.json": sc},
                          inv=(0, 5))], A)
        ok(iv(r, "I4_scope_answers") == 1 and "2 item" in r["interventions"]["I4_scope_answers"]["note"]
           and wait(r, "owner_response_h") == 3.0 and wait(r, "operator_latency_h") == 1.0,
           "HM13 a scope answer: I4 1 (2 items), owner response to the last answer")
        h = [{"at": at(1), "from": "SPEC", "to": "SIGN"}, {"at": at(1), "paused": "awaiting_signoff", "state": "SIGN"},
             {"at": at(3), "from": "SIGN", "to": "EVIDENCE", "why": SIGNED_BY_OWNER}]
        r = summarise([mk("r_both", {"history": h, "status": "completed"}, None, R, inv=(0, 2))], A)
        ok(r["interventions"]["I1_pauses"] == F(1, REC) and len(r["owner_wait"]["pauses"]) == 1,
           "HM14 an explicit pause entry is the record; no inferred copy")
        g = json.dumps({"approvals": {"G1": {"approved_by": "owner"}, "G2": {"approved_by": "lead"}}})
        d = mk("r_geo", {**fin, "status": "run_failed"}, None, {"data/geometry_signoff.json": g})
        r, out = rows([d], ledger={"r_geo": {"i9": 2}}), os.path.join(root, "o.json")
        with contextlib.redirect_stdout(io.StringIO()):
            main([d, "--json", out])
        js = json.load(open(out))
        ok(iv(r[0], "I3_geometry_approvals") == 1 and r[0]["interventions"]["I9_operator_actions"]["value"] == 2
           and r[0]["class"] == "not_completed" and js["summary"]["chains"] == 1 and "r_geo" in table(r)
           and all("from" in v for v in js["rows"][0]["interventions"].values() if isinstance(v, dict)),
           "HM15 owner geometry approvals, the operator ledger; every JSON field carries its derivation")
        r = summarise([mk("r_raw", done, {"models": {"deep": "m"}}, R)])
        ok(r["models"] == F({"deep": "m"}, REC) and r["sdk_version"]["from"] == NA,
           "HM16 model ids are read from metadata when recorded, else unavailable")
        ok(utc(local(0)[:-3] + "xx") is None and utc("2020-01-01T01:00:00", 3600) == T0
           and utc("2020-01-01T00:00:00+00:00") == T0, "HM17 utc(): naive local with offset, aware, garbage")
        # ---- Codex round 1
        r = summarise([mk("r_mis", done, {"status": "run_failed"}, R)])
        ok(r["status"]["value"] == "status_mismatch" and not r["completes"]["value"],
           "HM18 run_state.json and metadata.json disagree on the status: not completed")
        # HM18b/c (baseline hb3 §4.2): metadata.json now records a pause too; it must agree with run_state.json
        pz = {"state": "SPEC", "paused": "awaiting_spec_amendment", "status": None}
        r = summarise([mk("r_pz", pz, {"status": "paused:awaiting_spec_amendment"})])
        ok(r["status"]["value"] == "paused:awaiting_spec_amendment" and r["class"] == "not_completed",
           f"HM18b a pause recorded in both files counts as that pause, not completed ({r['status']})")
        r = summarise([mk("r_pz2", pz, {"status": "paused:awaiting_signoff"})])
        ok(r["status"]["value"] == "status_mismatch" and not r["completes"]["value"],
           f"HM18c run_state.json and metadata.json disagree on the pause: a mismatch ({r['status']})")
        d, d2 = mk("r_dir", done), mk("r_dangle", done)
        os.makedirs(os.path.join(d, "report.md"))
        os.symlink(os.path.join(root, "nowhere"), os.path.join(d2, "report.md"))
        ok(not summarise([d])["completes"]["value"] and not summarise([d2])["completes"]["value"],
           "HM19 a directory or a dangling link named report.md is no report")
        h = [{"at": at(1), "paused": "awaiting_budget", "state": "CRITIQUE"}, {"at": at(4), "from": "GATE", "to": "FINAL"}]
        r = summarise([mk("r_late", {"history": h, "status": "completed"}, {"finished": at(4)}, R, inv=(0, 3))], at(2))
        ok(not r["completes"]["value"] and iv(r, "I1_pauses") == 0 and iv(r, "I5_resumes") == 0 and wait(r) == 1.0
           and r["owner_wait"]["censored"], "HM20 events after the cut-off do not count")
        p = mk("r_amend", {"state": "SPEC", "paused": "awaiting_spec_amendment", "status": None, "history": [
            {"at": at(0.5), "from": "SIGN", "to": "EVIDENCE"}, {"at": at(1), "from": "GATE", "to": "SPEC"}]}, inv=(0, 3))
        c = mk("r_amend_c", done, {"parent_run": {"run_dir": "r_amend"}, "started": local(5)}, R)
        r = rows([p, c], cutoff=A)[0]
        ok(iv(r, "I1_pauses") == 2 and wait(r) == 4.0, "HM21 a resumed amendment pause re-pauses: two pauses, 4 h")
        h = [{"at": at(0), "from": "SPEC", "to": "SIGN"}, {"at": at(3), "from": "SIGN", "to": "EVIDENCE"}]
        ok(iv(summarise([mk("r_same", {"history": h, "status": "completed"}, None, R, inv=(0, 3))], A),
              "I1_pauses") == 1, "HM22 a resume in the same second as the transition out is still a resume")
        h = [{"at": at(0), "from": "SPEC", "to": "SIGN"}, {"at": at(0.5), "paused": "awaiting_budget", "state": "SIGN"},
             {"at": at(3.5), "from": "SIGN", "to": "EVIDENCE"}]
        r = summarise([mk("r_mix", {"history": h, "status": "completed"}, None, R, inv=(0, 1, 3))], A)
        ok(iv(r, "I1_pauses") == 2 and len(r["owner_wait"]["pauses"]) == 2,
           "HM23 a budget pause in a SIGN visit does not hide the later sign-off pause")
        h = [{"at": at(0.5), "from": "SPEC", "to": "SIGN"}, {"at": at(3.2), "from": "SIGN", "to": "EVIDENCE"}]
        r = summarise([mk("r_tz2", {"history": h, "status": "completed"}, {"started": at(0)}, R, off=None, inv=(0, 3))])
        ok(r["interventions"]["I1_pauses"]["from"] == NA and r["interventions"]["headline"]["from"] == NA,
           "HM24 a naive resume time with no offset: I1 and the headline are unavailable, not 0")
        fa, ra = {**fin, "status": "run_failed"}, {"parent_run": {"run_dir": "f_a"}}
        a, b, c = mk("f_a", fa), mk("f_b", fa, ra), mk("f_c", done, ra, R)
        x, y = mk("y_x", fa, {"parent_run": {"run_dir": "y_y"}}), mk("y_y", fa, {"parent_run": {"run_dir": "y_x"}})
        flat = sorted(os.path.basename(d) for ch in chains([a, b, c, x, y, a]) for d in ch)
        ok(flat == ["f_a", "f_b", "f_c", "y_x", "y_y"], "HM25 a fork, a cycle and a repeated dir: each in one chain")
        b2 = mk("s_b", fa, {"parent_run": {"run_dir": "r_ok", "run_state_sha256": "0" * 64}})
        ok(len(chains([os.path.join(root, "r_ok"), b2])) == 2, "HM26 a parent with another run_state hash: no link")
        d = mk("r_link", None)
        os.symlink(os.path.join(root, "r_ok", "run_state.json"), os.path.join(d, "run_state.json"))
        r = rows([d])
        ok(len(r) == 1 and r[0]["status"]["value"] == "unreadable", "HM27 a linked run_state.json is an unreadable row")
        a = mk("o_a", {"state": "SPEC", "paused": "awaiting_spec_amendment",
                       "history": [{"at": at(1), "from": "SIGN", "to": "EVIDENCE", "why": SIGNED_BY_OWNER}]})
        sc = json.dumps({"answers": {"f1": {"by": "owner", "at": at(2)}}})
        b = mk("o_b", done, {"parent_run": {"run_dir": "o_a"}}, {**R, "decision_spec.signoff.json": so,
                                                                "scope_signoff.json": sc})
        r = rows([a, b], cutoff=A)[0]
        ok(iv(r, "I2_spec_signatures") == 2 and iv(r, "I4_scope_answers") == 1,
           "HM28 owner decisions count per run across a chain")
        prov = json.dumps({"inputs": {"data/a.csv": {"sha256": hashlib.sha256(b"orig").hexdigest()}}})
        d = mk("r_edit", done, None, {**R, "data/inputs_provenance.json": prov, "data/a.csv": "changed"})
        ok(iv(summarise([d]), "I8_hand_edits") == 1, "HM29 a staged input edited by hand is I8")
        # ---- Codex round 2
        so3 = json.dumps({"signed_by": "owner", "signed_at": at(3.5)})
        p = mk("c_par", {"state": "SIGN", "paused": "awaiting_signoff",
                         "history": [{"at": at(1), "from": "SPEC", "to": "SIGN"}]})
        c = mk("c_kid", done, {"parent_run": {"run_dir": "c_par"}, "started": local(3)},
               {**R, "decision_spec.signoff.json": so3, "data/inputs_provenance.json": prov})
        r = rows([p, c], cutoff=at(2))[0]
        ok(iv(r, "headline") == 0 and iv(r, "owner_decisions") == 0 and r["interventions"]["pending_pauses"] == 1,
           "HM31 a relaunch after the cut-off, its edits and signature do not count; the pause is pending")
        h = [{"at": at(t), "paused": "awaiting_budget", "state": "SPEC"} for t in (1, 3)]
        r = summarise([mk("r_2bud", {"state": "SPEC", "paused": "awaiting_budget", "history": h}, inv=(0, 3))], A)
        ok(iv(r, "I1_pauses") == 1 and r["interventions"]["pending_pauses"] == 1, "HM32 one resume ends one pause")
        h = [{"at": at(1), "from": "SPEC", "to": "SPEC"}, {"at": at(4), "from": "SIGN", "to": "EVIDENCE"},
             {"at": at(8), "from": "GATE", "to": "SPEC"}]
        r = summarise([mk("r_retry", {"state": "SPEC", "paused": "awaiting_spec_amendment", "history": h,
                                      "tries": {"spec_amendment": True}}, inv=(0, 2))], A)
        ok(iv(r, "I1_pauses") == 0 and r["interventions"]["pending_pauses"] == 1,
           "HM33 a SPEC retry before signing is no pause")
        r = rows([os.path.join(root, "r_pend"), os.path.join(root, "r_bad")])[0]
        ok(wait(r) == 22.0, "HM34 an unreadable chain keeps the wave open to 24 h")
        a = os.path.join(root, "r_ok")
        ok(chains([], [[a], [a]]) == [[a]] and chains([], [[a, a]]) == [[a]], "HM35 a repeated --chain dir counts once")
        d = mk("r_slow", {"history": [{"at": at(27), "from": "GATE", "to": "FINAL"}], "status": "completed"}, None, R)
        ok(not summarise([d])["completes"]["value"], "HM37 no finished time: FINAL after the 24 h cut-off is not done")
        r = summarise([mk("r_noinv", done, {"invocations": None, "finished": at(1)}, R)])
        ok(r["interventions"]["I5_resumes"]["from"] == NA and r["interventions"]["headline"]["from"] == NA,
           "HM38 a run that records no invocations (before 2026-11-05): I5 and the headline are unavailable")
        # ---- records PR (Codex round on harness-run-records)
        h = [{"at": at(3), "from": "SPEC", "to": "SIGN"}, {"at": at(3), "paused": "awaiting_signoff", "state": "SIGN"}]
        r = summarise([mk("r_sec", {"state": "SIGN", "paused": "awaiting_signoff", "history": h}, inv=(0, 3))], A)
        ok(iv(r, "I1_pauses") == 0 and r["interventions"]["pending_pauses"] == 1,
           "HM39 a pause recorded in the same second as the resume that started the session is pending, not ended")
        h = [{"at": at(1), "from": "SPEC", "to": "SIGN"}, {"at": at(2.5), "paused": "awaiting_signoff", "state": "SIGN"}]
        r = summarise([mk("r_old", {"state": "SIGN", "paused": "awaiting_signoff", "history": h}, inv=(0, 2))], A)
        ok(iv(r, "I1_pauses") == 1 and r["interventions"]["pending_pauses"] == 1,
           "HM40 an older build's unrecorded pause before a recorded one in the same SIGN visit is still inferred")
        b3 = mk("s_c", fa, {"parent_run": {"run_dir": "r_ok", "run_state_sha256": None}})
        ok(len(chains([os.path.join(root, "r_ok"), b3])) == 2, "HM41 a parent_run with no hash is never linked")
        h = [{"at": at(1), "from": "SPEC", "to": "SIGN"}, {"at": at(2), "paused": "awaiting_signoff", "state": "SIGN"}]
        r = summarise([mk("r_sec2", {"state": "SIGN", "paused": "awaiting_signoff", "history": h}, inv=(0, 2))], A)
        h = [{"at": at(2), "from": "SPEC", "to": "SIGN"}, {"at": at(2.5), "paused": "awaiting_signoff", "state": "SIGN"}]
        r2 = summarise([mk("r_sec3", {"state": "SIGN", "paused": "awaiting_signoff", "history": h}, inv=(0, 2))], A)
        ok(iv(r, "I1_pauses") == 1 and iv(r2, "I1_pauses") == 0, "HM42 same-second edges: a resume in the second "
           "the new build re-pauses ends the old pause; a resume in the second SIGN is entered ends none")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
