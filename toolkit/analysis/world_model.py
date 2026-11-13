#!/usr/bin/env python3
"""The world model (design v2-progressive-steps.md §4): stub, tests first (F6 WM α)."""
from __future__ import annotations

import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


class WorldModelError(ValueError):
    pass


class Sweep:
    def __init__(self, features, packages, comparator, burden, y, cost, groups=None, points=None, kinds=None,
                 artifacts=None):
        self.features, self.packages, self.comparator = features, packages, comparator
        self.burden, self.y, self.cost, self.groups = burden, y, cost, groups or {}
        self.points, self.kinds, self.artifacts = points, kinds or {}, artifacts or {}


def validate_seed(seed):
    raise NotImplementedError


def summarise(sw, pairs=None):
    raise NotImplementedError


def check(hyps, ctx):
    raise NotImplementedError


def where_to_look(wm, top=10):
    raise NotImplementedError


def build(seed, sw, plan=None, units=None):
    raise NotImplementedError


def load_sweep(run_dir):
    raise NotImplementedError


# ---------------------------------------------------------------- self-test
def _planted(seed=3, n=200):
    """A planted-truth sweep: A's relative effect is constant (0.30); B's rises with f1 (0.6 x f1); f2 is irrelevant;
    equal costs, so A beats B on benefit per cost exactly where f1 < 0.5."""
    rng = np.random.default_rng(seed)
    f1, f2 = rng.uniform(0, 1, n), rng.uniform(0, 1, n)
    burden = 1.0 + 0.1 * rng.standard_normal(n)
    rA = 0.30 + 0.02 * rng.standard_normal(n)
    rB = 0.60 * f1 + 0.03 * rng.standard_normal(n)
    one = np.ones(n)
    return Sweep({"f1": f1, "f2": f2}, {"none": [], "A": ["a"], "B": ["b"]}, "none", burden,
                 {"none": burden, "A": burden * (1 - rA), "B": burden * (1 - rB)},
                 {"none": 0 * one, "A": one, "B": one}, groups={"a": "g", "b": "g"}, points=list(range(n)),
                 kinds={"f1": "unit", "f2": "unit"}, artifacts={"results": "synthetic"})


def _seed():
    lit = [{"citation": "Trial 2020", "location": "Table 2"}]
    return {"beliefs": [
        {"id": "B1", "mechanism": "A reduces the outcome by about a third", "about": {"package": "A"},
         "direction": "reduces", "magnitude": {"band": [0.2, 0.4], "scale": "relative_reduction"},
         "source_type": "literature", "evidence": lit, "confidence": "medium"},
        {"id": "B2", "mechanism": "B's effect grows with f1", "about": {"package": "B", "feature": "f1"},
         "direction": "increases", "magnitude": {"band": [0.7, 0.9], "scale": "relative_reduction"},
         "source_type": "literature", "evidence": lit, "confidence": "low", "decision_relevant": True},
        {"id": "B3", "mechanism": "A is preferred over B where f1 is low", "about": {"pair": ["A", "B"]},
         "direction": "flips", "conditions": [{"feature": "f1", "range": [0.3, 0.7]}],
         "source_type": "data", "evidence": [{"dossier_entry": "D-1"}], "confidence": "low",
         "decision_relevant": True}],
        "hypotheses": [
            {"id": "H1", "belief": "B1", "claim": "A's effect is in [0.2, 0.4]",
             "test": {"kind": "effect_band", "package": "A", "band": [0.2, 0.4]}},
            {"id": "H2", "belief": "B3", "claim": "A beats B wherever f1 is above 0.7",
             "test": {"kind": "paired_sign", "a": "A", "b": "B", "feature": "f1", "range": [0.7, 1.0],
                      "sign": "positive"}},
            {"id": "H3", "belief": "B3", "claim": "the A/B choice flips for f1 in [0.3, 0.7]",
             "test": {"kind": "flip_in", "a": "A", "b": "B", "feature": "f1", "range": [0.3, 0.7]}},
            {"id": "H4", "belief": "B2", "claim": "B's effect increases with f1",
             "test": {"kind": "effect_direction", "package": "B", "feature": "f1", "direction": "increases"}},
            {"id": "H5", "belief": "B3", "claim": "most low-f1 units get A",
             "test": {"kind": "plan_share", "component": "a", "where": {"f1": [0.0, 0.5]}, "min_share": 0.8}}]}


