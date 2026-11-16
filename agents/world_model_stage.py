#!/usr/bin/env python3
"""The world model in every v2 run: the PREREGISTER and SCORE states (docs/design/v2c-orchestration.md §14).
Fail soft; writes only world_model/ and the repository's trials/decisions/.  `--self-test` runs the cases."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import decision_beliefs as DB  # noqa: E402
import world_model_trials as WMT  # noqa: E402
from toolkit.analysis import belief_table as BT  # noqa: E402

WM = "world_model"
SEED_DIR = os.path.join(WM, "seed")
PREREG, SCORE = os.path.join(WM, "preregistration.json"), os.path.join(WM, "score.json")
TABLE, SECTION = os.path.join(WM, "belief_table.yaml"), os.path.join(WM, "world_model.md")
SEED_CAP_USD = 2.0          # one seed session; a repair gets the same cap
MAX_TOPICS = 30
SHARE_BANDS = (0.1, 0.5)    # few: at most a tenth; a minority: up to half; most: more than half
VARIANT_LOSS_PCT = 1.0      # a variant gives up "at most a hundredth" of the objective
JOURNAL_KEY = "PREREGISTER:seed"
SCORED = ("analysis/point.yaml", "analysis/allocation.csv", "analysis/decision_tests.yaml",
          "analysis/plan_variants.yaml", "analysis/value_of_information.yaml")
ENGINE_ROWS = ("analysis/results.jsonl", "analysis/batch/results.jsonl")
WORDS = ("few or no = at most a tenth of units; a minority = more than a tenth and at most half; most = more than "
         "half; at most a hundredth of the objective = value lost of one percent or less")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _yaml(path):
    import yaml
    with open(path) as f:
        return yaml.safe_load(f)


def _data(run_dir, rel):
    """An analysis artifact's payload (its `data` block when it has one), or None when absent or unreadable."""
    try:
        d = _yaml(os.path.join(run_dir, rel))
        return d.get("data", d) if isinstance(d, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _put(run_dir, rel, text) -> None:
    WMT._sio().write_nofollow(run_dir, rel, text.encode() if isinstance(text, str) else text, atomic=True)


def _topic(topic, kind, key, output, field, help_, claims, rules):
    return {"topic": topic, "kind": kind, "key": key, "output": output, "field": field, "help": help_,
            "claims": claims, "rules": rules}


def _bands(topic, kind, key, what, noun, help_):
    return _topic(topic, kind, key, "analysis/point.yaml", f"plan: share of units whose package {noun}", help_,
                  [f"few or no units {what}", f"a minority of units {what}", f"most units {what}"],
                  [f"at most a tenth of units {what}", f"more than a tenth and at most half of units {what}",
                   f"more than half of units {what}"])


def observables(spec) -> tuple[list[dict], list[str]]:
    """The topics of a run's beliefs, from its signed spec: each decision test, each plan variant, the departure from
    the comparator package, value of information (unless switched off), each component. Each has a fixed claim set
    and one rule per claim. Returns (topics, those dropped beyond MAX_TOPICS)."""
    an, ok = spec.get("analysis") or {}, re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,40}$").fullmatch
    ids = lambda xs: [x.get("id") for x in xs or [] if isinstance(x, dict) and isinstance(x.get("id"), str)  # noqa
                      and ok(x["id"])]
    obs = [_topic(f"test_{t}", "test", t, "analysis/decision_tests.yaml", f"tests[{t}].verdict",
                  f"whether decision test {t} changes the plan",
                  [f"decision test {t} changes the plan", f"decision test {t} does not change the plan"],
                  ["verdict CHANGES_PLAN", "verdict NO_CHANGE"])
           for t in ids(an.get("decision_tests") or spec.get("decision_tests"))]
    obs += [_topic(f"variant_{v}", "variant", v, "analysis/plan_variants.yaml", f"variants[{v}].value_lost_pct",
                   f"how much plan variant {v} gives up",
                   [f"plan variant {v} gives up at most a hundredth of the unconstrained optimum's objective",
                    f"plan variant {v} gives up more than a hundredth of the unconstrained optimum's objective"],
                   ["value lost of one percent or less", "value lost above one percent"])
            for v in ids((an.get("plan_variants") or {}).get("variants"))]
    comp = (spec.get("comparator") or {}).get("package")
    if isinstance(comp, str) and ok(comp):
        obs.append(_bands("departure", "departure", comp, f"get a package other than the comparator {comp}",
                          f"differs from {comp}", f"how far the recommended plan departs from the comparator {comp}"))
    if (an.get("value_of_information") or {}).get("enabled") is not False:
        obs.append(_topic("value_of_information", "voi", None, "analysis/value_of_information.yaml",
                          "inputs[].supported", "whether any uncertain input carries value of information",
                          ["at least one uncertain input carries value of information above its noise floor",
                           "no uncertain input carries value of information above its noise floor"],
                          ["some input supported", "inputs computed and none supported"]))
    obs += [_bands(f"share_{c}", "share", c, f"get {c}", f"includes {c}", f"the share of units whose package has {c}")
            for c in ((spec.get("options") or {}).get("components") or {}) if isinstance(c, str) and ok(c)]
    return obs[:MAX_TOPICS], [o["topic"] for o in obs[MAX_TOPICS:]]


