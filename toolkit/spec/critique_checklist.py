#!/usr/bin/env python3
"""The critique checklist: five structural checks the domain and red-team critics answer every round (owner,
2026-11-23), and the two the harness answers itself when the spec lets it.

    python3 toolkit/spec/critique_checklist.py <decision_spec.yaml>     # prints the harness's answers
    python3 toolkit/spec/critique_checklist.py --self-test

An outside benchmark found frontier agents' models stopped at "good" for five reasons. Each is a principle in
.claude/orchestration/decision_design_principles.md (8-12) and an item here. A critique file of a checklist reviewer
(CHECKLIST_REVIEWERS) carries

    checklist:
      burn_in_drift: {answer: pass, reason: "<one line: what was checked, where>"}
      surveillance_ranking: {answer: finding, reason: "...", blockers: [D-004]}
      natural_history_tension: {answer: not_applicable, reason: "<why it cannot arise here>"}

answer: pass | finding | not_applicable, each with a non-empty reason. A finding names the blocker(s) in the same
file that state it: the checklist never adds a finding of its own, so a pass or a not_applicable answer costs no
round. A missing item, an unknown item or a malformed answer is a schema error (fail closed; the gate's schema
repair carries every one at once).

Harness-answered items (computed from the spec, deterministically; the critic does not answer them):
  comparator_symmetry     always, for a spec with arms (packages): an arm that lacks a background component (one the
                          comparator holds and another arm holds too, or, with an empty comparator, one every other
                          arm but this one holds), when no arm is that arm with the component added.
  holdout_shock_overlap   when the spec declares `periods: {holdout: [[start, stop], ...], shocks: [{id, window:
                          [start, stop], what}, ...]}`: a holdout window that overlaps a declared shock period.
                          Without both lists the critic answers it.
Both are also pre-sign checks (toolkit/spec/spec_critique.py), so the owner sees them on the sign-off sheet, where a
finding is fixed or declared in principle_departures. Nothing here names a disease, a place or an intervention.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CHECKLIST_REVIEWERS = ("critique-domain", "critique-redteam")
ANSWERS = ("pass", "finding", "not_applicable")
# item -> (principle number, the question the critic answers)
ITEMS = {
    "comparator_symmetry": (8, "Does every arm carry the background or standard-of-care components that the "
                               "comparator and the other arms carry, so arms differ only in what they add?"),
    "holdout_shock_overlap": (9, "Does any validation or holdout window overlap a shock, outbreak or "
                                 "non-pharmaceutical-intervention period that the calibration window does not share?"),
    "burn_in_drift": (10, "After burn-in, are the engine's settings at the start of the decision window (coverage, "
                          "immunity, transmission level, population) the nominal ones the spec declares, or did the "
                          "burn-in move one?"),
    "surveillance_ranking": (11, "Are units ranked or binned on reported notifications without correcting for "
                                 "reporting completeness, so that under-reporting rather than burden drives the ranking?"),
    "natural_history_tension": (12, "Does a reduced-form natural history sidestep a known tension that the decision "
                                    "turns on, such as detection rate against the share of subclinical disease?"),
}
HARNESS_ITEMS = ("comparator_symmetry", "holdout_shock_overlap")


def _ans(answer, reason, **kw):
    return dict({"answer": answer, "reason": reason, "by": "harness"}, **kw)


# --------------------------------------------------------------------------- comparator symmetry (principle 8)
def asymmetries(packages: dict, comparator: str) -> list[tuple[str, str]]:
    """(arm, component) pairs: the arm lacks a background component and no arm is it with the component added.
    `packages`: {name: set of components} as execution resolves them. Background: a component the comparator holds
    and at least one other arm holds; with an empty comparator, one every other non-empty arm but one holds (three
    or more arms). A no-action arm (no components) is never flagged."""
    comp = set(packages.get(comparator) or ())
    arms = {n: set(s) for n, s in packages.items() if n != comparator and s}
    have = {frozenset(s) for s in packages.values()}
    pool = set().union(*arms.values()) if arms else set()
    if comp:
        bg = {c for c in comp if any(c in s for s in arms.values())}
    else:
        bg = {c for c in pool if len(arms) >= 3 and sum(c in s for s in arms.values()) == len(arms) - 1}
    return sorted((n, c) for n, s in arms.items() for c in bg if c not in s and frozenset(s | {c}) not in have)


def _resolved(raw) -> dict:
    from toolkit.spec import spec_critique as SC
    return SC._resolved(raw)


def comparator_symmetry(raw) -> dict | None:
    """The harness's answer, or None when it cannot compute one (the critic answers then)."""
    if not isinstance(raw, dict):
        return None
    if isinstance(raw.get("deliverable"), dict) and raw["deliverable"].get("kind") == "estimates":
        return _ans("not_applicable", "an estimates deliverable compares no arms")
    try:
        pk = _resolved(raw)
    except Exception:  # noqa: BLE001 -- packages that cannot be resolved: the critic answers (spec_critique reports)
        return None
    cmp_ = str((raw.get("comparator") or {}).get("package"))
    if cmp_ not in pk:
        return None
    bad = asymmetries(pk, cmp_)
    if not bad:
        return _ans("pass", f"every arm that lacks a background component of the comparator `{cmp_}` or of the other "
                            "arms has a counterpart arm with it (computed from options.packages)")
    return _ans("finding", "arm(s) without a background component the other arms carry, and no arm with it: "
                + "; ".join(f"`{a}` lacks `{c}`" for a, c in bad), asymmetric=[list(x) for x in bad])


