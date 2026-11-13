#!/usr/bin/env python3
"""The world model as one table of beliefs (docs/design/v2-progressive-steps.md §4, owner 2026-11-13).

    python3 toolkit/analysis/belief_table.py --self-test

A belief: {claim (a direction or a threshold, in words), p (the probability the claim holds), basis (paper,
simulation or data, with a reference), decides (would being wrong change the recommendation), history (dated
outcomes)}. Three rules:
  predict  before a simulation every belief bearing on it states its expectation and p (registered, remote-anchored)
  score    the outcome is confirmed, refuted or undecided; surprise = -log2 p(outcome); calibration is the observed
           frequency per p-bin; Brier and log score per batch over decided outcomes
  update   confirmed: p <- p + CONFIRM_STEP (1 - p); refuted: the belief is rewritten as the observed claim at
           p = REFUTED_P, with an open question; undecided: unchanged. The next simulation goes where decides is yes
           and p is nearest 0.5.
A guard (in the caller, which has the run's inputs: scripts/world_model_trials.py guard_reasons) sets aside a claim
whose outcome the simulation's declared inputs determine without running the dynamics. Pure arithmetic: no
engine, no I/O.
"""
from __future__ import annotations

import math
import sys

CONFIRM_STEP = 0.1      # confirmed: p moves a tenth of the way to 1
REFUTED_P = 0.3         # refuted: the observed claim starts at this p (one observation)
OUTCOMES = ("confirmed", "refuted", "undecided")


def label_rates(outcomes) -> dict:
    """{label: {p, confirmed, refuted, n}}: the Laplace-smoothed confirmation rate (confirmed + 1) / (decided + 2)
    of each confidence label over (label, outcome) pairs; undecided outcomes are not counted."""
    out = {}
    for lab, o in outcomes:
        r = out.setdefault(lab, {"confirmed": 0, "refuted": 0})
        if o in ("confirmed", "refuted"):
            r[o] += 1
    for lab in ("low", "medium", "high"):
        out.setdefault(lab, {"confirmed": 0, "refuted": 0})
    for r in out.values():
        r["n"] = r["confirmed"] + r["refuted"]
        r["p"] = (r["confirmed"] + 1) / (r["n"] + 2)
    return out


def surprise(p, outcome):
    """-log2 p(outcome) in bits; None when undecided."""
    if outcome == "confirmed":
        return -math.log2(p)
    if outcome == "refuted":
        return -math.log2(1.0 - p)
    return None


def update(belief, outcome, at, observed=None) -> dict:
    """The belief after one outcome (a new dict): the update rule above, the outcome appended to its history."""
    b = dict(belief, history=list(belief.get("history") or []))
    b["history"].append({"at": at, "outcome": outcome, "claim": belief["claim"], "p": belief["p"]})
    if outcome == "confirmed":
        b["p"] = belief["p"] + CONFIRM_STEP * (1.0 - belief["p"])
    elif outcome == "refuted":
        b.update(claim=observed or f"not: {belief['claim']}", p=REFUTED_P,
                 open_question=f"Why was '{belief['claim']}' refuted? State the cause as a claim and test it.")
    return b


def scores(rows) -> dict:
    """{batch: {decided, brier, log2}} over rows {p, outcome, batch} whose outcome is decided (log2: mean log2 of
    the probability given to what happened; higher is better)."""
    out = {}
    for r in rows:
        if r["outcome"] not in ("confirmed", "refuted"):
            continue
        y = 1.0 if r["outcome"] == "confirmed" else 0.0
        s = out.setdefault(str(r["batch"]), {"decided": 0, "_b": 0.0, "_l": 0.0})
        s["decided"] += 1
        s["_b"] += (r["p"] - y) ** 2
        s["_l"] += math.log2(r["p"] if y else 1.0 - r["p"])
    return {k: {"decided": v["decided"], "brier": v["_b"] / v["decided"], "log2": v["_l"] / v["decided"]}
            for k, v in out.items()}


def calibration(rows, bins=None) -> list:
    """Per p-bin [lo, hi): decided count, mean p and the observed confirmation frequency."""
    bins = bins or [0.0, 0.5, 0.7, 0.9, 1.0001]
    out = []
    for lo, hi in zip(bins[:-1], bins[1:]):
        d = [r for r in rows if lo <= r["p"] < hi and r["outcome"] in ("confirmed", "refuted")]
        out.append({"bin": [lo, hi], "decided": len(d),
                    "mean_p": sum(r["p"] for r in d) / len(d) if d else None,
                    "observed": sum(r["outcome"] == "confirmed" for r in d) / len(d) if d else None})
    return out