def _letters(hexs) -> str:
    """A hash written without digits, so no part of it can match an input number in the licensed scan."""
    return hexs.translate(str.maketrans("0123456789", "ghijklmnop"))


def decision_id(run_dir) -> str:
    """run-<sixteen letters from the run directory's path>; preregistration.json names the directory."""
    return f"run-{_letters(_sha(os.path.realpath(run_dir).encode())[:16])}"


def outcome_map(seed, obs, did, run) -> dict:
    """Per belief: the output, the field and the rule of each of its claims (in the seed's order)."""
    by, ents = {o["topic"]: o for o in obs}, []
    for b in seed["decision_beliefs"]:
        o = by[b["topic"]]
        canon = [c.strip().lower() for c in o["claims"]]
        ents.append({"belief": b["id"], "output": o["output"], "field": o["field"], "rules": {
            i: o["rules"][canon.index(c["claim"].strip().lower())] for i, c in enumerate(b["claims"])}})
    return {"decision": did, "run": run, "entries": ents, "conventions": {
        "words": WORDS, "scoring": "the claim whose rule holds; the leading claim is confirmed when it is that claim "
                                   "and refuted otherwise; a missing output or a test not run is undecided"}}


def _card(run_dir) -> dict:
    import analysis_backend as AB
    p = os.path.join(REPO, *(AB.engine_choice_ref(run_dir) or "").split(":")[0].split(".")[:-1], "card.yaml")
    return (_yaml(p) or {}) if os.path.isfile(p) else {}


def _ok_token(t, ok) -> bool:
    t = t.strip(".!?*-+")
    return not any(ch.isdigit() for ch in t) or t in ok or (t.isdigit() and DB.YEARS[0] <= int(t) <= DB.YEARS[1])


def strict_mask(x, ok):
    """Every token holding a digit that is not a run identifier or a calendar year becomes <value>, in keys and values
    (Codex design review: mask_obj keeps keys, and digits glued to letters survive it)."""
    if isinstance(x, dict):
        return {strict_mask(k, ok): strict_mask(v, ok) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [strict_mask(v, ok) for v in x]
    if isinstance(x, bool) or x is None:
        return x
    if isinstance(x, (int, float)):
        return x if isinstance(x, int) and DB.YEARS[0] <= x <= DB.YEARS[1] else "<value>"
    return DB.TOKEN_RE.sub(lambda m_: m_.group(0) if _ok_token(m_.group(0), ok) else "<value>", str(x))


def input_leaks(ctx, ok) -> list:
    """Digit-bearing tokens left in the whole serialized seed input (the harness's decision_run block included)."""
    run = {k: v for k, v in (ctx.get("decision_run") or {}).items() if k != "beliefs"}     # beliefs: min and max
    text = json.dumps({k: v for k, v in ctx.items() if k != "decision_run"}, ensure_ascii=False) \
        + json.dumps(run, ensure_ascii=False)
    return sorted({t for t in DB.TOKEN_RE.findall(text) if not _ok_token(t, ok)})


def binding(run_dir) -> dict:
    """What a registration is about: the signed spec's bytes, the chosen engine and the code that scores it."""
    import analysis_backend as AB
    r = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30)
    return {"spec_sha256": _sha(open(os.path.join(run_dir, "decision_spec.yaml"), "rb").read()),
            "engine": AB.engine_choice_ref(run_dir), "code_commit": r.stdout.strip() if r.returncode == 0 else None}


def _local(sha, why) -> dict:
    return {"kind": "local_only", "preregistered": False, "sha256": sha, "at": _now(), "why": str(why)[:400]}


