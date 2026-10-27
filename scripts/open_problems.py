#!/usr/bin/env python3
"""Phase 32 α/β: the open-problem list, the lead's round plan, and fix checks.

E7 attempt 6 got down to 6 blocking findings by round 4 and then diverged:
moderate findings carried from rounds 1-2 were re-sent with each patch but
never got a real turn, then all crossed the persistence rule in round 5; a
round-6 fix created three new blocking findings that nobody checked before
the round closed; findings that could not be fixed in-run had no exit left.

α  The gate (validate_critique_yaml.py) writes `open_problems.json` / `.md`
   after every in-round gate run: every open HIGH and MEDIUM, with the rounds
   left before a MEDIUM becomes blocking. Before the next round's work the
   lead writes `round_plan.yaml` giving each one an action:

     round: 5
     items:
       R-005: {action: fix, stage: ANALYZE}
       R-008: {action: rethink, stage: MODEL}
       R-017: {action: declare, reason: "no SMC funder map exists ..."}
       P-007: {action: after_write}          # target_stage WRITE only
       D-023: {action: defer, reason: "...", until_round: 7}
       "REG-*": {action: fix, stage: MODEL}  # patterns only for gate items

   The harness refuses planner / data-agent / modeler / analyst spawns until
   the plan covers the latest list, and refuses the round's critique until
   every fix / rethink item was handed to its stage's agent.

β  Fix check: after the fixes and before the round's full critique, each
   critic that raised a planned item re-checks only those items and what the
   fix changed, writing `fix_check_r<N>_<critic>.yaml`:

     round: 5
     reviewer: critique-redteam
     checks:
       - {id: R-005, verdict: fixed | partly | not_fixed, evidence: "..."}
     new_problems:
       - {id: R-042, severity: HIGH, target_stage: MODEL, claim: "..."}

   Anything not fixed, and any new HIGH, goes back to its stage's agent once
   before the full critique (skipped when the budget cannot cover it).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import sys
import tempfile

import yaml

STAGES = ("PLAN", "DATA", "MODEL", "ANALYZE", "WRITE")
STAGE_AGENT = {"PLAN": "planner", "DATA": "data-agent", "MODEL": "modeler",
               "ANALYZE": "analyst", "WRITE": "writer"}
ACTIONS = ("fix", "rethink", "declare", "after_write", "defer")
WORK_ACTIONS = ("fix", "rethink")
# Critics the lead can spawn for a fix check (cross-model is a script;
# sufficiency runs after the write-up).
FIX_CHECK_CRITICS = ("critique-domain", "critique-presentation",
                     "critique-redteam", "critique-methods")
VERDICTS = ("fixed", "partly", "not_fixed")
OPEN_JSON, OPEN_MD, PLAN_FILE = "open_problems.json", "open_problems.md", "round_plan.yaml"
FROZEN_FILE = "round_plan.frozen.json"   # Phase 32 γ: the actions as handed out (written by the hook)
MIN_REASON = 20

FIX_CHECK_NOTE = """

## Fix-check mode (Phase 32)

When your spawn prompt begins `This is a fix check after round N.`, you are
not doing a full critique. Re-check only the blocker ids the prompt lists
(yours, from round N) against the files the fix changed, and look for
anything the fix itself broke. Write ONLY `fix_check_rN_<your short name>.yaml`
(e.g. fix_check_r5_redteam.yaml); do not touch critique_*.yaml or .md:

```yaml
round: N
reviewer: <your agent name>
checks:
  - id: R-005
    verdict: fixed        # fixed | partly | not_fixed
    evidence: "results.md §2 now ...; still missing ..."
new_problems:             # problems the fix introduced; [] if none
  - id: R-042             # next id in your series; reuse it in the full critique
    severity: HIGH
    target_stage: MODEL
    claim: "..."
```

