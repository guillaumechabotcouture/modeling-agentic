#!/usr/bin/env python3
"""Derivation checks for the malaria pack's option-A trial targets (F6, owner decision 2026-11-18).

    python3 toolkit/engines/malaria/benchmarks/pack_trials.py b9g          # print B9g's derived target and band
    python3 toolkit/engines/malaria/benchmarks/pack_trials.py --self-test

B9d-B9f are Staedke et al. 2020 (LLINEUP, Uganda; Lancet 395:1292-1303, Table 2, as-treated): PBO vs non-PBO LLIN,
prevalence ratio by microscopy in children 2-10 years at 6, 12 and 18 months. They run in the pack's `llineup`
setting, whose unit is written from the trial description (an ASSUMPTION, as for every pack site) and whose
transmission is calibrated to the trial's non-PBO arm at 6 months (556/3844 = 0.145).

B9g is Mosha et al. 2022 (Misungwi; Lancet 399:1227): the dual-AI (chlorfenapyr) arm against the PBO arm at 24 months,
read from the same trial's two contrasts with its one pyrethroid-only control, B10 (OR 0.45, 0.30-0.67) and B9c
(aOR 0.99, 0.67-1.45). The trial publishes no direct dual-vs-PBO interval, so the band is derived (`or_contrast`):
the ratio of the two ORs, with each log standard error from its published 95% interval's half-width and the two taken
as independent (delta method). The shared control arm correlates them positively, so this band is wider than a direct
interval would be. The engine computes the same contrast directly (odds ratio of mis_dual against mis_pbo on common
random numbers), which equals the ratio of the two engine ORs against mis_pyr.

The checks here only read the pack, the coverage file and the stored results; they run no engine simulation.
"""
from __future__ import annotations

import math
import os
import sys
from statistics import NormalDist

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NEW_IDS = ("B9d", "B9e", "B9f", "B9g")
LLINEUP = {"B9d": ("m6", 0.74, 0.62, 0.87), "B9e": ("m12", 0.73, 0.63, 0.85), "B9f": ("m18", 0.84, 0.72, 0.98)}
LLINEUP_CONTROL_6M = (556, 3844)          # Table 2, as-treated, non-PBO arm at 6 months (positive / tested)
LLINEUP_USE = ((0.5, 0.85), (1.5, 0.73))  # reported use the previous night at 6 and 18 months (years, share)


def or_contrast(num: dict, den: dict, level: float = 0.95, digits: int = 3) -> tuple[float, float, float]:
    """(target, lo, hi) of OR_num / OR_den from two published intervals {target, lower, upper} at `level`: the log
    standard errors are each interval's log half-width over the normal quantile, combined as independent."""
    z = NormalDist().inv_cdf(0.5 + level / 2)
    se = [(math.log(x["upper"]) - math.log(x["lower"])) / (2 * z) for x in (num, den)]
    c = math.log(num["target"]) - math.log(den["target"])
    s = math.hypot(*se)
    return tuple(round(v, digits) for v in (math.exp(c), math.exp(c - z * s), math.exp(c + z * s)))