def anchor(repo, did, files: dict, check) -> dict:
    """Publish {name: canonical bytes} to wm-trials and read the anchor time; else a local-only record."""
    sha = {k: _sha(v) for k, v in files.items()}
    if subprocess.run(["git", "-C", repo, "remote", "get-url", WMT.REMOTE], capture_output=True,
                      timeout=30).returncode:
        return _local(sha, f"no remote {WMT.REMOTE!r} is configured: not pre-registered")
    old = os.environ.get("GIT_TERMINAL_PROMPT")
    os.environ["GIT_TERMINAL_PROMPT"] = "0"        # a push that would ask for credentials fails instead of waiting
    try:
        rels = [f"trials/decisions/{did}/{k}" for k in files]
        for rel, data in zip(rels, files.values()):
            WMT._sio().write_nofollow(repo, rel, data, atomic=True)
        a = WMT.GitAnchor(repo, os.path.join(repo, "trials", ".wm-trials"))
        r = a.publish(rels, f"decision {did}", check=lambda rel, data: check(os.path.basename(rel), data))
        at = DB.reachable_at(a.wt, a.ref, r["commit"])
        if at is None:
            return _local(sha, "no reflog entry shows the remote holding the commit")
        return {"kind": "remote", "preregistered": True, "commit": r["commit"], "ref": r["ref"], "anchored_at": at,
                "files": rels, "sha256": sha, "rule": "first reflog entry of the remote-tracking ref holding the commit"}
    except Exception as e:  # noqa: BLE001 -- fail soft: recorded, the run goes on
        return _local(sha, f"the anchor failed: {type(e).__name__}: {e}")
    finally:
        os.environ.pop("GIT_TERMINAL_PROMPT") if old is None else os.environ.update(GIT_TERMINAL_PROMPT=old)


def _seed_sha(rd):
    return _sha(WMT._sio().read_nofollow(os.path.join(rd, SEED_DIR), WMT.SEED)[0])


def _seed(m, ctx, did) -> dict:
    """The seed session and its one repair. The run's journal is the one accounting owner: it charges what the
    sessions reported, and an interrupted attempt its allowance (Machine._settle_interrupted); the per-attempt
    SpendLedger only enforces the cap. A completed seed is reused only when its bytes match the journal's sha256."""
    rd, st = m.run_dir, m.st
    j = st.journal.get(JOURNAL_KEY) or {}
    if j.get("status") == "completed":
        out = j.get("output") or {}
        same = j.get("ok") and os.path.isfile(os.path.join(rd, SEED_DIR, WMT.SEED)) and \
            _seed_sha(rd) == out.get("seed_sha256")
        return {"status": "ok" if same else out.get("status") if not j.get("ok") else "seed_changed", "usd": 0.0,
                "reused": True}
    a = m._allowance()
    cap = SEED_CAP_USD if a is None else round(min(SEED_CAP_USD, a / 2), 4)
    if cap < 0.05:
        return {"status": "skipped", "why": "budget: no allowance left for the seed", "usd": 0.0}
    n = int(j.get("attempts", 0)) + 1
    st.journal[JOURNAL_KEY] = dict(j, task="seed_world_model", status="started", started=_now(), allowance_usd=2 * cap,
                                   attempts=n)
    m.save()
    led = WMT.SpendLedger(os.path.join(rd, WM, f"seed_spend_{n}.jsonl"), 2 * cap)
    os.makedirs(os.path.join(rd, SEED_DIR), exist_ok=True)
    r = WMT.seed_step({"id": did}, os.path.join(rd, SEED_DIR), ctx, m.seed_fn, led, cap)
    usd = float(r.get("usd") or 0.0)
    st.spent_usd += usd
    st.journal[JOURNAL_KEY].update(status="completed", completed=_now(), ok=r.get("status") == "ok", usd=usd, output={
        "status": r.get("status"), "seed_sha256": _seed_sha(rd) if r.get("status") == "ok" else None})
    m.save()
    return r