Be brief and concrete: the evidence line says what you read and what is
still missing. A fix check is a short session; the full critique follows.
"""


def _load_yaml(path: str):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _int(x, default=None):
    try:
        return int(x)
    except (TypeError, ValueError):
        return default


def _short(text, n=160) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def critic_short(reviewer: str) -> str:
    return reviewer.replace("critique-", "")


def fix_check_name(round_n: int, reviewer: str) -> str:
    return f"fix_check_r{round_n}_{critic_short(reviewer)}.yaml"


# ---------------------------------------------------------------------------
# α: the open-problem list (written by the gate)
# ---------------------------------------------------------------------------

def build(run_dir: str, round_n: int, max_rounds: int, critiques: dict,
          decision: dict, kind_rounds: dict, kind_thresholds: dict,
          blocker_threshold: int) -> dict:
    """Every open HIGH / MEDIUM after this gate run, soonest-blocking first.

    `kind_rounds` maps a validator finding kind to the number of distinct
    rounds it has appeared in (this one included)."""
    written = any(os.path.exists(os.path.join(run_dir, f)) for f in ("report.md", "manuscript.md"))
    items: dict[str, dict] = {}
    outside = set((decision or {}).get("outside_brief") or ())  # Phase 33: notes, not work
    outside |= set((decision or {}).get("resolved_by_test") or ())  # Phase 34: its test passes
    tests = (decision or {}).get("done_when_status") or {}
    for reviewer, doc in (critiques or {}).items():
        notes = {e.get("id"): e.get("notes") for e in (doc or {}).get("carried_forward") or []
                 if isinstance(e, dict)}
        for b in (doc or {}).get("blockers") or []:
            if not isinstance(b, dict) or b.get("resolved") is True or str(b.get("id")) in outside:
                continue
            sev = b.get("severity")
            if sev not in ("HIGH", "MEDIUM"):
                continue
            first = _int(b.get("first_seen_round"), round_n)
            stage = str(b.get("target_stage") or "").upper()
            if stage == "WRITE" and not written:
                blocks_in = None  # nothing to fix before a write-up exists (HIGH included)
            elif sev == "HIGH":
                blocks_in = 0
            else:
                blocks_in = max(0, blocker_threshold - (round_n - first))
            items[b["id"]] = {
                "id": b["id"], "source": reviewer, "severity": sev, "target_stage": stage,
                "category": b.get("category"), "first_seen_round": first,
                "blocks_in": blocks_in, "scope_declarable": True,
                # Phase 34: a runnable test is checked by the gate, not by a critic.
                "fix_check": reviewer in FIX_CHECK_CRITICS and not (tests.get(b["id"]) or {}).get("mechanical"),
                "claim": _short(b.get("claim")), "fix_requires": _short(b.get("fix_requires")),
                "status": _short(notes.get(b["id"]), 200),
                "test": _short((tests.get(b["id"]) or {}).get("detail") or "no done_when test", 200),
            }
    # Gate findings are grouped by kind (one item per kind, its RIG/REG/OBJ ids
    # listed), so 26 stale figures are one thing to plan, not 26.
    groups: dict[str, list] = {}
    for u in (decision or {}).get("unresolved_high") or []:
        if u.get("kind") == "critique_blocker":
            continue
        kind = str(u.get("kind") or u.get("id") or "gate_finding")
        if kind.endswith("_persistent") and kind[: -len("_persistent")] in items:
            items[kind[: -len("_persistent")]]["blocks_in"] = 0  # already escalated
            continue
        groups.setdefault(kind[: -len("_persistent")] if kind.endswith("_persistent") else kind, []).append(u)
    for kind, us in groups.items():
        u = us[0]
        items[kind] = {
            "id": kind, "source": u.get("reviewer") or "gate", "severity": "HIGH", "kind": kind,
            "gate_ids": [str(x.get("id")) for x in us if x.get("id")],
            "target_stage": str(u.get("target_stage") or "").upper(), "category": u.get("category"),
            "first_seen_round": min(_int(x.get("first_seen_round"), round_n) for x in us), "blocks_in": 0,
            "scope_declarable": all(x.get("scope_declarable") is not False
                                    and str(x.get("scope_declarable")) != "False" for x in us),
            "fix_check": False,
            "claim": _short((f"{len(us)} findings ({', '.join(str(x.get('id')) for x in us[:6])}"
                             f"{', …' if len(us) > 6 else ''}): " if len(us) > 1 else "") + str(u.get("claim"))),
            "fix_requires": "", "status": "",
        }
    for a in (decision or {}).get("accept_blockers") or []:
        iid = str(a.get("id") or f"ARTIFACT:{a.get('artifact') or a.get('kind')}")
        items[iid] = {
            "id": iid, "source": "gate", "severity": "HIGH", "kind": a.get("kind"),
            "target_stage": str(a.get("target_stage") or a.get("stage") or "MODEL").upper(),
            "first_seen_round": round_n, "blocks_in": 0, "scope_declarable": False,
            "fix_check": False, "claim": _short(a.get("claim") or a.get("artifact")),
            "fix_requires": "", "status": "",
        }
    counts: dict[str, list] = {}
    for v in (decision or {}).get("rigor_violations") or []:
        k = v.get("kind")
        if v.get("severity") == "MEDIUM" and k in (kind_thresholds or {}):
            counts.setdefault(k, []).append(v)
    for k, vs in counts.items():
        if k in items:  # already blocking (escalated to its _persistent form)
            continue
        seen = int((kind_rounds or {}).get(k, 1))
        items[k] = {
            "id": k, "source": "gate", "severity": "MEDIUM", "kind": k,
            "target_stage": str(vs[0].get("target_stage") or "MODEL").upper(),
            "first_seen_round": None, "blocks_in": max(0, kind_thresholds[k] - seen),
            "scope_declarable": True, "fix_check": False,
            "claim": _short(f"{len(vs)} finding(s): {vs[0].get('claim')}"), "fix_requires": "", "status": "",
        }
    order = sorted(items.values(), key=lambda i: (
        99 if i["blocks_in"] is None else i["blocks_in"], i["severity"] != "HIGH", i["id"]))
    return {"round": round_n, "max_rounds": max_rounds, "generated_by": "scripts/open_problems.py",
            "items": order, "outside_brief": sorted(set((decision or {}).get("outside_brief") or ())),
            "resolved_by_test": sorted((decision or {}).get("resolved_by_test") or ())}


def render_md(doc: dict) -> str:
    n, items = doc["round"], doc["items"]
    blocking = sum(1 for i in items if i["blocks_in"] == 0)
    soon = sum(1 for i in items if i["blocks_in"] == 1)
    out = [f"# Open problems after the round {n} gate", "",
           f"{len(items)} open: {blocking} blocking now, {soon} that become blocking at the next gate "
           f"if not fixed. Written by the gate; do not edit.", "",
           f"Before the next round's work, write `{PLAN_FILE}` (round: {n}) giving every id below an "
           "action: fix or rethink (with the stage that does it), declare (cannot be fixed with the data "
           "or tools available; with the reason), after_write (write-up items only), or defer (with the "
           "reason; not for blocking items or ones that block at the next gate). Decide early what cannot "
           "be fixed: declaring in round 1-2 keeps the rounds for what can.", "",
           *([f"Outside the decision brief (notes for the next run, not work for this one): "
              f"{', '.join(doc['outside_brief'])}.", ""] if doc.get("outside_brief") else []),
           *([f"Closed by their done_when tests this gate: {', '.join(doc['resolved_by_test'])}.", ""]
             if doc.get("resolved_by_test") else []),
           "Each critic finding's `done_when` test is shown as it stands now; it is fixed when the test "
           "passes (`python3 scripts/done_when.py check <run_dir> <ID>`).", "",
           "| id | from | severity | stage | open since | blocks in | latest status | fix needs | test now |",
           "|---|---|---|---|---|---|---|---|---|"]
    for i in items:
        b = "now" if i["blocks_in"] == 0 else ("after write-up" if i["blocks_in"] is None
                                               else f"{i['blocks_in']} round(s)")
        cell = lambda s: str(s or "").replace("|", "/")  # noqa: E731
        out.append(f"| {i['id']} | {i['source']} | {i['severity']} | {i['target_stage']} | "
                   f"{'r' + str(i['first_seen_round']) if i.get('first_seen_round') else '-'} | {b} | "
                   f"{cell(i.get('status') or i['claim'])} | {cell(i.get('fix_requires'))} | "
                   f"{cell(i.get('test') or '-')} |")
    return "\n".join(out) + "\n"


def write(run_dir: str, doc: dict) -> None:
    for name, text in ((OPEN_JSON, json.dumps(doc, indent=1)), (OPEN_MD, render_md(doc))):
        tmp = os.path.join(run_dir, name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, os.path.join(run_dir, name))


def load_open(run_dir: str) -> dict | None:
    try:
        with open(os.path.join(run_dir, OPEN_JSON), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) and isinstance(doc.get("items"), list) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# α: the round plan (written by the lead)
# ---------------------------------------------------------------------------

def _plan(run_dir: str):
    """(entries, error)."""
    path = os.path.join(run_dir, PLAN_FILE)
    if not os.path.exists(path):
        return None, f"{PLAN_FILE} does not exist"
    try:
        plan = _load_yaml(path)
    except (yaml.YAMLError, OSError) as e:
        return None, f"{PLAN_FILE} does not parse: {_short(e, 120)}"
    if not isinstance(plan, dict) or not isinstance(plan.get("items"), dict):
        return None, f"{PLAN_FILE} needs `round:` and an `items:` mapping of id -> action"
    return plan, None


def _resolve(doc: dict, plan: dict):
    """{item id: (key, entry)} plus errors for bad pattern keys."""
    errors, out = [], {}
    by_id = {i["id"]: i for i in doc["items"]}
    entries = plan["items"]
    for key in entries:
        if "*" in str(key):
            hit = [i for i in by_id if fnmatch.fnmatch(i, str(key))]
            if not hit:
                errors.append(f"pattern {key} matches no open item")
            elif any(by_id[i]["source"].startswith("critique-") for i in hit):
                errors.append(f"pattern {key} matches critic findings; give those by id")
    for iid in by_id:
        if iid in entries:
            out[iid] = (iid, entries[iid])
            continue
        for key in entries:
            if "*" in str(key) and fnmatch.fnmatch(iid, str(key)) \
                    and not by_id[iid]["source"].startswith("critique-"):
                out[iid] = (key, entries[key])
                break
    return out, errors


def check_plan(run_dir: str) -> list[str]:
    """Why the plan does not cover the latest open-problem list ([] = it does,
    or there is no list)."""
    doc = load_open(run_dir)
    if doc is None:
        return []
    plan, err = _plan(run_dir)
    if err:
        return [err]
    errors = []
    if _int(plan.get("round")) != doc["round"]:
        errors.append(f"{PLAN_FILE} is for round {plan.get('round')}; the latest gate is round {doc['round']}")
    resolved, perr = _resolve(doc, plan)
    errors += perr
    missing = [i["id"] for i in doc["items"] if i["id"] not in resolved]
    if missing:
        errors.append("no action for: " + ", ".join(missing))
    frozen = load_frozen(run_dir)
    if frozen is not None and _int(frozen.get("round")) == doc["round"]:
        for iid, was in (frozen.get("items") or {}).items():
            if iid not in resolved:
                continue
            e = resolved[iid][1]
            e = e if isinstance(e, dict) else {"action": e}
            now = {"action": e.get("action"), "stage": str(e.get("stage") or "").upper() or None}
            if now != was:
                errors.append(f"{iid}: was planned as {was['action']}{' (' + was['stage'] + ')' if was.get('stage') else ''} "
                              f"when the round's work was handed out; it stays that way this round (a fix that "
                              f"did not work stays open for the next round's plan)")
    for i in doc["items"]:
        if i["id"] not in resolved:
            continue
        key, e = resolved[i["id"]]
        e = e if isinstance(e, dict) else {"action": e}
        act, stage, reason = e.get("action"), str(e.get("stage") or "").upper(), str(e.get("reason") or "")
        if act not in ACTIONS:
            errors.append(f"{i['id']}: action {act!r} is not one of {', '.join(ACTIONS)}")
        elif act in WORK_ACTIONS and stage not in STAGES:
            errors.append(f"{i['id']}: {act} needs stage: one of {', '.join(STAGES)}")
        elif act in ("declare", "defer") and len(reason.strip()) < MIN_REASON:
            errors.append(f"{i['id']}: {act} needs a reason")
        elif act == "after_write" and i["target_stage"] != "WRITE":
            errors.append(f"{i['id']}: after_write is only for write-up findings (this one is {i['target_stage']})")
        elif act == "defer" and (i["blocks_in"] is not None and i["blocks_in"] <= 1):
            errors.append(f"{i['id']}: cannot be deferred: it is blocking now or blocks at the next gate")
        elif act == "declare" and not i.get("scope_declarable", True):
            errors.append(f"{i['id']}: cannot be scope-declared ({i.get('kind')}); it has to be fixed")
    return errors


def load_frozen(run_dir: str) -> dict | None:
    try:
        with open(os.path.join(run_dir, FROZEN_FILE), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and isinstance(d.get("items"), dict) else None
    except (OSError, ValueError):
        return None


def round_start(run_dir: str) -> float:
    """When this round's work began: the hand-out snapshot for the current
    list's round, else the plan's last change. Adding an item to the plan
    later does not make earlier hand-outs and fix checks count for nothing."""
    doc = load_open(run_dir)
    fz = load_frozen(run_dir)
    if doc is not None and fz is not None and _int(fz.get("round")) == doc["round"]:
        return os.path.getmtime(os.path.join(run_dir, FROZEN_FILE))
    return os.path.getmtime(os.path.join(run_dir, PLAN_FILE))


def freeze(run_dir: str) -> bool:
    """Phase 32 γ: record each item's action and stage when the round's work
    is first handed out. Later the plan may add items (a same-round gate rerun
    can list new ones) but not change these (check_plan). Returns True when a
    snapshot was written."""
    doc = load_open(run_dir)
    plan, err = _plan(run_dir)
    if doc is None or err or check_plan(run_dir):
        return False
    old = load_frozen(run_dir)
    if old is not None and _int(old.get("round")) == doc["round"]:
        return False
    resolved, _ = _resolve(doc, plan)
    items = {}
    for iid, (_, e) in resolved.items():
        e = e if isinstance(e, dict) else {"action": e}
        items[iid] = {"action": e.get("action"), "stage": str(e.get("stage") or "").upper() or None}
    tmp = os.path.join(run_dir, FROZEN_FILE + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"round": doc["round"], "items": items}, f, indent=1)
    os.replace(tmp, os.path.join(run_dir, FROZEN_FILE))
    return True


def planned_work(run_dir: str) -> list[dict]:
    """fix / rethink items: [{id, stage, agent, source, fix_check}]."""
    doc = load_open(run_dir)
    plan, err = _plan(run_dir)
    if doc is None or err:
        return []
    resolved, _ = _resolve(doc, plan)
    out = []
    for i in doc["items"]:
        if i["id"] not in resolved:
            continue
        e = resolved[i["id"]][1]
        e = e if isinstance(e, dict) else {"action": e}
        stage = str(e.get("stage") or "").upper()
        if e.get("action") in WORK_ACTIONS and stage in STAGES:
            out.append({"id": i["id"], "stage": stage, "agent": STAGE_AGENT[stage],
                        "source": i["source"], "fix_check": bool(i.get("fix_check"))})
    return out


def mentioned(text: str, token: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(token) + r"(?![\w])", text or "") is not None


def mentions(text: str, run_dir: str) -> list[str]:
    """Open-item ids named in a spawn prompt (recorded in the trace)."""
    doc = load_open(run_dir)
    hit = {i["id"] for i in (doc or {}).get("items", [])
           if any(mentioned(text, t) for t in [i["id"], *(i.get("gate_ids") or [])])}
    extra = re.findall(r"(?<![\w-])[A-Z]{1,4}-\d{2,4}(?![\w])", text or "")
    return sorted(hit | set(extra))


def unsent(work: list[dict], spawns: list[tuple[str, set]]) -> list[str]:
    """Planned items never named in a spawn prompt to their stage's agent."""
    sent: dict[str, set] = {}
    for agent, ids in spawns:
        sent.setdefault(agent, set()).update(ids)
    return [f"{w['id']} -> {w['agent']}" for w in work if w["id"] not in sent.get(w["agent"], set())]


