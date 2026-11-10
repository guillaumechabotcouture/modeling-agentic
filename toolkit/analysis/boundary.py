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
import math
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
PRODUCER = "toolkit/analysis/boundary.py"
OUT_REL = "analysis/boundary/results.jsonl"


def thresholds() -> dict:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        t = (yaml.safe_load(f) or {}).get("emulator") or {}
    return {"units": int(t["boundary_units"]), "min_share": float(t["boundary_rank_agreement_min"]),
            "tie": float(t["boundary_tie_rel_gap"])}


def _ratio(b, c):
    """Benefit per cost: None unless both are finite numbers and the cost is positive (Codex r1 P1-4)."""
    try:
        b, c = float(b), float(c)
    except (TypeError, ValueError):
        return None
    return b / c if math.isfinite(b) and math.isfinite(c) and c > 0 else None


def _sign(x):
    return (x > 0) - (x < 0)


def table(A, plan, ben, cost) -> dict:
    """{unit: {"plan": package, "ratios": {package: benefit per cost}}} for every unit whose plan package is not the
    comparator: the plan package and every other feasible non-comparator package with a ratio. Recorded in the
    artifact, so the gate rebuilds the candidates from it."""
    feas, out = A.feas["feasible"], {}
    for u in A.ids:
        ch = plan[u]
        if ch == A.comp:
            continue
        r = {p: x for p in A.pk if p != A.comp and (p == ch or feas[u].get(p))
             for x in [_ratio(ben[u][p], cost[u][p])] if x is not None}
        if ch in r:
            out[str(u)] = {"plan": ch, "ratios": r}
    return out


def pick(tab, k) -> list[dict]:
    """The k units closest to switching: smallest relative gap between the plan package's ratio and the runner-up's
    (the best other package in the table)."""
    rows = []
    for u, e in tab.items():
        ch, r = e["plan"], e["ratios"]
        others = sorted(((x, p) for p, x in r.items() if p != ch), reverse=True)
        if not others:
            continue
        rr, ru = others[0]
        rc = r[ch]
        rows.append({"unit": u, "chosen": ch, "runner_up": ru, "gap": abs(rc - rr) / max(abs(rc), abs(rr), 1e-300)})
    rows.sort(key=lambda x: (x["gap"], str(x["unit"])))
    return rows[:k]


def needed(pk, comp, ch, ru) -> list[str]:
    """The packages whose outcomes the benefit and cost of `ch` and `ru` read: the comparator, the two packages, and
    each of them without one of its components (an incremental per-outcome cost item reads that package,
    toolkit/alloc/costs.py item_cost; Codex r1 P1-1)."""
    sets = {n: frozenset(cs) for n, cs in pk.items()}
    out = [comp, ch, ru]
    for p in (ch, ru):
        for c in sorted(sets[p]):
            out += [n for n, s in sets.items() if s == sets[p] - {c}]
    return list(dict.fromkeys(out))


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
        for k in needed(A.pk, A.comp, c["chosen"], c["runner_up"]):
            out.append({"id": f"boundary:{u}|{k}", "point": f"boundary:{u}", "package": k,
                        "components": sp.engine_package(A.pk[k]), "unit": a, "params": dict(A.theta0), "seed": seed,
                        "engine": sp["engine"], "horizon": horizon, "windows": Bt.windows(sp)})
    return out


