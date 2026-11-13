#!/usr/bin/env python3
"""The world model as one table of beliefs (docs/design/v2-progressive-steps.md §4, owner 2026-11-13).

Not yet implemented: the self-test defines the behaviour.
"""
from __future__ import annotations

import math
import sys


def label_rates(outcomes):
    raise NotImplementedError


def surprise(p, outcome):
    raise NotImplementedError


def update(belief, outcome, at):
    raise NotImplementedError


def scores(rows):
    raise NotImplementedError


def calibration(rows, bins=None):
    raise NotImplementedError


def next_simulation(beliefs):
    raise NotImplementedError


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
