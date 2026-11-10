"""Emulator ranking near package switches (F6 S2-4 part 2): does the emulator order the chosen package and its
runner-up as the engine does, for the units closest to switching?

A unit's package is close to switching when the benefit per cost of its point-plan package and of its runner-up (the
feasible, non-comparator package with the best benefit per cost among the others) are close: the relative gap
|r_chosen - r_runner| / max(|r_chosen|, |r_runner|). The `boundary_units` units with the smallest gap (units whose plan
package is not the comparator and that have a runner-up; thresholds.yaml `emulator`) are run on the engine at their
calibrated values and the point parameters, for the comparator, the chosen package and the runner-up; their benefit
and cost are computed as the analysis computes them (toolkit/analysis/core.py evaluate), and the order of the two
packages by benefit per cost is compared with the emulator's.

Held-out source. The design's held-out points cannot serve: each is a real unit at Sobol values of the calibrated
attribute and of every design parameter, not at the unit's calibrated value and the point parameters, and the plan's
package (the boundary) is defined only there; few held-out points are boundary units at all. So the check makes its own
engine runs: 3 per boundary unit (one shared-state task per unit for an engine whose packages share a burn-in), through
the same compute path, provenance check and stored-row reuse as the design batch (analysis/boundary/results.jsonl).

The share of boundary units whose order matches is gated (toolkit/analysis/contract.py) against
`boundary_rank_agreement_min`. A unit whose two engine ratios are within `boundary_tie_rel_gap` (relative) is an engine tie and counts as matched. With no unit
that has a runner-up, the check is not judged (status not_judged), never passed.
"""
from __future__ import annotations

import copy
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PRODUCER = "toolkit/analysis/boundary.py"
OUT_REL = "analysis/boundary/results.jsonl"


def thresholds() -> dict:
    raise NotImplementedError("F6 S2-4b beta")


def _ratio(b, c):
    return float(b) / float(c) if c > 0 else None


def _sign(x):
    return (x > 0) - (x < 0)


def candidates(A, plan, ben, cost, k) -> list[dict]:
    """The k units closest to switching, by relative benefit-per-cost gap between the plan package and the runner-up."""
    raise NotImplementedError("F6 S2-4b beta")


def jobs(A, cands, z0, horizon, seed=1) -> list[dict]:
    """Engine jobs at the units' calibrated values and the point parameters (the design batch's job form)."""
    from toolkit.analysis import batch as Bt
    sp, out = A.spec, []
    attr = sp["calibration"].get("attribute")
    for c in cands:
        u = c["unit"]
        a = sp.unit_attributes(A.by_id[u])
        if attr and z0.get(u) is not None:
            a[attr] = Bt.calibrated_value(sp, z0[u])
        for k in dict.fromkeys([A.comp, c["chosen"], c["runner_up"]]):
            out.append({"id": f"boundary:{u}|{k}", "point": f"boundary:{u}", "package": k,
                        "components": sp.engine_package(A.pk[k]), "unit": a, "params": dict(A.theta0), "seed": seed,
                        "engine": sp["engine"], "horizon": horizon, "windows": Bt.windows(sp)})
    return out


def engine_tables(A, cands, z0, out0, res):
    """(benefit, cost) on the engine for the boundary units: the emulator's point outcomes with the comparator, chosen
    and runner-up packages replaced by the engine's, through the analysis's own benefit and cost computation."""
    from toolkit.analysis.model import DirectModel
    from toolkit.engines.contract import Outcomes
    dm = DirectModel(A.spec, log=lambda *a: None)
    hyb = {}
    for c in cands:
        u = c["unit"]
        dm.packages = list(dict.fromkeys([A.comp, c["chosen"], c["runner_up"]]))
        rs = {(u, k): Outcomes(**res[f"boundary:{u}|{k}"]) for k in dm.packages}
        dm._runs = lambda theta, ids, zs, rs=rs: rs
        row = dm.predict(z0, A.theta0, [u])[u]
        h = copy.deepcopy(out0[u])
        for k in dm.packages:
            h["y"][k], h["cost"][k] = row["y"][k], row["cost"][k]
        hyb[u] = h

    class _Stub:
        last_events = []

        def predict(self, zs, theta, ids, noise=None):
            return {i: hyb[i] for i in ids}
    saved, saved_ev, saved_fl = A.model, A.last_events, A.last_cost_floors
    A.model = _Stub()
    try:
        _o, ben, cost = A.evaluate(A.theta0, z0, ids=[c["unit"] for c in cands])
    finally:
        A.model, A.last_events, A.last_cost_floors = saved, saved_ev, saved_fl
    return ben, cost


def compare(cands, ben_em, cost_em, ben_en, cost_en, tie) -> list[dict]:
    raise NotImplementedError("F6 S2-4b beta")


def _tie(en, tie) -> bool:
    """The engine's two ratios within `tie` of each other (relative): either order loses at most that share."""
    return None not in en and abs(en[0] - en[1]) <= tie * max(abs(en[0]), abs(en[1]))


def _match(em, en, tie) -> bool:
    raise NotImplementedError("F6 S2-4b beta")