def engine_tables(A, cands, z0, out0, res):
    """(benefit, cost) on the engine for the boundary units: the emulator's point outcomes with every package the two
    compared packages read replaced by the engine's (needed()), through the analysis's own benefit and cost
    computation. Evaluated twice, with the packages not run filled once by the emulator's values and once by other
    values: the compared packages must not move (fail closed), so nothing emulated reaches the engine side."""
    from toolkit.analysis.model import DirectModel
    from toolkit.engines.contract import Outcomes
    dm = DirectModel(A.spec, log=lambda *a: None)
    eng = {}
    for c in cands:
        u = c["unit"]
        dm.packages = needed(A.pk, A.comp, c["chosen"], c["runner_up"])
        rs = {(u, k): Outcomes(**res[f"boundary:{u}|{k}"]) for k in dm.packages}
        dm._runs = lambda theta, ids, zs, rs=rs: rs
        eng[u] = (list(dm.packages), dm.predict(z0, A.theta0, [u])[u])

    def hybrid(scale):
        hyb = {}
        for u, (pks, row) in eng.items():
            h = copy.deepcopy(out0[u])
            for k in h["y"]:
                if k in pks:
                    h["y"][k], h["cost"][k] = row["y"][k], row["cost"][k]
                else:
                    h["y"][k] = {o: v * scale for o, v in h["y"][k].items()}
                    h["cost"][k] = {o: v * scale + (scale - 1.0) for o, v in h["cost"][k].items()}
            hyb[u] = h
        return hyb

    class _Stub:
        last_events = []

        def __init__(self, hyb):
            self.hyb = hyb

        def predict(self, zs, theta, ids, noise=None):
            return {i: self.hyb[i] for i in ids}
    saved, saved_ev, saved_fl = A.model, A.last_events, A.last_cost_floors
    try:
        tabs = []
        for scale in (1.0, 2.0):
            A.model = _Stub(hybrid(scale))
            tabs.append(A.evaluate(A.theta0, z0, ids=list(eng))[1:])
    finally:
        A.model, A.last_events, A.last_cost_floors = saved, saved_ev, saved_fl
    (ben, cost), (ben2, cost2) = tabs
    for c in cands:
        u = c["unit"]
        for k in (c["chosen"], c["runner_up"]):
            if ben[u][k] != ben2[u][k] or cost[u][k] != cost2[u][k]:
                raise ValueError(f"boundary ranking: unit {u}, package {k}: the engine-side benefit or cost reads a "
                                 "package that was not run on the engine: fails closed")
    return ben, cost


def compare(cands, ben_em, cost_em, ben_en, cost_en, tie) -> list[dict]:
    rows = []
    for c in cands:
        u, ch, ru = c["unit"], c["chosen"], c["runner_up"]
        em = [_ratio(ben_em[u][p], cost_em[u][p]) for p in (ch, ru)]
        en = [_ratio(ben_en[u][p], cost_en[u][p]) for p in (ch, ru)]
        rows.append({"unit": u, "chosen": ch, "runner_up": ru, "gap": c["gap"],
                     "emulator": {"ratio_chosen": em[0], "ratio_runner_up": em[1]},
                     "engine": {"ratio_chosen": en[0], "ratio_runner_up": en[1]}, "engine_tie": _tie(en, tie),
                     "match": _match(em, en, tie)})
    return rows


def _fin(xs) -> bool:
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in xs)


def _tie(en, tie) -> bool:
    """The engine's two ratios within `tie` of each other (relative): either order loses at most that share."""
    return _fin(en) and abs(en[0] - en[1]) <= tie * max(abs(en[0]), abs(en[1]))


def _match(em, en, tie) -> bool:
    if not (_fin(em) and _fin(en)):
        return False
    return _tie(en, tie) or _sign(em[0] - em[1]) == _sign(en[0] - en[1])


def summarise(rows, t, tab, n_runs) -> dict:
    n, m = len(rows), sum(1 for r in rows if r["match"])
    return {"status": "judged" if n else "not_judged", "units_requested": t["units"], "units": n, "matched": m,
            "share_matched": m / n if n else None, "threshold": t["min_share"], "tie_rel_gap": t["tie"],
            "engine_ties": sum(1 for r in rows if r.get("engine_tie")),
            "passes": (m / n >= t["min_share"]) if n else None, "rows": rows, "table": tab,
            "held_out_source": "engine runs at the boundary units' calibrated values and the point parameters "
                               "(the design's held-out points are at design values, not calibrated ones)",
            "engine_runs": n_runs}