def _preregister(m) -> dict:
    import yaml
    rd = m.run_dir
    if m.seed_fn is None:
        return {"status": "skipped", "why": "no seed task in this run's tasks"}
    spec = _yaml(os.path.join(rd, "decision_spec.yaml")) or {}
    from toolkit.spec import estimates as ES
    if ES.is_estimates(spec):
        return {"status": "skipped", "why": "an estimates analysis: no decision outputs to score yet (declared)"}
    obs, dropped = observables(spec)
    did, bnd = decision_id(rd), binding(rd)
    tag = "spec" + _letters(bnd["spec_sha256"][:16])
    tok = frozenset([did, tag] + [o["topic"] for o in obs] + [o["key"] for o in obs if o["key"]])
    topics = [o["topic"] for o in obs]
    ctx = DB.decision_context(spec, _card(rd), {}, (len(obs), len(obs)), topics=topics,
                              topic_help={o["topic"]: o["help"] for o in obs},
                              outputs={o["output"]: o["field"] for o in obs},
                              claims={o["topic"]: o["claims"] for o in obs})
    run = ctx.pop("decision_run")
    ctx = dict(strict_mask(ctx, tok), decision_run=run)
    paths = [os.path.join(rd, p) for p in ("data", "decision_spec.yaml", "analysis_settings.yaml")]
    lic = DB.licensed_values([p for p in paths if os.path.exists(p)])     # every local input is treated as licensed
    out = {"decision_id": did, "run_dir": rd, "binding": bnd, "topics": topics, "dropped_topics": dropped}
    leak = input_leaks(ctx, tok) + DB.licensed_problems(ctx, lic)
    if leak:
        return dict(out, status="skipped", why=f"the seed input holds numbers after masking: {leak[:5]}")
    r = _seed(m, ctx, did)
    out["seed"] = {k: r.get(k) for k in ("status", "sessions", "usd", "refusals", "error", "why", "reused") if k in r}
    if r.get("status") != "ok":
        return dict(out, status=r.get("status") or "seed_refused", why=r.get("why") or r.get("error") or "seed refused")
    seed_b = WMT.canonical_yaml(WMT._sio().read_nofollow(os.path.join(rd, SEED_DIR), WMT.SEED)[0])
    seed = yaml.safe_load(seed_b)
    map_b = WMT.canonical_yaml(yaml.safe_dump(outcome_map(seed, obs, did, f"{did} {tag}")))
    files = {DB.SEED_REL: seed_b, DB.MAP_REL: map_b}
    for name, data in files.items():
        _put(rd, os.path.join(WM, name), data)

    def check(name, data):
        return DB.publish_problems("seed" if name == DB.SEED_REL else "map", data, lic, (), topics, seed=seed,
                                   tokens=tok)
    problems = check(DB.SEED_REL, seed_b) + check(DB.MAP_REL, map_b)
    anc = _local({k: _sha(v) for k, v in files.items()}, f"the publish check refused the files (not pushed): "
                 f"{problems[:5]}") if problems else anchor(m.wm_repo, did, files, check)
    return dict(out, status="ok", check=problems[:20], anchor=anc, preregistered=anc["preregistered"])


def preregister(m) -> str:
    """The PREREGISTER state's work. On a back-edge the first registration stands; a changed spec or engine puts
    what follows outside it (Codex design review)."""
    wm = m.st.world_model
    try:
        if wm.get("status"):
            now = binding(m.run_dir)
            if wm.get("status") == "ok" and {k: now[k] for k in ("spec_sha256", "engine")} != wm.get("binding"):
                wm["outside_registration"] = "the spec or the engine changed after the registration"
            wm.setdefault("revisits", []).append(_now())
            return f"world model: already registered ({wm['status']})"
        rec = _preregister(m)
    except Exception as e:  # noqa: BLE001 -- fail soft
        rec = {"status": "error", "why": f"{type(e).__name__}: {e}"[:400]}
    rec["at"] = _now()
    _put(m.run_dir, PREREG, json.dumps(rec, indent=1, default=str) + "\n")     # Machine._world_model catches
    anc = rec.get("anchor") or {}
    wm.update(status=rec["status"], decision_id=rec.get("decision_id"), anchor=anc.get("kind"),
              preregistered=bool(anc.get("preregistered")), why=rec.get("why"), anchored_at=anc.get("anchored_at"),
              sha256=anc.get("sha256"), binding={k: (rec.get("binding") or {}).get(k) for k in ("spec_sha256", "engine")})
    return f"world model: {rec['status']}" + (f", anchor {anc.get('kind')}" if anc else "")


# ------------------------------------------------------------------ scoring
def _plan(run_dir):
    pt = _data(run_dir, "analysis/point.yaml")
    if isinstance(pt, dict) and isinstance(pt.get("plan"), dict):
        return pt["plan"], "analysis/point.yaml"
    import csv
    try:
        with open(os.path.join(run_dir, "analysis", "allocation.csv")) as f:
            rows = list(csv.DictReader(f))
        return {r[list(rows[0])[0]]: r["package"] for r in rows}, "analysis/allocation.csv"
    except Exception:  # noqa: BLE001
        return None, None