def next_simulation(beliefs) -> list:
    """Beliefs with decides yes, nearest p = 0.5 first: where a simulation is most informative for the choice."""
    return sorted((b for b in beliefs if b.get("decides")), key=lambda b: (abs(b["p"] - 0.5), str(b.get("id"))))


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tryit(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok(False, f"{name}: {type(e).__name__}: {e}")

    def bt1():
        r = label_rates([("low", "confirmed"), ("low", "refuted"), ("low", "confirmed"), ("high", "confirmed"),
                         ("medium", "undecided")])
        ok(abs(r["low"]["p"] - 3 / 5) < 1e-12 and abs(r["high"]["p"] - 2 / 3) < 1e-12 and r["medium"]["p"] == 0.5
           and r["low"]["n"] == 3, f"BT1 a label's p is its Laplace-smoothed confirmation rate over decided outcomes "
                                   f"(undecided ones not counted): {r}")

    def bt2():
        ok(abs(surprise(0.8, "confirmed") - (-math.log2(0.8))) < 1e-12
           and abs(surprise(0.8, "refuted") - (-math.log2(0.2))) < 1e-12 and surprise(0.8, "undecided") is None,
           "BT2 surprise = -log2 p(outcome); none when undecided")

    def bt3():
        b = {"claim": "A beats B", "p": 0.6, "decides": True, "history": []}
        c = update(b, "confirmed", "2026-11-13")
        r = update(b, "refuted", "2026-11-13", observed="B beats A")
        u = update(b, "undecided", "2026-11-13")
        ok(abs(c["p"] - (0.6 + 0.1 * 0.4)) < 1e-12 and c["claim"] == "A beats B" and len(c["history"]) == 1,
           f"BT3 confirmed: p rises by the stated rule p + 0.1 (1 - p): {c['p']}")
        ok(r["claim"] == "B beats A" and r["p"] == 0.3 and r["open_question"] and r["history"][-1]["outcome"] == "refuted"
           and r["history"][-1]["claim"] == "A beats B",
           f"BT3b refuted: the belief is rewritten as the observed claim at p 0.3 and keeps an open question: {r}")
        ok(u["p"] == 0.6 and u["claim"] == "A beats B", "BT3c undecided: nothing changes but the history")

    def bt4():
        rows = [{"p": 0.8, "outcome": "confirmed", "batch": "3"}, {"p": 0.8, "outcome": "refuted", "batch": "3"},
                {"p": 0.6, "outcome": "undecided", "batch": "3"}, {"p": 0.6, "outcome": "confirmed", "batch": "4"}]
        s = scores(rows)
        ok(s["3"]["decided"] == 2 and abs(s["3"]["brier"] - ((0.2 ** 2 + 0.8 ** 2) / 2)) < 1e-12
           and abs(s["3"]["log2"] - (math.log2(0.8) + math.log2(0.2)) / 2) < 1e-12 and s["4"]["decided"] == 1,
           f"BT4 Brier and log score per batch over decided outcomes: {s}")
        cal = calibration(rows, bins=[0.0, 0.7, 1.0])
        ok(cal[0]["decided"] == 1 and cal[0]["observed"] == 1.0 and cal[1]["decided"] == 2 and cal[1]["observed"] == 0.5
           and abs(cal[1]["mean_p"] - 0.8) < 1e-12, f"BT4b calibration: observed frequency per p-bin: {cal}")

    def bt5():
        bs = [{"id": "a", "p": 0.9, "decides": True}, {"id": "b", "p": 0.55, "decides": True},
              {"id": "c", "p": 0.5, "decides": False}, {"id": "d", "p": 0.3, "decides": True}]
        ok([b["id"] for b in next_simulation(bs)] == ["b", "d", "a"],
           "BT5 the next simulation goes where decides is yes and p is nearest 0.5")

    for n, f in (("BT1", bt1), ("BT2", bt2), ("BT3", bt3), ("BT4", bt4), ("BT5", bt5)):
        tryit(n, f)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else 2)
