#!/usr/bin/env python3
"""Equilibration evidence for the v0.5 burn-in (the source of the card's `burnin_years`).

    python3 toolkit/engines/malaria/benchmarks/equilibration.py [--workers N] [--seeds K] [--out PATH]
    python3 toolkit/engines/malaria/benchmarks/equilibration.py --self-test

At four transmission levels (m0 1.5, 3.5, 9, 28: PfPR 6-59 months about 5-45%), each seed's state at
horizon.start is computed (engine.initial_state) after:
  base      the card's burn-in (burnin_years)
  doubled   twice that burn-in
  guess_lo  the card's burn-in from a closed-form immunity guess 4x lower (init_eir_per_m0 x 0.25)
  guess_hi  the same, 4x higher (x 4)
  none      no burn-in (the v0.4 start), for reference
The criterion (CRITERION): for adult IC and ID in each adult age band, the seed mean of `doubled` is within 5% of
`base` (equilibrated), and `guess_lo` within 5% of `guess_hi` (the guess no longer matters). Writes
equilibration.yaml beside this file; the engine self-test (V7, V8) checks it with problems().
The unit is a synthetic Sahel/savanna setting (no licensed inputs).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
OUT = os.path.join(HERE, "equilibration.yaml")

LEVELS = [1.5, 3.5, 9.0, 28.0]
SEEDS = [1, 2, 3, 4, 5, 6]
N_AGENTS = 4000
START = 2050.0   # no calendar inputs; late enough that the doubled burn-in starts after 1900 (engine MIN_START_YEAR)
CRITERION = {"rel_tol": 0.05, "quantities": ["IC", "ID"], "bands": ["15_30", "30p", "15p"]}
_S = np.array([0.15, 0.10, 0.10, 0.15, 0.35, 0.75, 1.40, 2.30, 2.70, 2.00, 1.00, 0.40])
UNIT = {"season": [round(float(x), 6) for x in _S / _S.mean()], "resistance": 0.5,
        "species": {"gambiae": 0.6, "arabiensis": 0.3, "funestus": 0.1}, "cm_ft": 0.3, "itn_use": 0.55}
GUESS = {"guess_lo": 0.25, "guess_hi": 4.0}


def arms(burnin: int, guess: float) -> dict:
    """arm -> engine parameter overrides."""
    return {"base": {"burnin_years": burnin}, "doubled": {"burnin_years": 2 * burnin},
            "guess_lo": {"burnin_years": burnin, "init_eir_per_m0": guess * GUESS["guess_lo"]},
            "guess_hi": {"burnin_years": burnin, "init_eir_per_m0": guess * GUESS["guess_hi"]},
            "none": {"burnin_years": 0}}


def _job(args):
    m0, arm, over, seed = args
    from toolkit.engines.malaria.engine import MalariaEngine
    from toolkit.engines.contract import UnitSpec, Horizon
    t0 = time.time()
    st = MalariaEngine().initial_state(dict(over, n_agents=N_AGENTS), UnitSpec("eq", dict(UNIT, m0=m0)),
                                       horizon=Horizon(START, START + 1.0, (START, START + 1.0), {}), seed=seed)
    return {"m0": m0, "arm": arm, "seed": seed, "N": st["N"], "immunity": st["immunity"],
            "pfpr_u5": st["pfpr"]["pfpr_u5"], "seconds": round(time.time() - t0, 1)}


def summarise(rows: list, levels, criterion=CRITERION) -> list:
    """Per level: seed means by arm, and the relative differences the criterion judges."""
    out = []
    for m0 in levels:
        r = [x for x in rows if x["m0"] == m0]
        lv = {"m0": m0, "arms": {}, "doubling_rel": {}, "init_guess_rel": {}}
        for arm in sorted({x["arm"] for x in r}):
            ra = [x for x in r if x["arm"] == arm]
            lv["arms"][arm] = {
                "seeds": len(ra), "N": round(float(np.mean([x["N"] for x in ra])), 1),
                "pfpr_u5": round(float(np.mean([x["pfpr_u5"] for x in ra])), 4),
                "immunity": {nm: {b: round(float(np.mean([x["immunity"][nm][b] for x in ra])), 4)
                                  for b in ra[0]["immunity"][nm]} for nm in ra[0]["immunity"]},
                "immunity_sd": {nm: {b: round(float(np.std([x["immunity"][nm][b] for x in ra], ddof=1)), 4)
                                     for b in criterion["bands"]} for nm in criterion["quantities"]} if len(ra) > 1 else {}}
        A = lv["arms"]
        for q in criterion["quantities"]:
            for b in criterion["bands"]:
                k = f"{q}_{b}"
                if "base" in A and "doubled" in A:
                    lv["doubling_rel"][k] = round(A["doubled"]["immunity"][q][b] / A["base"]["immunity"][q][b] - 1, 4)
                if "guess_lo" in A and "guess_hi" in A:
                    lv["init_guess_rel"][k] = round(A["guess_lo"]["immunity"][q][b] / A["guess_hi"]["immunity"][q][b]
                                                    - 1, 4)
        lv["max_abs_doubling_rel"] = max((abs(v) for v in lv["doubling_rel"].values()), default=None)
        lv["max_abs_init_guess_rel"] = max((abs(v) for v in lv["init_guess_rel"].values()), default=None)
        out.append(lv)
    return out


def problems(doc, burnin_years, version) -> list[str]:
    """What keeps the evidence from supporting the card's burn-in: messages starting `evidence:` (missing, stale or
    too thin), `doubling:` (not equilibrated) or `init_guess:` (the starting guess still matters). The relative
    differences are recomputed from the stored seed means, never read from a pass flag."""
    if not isinstance(doc, dict):
        return ["evidence: benchmarks/equilibration.yaml is missing or unreadable"]
    bad = []
    if str(doc.get("engine_version")) != str(version):
        bad.append(f"evidence: computed for engine {doc.get('engine_version')!r}, not {version!r}")
    if doc.get("burnin_years") != int(round(float(burnin_years))):
        bad.append(f"evidence: burn-in {doc.get('burnin_years')!r} differs from the card's {burnin_years!r}")
    crit = doc.get("criterion") or {}
    tol = crit.get("rel_tol")
    if not isinstance(tol, (int, float)) or tol > CRITERION["rel_tol"] or not set(CRITERION["quantities"]) <= set(
            crit.get("quantities") or []) or not {"15_30", "30p"} <= set(crit.get("bands") or []):
        bad.append(f"evidence: criterion {crit} is weaker than {CRITERION}")
        return bad
    levels = doc.get("levels") or []
    if len(levels) < 2:
        bad.append(f"evidence: {len(levels)} transmission level(s); at least 2 are needed")
    for lv in levels:
        A = lv.get("arms") or {}
        for arm in ("base", "doubled", "guess_lo", "guess_hi"):
            if arm not in A or (A[arm].get("seeds") or 0) < 2:
                bad.append(f"evidence: m0 {lv.get('m0')}: arm {arm} missing or with fewer than 2 seeds")
        if any(m.startswith(f"evidence: m0 {lv.get('m0')}:") for m in bad):
            continue
        for q in crit["quantities"]:
            for b in crit["bands"]:
                try:
                    d = A["doubled"]["immunity"][q][b] / A["base"]["immunity"][q][b] - 1
                    g = A["guess_lo"]["immunity"][q][b] / A["guess_hi"]["immunity"][q][b] - 1
                except (KeyError, TypeError, ZeroDivisionError):
                    bad.append(f"evidence: m0 {lv.get('m0')}: {q} {b} missing")
                    continue
                if not abs(d) <= tol:
                    bad.append(f"doubling: m0 {lv.get('m0')}: {q} {b} changes {d:+.1%} when the burn-in is doubled")
                if not abs(g) <= tol:
                    bad.append(f"init_guess: m0 {lv.get('m0')}: {q} {b} differs {g:+.1%} between the x0.25 and x4 "
                               "guesses")
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--seeds", type=int, default=len(SEEDS))
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--burnin", type=int, default=None, help="test another burn-in than the card's (a sensitivity run)")
    a = ap.parse_args(argv)
    from toolkit.engines.malaria.engine import MalariaEngine, VERSION
    from toolkit.engines.contract import code_fingerprint
    from toolkit.engines.contract import resolve_params
    eng = MalariaEngine()
    P = resolve_params(eng, {})
    B = int(round(P["burnin_years"])) if a.burnin is None else int(a.burnin)
    seeds = SEEDS[:a.seeds]
    jobs = [(m0, arm, over, s) for m0 in LEVELS for arm, over in arms(B, P["init_eir_per_m0"]).items() for s in seeds]
    jobs.sort(key=lambda j: -j[2].get("burnin_years", 0))           # longest first
    t0 = time.time()
    with ProcessPoolExecutor(max(1, min(a.workers, len(jobs)))) as ex:
        rows = list(ex.map(_job, jobs, chunksize=1))
    doc = {"producer": "toolkit/engines/malaria/benchmarks/equilibration.py", "engine_version": VERSION,
           "code_sha256": code_fingerprint(eng), "burnin_years": B, "init_eir_per_m0": P["init_eir_per_m0"],
           "criterion": CRITERION, "n_agents": N_AGENTS, "horizon_start": START, "seeds": seeds,
           "unit": UNIT, "arms": {k: v for k, v in arms(B, P["init_eir_per_m0"]).items()},
           "levels": summarise(rows, LEVELS), "wall_s": round(time.time() - t0, 1),
           "cpu_s": round(sum(r["seconds"] for r in rows), 1)}
    doc["problems"] = problems(doc, B, VERSION)
    yaml.safe_dump(doc, open(a.out, "w"), sort_keys=False, width=110)
    print(f"wrote {a.out}; problems: {doc['problems'] or 'none'}")
    return 0


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def row(m0, arm, seed, ic, idd):
        imm = {nm: {b: (ic if nm == "IC" else idd) for b in ("0_5", "5_15", "15_30", "30p", "15p")}
               for nm in ("IB", "IC", "ID", "IV")}
        return {"m0": m0, "arm": arm, "seed": seed, "N": 4000, "immunity": imm, "pfpr_u5": 0.2}
    good = {"base": (100, 50), "doubled": (102, 50.5), "guess_lo": (99, 49), "guess_hi": (101, 50), "none": (60, 30)}
    rows = [row(m, a, s, *v) for m in (1.5, 9.0) for a, v in good.items() for s in (1, 2)]
    doc = {"engine_version": "0.5", "burnin_years": 50, "criterion": CRITERION, "levels": summarise(rows, [1.5, 9.0])}
    ok(problems(doc, 50, "0.5") == [], "Q1 an equilibrated, guess-independent result at two levels passes")
    ok(doc["levels"][0]["doubling_rel"]["IC_30p"] == 0.02 and doc["levels"][0]["arms"]["none"]["immunity"]["IC"]["15p"] == 60,
       "Q2 the summary keeps seed means by arm and the relative differences")
    bad_rows = [row(m, a, s, *((110, 50) if a == "doubled" and m == 9.0 else v)) for m in (1.5, 9.0)
                for a, v in good.items() for s in (1, 2)]
    pb = problems(dict(doc, levels=summarise(bad_rows, [1.5, 9.0])), 50, "0.5")
    ok(any(p.startswith("doubling: m0 9.0: IC") for p in pb) and not any(p.startswith("init_guess") for p in pb),
       f"Q3 a 10% change on doubling is reported as not equilibrated: {pb[:2]}")
    g_rows = [row(m, a, s, *((80, 50) if a == "guess_lo" else v)) for m in (1.5, 9.0) for a, v in good.items()
              for s in (1, 2)]
    pg = problems(dict(doc, levels=summarise(g_rows, [1.5, 9.0])), 50, "0.5")
    ok(any(p.startswith("init_guess:") for p in pg), "Q4 a result that still depends on the starting guess is reported")
    ok(any(p.startswith("evidence:") for p in problems(dict(doc, levels=doc["levels"][:1]), 50, "0.5"))
       and any(p.startswith("evidence:") for p in problems(doc, 40, "0.5"))
       and any(p.startswith("evidence:") for p in problems(doc, 50, "0.6"))
       and problems(None, 50, "0.5")[0].startswith("evidence:")
       and any(p.startswith("evidence:") for p in problems(dict(doc, criterion=dict(CRITERION, rel_tol=0.2)), 50, "0.5")),
       "Q5 fewer than two levels, another burn-in or engine version, a missing file or a looser criterion fail closed")
    ok(set(arms(50, 0.6)) == {"base", "doubled", "guess_lo", "guess_hi", "none"} and arms(50, 0.6)["doubled"]
       == {"burnin_years": 100} and abs(arms(50, 0.6)["guess_hi"]["init_eir_per_m0"] / arms(50, 0.6)["guess_lo"][
           "init_eir_per_m0"] - 16) < 1e-12, "Q6 the arms: doubled burn-in, and guesses x0.25 and x4 of the card's")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    sys.exit(main())