def retention_median(use_a: tuple, use_b: tuple, k: float, lo: float = 100.0, hi: float = 20000.0) -> float:
    """The median net retention (days) whose survival ratio S(t_b) / S(t_a) equals the reported use ratio, under the
    engine's smooth-compact survival curve (model.net_survival), by bisection."""
    from toolkit.engines.malaria import model as M
    (ta, ua), (tb, ub) = use_a, use_b
    want = ub / ua

    def f(m):
        return float(M.net_survival(tb * 365.0, m, k) / M.net_survival(ta * 365.0, m, k)) - want
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if f(mid) < 0:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def load_pack() -> dict:
    return yaml.safe_load(open(os.path.join(HERE, "pack.yaml")))


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def case(name, fn):
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            ok(False, f"{name} raised {type(ex).__name__}: {ex}")

    pack = load_pack()
    by = {b["id"]: b for b in pack["benchmarks"]}

    def pt0():
        t, lo, hi = or_contrast({"target": 0.5, "lower": 0.25, "upper": 1.0}, {"target": 0.5, "lower": 0.25, "upper": 1.0})
        ok(t == 1.0 and abs(math.log(hi) + math.log(lo)) < 0.01 and abs(math.log(hi) - math.log(2) * math.sqrt(2)) < 0.01,
           f"PT0 or_contrast: equal ORs give 1 with sqrt(2) times each log half-width ({t}, {lo}, {hi})")
    case("PT0", pt0)

    def pt1():
        for i, (w, t, lo, hi) in LLINEUP.items():
            b = by.get(i)
            ok(b is not None, f"PT1 {i} is in the pack")
            if b is None:
                continue
            e, p = b.get("estimand") or {}, b.get("published") or {}
            ok(b.get("target") == t and b.get("band") == [lo, hi] and (p.get("lower"), p.get("upper"), p.get("level"))
               == (lo, hi, 0.95) and "Staedke" in b.get("source", "") and "Table 2" in p.get("table", "")
               and "band_override" not in b,
               f"PT1 {i}: target {t} and band [{lo}, {hi}] equal LLINEUP Table 2's as-treated 95% CI")
            ok(e == {"type": "ratio", "num": "ug_pbo", "den": "ug_pyr", "value": f"windows.{w}.pfpr_2_10"}
               and b.get("estimand_type") == "risk_ratio" and "microscopy" in b.get("diagnostic", "")
               and "2-10" in b.get("age_band", ""),
               f"PT1 {i}: a prevalence ratio (engine ratio, source risk_ratio) by microscopy at ages 2-10 in window {w}")
    case("PT1", pt1)

    def pt2():
        s = pack["settings"].get("llineup") or {}
        u, c, h = s.get("unit") or {}, s.get("calibrate") or {}, s.get("horizon") or {}
        cy = u.get("campaign_year")
        win = h.get("windows") or {}
        ok(all(abs(sum(win.get(w, [0, 0])) / 2 - (cy + m / 12)) < 0.01 and win[w][1] - win[w][0] <= 0.17
               for w, m in (("m6", 6), ("m12", 12), ("m18", 18))) and h.get("stop", 0) >= max(v[1] for v in win.values()),
           f"PT2 the llineup windows are centred 6, 12 and 18 months after its distribution ({win}, campaign {cy})")
        n, d = LLINEUP_CONTROL_6M
        ok(c.get("attribute") == "m0" and c.get("arm_package") == {"net": "pyr"}
           and c.get("value_path") == "windows.m6.pfpr_2_10" and c.get("target") == round(n / d, 3),
           f"PT2 llineup is calibrated to the non-PBO arm at 6 months, ages 2-10 ({c})")
        ok(abs(sum(u.get("species", {}).values()) - 1) < 1e-9 and abs(sum(u.get("season", [])) / 12 - 1) < 1e-9
           and 0 < u.get("resistance", 1) < 0.30 and u.get("itn_use") == 0.85 and u.get("itn_use_at") == 0.5,
           f"PT2 llineup: species shares sum to 1, seasonality averages 1, bioassay mortality in Gleave 2021's high "
           f"category (below 30%), reported use 85% at 6 months ({u})")
        from toolkit.engines.malaria import engine as E
        k = E.resolve_params(E.MalariaEngine(), {})["net_survival_k"]
        med = (s.get("params") or {}).get("net_retention_half")
        want = retention_median(*LLINEUP_USE, k)
        ok(med is not None and abs(med - want) < 0.1,
           f"PT2 llineup's median retention ({med} d) reproduces its trial's 18-month / 6-month use ratio 0.73 / 0.85 "
           f"under the engine's survival curve ({want:.1f} d)")
        arms = {a: v for a, v in pack["arms"].items() if v["setting"] == "llineup"}
        ok(arms == {"ug_pyr": {"setting": "llineup", "package": {"net": "pyr"}},
                    "ug_pbo": {"setting": "llineup", "package": {"net": "pbo"}}},
           f"PT2 llineup has exactly the trial's two arms ({arms})")
    case("PT2", pt2)

    def pt3():
        b = by.get("B9g") or {}
        o = b.get("band_override") or {}
        t, lo, hi = or_contrast({"target": by["B10"]["target"], **{k: by["B10"]["published"][k] for k in ("lower", "upper")}},
                                {"target": by["B9c"]["target"], **{k: by["B9c"]["published"][k] for k in ("lower", "upper")}})
        ok(b.get("estimand") == {"type": "odds_ratio", "num": "mis_dual", "den": "mis_pbo", "value": "windows.m24.pfpr_6m14y"}
           and b.get("estimand_type") == "odds_ratio" and "published" not in b,
           "PT3 B9g is the Misungwi dual-AI vs PBO odds ratio at 24 months on the existing arms, with no published interval")
        ok(b.get("target") == t and b.get("band") == [lo, hi] and o.get("band") == [lo, hi] and o.get("level") == 0.95
           and all(w in o.get("reason", "") for w in ("B10", "B9c", "delta", "independent", "derived")),
           f"PT3 B9g's band is the derived delta-method interval of B10 / B9c ({t}, [{lo}, {hi}]; pack "
           f"{b.get('target')}, {b.get('band')}), recorded as a band_override with its level and reason")
    case("PT3", pt3)

    def pt4():
        from toolkit.engines.benchmark_sources import check_pack
        bad = [x for x in check_pack(pack) if x.split(":")[0] in NEW_IDS]
        ok(not set(NEW_IDS) & set(pack["fit_joint"]["targets"]) and not bad,
           f"PT4 B9d-B9g are held out (not fit_joint targets) and pass the source check ({bad})")
    case("PT4", pt4)

    def pt5():
        from toolkit.engines import benchmark_coverage as BC
        cov, probs = BC.load(os.path.join(os.path.dirname(HERE), BC.FILE))
        ent = (cov or {}).get("benchmarks") or {}
        ok(not probs and set(ent) == set(by)
           and all(ent.get(i, {}).get("covers") == {"net": ["pbo"]} for i in LLINEUP)
           and ent.get("B9g", {}).get("covers") == {"net": ["dual"]} and ent["B9g"].get("versus") == {"net": "pbo"},
           f"PT5 the coverage file maps exactly the pack's ids; B9d-B9f cover PBO nets, B9g dual-AI nets against PBO "
           f"({probs})")
    case("PT5", pt5)

    def pt6():
        # the stored record has no rows for a new id until the pack is re-run (on the cloud): it is refused, and the
        # refusal says "not yet run" for exactly those ids, never "rerun required" (a stale row)
        from toolkit.engines import benchmark as BM
        from toolkit.engines.malaria.engine import MalariaEngine
        res = yaml.safe_load(open(os.path.join(HERE, "results.yaml"))) or {}
        have = {r.get("id") for r in res.get("benchmarks") or []}
        missing = [i for i in by if i not in have]
        doc, why = BM.verified_results(MalariaEngine())
        if missing:
            ok(doc is None and f"not yet run: {', '.join(missing)}" in str(why)
               and not any(f"rerun required: {i}" in str(why) for i in missing),
               f"PT6 a pack benchmark with no stored row refuses the record as not yet run ({missing}: {why})")
        else:
            ok(True, "PT6 every pack benchmark has a stored row")
    case("PT6", pt6)

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    if sys.argv[1:] == ["b9g"]:
        p = {b["id"]: b for b in load_pack()["benchmarks"]}
        print(or_contrast({"target": p["B10"]["target"], **p["B10"]["published"]},
                          {"target": p["B9c"]["target"], **p["B9c"]["published"]}))
        sys.exit(0)
    print(__doc__)
    sys.exit(2)