def evaluate(o, run_dir, plan):
    """The index (in the topic's claim list) of the claim whose rule holds, or None (undecided)."""
    if o["kind"] in ("share", "departure"):
        if not plan:
            return None
        hit = [(o["key"] in str(p).split("+")) if o["kind"] == "share" else str(p) != o["key"] for p in plan.values()]
        s = sum(hit) / len(hit)
        return 0 if s <= SHARE_BANDS[0] else 1 if s <= SHARE_BANDS[1] else 2
    d = _data(run_dir, o["output"])
    if not isinstance(d, dict):
        return None
    if o["kind"] == "test":
        v = next((t.get("verdict") for t in d.get("tests") or [] if t.get("test_id") == o["key"]), None)
        return {"CHANGES_PLAN": 0, "NO_CHANGE": 1}.get(v)
    if o["kind"] == "variant":
        lost = next((x for x in d.get("variants") or [] if x.get("id") == o["key"]), {}).get("value_lost_pct")
        return None if not isinstance(lost, (int, float)) else 0 if lost <= VARIANT_LOSS_PCT else 1
    ins = [r for r in d.get("inputs") or [] if r.get("status") == "computed"]
    return None if not ins else 0 if any(r.get("supported") for r in ins) else 1


def _analysis_hashes(run_dir) -> dict:
    return {rel: _sha(open(os.path.join(run_dir, rel), "rb").read()) for rel in SCORED
            if os.path.isfile(os.path.join(run_dir, rel))}


def standing(run_dir, wm) -> tuple[bool, list]:
    """Is the score pre-registered? The anchor is remote; the scored files are the anchored bytes; the anchor
    precedes every scored output and the engine rows; the spec and the engine did not change since."""
    why = [] if wm.get("preregistered") else [f"the anchor is {wm.get('anchor') or 'missing'}"]
    for rel, sha in (wm.get("sha256") or {}).items():
        try:
            if _sha(open(os.path.join(run_dir, WM, rel), "rb").read()) != sha:
                why.append(f"{rel} changed since the anchor")
        except OSError:
            why.append(f"{rel} is missing")
    if wm.get("outside_registration"):
        why.append(wm["outside_registration"])
    made = [os.path.getmtime(os.path.join(run_dir, r)) for r in SCORED + ENGINE_ROWS
            if os.path.isfile(os.path.join(run_dir, r))]
    if wm.get("anchored_at") and made and datetime.fromisoformat(wm["anchored_at"]).timestamp() > min(made):
        why.append("an analysis output or engine result predates the anchor")
    return not why, why


def score(run_dir, wm) -> dict:
    """Score every registered belief against the analysis files, from the anchored seed every time (a re-score never
    applies an update twice); write score.json, belief_table.yaml and the world-model section."""
    import yaml
    seed = _yaml(os.path.join(run_dir, WM, DB.SEED_REL))
    by = {o["topic"]: o for o in observables(_yaml(os.path.join(run_dir, "decision_spec.yaml")) or {})[0]}
    plan, src = _plan(run_dir)
    at, rows, table = _now(), [], []
    for b, t in zip(seed["decision_beliefs"], BT.decision_table(seed)):
        o = by.get(b["topic"])
        k = evaluate(o, run_dir, plan) if o else None
        held = [c["claim"].strip().lower() for c in b["claims"]].index(o["claims"][k].strip().lower()) \
            if k is not None else None
        lead = max(range(len(b["claims"])), key=lambda i: (b["claims"][i]["weight"], -i))
        outcome = "undecided" if held is None else "confirmed" if held == lead else "refuted"
        w = None if held is None else b["claims"][held]["weight"]
        rows.append({"id": b["id"], "topic": b["topic"], "decides": b["decides"], "claim": t["claim"], "p": t["p"],
                     "outcome": outcome, "held": None if held is None else b["claims"][held]["claim"],
                     "surprise_bits": None if w is None else -math.log2(w),       # of the claim that held
                     "lead_surprise_bits": BT.surprise(t["p"], outcome),          # binary, of the leading claim
                     "batch": wm.get("decision_id")})
        u = BT.update(t, outcome, at, observed=rows[-1]["held"] if outcome == "refuted" else None)
        u["prior_weights"] = u.pop("weights")
        table.append(u)
    refuted = sorted((r for r in rows if r["outcome"] == "refuted" and r["decides"]), key=lambda r: -r["surprise_bits"])
    dec = [r["surprise_bits"] for r in rows if r["surprise_bits"] is not None]
    pre, why = standing(run_dir, wm)
    rec = {"status": "scored", "at": at, "preregistered": pre, "not_preregistered_because": why, "plan_source": src,
           "analysis_sha256": _analysis_hashes(run_dir), "rows": rows, "scores": BT.scores(rows),
           "categorical_log2": -sum(dec) / len(dec) if dec else None, "calibration": BT.calibration(rows),
           "where_to_look": {"refuted": [r["id"] for r in refuted],
                             "nearest_even": [b["id"] for b in BT.next_simulation(table)][:3]},
           "counts": {o: sum(r["outcome"] == o for r in rows) for o in BT.OUTCOMES}}
    _put(run_dir, SCORE, json.dumps(rec, indent=1, default=str) + "\n")
    _put(run_dir, TABLE, yaml.safe_dump({"decision": wm.get("decision_id"), "beliefs": table}, sort_keys=False))
    _put(run_dir, SECTION, section(rec))
    return rec