def run(A, plan, z0, out0, ben0, cost0, horizon, run_dir, compute_kw=None, log=print) -> dict:
    from toolkit.analysis import batch as Bt
    t = thresholds()
    tab = table(A, plan, ben0, cost0)
    cands = pick(tab, t["units"])
    if not cands:
        return summarise([], t, tab, 0)
    js = jobs(A, cands, z0, horizon, seed=int((A.spec["analysis"].get("direct") or {}).get("seed", 1)))
    b = Bt.run(run_dir, js, A.eng, out_rel=OUT_REL, **(compute_kw or {}))
    miss = [j["id"] for j in js if j["id"] not in b["results"]]
    if b["problems"] or miss:
        raise ValueError(f"boundary ranking: engine results missing or without matching provenance "
                         f"({(b['problems'] + miss)[:3]}): fails closed")
    ben_en, cost_en = engine_tables(A, cands, z0, out0, b["results"])
    s = summarise(compare(cands, ben0, cost0, ben_en, cost_en, t["tie"]), t, tab, len(js))
    log(f"boundary ranking: emulator order matches the engine for {s['matched']} of {s['units']} boundary units "
        f"(threshold {t['min_share']:.0%})")
    return s


def findings(data, plan) -> list[dict]:
    """The gate: the candidates rebuilt from the artifact's ratio table and checked against the point plan (`plan`,
    from analysis/point.yaml), the rows bound to them, and the share rejudged from the rows against thresholds.yaml
    (never the artifact's own verdict)."""
    def f(kind, claim, declarable):
        return {"kind": kind, "severity": "HIGH", "stage": "ANALYZE", "target_stage": "ANALYZE", "claim": claim,
                "key": "emulator_boundary", "scope_declarable": declarable, "source": PRODUCER}
    W = "analysis/emulator_boundary.yaml"
    try:
        t = thresholds()
        tab, rows = data["table"], data["rows"]
        want = [(c["unit"], c["chosen"], c["runner_up"]) for c in pick(tab, t["units"])]
        got = [(r["unit"], r["chosen"], r["runner_up"]) for r in rows]
        em = [[r["emulator"]["ratio_chosen"], r["emulator"]["ratio_runner_up"]] for r in rows]
        en = [[r["engine"]["ratio_chosen"], r["engine"]["ratio_runner_up"]] for r in rows]
        recorded = [[tab[u]["ratios"][ch], tab[u]["ratios"][ru]] for u, ch, ru in got]
        m = sum(1 for a, b in zip(em, en) if _match(a, b, t["tie"]))
    except (OSError, KeyError, TypeError, ValueError, AttributeError) as e:
        return [f("emulator_boundary_unreadable", f"{W}: unreadable ({type(e).__name__}: {e})", False)]
    bad = []
    if got != want:
        bad.append(f"its rows ({len(got)}) are not the {len(want)} candidates its ratio table gives")
    if em != recorded:
        bad.append("its rows' emulator ratios are not its table's")
    if not isinstance(plan, dict) or any(plan.get(u) != e.get("plan") for u, e in tab.items()):
        bad.append("its table's plan packages are not analysis/point.yaml's plan")
    if data.get("units") != len(rows) or data.get("matched") != m or data.get("units_requested") != t["units"] \
            or (len(rows) == 0) != (data.get("status") == "not_judged"):
        bad.append(f"its counts ({data.get('matched')} of {data.get('units')}) are not its rows' ({m} of {len(rows)})")
    if bad:
        return [f("emulator_boundary_inconsistent", f"{W}: " + "; ".join(bad), False)]
    n = len(rows)
    if n and m / n < t["min_share"]:
        return [f("emulator_boundary_ranking", f"the emulator orders the chosen package and its runner-up as the "
                                               f"engine does for {m} of {n} units closest to a package switch "
                                               f"({m / n:.0%}, below {t['min_share']:.0%}): add design points or "
                                               "emulator features, or declare it with its decision test", True)]
    return []


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
    tab = table(_A, plan, ben, cost)
    c = pick(tab, 2)
    ok([(x["unit"], x["runner_up"]) for x in c] == [("u1", "b"), ("u2", "ab")] and abs(c[0]["gap"] - 0.05) < 1e-12
       and "u4" not in tab and "b" not in tab["u3"]["ratios"],
       f"BD1 the units closest to switching, with their runner-up among feasible non-comparator packages: {c}")
    en = {u: dict(v) for u, v in ben.items()}
    en["u1"]["b"] = 10.5                       # the engine puts the runner-up first for u1
    rows = compare(c, ben, cost, en, cost, 0.01)
    inf, nan = float("inf"), float("nan")
    ok([r["match"] for r in rows] == [False, True] and _match([1.0, 1.1], [2.0, 2.0], 0.01)
       and _match([1.1, 1.0], [1.0, 1.005], 0.01) and not _match([1.1, 1.0], [1.0, 1.05], 0.01)
       and not _match([None, 1.0], [1.0, 0.5], 0.01) and not _match([1.0, 2.0], [inf, 1.0], 0.01)
       and not _match([nan, nan], [nan, nan], 0.01) and _ratio(nan, 1.0) is None and _ratio(1.0, inf) is None,
       "BD2 order agreement per unit: the engine's order, or an engine tie within the tolerance; a missing or "
       "non-finite ratio never matches (Codex r1 P1-4)")
    s = summarise(rows, {"units": 2, "min_share": 0.75, "tie": 0.01}, tab, 6)
    ok(s["status"] == "judged" and s["matched"] == 1 and s["share_matched"] == 0.5 and s["passes"] is False
       and s["engine_runs"] == 6 and s["table"] == tab, f"BD3 the share matched and the extra engine runs are reported")
    real = thresholds()
    K = real["units"]
    br = {f"v{i:02d}": {"a": 1.0, "b": 1.0 - 0.002 * (i + 1)} for i in range(K + 5)}
    cr = {u: {"a": 1.0, "b": 1.0} for u in br}
    pr = {u: "a" for u in br}

    class _B:
        ids, comp, pk = list(br), "none", {"none": [], "a": ["a"], "b": ["b"]}
        feas = {"feasible": {u: {"none": True, "a": True, "b": True} for u in br}}
    tr = table(_B, pr, br, cr)
    cand = pick(tr, K)
    rev = {u: {"a": 1.0, "b": 2.0} for u in br}         # the engine puts b first, far beyond the tie tolerance
    good = summarise(compare(cand, br, cr, br, cr, real["tie"]), real, tr, 3 * K)
    bad = summarise(compare(cand, br, cr, rev, cr, real["tie"]), real, tr, 3 * K)
    u0 = cand[0]["unit"]
    cases = {"dup": dict(bad, rows=[good["rows"][0]] * K, matched=K),
             "drop": dict(bad, rows=[r for r in bad["rows"] if r["match"]], units=0, matched=0),
             "empty": dict(bad, rows=[], units=0, matched=0, status="not_judged"),
             "ratio": dict(good, rows=[dict(good["rows"][0], emulator={"ratio_chosen": inf, "ratio_runner_up": 1.0})]
                          + good["rows"][1:])}
    got = {k: [x["kind"] for x in findings(v, pr)] for k, v in cases.items()}
    ok(findings(good, pr) == [] and [x["kind"] for x in findings(bad, pr)] == ["emulator_boundary_ranking"]
       and findings(bad, pr)[0]["scope_declarable"]
       and all(v == ["emulator_boundary_inconsistent"] for v in got.values())
       and [x["kind"] for x in findings(good, dict(pr, **{u0: "b"}))] == ["emulator_boundary_inconsistent"]
       and [x["kind"] for x in findings(good, None)] == ["emulator_boundary_inconsistent"]
       and [x["kind"] for x in findings({"units": 1}, pr)] == ["emulator_boundary_unreadable"]
       and findings(summarise([], real, {}, 0), {}) == [],
       f"BD4 the gate rebuilds the candidates from the recorded ratio table and the point plan: below the share is "
       f"declarable; duplicated, dropped, emptied or edited rows, another plan, or an unreadable artifact are not; "
       f"an empty table is not judged, no finding (Codex r1 P1-3): {got}")
    ok(needed({"none": [], "a": ["a"], "b": ["b"], "a+b": ["a", "b"]}, "none", "a+b", "a") == ["none", "a+b", "a", "b"],
       "BD6 the engine runs every package the two packages' costs read: each without one of its components "
       "(incremental per-outcome items; Codex r1 P1-1)")
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