def summarise(rows, t) -> dict:
    raise NotImplementedError("F6 S2-4b beta")


def run(A, plan, z0, out0, ben0, cost0, horizon, run_dir, compute_kw=None, log=print) -> dict:
    from toolkit.analysis import batch as Bt
    t = thresholds()
    cands = candidates(A, plan, ben0, cost0, t["units"])
    if not cands:
        return summarise([], t)
    js = jobs(A, cands, z0, horizon, seed=int((A.spec["analysis"].get("direct") or {}).get("seed", 1)))
    b = Bt.run(run_dir, js, A.eng, out_rel=OUT_REL, **(compute_kw or {}))
    miss = [j["id"] for j in js if j["id"] not in b["results"]]
    if b["problems"] or miss:
        raise ValueError(f"boundary ranking: engine results missing or without matching provenance "
                         f"({(b['problems'] + miss)[:3]}): fails closed")
    ben_en, cost_en = engine_tables(A, cands, z0, out0, b["results"])
    s = summarise(compare(cands, ben0, cost0, ben_en, cost_en, t["tie"]), t)
    log(f"boundary ranking: emulator order matches the engine for {s['matched']} of {s['units']} boundary units "
        f"(threshold {t['min_share']:.0%})")
    return s


def findings(data) -> list[dict]:
    """The gate: rejudged from the rows (never the artifact's own verdict) against thresholds.yaml."""
    raise NotImplementedError("F6 S2-4b beta")


def _self_test() -> int:
    """python3 toolkit/analysis/boundary.py --self-test (the end-to-end case, engine runs included, is run.py BD-R1)."""
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    class _A:
        ids, comp = ["u1", "u2", "u3", "u4"], "none"
        pk = {"none": [], "a": ["a"], "b": ["b"], "ab": ["a", "b"]}
        feas = {"feasible": {u: {"none": True, "a": True, "b": u != "u3", "ab": True} for u in ids}}
    ben = {"u1": {"none": 0, "a": 10, "b": 9.5, "ab": 12}, "u2": {"none": 0, "a": 10, "b": 2, "ab": 11},
           "u3": {"none": 0, "a": 10, "b": 50, "ab": 10}, "u4": {"none": 0, "a": 5, "b": 1, "ab": 6}}
    cost = {u: {"none": 0, "a": 1.0, "b": 1.0, "ab": 2.0} for u in ben}
    plan = {"u1": "a", "u2": "a", "u3": "a", "u4": "none"}
    c = candidates(_A, plan, ben, cost, 2)
    ok([(x["unit"], x["runner_up"]) for x in c] == [("u1", "b"), ("u2", "ab")] and abs(c[0]["gap"] - 0.05) < 1e-12,
       f"BD1 the units closest to switching, with their runner-up among feasible non-comparator packages: {c}")
    en = {u: dict(v) for u, v in ben.items()}
    en["u1"]["b"] = 10.5                       # the engine puts the runner-up first for u1
    rows = compare(c, ben, cost, en, cost, 0.01)
    ok([r["match"] for r in rows] == [False, True] and _match([1.0, 1.1], [2.0, 2.0], 0.01)
       and _match([1.1, 1.0], [1.0, 1.005], 0.01) and not _match([1.1, 1.0], [1.0, 1.05], 0.01)
       and not _match([None, 1.0], [1.0, 0.5], 0.01),
       "BD2 order agreement per unit: the engine's order, or an engine tie within the tolerance; no ratio fails")
    s = summarise(rows, {"units": 2, "min_share": 0.75, "tie": 0.01})
    ok(s["status"] == "judged" and s["matched"] == 1 and s["share_matched"] == 0.5 and s["passes"] is False
       and s["engine_runs"] == 6, f"BD3 the share matched and the extra engine runs are reported: {s}")
    real = thresholds()
    base = summarise(rows, real)
    f1 = findings(dict(base, rows=[rows[1]] * 4, units=4, matched=4))
    f2 = findings(dict(base, rows=[rows[0]] * 4, units=4, matched=0))
    f3 = findings(dict(base, matched=2))
    f4 = findings({"status": "not_judged", "units": 0, "matched": 0, "rows": []})
    f5 = findings({"units": 1})
    ok(f1 == [] and [x["kind"] for x in f2] == ["emulator_boundary_ranking"] and f2[0]["scope_declarable"]
       and [x["kind"] for x in f3] == ["emulator_boundary_inconsistent"] and f4 == []
       and [x["kind"] for x in f5] == ["emulator_boundary_unreadable"],
       "BD4 the gate rejudges from the rows against thresholds.yaml: below the share is declarable; counts that "
       "disagree with the rows or an unreadable artifact are not; not judged is no finding")
    from toolkit.engines.benchmark_sources import CALIBRATOR_THRESHOLD_SECTIONS
    ok("emulator" not in CALIBRATOR_THRESHOLD_SECTIONS and real["units"] >= 1 and 0 < real["min_share"] <= 1,
       f"BD5 the thresholds sit outside the sections the evidence digests bind (no engine restamp): {real}")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