def section(rec) -> str:
    """The world-model block the report and the critics read (rendered by the harness)."""
    anc = "pre-registered: anchored on the wm-trials branch before the analysis ran" if rec["preregistered"] \
        else f"NOT pre-registered ({'; '.join(rec['not_preregistered_because'])})"
    c, ids = rec["counts"], {r["id"]: r for r in rec["rows"]}
    L = ["## World model: beliefs before the run, and what the run showed (rendered by the harness)", "",
         f"The world model's seed task wrote these beliefs before the analysis; {anc}. Each belief's claims compete; "
         "the leading one is scored against the analysis outputs (world_model/score.json). Surprise is -log2 of the "
         "weight the claim that held had.", "",
         "| belief | topic | leading claim | p | outcome | claim that held | surprise (bits) |", "|---|---|---|---|---|---|---|"]
    L += [f"| {r['id']} | {r['topic']} | {r['claim']} | {r['p']:.2f} | {r['outcome']} | {r['held'] or ''} | "
          f"{'' if r['surprise_bits'] is None else format(r['surprise_bits'], '.2f')} |" for r in rec["rows"]]
    L += ["", f"Decided {c['confirmed'] + c['refuted']} of {len(rec['rows'])}: {c['confirmed']} confirmed, "
              f"{c['refuted']} refuted, {c['undecided']} undecided.", "", "**Where to look.**"]
    L += [f"- Refuted, decision-relevant: {i} ({ids[i]['topic']}): expected \"{ids[i]['claim']}\", the run shows "
          f"\"{ids[i]['held']}\". Check whether the engine, an input or the belief is wrong."
          for i in rec["where_to_look"]["refuted"]] or ["- No decision-relevant belief was refuted."]
    return "\n".join(L + [f"- Least settled after this run: {', '.join(rec['where_to_look']['nearest_even']) or 'none'}."]) + "\n"


def score_run(m) -> str:
    """The SCORE state's work, and the refresh's: fail soft, recorded."""
    wm = m.st.world_model
    try:
        rec = score(m.run_dir, wm) if wm.get("status") == "ok" else \
            {"status": "not_scored", "at": _now(), "why": f"no registered beliefs ({wm.get('status')})"}
    except Exception as e:  # noqa: BLE001 -- fail soft
        rec = {"status": "error", "at": _now(), "why": f"{type(e).__name__}: {e}"[:400]}
    if rec["status"] != "scored":
        _put(m.run_dir, SCORE, json.dumps(rec, indent=1) + "\n")      # Machine._world_model catches a failure here
    wm.update(scored=rec["status"] == "scored", counts=rec.get("counts"), score_preregistered=rec.get("preregistered"))
    return f"world model: {rec['status']}" + (f" {rec['counts']}" if rec.get("counts") else "")


def refresh(m) -> str | None:
    """Before CRITIQUE and WRITE: score again when the analysis files changed since the last score (the fix loop
    rebuilds them without passing through ANALYZE)."""
    if m.st.world_model.get("status") != "ok":
        return None
    p = os.path.join(m.run_dir, SCORE)
    old = json.load(open(p)).get("analysis_sha256") if os.path.isfile(p) else None
    return None if old == _analysis_hashes(m.run_dir) else score_run(m)