# ---------------------------------------------------------------------------
# β: fix checks (written by the critics)
# ---------------------------------------------------------------------------

def _verdict(v) -> str | None:
    """fixed | partly | not_fixed from the ways a critic writes it."""
    t = re.sub(r"[\s\-]+", "_", str(v or "").strip().lower())
    t = {"partial": "partly", "partially": "partly", "partially_fixed": "partly", "partly_fixed": "partly",
         "notfixed": "not_fixed", "unfixed": "not_fixed", "unresolved": "not_fixed", "open": "not_fixed",
         "resolved": "fixed", "done": "fixed"}.get(t, t)
    return t if t in VERDICTS else None


def _checks(fc) -> dict[str, dict]:
    """{id: {verdict, evidence}} from a list of entries, an id -> entry
    mapping, or an id -> verdict mapping (Haiku wrote the latter forms in the
    first real-SDK check)."""
    raw = fc.get("checks") if isinstance(fc, dict) else None
    out: dict[str, dict] = {}
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, dict) and c.get("id"):
                out[str(c["id"])] = c
    elif isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = v if isinstance(v, dict) else {"verdict": v}
    return out


def check_fix_checks(run_dir: str) -> tuple[list[str], dict[str, list[str]]]:
    """(missing, redo). `missing` names critics whose fix check is absent,
    stale (older than the plan), unparseable or incomplete. `redo` maps a
    stage agent to the ids it must take once more: items not fixed, and new
    HIGH problems the fix introduced."""
    doc = load_open(run_dir)
    work = [w for w in planned_work(run_dir) if w["fix_check"]]
    if doc is None or not work:
        return [], {}
    plan_mtime = round_start(run_dir)
    missing, redo = [], {}
    by_critic: dict[str, list[dict]] = {}
    for w in work:
        by_critic.setdefault(w["source"], []).append(w)
    for critic, ws in by_critic.items():
        path = os.path.join(run_dir, fix_check_name(doc["round"], critic))
        ids = ", ".join(sorted(w["id"] for w in ws))
        if not os.path.exists(path) or os.path.getmtime(path) < plan_mtime:
            missing.append(f"{critic}: {ids} ({os.path.basename(path)})")
            continue
        try:
            fc = _load_yaml(path) or {}
            checks = _checks(fc)
            news = [p for p in (fc.get("new_problems") or []) if isinstance(p, dict)] \
                if isinstance(fc, dict) else []
        except (yaml.YAMLError, OSError, AttributeError, TypeError) as e:
            missing.append(f"{critic}: {os.path.basename(path)} does not parse ({_short(e, 80)})")
            continue
        bad = [w["id"] for w in ws if _verdict((checks.get(w["id"]) or {}).get("verdict")) is None]
        if bad:
            found = "; ".join(f"{i}: {'no entry' if i not in checks else repr(checks[i].get('verdict'))}"
                              for i in bad)
            missing.append(f"{critic}: {os.path.basename(path)} needs `checks:` entries `- {{id: <id>, verdict: "
                           f"fixed | partly | not_fixed, evidence: ...}}`; found {found} "
                           f"(ids present: {', '.join(checks) or 'none'})")
            continue
        for w in ws:
            if _verdict(checks[w["id"]].get("verdict")) != "fixed":
                redo.setdefault(w["agent"], []).append(w["id"])
        for p in news:
            if p.get("severity") == "HIGH":
                stage = str(p.get("target_stage") or "MODEL").upper()
                redo.setdefault(STAGE_AGENT.get(stage, "modeler"), []).append(str(p.get("id") or "new problem"))
    return missing, redo