# --------------------------------------------------------------------------- holdout windows and shocks (principle 9)
def _window(w) -> tuple[float, float] | None:
    if not isinstance(w, (list, tuple)) or len(w) != 2 or any(isinstance(x, bool) for x in w):
        return None
    try:
        a, b = float(w[0]), float(w[1])
    except (TypeError, ValueError):
        return None
    return (a, b) if a == a and b == b and a < b else None


def period_problems(raw) -> list[str]:
    """Problems with the optional `periods` section (empty when it is absent or well formed)."""
    p = raw.get("periods") if isinstance(raw, dict) else None
    if p is None:
        return []
    if not isinstance(p, dict):
        return ["periods: a mapping {holdout: [[start, stop], ...], shocks: [{id, window, what}, ...]}"]
    bad = [f"periods: unknown key {k} (known: holdout, shocks)" for k in sorted(set(map(str, p)) - {"holdout", "shocks"})]
    h = p.get("holdout", [])
    if not isinstance(h, list):
        bad.append("periods.holdout: a list of [start, stop] windows")
    else:
        bad += [f"periods.holdout[{i}]: {w!r} is not [start, stop] with start < stop" for i, w in enumerate(h)
                if _window(w) is None]
    s = p.get("shocks", [])
    if not isinstance(s, list):
        bad.append("periods.shocks: a list of {id, window: [start, stop], what}")
    else:
        for i, e in enumerate(s):
            if not isinstance(e, dict) or not str(e.get("id") or "").strip() or _window(e.get("window")) is None \
                    or not str(e.get("what") or "").strip():
                bad.append(f"periods.shocks[{i}]: {e!r} needs an id, a window [start, stop] with start < stop and "
                           f"`what` (what happened)"[:300])
    return bad


def overlaps(raw) -> list[tuple[list, str]]:
    """(holdout window, shock id) pairs that overlap (open intervals: windows that only touch do not)."""
    p = raw.get("periods") or {}
    out = []
    for w in p.get("holdout") or []:
        hw = _window(w)
        for e in p.get("shocks") or []:
            sw = _window((e or {}).get("window")) if isinstance(e, dict) else None
            if hw and sw and hw[0] < sw[1] and sw[0] < hw[1]:
                out.append(([hw[0], hw[1]], str(e.get("id"))))
    return out


def holdout_shock_overlap(raw) -> dict | None:
    """The harness's answer when the spec declares both lists in `periods`; None otherwise (the critic answers)."""
    p = raw.get("periods") if isinstance(raw, dict) else None
    if not isinstance(p, dict) or "holdout" not in p or "shocks" not in p or period_problems(raw):
        return None
    if not p["holdout"]:
        return _ans("not_applicable", "the spec declares no holdout window (periods.holdout is empty)")
    hits = overlaps(raw)
    if not hits:
        return _ans("pass", f"none of the {len(p['holdout'])} holdout window(s) overlaps the {len(p['shocks'])} "
                            "declared shock period(s) (periods)")
    return _ans("finding", "holdout window(s) overlap declared shock periods: "
                + "; ".join(f"{w[0]:g}-{w[1]:g} overlaps `{s}`" for w, s in hits), overlaps=[[w, s] for w, s in hits])


