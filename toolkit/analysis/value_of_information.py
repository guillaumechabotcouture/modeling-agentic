"""Value of information over the uncertainty draws (F6): which uncertain input is worth better data.

Stub (α): the self-tests below are written first and fail until the method is implemented.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PRODUCER = "toolkit/analysis/value_of_information.py"


def thresholds() -> dict:
    raise NotImplementedError


def config(raw: dict) -> dict:
    raise NotImplementedError


def validate(raw: dict) -> list[str]:
    raise NotImplementedError


def expected(raw: dict, draws) -> bool:
    raise NotImplementedError


def hat(X, max_df=None):
    raise NotImplementedError


def analyse(nb, nb_opt, over, inputs, groups=None, max_share=0.0, level=0.95, thr=None, seed=0, ids=None) -> dict:
    raise NotImplementedError


def contract_findings(raw: dict, unc, voi) -> list[str]:
    raise NotImplementedError


# --------------------------------------------------------------------------- self-test
def _valid_doc(n=64):
    rng = np.random.default_rng(3)
    x1, x2 = rng.normal(size=n), rng.normal(size=n)
    nb = np.column_stack([np.zeros(n), x1 + 0.1 * rng.normal(size=n)]) + 100.0
    return analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"a": x1, "b": x2}, seed=1,
                   thr=dict(thresholds(), min_draws=30))


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def run_case(label, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- a missing or broken function is a failing case
            ok(False, f"{label}: {type(e).__name__}: {e}")

    def toy():
        # VI1-VI3: two plans; plan B's net benefit over A is phi1 plus a little noise, phi2 is unrelated. The choice
        # turns on phi1 only: EVPPI(phi1) is close to EVPI, EVPPI(phi2) is within its permutation noise floor.
        rng = np.random.default_rng(11)
        n = 400
        p1, p2 = rng.normal(0.2, 1.0, n), rng.normal(0.0, 1.0, n)
        inb = p1 + 0.1 * rng.normal(size=n)
        nb = np.column_stack([np.full(n, 50.0), 50.0 + inb]) + rng.normal(0, 3.0, n)[:, None]   # shared noise cancels
        r = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"phi1": p1, "phi2": p2}, seed=2)
        ev = {x["input"]: x for x in r["inputs"]}
        evpi = r["evpi"]["value"]
        exact = float(np.mean(np.maximum(inb, 0)) - max(0.0, float(np.mean(inb))))
        ok(r["status"] == "indicative" and abs(evpi - exact) < 1e-9,
           f"VI1 EVPI is mean(max) - max(mean) over the paired draws ({evpi:.4f} vs {exact:.4f}, {r['status']})")
        ok(ev["phi1"]["evppi"] > 0.8 * evpi and ev["phi1"]["above_noise_floor"] is True
           and ev["phi2"]["evppi"] < 0.1 * evpi and ev["phi2"]["above_noise_floor"] is False
           and r["inputs"][0]["input"] == "phi1",
           f"VI2 the input that drives the choice ranks first and clears its noise floor; the other does not "
           f"({ev['phi1']['evppi']:.4f}, {ev['phi2']['evppi']:.4f} of {evpi:.4f})")
        tol = 1e-9 + 0.02 * evpi
        ok(all(-1e-12 <= x["evppi"] <= evpi + tol for x in r["inputs"]) and evpi >= 0,
           "VI3 0 <= EVPPI <= EVPI (within tolerance) for every input")
        b = r["evpi"]["bootstrap"]
        ok(b["low"] <= evpi <= b["high"] and r["evpi"]["se_paired"] > 0
           and all(x["bootstrap"]["low"] <= x["bootstrap"]["high"] for x in r["inputs"]),
           f"VI4 EVPI carries a paired standard error and a bootstrap interval; each EVPPI a bootstrap interval ({b})")
        # three plans, random tables: the ordering EVPI >= every EVPPI >= 0 holds for groups too
        rng = np.random.default_rng(5)
        X = rng.uniform(size=(200, 3))
        nb3 = np.column_stack([X[:, 0] * 2, X[:, 1] + X[:, 2], np.full(200, 1.0)]) + rng.normal(0, 0.2, (200, 3))
        r3 = analyse(nb3, nb3.max(1), np.zeros_like(nb3, bool), {f"x{k}": X[:, k] for k in range(3)},
                     groups={"x1x2": ["x1", "x2"]}, seed=3)
        e3 = r3["evpi"]["value"]
        grp = [x for x in r3["inputs"] if x["kind"] == "group"]
        ok(grp and all(-1e-12 <= x["evppi"] <= e3 * 1.02 + 1e-9 for x in r3["inputs"])
           and grp[0]["evppi"] >= max(x["evppi"] for x in r3["inputs"] if x["input"] in ("x1", "x2")) - 0.02 * e3,
           "VI5 three plans: a group's EVPPI is at least each member's and at most EVPI; all non-negative")

    def paired():
        # VI6: hand arithmetic on four draws and two plans, paired by draw
        nb = np.array([[10.0, 12.0], [10.0, 8.0], [10.0, 11.0], [10.0, 9.0]])
        r = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"x": np.array([1.0, 2.0, 3.0, 4.0])},
                    thr=dict(thresholds(), min_draws=4, informative_draws=4))
        diff = nb.max(1) - nb[:, 0]          # plan 0 (mean 10) vs plan 1 (mean 10): tie -> the first
        ok(abs(r["evpi"]["value"] - 1.5) < 1e-12 and abs(r["evpi"]["se_paired"] - diff.std(ddof=1) / 2) < 1e-12
           and abs(r["evpi"]["pct"] - 15.0) < 1e-9 and abs(r["expected_value_decide_now"] - 10.0) < 1e-12
           and abs(r["expected_value_perfect_information"] - 11.5) < 1e-12,
           f"VI6 paired arithmetic: EVPI 1.5 (15% of 10), SE sd(diff)/sqrt(4) ({r.get('evpi')})")
        # VI7: the per-draw optimum can beat every candidate in a draw; perfect information takes the best of both
        r2 = analyse(nb, np.array([13.0, 10.0, 11.0, 10.0]), np.zeros_like(nb, bool), {"x": np.arange(4.0)},
                     thr=dict(thresholds(), min_draws=4))
        ok(abs(r2["expected_value_perfect_information"] - 11.75) < 1e-12,
           "VI7 per draw, perfect information is the better of the draw's optimum and every eligible candidate")

    def budget():
        nb = np.array([[10.0, 14.0], [10.0, 14.0], [10.0, 14.0], [10.0, 14.0]])
        over = np.array([[False, True], [False, False], [False, False], [False, False]])
        th = dict(thresholds(), min_draws=4)
        r = analyse(nb, nb[:, 0].copy(), over, {"x": np.arange(4.0)}, thr=th, ids=["point", "draw:1"])
        c = r["candidates"]
        ok(c["eligible"] == 1 and c["ineligible"] == [{"id": "draw:1", "draws_over_budget": 1, "share_over_budget": 0.25}]
           and r["decide_now"] == "point" and abs(r["evpi"]["value"]) < 1e-12,
           f"VI8 a plan over the budget in one draw is infeasible there; with no affordability rule it cannot be the "
           f"decision now, and is listed with its count, not counted ({c}, {r.get('decide_now')})")
        r2 = analyse(nb, nb[:, 0].copy(), over, {"x": np.arange(4.0)}, max_share=0.25, thr=th, ids=["point", "draw:1"])
        ok(r2["decide_now"] == "draw:1" and r2["candidates"]["eligible"] == 2
           and r2["candidates"]["over_budget_counted"] == [{"id": "draw:1", "draws_over_budget": 1}],
           "VI9 with the spec's affordability rule (at most 25% of draws over), the plan is eligible and the draw it "
           "overspends in is counted and reported")
        r3 = analyse(nb, nb[:, 0].copy(), np.ones_like(over), {"x": np.arange(4.0)}, thr=th)
        ok(r3["status"] == "no_eligible_plan" and r3["evpi"] is None and r3["inputs"] == [],
           "VI10 no candidate within the budget in enough draws: not computed, with the reason, never a number")

    def floors():
        rng = np.random.default_rng(1)
        nb = rng.normal(size=(10, 2))
        r = analyse(nb, nb.max(1), np.zeros_like(nb, bool), {"x": rng.normal(size=10)})
        ok(r["status"] == "too_few_draws" and r["evpi"] is None and r["min_draws"] == thresholds()["min_draws"],
           f"VI11 below the minimum draws nothing is computed ({r['status']})")
        ok(thresholds()["min_draws"] >= 20 and thresholds()["informative_draws"] > thresholds()["min_draws"],
           "VI12 the minimum and the informative draw counts are read from thresholds.yaml")
        H, df = hat(np.linspace(0, 1, 50)[:, None])
        y = np.sin(3 * np.linspace(0, 1, 50))
        ok(abs((H @ y).mean() - y.mean()) < 1e-12 and np.max(np.abs(H @ y - y)) < 0.02 and 2 <= df <= 10,
           f"VI13 the smoother keeps the mean (intercept) and follows a smooth curve (df {df})")

    def gate():
        doc = _valid_doc()
        raw = {"uncertainty": {"draws": 64}}
        unc = {"draws": 64}
        ok(contract_findings(raw, unc, doc) == [], f"VG1 a well-formed artifact passes: {contract_findings(raw, unc, doc)}")
        ok(contract_findings(raw, unc, None), "VG2 the artifact is expected (enabled, enough draws) and missing")
        ok(contract_findings({"analysis": {"value_of_information": {"enabled": False}}}, unc, None) == []
           and contract_findings(raw, {"draws": 8}, None) == [],
           "VG3 switched off, or below the minimum draws: no artifact is expected")
        ok(contract_findings({"analysis": {"value_of_information": {"enabled": False}}}, unc, doc),
           "VG4 an artifact for a spec that switched it off is a finding")
        import copy
        muts = [("not a mapping", lambda d: "x"), ("an unknown status", lambda d: dict(d, status="fine")),
                ("other draws", lambda d: dict(d, draws=63)), ("no evpi", lambda d: dict(d, evpi=None)),
                ("a negative EVPI", lambda d: dict(d, evpi=dict(d["evpi"], value=-1.0))),
                ("a non-finite EVPI", lambda d: dict(d, evpi=dict(d["evpi"], value=float("nan")))),
                ("inputs not a list", lambda d: dict(d, inputs={"a": 1})),
                ("a negative EVPPI", lambda d: dict(d, inputs=[dict(d["inputs"][0], evppi=-1.0)] + d["inputs"][1:])),
                ("EVPPI above EVPI unflagged", lambda d: dict(d, inputs=[dict(d["inputs"][0], evppi=d["evpi"]["value"] * 2,
                                                                              exceeds_evpi=False)])),
                ("not_drawn missing", lambda d: {k: v for k, v in d.items() if k != "not_drawn"}),
                ("an indicative label on enough draws", lambda d: dict(d, status="computed")),
                ("too few draws claimed on enough", lambda d: dict(d, status="too_few_draws"))]
        for label, m in muts:
            ok(bool(contract_findings(raw, unc, m(copy.deepcopy(doc)))), f"VG5 {label} is a finding (fail closed)")
        ok(validate({"analysis": {"value_of_information": {"enabled": "yes"}}})
           and validate({"analysis": {"value_of_information": {"bogus": 1}}})
           and validate({"analysis": {"value_of_information": {"groups": {"g": "a"}}}})
           and validate({"analysis": {"value_of_information": {"groups": {"g": ["a", "b"]}, "enabled": True}}}) == []
           and validate({}) == [], "VG6 spec validation: enabled a boolean, groups of input names, no unknown key")
        ok(expected({}, 64) is True and expected({}, 8) is False
           and expected({"analysis": {"value_of_information": {"enabled": False}}}, 64) is False,
           "VG7 on by default at or above the minimum draws; off below it or when switched off")

    for label, fn in (("VI1-5 toy", toy), ("VI6-7 paired", paired), ("VI8-10 budget", budget),
                      ("VI11-13 floors", floors), ("VG gate", gate)):
        run_case(label, fn)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