def fix_check_mtime(run_dir: str) -> float | None:
    doc = load_open(run_dir)
    if doc is None:
        return None
    times = [os.path.getmtime(os.path.join(run_dir, f)) for f in os.listdir(run_dir)
             if f.startswith(f"fix_check_r{doc['round']}_") and f.endswith(".yaml")]
    return max(times) if times else None


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    fails = []

    def ok(cond, msg):
        if not cond:
            fails.append(msg)

    def crit(blockers, cf=()):
        return {"blockers": blockers, "carried_forward": list(cf)}

    def blk(i, sev="MEDIUM", stage="ANALYZE", first=1, resolved=False):
        return {"id": i, "severity": sev, "target_stage": stage, "first_seen_round": first,
                "category": "METHODS", "claim": f"claim {i}", "fix_requires": f"fix {i}", "resolved": resolved}

    with tempfile.TemporaryDirectory() as d:
        critiques = {
            "critique-redteam": crit([blk("R-005", first=2), blk("R-008", stage="MODEL", first=1),
                                      blk("R-035", sev="HIGH", stage="DATA", first=4),
                                      blk("R-001", first=1, resolved=True), blk("R-009", sev="LOW")],
                                     [{"id": "R-005", "still_present": True, "notes": "Narrowed, still present."}]),
            "critique-presentation": crit([blk("P-007", stage="WRITE", first=1)]),
            "critique-crossmodel": crit([blk("X-001", sev="HIGH", stage="MODEL", first=4)]),
        }
        decision = {
            "unresolved_high": [
                {"reviewer": "critique-redteam", "id": "R-035", "kind": "critique_blocker"},
                {"reviewer": "rigor-artifacts", "id": "RIG-001", "kind": "ci_degenerate",
                 "target_stage": "MODEL", "first_seen_round": "1", "scope_declarable": False, "claim": "zero width"},
                {"reviewer": "rigor-artifacts", "id": "RIG-002", "kind": "R-008_persistent",
                 "target_stage": "MODEL", "claim": "R-008 persistent"},
                {"reviewer": "registry", "id": "REG-001", "kind": "param_unregistered", "claim": "x"},
                {"reviewer": "registry", "id": "REG-002", "kind": "param_unregistered", "claim": "y"},
            ],
            "rigor_violations": [{"kind": "weakly_identified_parameters", "severity": "MEDIUM", "claim": "beta"},
                                 {"kind": "figure_label_small", "severity": "MEDIUM", "claim": "z"}],
        }
        doc = build(d, 5, 8, critiques, decision, {"weakly_identified_parameters": 2},
                    {"weakly_identified_parameters": 3}, 4)
        by = {i["id"]: i for i in doc["items"]}
        ok(set(by) == {"R-005", "R-008", "R-035", "P-007", "X-001", "ci_degenerate", "param_unregistered",
                       "weakly_identified_parameters"}, f"OP1: open items {sorted(by)}")
        ok(by["param_unregistered"]["gate_ids"] == ["REG-001", "REG-002"]
           and by["param_unregistered"]["claim"].startswith("2 findings (REG-001, REG-002)"),
           "OP1b: gate findings grouped by kind, ids kept")
        ok(by["R-005"]["blocks_in"] == 1 and by["R-005"]["status"] == "Narrowed, still present.",
           "OP2: MEDIUM first seen r2 at r5 blocks in 1 round; carried-forward note kept")
        ok(by["R-008"]["blocks_in"] == 0, "OP3: an escalated MEDIUM (its _persistent gate item) is blocking now")
        ok(by["P-007"]["blocks_in"] is None, "OP4: write-up finding does not escalate before a draft exists")
        ok(by["weakly_identified_parameters"]["blocks_in"] == 1, "OP5: validator kind seen 2 of 3 rounds")
        ok(by["ci_degenerate"]["scope_declarable"] is False and not by["X-001"]["fix_check"],
           "OP6: non-declarable gate item; cross-model findings need no spawned fix check")
        ok(doc["items"][0]["blocks_in"] == 0 and doc["items"][-1]["id"] == "P-007", "OP7: soonest-blocking first")
        write(d, doc)
        md = open(os.path.join(d, OPEN_MD)).read()
        ok("| R-005 | critique-redteam | MEDIUM | ANALYZE | r2 | 1 round(s) |" in md, "OP8: markdown row")

        def plan(items, rnd=5):
            with open(os.path.join(d, PLAN_FILE), "w") as f:
                yaml.safe_dump({"round": rnd, "items": items}, f)

        good = {"R-005": {"action": "fix", "stage": "ANALYZE"}, "R-008": {"action": "rethink", "stage": "MODEL"},
                "R-035": {"action": "fix", "stage": "DATA"}, "P-007": {"action": "after_write"},
                "X-001": {"action": "fix", "stage": "MODEL"}, "ci_degenerate": {"action": "fix", "stage": "MODEL"},
                "param_*": {"action": "fix", "stage": "MODEL"},
                "weakly_identified_parameters": {"action": "declare",
                                                 "reason": "two parameters share one target; no data separates them"}}
        ok(check_plan(d) == [f"{PLAN_FILE} does not exist"], "PL1: missing plan")
        plan(good)
        ok(check_plan(d) == [], f"PL2: complete plan passes: {check_plan(d)}")
        plan(good, rnd=4)
        ok(any("round 4" in e for e in check_plan(d)), "PL3: stale round")
        plan({k: v for k, v in good.items() if k != "R-005"})
        ok(any("no action for: R-005" in e for e in check_plan(d)), "PL4: uncovered item")
        plan({**good, "R-*": {"action": "fix", "stage": "MODEL"}})
        ok(any("give those by id" in e for e in check_plan(d)), "PL5: pattern over critic findings refused")
        plan({**good, "R-005": {"action": "defer", "reason": "the analyst is busy with the rethink this round"}})
        ok(any("R-005: cannot be deferred" in e for e in check_plan(d)), "PL6: defer refused one round from blocking")
        plan({**good, "ci_degenerate": {"action": "declare", "reason": "constraint-fixed output, deterministic"}})
        ok(any("ci_degenerate: cannot be scope-declared" in e for e in check_plan(d)), "PL7: non-declarable")
        plan({**good, "R-035": {"action": "after_write"}})
        ok(any("R-035: after_write" in e for e in check_plan(d)), "PL8: after_write only for WRITE items")
        plan({**good, "R-008": {"action": "rethink"}})
        ok(any("R-008: rethink needs stage" in e for e in check_plan(d)), "PL9: work action needs a stage")
        with open(os.path.join(d, PLAN_FILE), "w") as f:
            f.write("round: 5\nitems: [unclosed\n")
        ok(check_plan(d) and "does not parse" in check_plan(d)[0], "PL10: unparseable plan fails closed")

        plan(good)
        work = planned_work(d)
        ok({w["id"] for w in work} == {"R-005", "R-008", "R-035", "X-001", "ci_degenerate", "param_unregistered"},
           f"PW1: planned work {sorted(w['id'] for w in work)}")
        ok(mentions("Fix R-005 and R-008 now; also ci_degenerate.", d) == ["R-005", "R-008", "ci_degenerate"],
           "PW2: ids found in a prompt")
        ok(not mentioned("R-0055", "R-005") and mentioned("(R-005)", "R-005"), "PW3: id boundaries")
        ok("param_unregistered" in mentions("Register REG-002 in citations.md", d),
           "PW3b: a grouped gate item counts as named when one of its gate ids is")
        spawns = [("analyst", {"R-005"}), ("modeler", {"R-008", "X-001", "ci_degenerate", "param_unregistered"}),
                  ("analyst", {"R-035"})]
        ok(unsent(work, spawns) == ["R-035 -> data-agent"], f"PW4: unsent {unsent(work, spawns)}")

        missing, redo = check_fix_checks(d)
        ok(len(missing) == 1 and "critique-redteam: R-005, R-008, R-035" in missing[0],
           f"FC1: fix check missing: {missing}")
        import time
        time.sleep(0.01)
        fc = os.path.join(d, fix_check_name(5, "critique-redteam"))
        with open(fc, "w") as f:
            yaml.safe_dump({"round": 5, "reviewer": "critique-redteam",
                            "checks": [{"id": "R-005", "verdict": "fixed"}, {"id": "R-008", "verdict": "partly"}]}, f)
        missing, _ = check_fix_checks(d)
        ok(missing and "R-035: no entry" in missing[0], f"FC2: incomplete fix check says what it found: {missing}")
        with open(fc, "w") as f:
            yaml.safe_dump({"round": 5, "reviewer": "critique-redteam",
                            "checks": [{"id": "R-005", "verdict": "fixed"}, {"id": "R-008", "verdict": "partly"},
                                       {"id": "R-035", "verdict": "fixed"}],
                            "new_problems": [{"id": "R-041", "severity": "HIGH", "target_stage": "MODEL"},
                                             {"id": "R-042", "severity": "MEDIUM", "target_stage": "ANALYZE"}]}, f)
        missing, redo = check_fix_checks(d)
        ok(missing == [] and redo == {"modeler": ["R-008", "R-041"]}, f"FC3: redo {redo} missing {missing}")
        with open(fc, "w") as f:
            yaml.safe_dump({"round": 5, "checks": {"R-005": "Fixed", "R-008": {"verdict": "partially fixed"},
                                                   "R-035": "not fixed"}}, f)
        missing, redo = check_fix_checks(d)
        ok(missing == [] and redo == {"modeler": ["R-008"], "data-agent": ["R-035"]},
           f"FC3b: mapping forms and verdict spellings are read: {missing} {redo}")
        os.utime(fc, (1, 1))
        ok(check_fix_checks(d)[0], "FC4: fix check older than the plan is stale")
        with open(fc, "w") as f:
            f.write("checks: [unclosed\n")
        ok(check_fix_checks(d)[0] and "does not parse" in check_fix_checks(d)[0][0], "FC5: unparseable fails closed")
        ok("fix_check_rN_" in FIX_CHECK_NOTE and fix_check_name(5, "critique-redteam") == "fix_check_r5_redteam.yaml",
           "FC6: file name matches the critic note")

    with tempfile.TemporaryDirectory() as d:
        ok(check_plan(d) == [] and planned_work(d) == [] and check_fix_checks(d) == ([], {}),
           "OP9: no open-problem list, nothing required")

    if fails:
        for m in fails:
            print("FAIL:", m)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return _run_self_test()
    if len(sys.argv) >= 2 and os.path.isdir(sys.argv[1]):
        run_dir = sys.argv[1]
        errs = check_plan(run_dir)
        print("plan:", "OK" if not errs else "; ".join(errs))
        missing, redo = check_fix_checks(run_dir)
        print("fix checks:", "OK" if not missing else "; ".join(missing))
        if redo:
            print("redo:", json.dumps(redo))
        return 0 if not errs and not missing else 1
    print("usage: open_problems.py <run_dir> | --self-test", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