def harness_answers(raw) -> dict:
    """{item: answer} for the items the harness computed on this spec; an item it could not compute is absent."""
    out = {}
    for item, fn in (("comparator_symmetry", comparator_symmetry), ("holdout_shock_overlap", holdout_shock_overlap)):
        try:
            a = fn(raw)
        except Exception:  # noqa: BLE001 -- a check that cannot run leaves the item to the critic
            a = None
        if a is not None:
            out[item] = a
    return out


def load_spec(run_dir: str):
    """The run's decision_spec.yaml, or None when it cannot be read (every item is then the critic's)."""
    import yaml
    try:
        with open(os.path.join(run_dir, "decision_spec.yaml"), encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except Exception:  # noqa: BLE001
        return None
    return raw if isinstance(raw, dict) else None


# --------------------------------------------------------------------------- the critique file's checklist
def critic_items(harness: dict | set | None) -> list[str]:
    return [i for i in ITEMS if i not in set(harness or ())]


def problems(checklist, ctx: str, harness, blocker_ids) -> list[str]:
    """Schema errors in a critique's `checklist` (empty = valid). `harness`: the items the harness answered (the
    critic's entry for one is not required and not read). `blocker_ids`: the ids of the file's blockers."""
    need = critic_items(harness)
    if checklist is None:
        return [f"{ctx}: missing required key 'checklist' (answer each of {', '.join(need)}: "
                f"{{answer: {' | '.join(ANSWERS)}, reason: ...}})"]
    if not isinstance(checklist, dict):
        return [f"{ctx}: checklist must be a mapping {{item: {{answer, reason}}}}"]
    bad = [f"{ctx}: checklist: unknown item {k!r} (items: {', '.join(ITEMS)})" for k in checklist if k not in ITEMS]
    ids = {str(b) for b in blocker_ids or ()}
    for item in need:
        e = checklist.get(item)
        if e is None:
            bad.append(f"{ctx}: checklist item '{item}' is missing (principle {ITEMS[item][0]}: {ITEMS[item][1]})")
            continue
        if not isinstance(e, dict):
            bad.append(f"{ctx}: checklist.{item} must be a mapping {{answer, reason}}")
            continue
        a = e.get("answer")
        if not (isinstance(a, str) and a in ANSWERS):
            bad.append(f"{ctx}: checklist.{item}.answer must be one of {' | '.join(ANSWERS)}, got {a!r}"[:300])
        if not (isinstance(e.get("reason"), str) and e["reason"].strip()):
            bad.append(f"{ctx}: checklist.{item} needs a non-empty reason"
                       + (" (not_applicable says why the check cannot arise here)" if a == "not_applicable" else ""))
        bl = e.get("blockers")
        if a == "finding":
            if not (isinstance(bl, list) and bl and all(isinstance(x, str) for x in bl)):
                bad.append(f"{ctx}: checklist.{item}: a finding names its blocker id(s) in this file (blockers: [...])")
            else:
                bad += [f"{ctx}: checklist.{item}: blocker {x} is not a blocker in this file" for x in bl if x not in ids]
        elif bl not in (None, []):
            bad.append(f"{ctx}: checklist.{item}: only a finding names blockers (answer is {a!r})"[:300])
    return bad


def record(critiques: dict, harness: dict) -> dict:
    """The gate record's checklist: {item: {principle, harness: answer | None, critics: {reviewer: answer}}}."""
    out = {}
    for item, (pr, _q) in ITEMS.items():
        cr = {}
        for reviewer, doc in sorted(critiques.items()):
            if reviewer not in CHECKLIST_REVIEWERS or item in harness:
                continue
            e = (doc.get("checklist") or {}).get(item) if isinstance(doc, dict) else None
            if isinstance(e, dict):
                cr[reviewer] = {k: e.get(k) for k in ("answer", "reason", "blockers") if e.get(k) is not None}
        out[item] = {"principle": pr, "harness": harness.get(item), "critics": cr}
    return out


def prompt_block(harness: dict) -> str:
    """The checklist text for a checklist reviewer's prompt: the items it answers and the harness's answers."""
    lines = ["Checklist (required in your file, every round): a mapping `checklist:` with one entry per item below,",
             "{answer: pass | finding | not_applicable, reason: \"<one line: what you checked and where, or why it "
             "cannot arise here>\"}.",
             "A finding also names the blocker(s) of this file that state it: blockers: [<id>, ...]. The checklist "
             "replaces prose on",
             "these questions; it adds no finding of its own. Raise a blocker only for a real problem that bears on "
             "the decision;",
             "pass and not_applicable cost nothing. A missing item or a not_applicable without a reason is a schema "
             "error."]
    for item in critic_items(harness):
        lines.append(f"  - {item} (principle {ITEMS[item][0]}): {ITEMS[item][1]}")
    if harness:
        lines.append("The harness computed these from decision_spec.yaml (leave them out of your checklist; they were "
                     "on the sign-off sheet, so do not raise a blocker on them unless the spec declares a departure "
                     "you can show is wrong):")
        for item, a in harness.items():
            lines.append(f"  - {item} (principle {ITEMS[item][0]}): {a['answer']}: {a['reason']}"[:600])
    return "\n".join(lines)


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        c or fails.append(m)

    def spec(packages, comparator, **kw):
        comps = sorted({c for v in packages.values() if isinstance(v, list) for c in v}) or ["a"]
        r = {"options": {"components": {c: {} for c in comps}, "packages": packages},
             "comparator": {"package": comparator}}
        r.update(kw)
        return r

    # CL1-CL6: comparator symmetry on toy specs
    r = spec({"soc": ["sc"], "soc_a": ["sc", "a"], "soc_b": ["sc", "b"], "c_alone": ["c"]}, "soc")
    a = comparator_symmetry(r)
    ok(a and a["answer"] == "finding" and a["asymmetric"] == [["c_alone", "sc"]],
       f"CL1 a standalone arm without the comparator's component that other arms carry is a finding: {a}")
    r["options"]["packages"]["c_soc"] = ["c", "sc"]
    ok(comparator_symmetry(r)["answer"] == "pass",
       "CL2 the same arm is symmetric once an arm is it with the component added (a reprogramming option)")
    r = spec({"none": [], "a_sc": ["a", "sc"], "b_sc": ["b", "sc"], "c": ["c"]}, "none")
    a = comparator_symmetry(r)
    ok(a["answer"] == "finding" and a["asymmetric"] == [["c", "sc"]],
       f"CL3 with a no-action comparator, the component every other arm carries is background: {a}")
    r = spec({"none": [], "a": ["a"], "b": ["b"], "ab": ["a", "b"]}, "none")
    ok(comparator_symmetry(r)["answer"] == "pass", "CL4 a factorial design is symmetric")
    r = spec({"c1": ["c1"], "none": [], "c2": ["c2"]}, "c1")
    ok(comparator_symmetry(r)["answer"] == "pass",
       "CL5 a component only the comparator holds is not background (no other arm carries it)")
    r = spec({"generate": True}, "none")
    r["options"]["components"] = {"x": {}, "y": {}, "z": {}}
    ok(comparator_symmetry(r)["answer"] == "pass", "CL6 generated packages (every combination) are symmetric")
    ok(comparator_symmetry(dict(spec({"a": ["a"]}, "a"), deliverable={"kind": "estimates"}))["answer"]
       == "not_applicable", "CL7 an estimates deliverable has no arms")
    ok(comparator_symmetry(spec({"a": ["a"]}, "nope")) is None and comparator_symmetry({"options": 3}) is None,
       "CL8 an unknown comparator or unreadable packages leave the item to the critic")

    # CL9-CL14: holdout windows against declared shock periods
    base = spec({"a": ["a"]}, "a")
    ok(holdout_shock_overlap(base) is None, "CL9 no periods: the critic answers")
    r = dict(base, periods={"holdout": [[2019.0, 2020.5]], "shocks": [{"id": "s1", "window": [2020.2, 2021.5],
                                                                     "what": "service disruption"}]})
    a = holdout_shock_overlap(r)
    ok(a["answer"] == "finding" and a["overlaps"] == [[[2019.0, 2020.5], "s1"]], f"CL10 an overlap is a finding: {a}")
    r["periods"]["holdout"] = [[2017.0, 2018.0], [2021.5, 2023.0]]
    ok(holdout_shock_overlap(r)["answer"] == "pass", "CL11 windows that do not overlap (or only touch) pass")
    r["periods"]["holdout"] = []
    ok(holdout_shock_overlap(r)["answer"] == "not_applicable", "CL12 no holdout window: not applicable")
    ok(holdout_shock_overlap(dict(base, periods={"holdout": [[2019, 2020]]})) is None,
       "CL13 holdout without a shocks list: the critic answers")
    bad = dict(base, periods={"holdout": [[2020, 2019]], "shocks": [{"id": "s", "window": [1, 2]}], "x": 1})
    pp = period_problems(bad)
    ok(len(pp) == 3 and holdout_shock_overlap(bad) is None,
       f"CL14 a malformed periods section is refused (reversed window, shock without `what`, unknown key): {pp}")
    ok(set(harness_answers(r)) == {"comparator_symmetry", "holdout_shock_overlap"}
       and set(harness_answers(base)) == {"comparator_symmetry"}, "CL15 harness_answers lists what it computed")

    # CL16-CL24: the critique file's checklist schema
    full = {i: {"answer": "pass", "reason": "checked"} for i in ITEMS}
    ok(problems(full, "f", {}, []) == [], "CL16 every item answered with a reason is accepted")
    ok(problems({k: v for k, v in full.items() if k not in HARNESS_ITEMS}, "f", {"comparator_symmetry": 1,
                                                                                   "holdout_shock_overlap": 1}, []) == [],
       "CL17 harness-answered items are not required from the critic")
    p = problems({k: v for k, v in full.items() if k != "burn_in_drift"}, "f", {}, [])
    ok(len(p) == 1 and "burn_in_drift" in p[0] and "missing" in p[0], f"CL18 a missing item is a schema error: {p}")
    ok(problems(None, "f", {}, []) and "missing required key 'checklist'" in problems(None, "f", {}, [])[0],
       "CL19 a missing checklist fails closed")
    p = problems(dict(full, natural_history_tension={"answer": "not_applicable", "reason": " "}), "f", {}, [])
    ok(len(p) == 1 and "reason" in p[0], f"CL20 not_applicable needs a reason: {p}")
    p = problems(dict(full, burn_in_drift={"answer": "finding", "reason": "r"}), "f", {}, [])
    ok(len(p) == 1 and "blocker" in p[0], f"CL21 a finding names its blocker: {p}")
    ok(problems(dict(full, burn_in_drift={"answer": "finding", "reason": "r", "blockers": ["D-001"]}), "f", {},
                ["D-001"]) == [], "CL22 a finding naming a blocker of the file is accepted")
    p = problems(dict(full, burn_in_drift={"answer": "finding", "reason": "r", "blockers": ["D-009"]}), "f", {},
                 ["D-001"])
    ok(len(p) == 1 and "D-009" in p[0], f"CL23 a finding naming an unknown blocker is refused: {p}")
    p = problems(dict(full, burn_in_drift={"answer": "maybe", "reason": "r"}, extra={"answer": "pass"}), "f", {}, [])
    ok(len(p) == 2 and any("unknown item" in x for x in p) and any("maybe" in x for x in p),
       f"CL24 an unknown answer and an unknown item are refused: {p}")
    p = problems(dict(full, burn_in_drift={"answer": "pass", "reason": "r", "blockers": ["D-001"]}), "f", {},
                 ["D-001"])
    ok(len(p) == 1, f"CL25 a pass that names blockers is contradictory: {p}")
    rec = record({"critique-domain": {"checklist": full}, "critique-presentation": {}},
                 {"comparator_symmetry": _ans("pass", "x")})
    ok(set(rec) == set(ITEMS) and rec["comparator_symmetry"]["harness"]["answer"] == "pass"
       and rec["comparator_symmetry"]["critics"] == {} and rec["burn_in_drift"]["critics"]["critique-domain"]["answer"]
       == "pass", f"CL26 the gate record holds the harness's and each critic's answers: {rec['burn_in_drift']}")
    pb = prompt_block({"comparator_symmetry": _ans("finding", "`c` lacks `sc`")})
    ok(all(i in pb for i in ITEMS) and "`c` lacks `sc`" in pb and "  - comparator_symmetry (principle 8): finding" in pb,
       "CL27 the prompt block lists the critic's items and the harness's answers")
    print(f"{'FAIL' if fails else 'OK'}: " + (f"{len(fails)} case(s) failed" if fails else
                                               "all self-test cases passed."))
    return 1 if fails else 0


def main(argv=None) -> int:
    import argparse
    import json
    import yaml
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.spec:
        ap.error("spec is required")
    with open(a.spec, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    print(json.dumps(harness_answers(raw), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