# ------------------------------------------------------------------ self-test
def _fake_seed(log, bad=None):
    """A seed task: one belief per topic with the topic's claims, the first claim leading."""
    from v2_tasks import TaskResult

    def fn(run_dir, ctx, feedback, cap):
        import yaml
        log.append(cap)
        if bad == "crash":
            raise RuntimeError("seed session crashed")
        run = ctx["decision_run"]
        bs = [{"id": f"D{k + 1}", "topic": t, "question": run["topic_help"][t], "decides": True, "layer": "engine",
               "claims": [{"claim": c, "weight": w} for c, w in zip(run["claims"][t], (
                   [0.7, 0.3] if len(run["claims"][t]) == 2 else [0.6, 0.25, 0.15]))], "why_decides": "it sets the plan",
               "basis": [{"mechanism": "the solver picks the package with the most benefit per dollar"}]}
              for k, t in enumerate(run["topics"])]
        if bad == "digits":
            bs[0]["why_decides"] = "about 1050 people"
        with open(os.path.join(run_dir, WMT.SEED_FILE), "w") as f:
            yaml.safe_dump({"decision_beliefs": bs, "notes": "toy"}, f)
        return TaskResult(True, {"ok": True}, 0.25, "fake seed")
    return fn


def _norm(x):           # wall-clock fields, which differ between any two runs of the same analysis
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items() if k not in ("wall_s", "seconds")}
    return [_norm(v) for v in x] if isinstance(x, list) else x


def _tree(d) -> dict:
    """sha256 of every analysis file; JSON(L) normalised for wall-clock fields and worker row order."""
    out = {}
    for dp, _, fn in os.walk(os.path.join(d, "analysis")):
        for f in fn:
            raw = open(os.path.join(dp, f), "rb").read()
            if f.endswith(".jsonl"):
                raw = "\n".join(sorted(json.dumps(_norm(json.loads(x)), sort_keys=True)
                                       for x in raw.decode().splitlines() if x.strip())).encode()
            elif f.endswith(".json"):
                raw = json.dumps(_norm(json.loads(raw)), sort_keys=True).encode()
            out[os.path.relpath(os.path.join(dp, f), d)] = _sha(raw)
    return out