def _self_test() -> int:
    import copy
    import json
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tryit(label, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            ok(False, f"{label}: {type(e).__name__}: {e}")
    sw = _planted()

    def wm1():
        s = summarise(sw)
        eb = s["effects"]["B"]["direction"]
        ok(eb["f1"]["direction"] == "increases" and eb["f2"]["direction"] == "none",
           f"WM1 the summariser recovers B's direction along f1 and none along the irrelevant f2: {eb}")
        ea = s["effects"]["A"]
        ok(ea["band"][0] <= 0.30 <= ea["band"][1] and all(v["direction"] == "none" for v in ea["direction"].values()),
           f"WM2 A's effect band contains the true 0.30 and A has no direction along any feature: {ea}")
        fl = s["flips"]["A~B"]
        ok(fl["along"] and fl["along"][0]["feature"] == "f1" and fl["along"][0]["range"][0] <= 0.5
           <= fl["along"][0]["range"][1] and fl["along"][0]["first_preferred_below"] is True,
           f"WM3 the A/B flip is located along f1 with a range containing the true 0.5, A preferred below: {fl}")
        ok(all(a["feature"] != "f2" for a in fl["along"]), f"WM4 no flip is reported along the irrelevant f2: {fl}")
        ok(json.dumps(summarise(_planted()), sort_keys=True) == json.dumps(s, sort_keys=True),
           "WM5 the summary is deterministic for a seed")
    tryit("WM1-5 summariser", wm1)

    def wm6():
        for mut, why in ((lambda b: b.pop("evidence"), "no evidence"),
                         (lambda b: b["evidence"][0].pop("location"), "literature without table or page"),
                         (lambda b: b.update(source_type="hunch"), "unknown source type"),
                         (lambda b: b.update(confidence="certain"), "unknown confidence")):
            sd = _seed()
            mut(sd["beliefs"][0])
            try:
                validate_seed(sd)
                ok(False, f"WM6 validation refuses a belief with {why}")
            except WorldModelError:
                ok(True, f"WM6 validation refuses a belief with {why}")
        for mut, why in ((lambda h: h.pop("test"), "no refutation test"),
                         (lambda h: h["test"].update(kind="vibes"), "an unknown test kind"),
                         (lambda h: h["test"].pop("band"), "a test missing a field"),
                         (lambda h: h.update(belief="B9"), "an unknown belief")):
            sd = _seed()
            mut(sd["hypotheses"][0])
            try:
                validate_seed(sd)
                ok(False, f"WM7 validation refuses a hypothesis with {why}")
            except WorldModelError:
                ok(True, f"WM7 validation refuses a hypothesis with {why}")
        validate_seed(_seed())
        ok(True, "WM6b a seed whose beliefs all have evidence and whose hypotheses all have tests is accepted")
    tryit("WM6-7 validation", wm6)

    def wm8():
        rng = np.random.default_rng(1)
        units = {str(i): {"f1": float(x), "f2": 0.5} for i, x in enumerate(rng.uniform(0, 1, 40))}
        plan = {u: ("A" if r["f1"] < 0.5 else "B") for u, r in units.items()}
        wm, hy = build(_seed(), sw, plan=plan, units=units)
        st = {h["id"]: h["status"] for h in hy["hypotheses"]}
        ok(st == {"H1": "supported", "H2": "refuted", "H3": "supported", "H4": "supported", "H5": "supported"},
           f"WM8 check() supports the true hypotheses and flags the false one (H2) as refuted: {st}")
        tr = wm["track_record"]
        ok(tr["confirmed"] == 4 and tr["contradicted"] == 1 and any(h["hypothesis"] == "H2" for h in tr["history"]),
           f"WM9 the track record counts confirmations and contradictions: {tr}")
        b2 = next(b for b in wm["beliefs"] if b["id"] == "B2")
        ok(any(c["belief"].startswith("sim.") for c in b2.get("conflicts", [])),
           f"WM10 the literature band for B (0.7-0.9) and the simulated band are kept side by side as a conflict: {b2}")
        q = next(o for o in wm["open_questions"] if "H2" in o["question"])
        ok(q["decision_relevant"], f"WM11 a contradiction becomes a decision-relevant open question: {q}")
        hy2 = copy.deepcopy(hy)
        ok(all(h["result"].get("artifact") for h in hy2["hypotheses"]), "WM12 every result names its artifact")
        look = where_to_look(wm)
        ok(look and look[0]["belief"] in ("B2", "B3"), f"WM13 where_to_look ranks the low-confidence, decision-relevant,"
                                                       f" contradicted or conflicting belief first: {look[:2]}")
        reg = next((x for x in look if x["kind"] == "simulation" and "f1" in (x.get("region") or {})), None)
        ok(reg is not None and reg["region"]["f1"][0] <= 0.5 <= reg["region"]["f1"][1],
           f"WM14 where_to_look proposes simulation points over the flip range, which contains the true 0.5: {reg}")
        h5 = copy.deepcopy(_seed())
        wm_np, hy_np = build(h5, sw, plan=None, units=None)
        ok(next(h for h in hy_np["hypotheses"] if h["id"] == "H5")["status"] == "untestable",
           "WM15 a plan hypothesis without a plan is untestable, never supported")
    tryit("WM8-15 build, check, where_to_look", wm8)

    def wm16():
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "analysis", "batch"))
            json.dump({"points": [], "packages": []}, open(os.path.join(d, "analysis", "batch", "design.json"), "w"))
            try:
                load_sweep(d)
                ok(False, "WM16 a run without results.jsonl fails closed")
            except WorldModelError as e:
                ok("results" in str(e) or "decision_spec" in str(e), f"WM16 a run without results fails closed: {e}")
    tryit("WM16 load", wm16)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