def _self_test() -> int:
    import shutil
    import tempfile
    import analysis_backend as AB
    import stage_machine as SM
    import v2_sdk_tasks as SDK
    from v2_tasks import StubTasks
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    with tempfile.TemporaryDirectory() as td:
        repo, bare = WMT._toy_repo(os.path.join(td, "toyrepo")), os.path.join(td, "noremote")
        subprocess.run(["git", "init", "-q", bare], check=True, capture_output=True)    # a repository, no remote

        def run(name, seed=None, wm_on=True, repo_=repo, stop_after=None):
            d = os.path.join(td, name)
            os.makedirs(d)
            AB.write_toy_units(os.path.join(d, "units.csv"), n=12)
            spec = dict(AB.toy_spec("units.csv"))
            spec.pop("engine")
            st = SM.init_run(d, "toy question", 2, "none", "v2", None, derived_inputs=False, world_model=wm_on)
            T = StubTasks({"spec": spec, "settings": AB.toy_settings(draws=24, points=16)})
            if seed is not None:
                T.world_model_seed = seed

            def machine(state):
                m = SM.Machine(d, T, state, backend=AB.backend("spike"), log=lambda *a: None, cross_engine_check=False)
                m.wm_repo = repo_
                return m
            m = machine(st)
            while stop_after and m.st.state != stop_after:
                m.step()
            if stop_after:
                m.step()
                m = machine(None)                       # resumed from run_state.json
            return d, m.run(), SM.load_state(d)

        def j(d, rel):
            return json.load(open(os.path.join(d, rel)))

        calls = []
        d1, s1, st1 = run("wm", _fake_seed(calls))
        hist = [h["to"] for h in st1.history if "to" in h]
        ok(s1 == "completed" and hist[2:8] == ["ENGINE_CHOICE", "PREREGISTER", "CONFIGURE", "ANALYZE", "SCORE",
                                               "CRITIQUE"], f"W1 PREREGISTER before CONFIGURE, SCORE before CRITIQUE {hist}")
        pr, sc = j(d1, PREREG), j(d1, SCORE)
        held = subprocess.run(["git", "-C", repo, "ls-tree", "-r", "--name-only", "origin/wm-trials"],
                              capture_output=True, text=True).stdout
        ok(pr["status"] == "ok" and pr["anchor"]["kind"] == "remote" and pr["anchor"]["anchored_at"]
           and st1.world_model["preregistered"] is True and len(calls) == 1
           and f"trials/decisions/{pr['decision_id']}/outcome_map.yaml" in held,
           f"W2 one seed session; seed and outcome map anchored on the remote's wm-trials ({pr['anchor'].get('kind')})")
        rep = open(os.path.join(d1, "report.md")).read()
        ok(sc["status"] == "scored" and sc["preregistered"] and sc["counts"]["confirmed"] + sc["counts"]["refuted"] >= 2
           and os.path.isfile(os.path.join(d1, TABLE)) and st1.world_model.get("scored") is True
           and "## World model" in rep and "pre-registered: anchored" in rep and "Where to look" in rep,
           f"W3 scored, belief table written, section in the report ({sc.get('counts')})")
        ok(st1.journal[JOURNAL_KEY]["usd"] == 0.25 and st1.spent_usd >= 0.25, "W4 the seed's cost is in the run's spend")

        d2, s2, _ = run("noremote_run", _fake_seed([]), repo_=bare)
        pr2 = j(d2, PREREG)
        ok(s2 == "completed" and pr2["anchor"]["kind"] == "local_only" and pr2["preregistered"] is False
           and "NOT pre-registered" in open(os.path.join(d2, SECTION)).read() and j(d2, SCORE)["status"] == "scored",
           f"W5 no remote: a local anchor marked not pre-registered; scored; completes ({pr2['anchor'].get('why')})")
        d3, s3, _ = run("crash", _fake_seed([], bad="crash"))
        ok(s3 == "completed" and j(d3, PREREG)["status"] == "seed_failed" and j(d3, SCORE)["status"] == "not_scored"
           and "## World model" not in open(os.path.join(d3, "report.md")).read(),
           f"W6 a seed failure is recorded and the run goes on ({s3})")
        d4, s4, _ = run("digits", _fake_seed([], bad="digits"))
        a4 = j(d4, PREREG)["anchor"]
        ok(s4 == "completed" and a4["kind"] == "local_only" and "publish check" in a4["why"],
           "W7 a seed holding a number is never pushed: local anchor, recorded")
        calls5 = []
        d5, s5, _ = run("resume", _fake_seed(calls5), stop_after="PREREGISTER")
        ok(s5 == "completed" and len(calls5) == 1 and j(d5, SCORE)["status"] == "scored",
           f"W8 a run resumed after PREREGISTER does not pay for the seed twice ({len(calls5)} sessions)")
        d6, s6, st6 = run("off", None, wm_on=False)
        t1, t6 = _tree(d1), _tree(d6)
        ok(s6 == "completed" and "PREREGISTER" not in [h.get("to") for h in st6.history] and t1 and t1 == t6,
           f"W9 every analysis file is identical with and without the stages, byte for byte after normalising "
           f"wall-clock fields and worker row order ({len(t1)} files; differing "
           f"{sorted(k for k in set(t1) | set(t6) if t1.get(k) != t6.get(k))[:5]})")

        spec = dict(AB.toy_spec("units.csv"), analysis={"decision_tests": [{"id": "DT1"}],
                                                        "value_of_information": {"enabled": False}})
        obs = observables(spec)[0]
        ok([o["topic"] for o in obs] == ["test_DT1", "departure", "share_a", "share_b"],
           f"W10 the topics come from the run's spec ({[o['topic'] for o in obs]})")
        seed = {"decision_beliefs": [{"id": "D1", "topic": "test_DT1", "question": "q", "decides": True,
                                      "layer": "engine", "basis": [{"mechanism": "m"}], "claims": [
                                          {"claim": "it does", "weight": 0.5}, {"claim": "it does not", "weight": 0.5}]}]}
        ok(any("topic's claims" in x for x in BT.decision_seed_problems(seed, {
            "topics": ["test_DT1"], "beliefs": {"min": 1}, "claims": {"test_DT1": obs[0]["claims"]}})),
           "W11 a belief whose claims are not its topic's set is refused")
        mk = strict_mask({"cap USD9876": "a 12,5 share", "id": "DT1", "years": [2026, 7]}, frozenset({"DT1"}))
        ok(mk == {"cap <value>": "a <value>,<value> share", "id": "DT1", "years": [2026, "<value>"]}
           and input_leaks({"x": "USD9876"}, frozenset()) == ["USD9876"],
           f"W12 the seed input is masked in keys and glued digits, and checked whole ({mk})")
    rd = tempfile.mkdtemp(dir=os.path.join(REPO, ".claude"))        # a run inside the repository's readable tree
    try:
        open(os.path.join(rd, "run_state.json"), "w").write("{}")
        sd = os.path.join(rd, SEED_DIR)
        os.makedirs(sd)
        ok(SDK._seed_read_denied("Read", {"file_path": os.path.join(rd, "decision_spec.yaml")}, sd) is not None
           and SDK._seed_read_denied("Read", {"file_path": os.path.join(sd, "_task_input.json")}, sd) is None,
           "W13 the seed task cannot read the run it belongs to, even under the repository's .claude/")
    finally:
        shutil.rmtree(rd)
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:] == ["--self-test"] else (print(__doc__) or 2))
