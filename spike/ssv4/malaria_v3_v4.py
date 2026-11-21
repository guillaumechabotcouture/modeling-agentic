#!/usr/bin/env python3
"""Compare the malaria engine on Starsim 3.3.2 (the unchanged code) and Starsim 4 (this tree), same settings.

    python3 spike/ssv4/malaria_v3_v4.py [--seeds 8] [--workers 4] [--agents 2000] [--out spike/ssv4/logs/v3_v4.json]

Each (version, unit, seed) is one run_batch() call (shared burn-in, packages forked) in its own process:
  v3: /software/conda/envs/modag/bin/python (Starsim 3.3.2) on the engine code at V3_REV, extracted with git archive
  v4: python3 (Starsim 4) on this tree's toolkit/
Settings: 3 units (m0 3.5, 9, 28; the seasonal Sahel/savanna unit of benchmarks/equilibration.py, a past net campaign
in 2019.5), 5 packages (none, pyrethroid nets, dual-AI nets + SMC, IRS, enhanced case management), burn-in 3 years,
horizon 2019-2026, decision window 2022-2025, so 10 simulated years at `--agents` agents.
Prints, per unit, outcome and version: the seed mean and SD, and the v4 - v3 difference over its standard error.
--worker runs one call and prints JSON (used by the driver).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
V3_REV = "82a15f71"                      # the commit this port starts from (engine v0.18, Starsim 3.3.2)
V3_PY = "/software/conda/envs/modag/bin/python"
V4_PY = sys.executable
SEASON = None                            # filled from equilibration.UNIT in the worker
UNITS = {"m0_3.5": 3.5, "m0_9": 9.0, "m0_28": 28.0}
PACKAGES = {"none": {}, "pyr": {"net": "pyr"}, "dual_smc": {"net": "dual", "smc": 1}, "irs": {"irs": 1},
            "cm": {"cm": 1}}
HORIZON = dict(start=2019.0, stop=2026.0, window=(2022.0, 2025.0),
               windows={"pre": (2021.0, 2022.0), "win": (2022.0, 2025.0), "y1": (2022.0, 2023.0)})
ENV = dict(SCIRIS_NUM_THREADS="1", OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1",
           NUMBA_NUM_THREADS="1", MPLBACKEND="agg", PYTHONWARNINGS="ignore")


def worker(tree: str, m0: float, seed: int, agents: int, burnin: int) -> dict:
    sys.path.insert(0, tree)
    import starsim
    from toolkit.engines.malaria.engine import MalariaEngine, VERSION
    from toolkit.engines.malaria.benchmarks.equilibration import UNIT
    from toolkit.engines.contract import UnitSpec, Package, Horizon
    e = MalariaEngine()
    u = UnitSpec("cmp", dict(UNIT, m0=m0, itn_hist_years=[2019.5], campaign_year=2022.0))
    h = Horizon(HORIZON["start"], HORIZON["stop"], HORIZON["window"], dict(HORIZON["windows"]))
    pk = [Package(k, v) for k, v in PACKAGES.items()]
    t = time.process_time()
    outs = e.run_batch([{"n_agents": agents, "burnin_years": burnin}] * len(pk), u, pk, h, seed)
    cpu = time.process_time() - t
    res = {}
    for k, o in zip(PACKAGES, outs):
        pc, sh = o.per_capita, o.meta["age_shares"]
        r = {f"{m}_all": sum(pc[m][g] * sh[g] for g in sh) for m in ("cases", "severe", "deaths", "dalys")}
        r.update({f"{m}_u5": pc[m]["u5"] for m in ("cases", "severe", "deaths")})
        for w in ("pre", "win"):
            for b in ("pfpr_u5", "pfpr_2_10", "pfpr_all"):
                r[f"{b}_{w}"] = o.windows[w][b]
        r["eir_win"] = o.windows["win"]["eir_annual"]
        r["net_use_win"] = o.windows["win"]["net_use"]
        r["n_start"] = o.meta["n_start"]
        r["share_u5"] = sh["u5"]                 # the window's person-days under 5
        res[k] = r
    return {"starsim": starsim.__version__, "engine": VERSION, "m0": m0, "seed": seed, "cpu_s": round(cpu, 1),
            "res": res}


def _v3_tree() -> str:
    """The unchanged engine code (toolkit/ at V3_REV), extracted once beside this script."""
    d = os.path.join(HERE, "_v3tree")
    if not os.path.isdir(os.path.join(d, "toolkit")):
        os.makedirs(d, exist_ok=True)
        arc = subprocess.run(["git", "-C", ROOT, "archive", V3_REV, "toolkit"], check=True, capture_output=True).stdout
        subprocess.run(["tar", "-x", "-C", d], input=arc, check=True)
    return d


def _call(job):
    ver, py, tree, m0, seed, a = job
    cmd = [py, os.path.abspath(__file__), "--worker", "--tree", tree, "--m0", str(m0), "--seed", str(seed),
           "--agents", str(a.agents), "--burnin", str(a.burnin)]
    p = subprocess.run(cmd, capture_output=True, text=True, env=dict(os.environ, **ENV), timeout=1800)
    if p.returncode:
        raise RuntimeError(f"{ver} m0 {m0} seed {seed} failed:\n{p.stderr[-3000:]}")
    return ver, json.loads(p.stdout.strip().splitlines()[-1])


def _stats(xs):
    import numpy as np
    x = np.asarray(xs, float)
    return float(x.mean()), float(x.std(ddof=1)) if len(x) > 1 else 0.0, len(x)


def report(rows: list) -> str:
    import numpy as np
    by = {}
    for ver, r in rows:
        by.setdefault((ver, r["m0"]), []).append(r)
    lines, flags = [], []
    metrics = ["cases_all", "cases_u5", "severe_all", "deaths_all", "dalys_all", "pfpr_u5_pre", "pfpr_2_10_pre",
               "pfpr_all_pre", "pfpr_u5_win", "pfpr_all_win", "eir_win", "n_start", "share_u5"]
    for name, m0 in UNITS.items():
        if ("v3", m0) not in by or ("v4", m0) not in by:
            continue
        lines.append(f"\n=== unit {name} ({len(by.get(('v3', m0), []))} v3 seeds, {len(by.get(('v4', m0), []))} v4 seeds)")
        lines.append(f"{'outcome':34s} {'v3 mean':>10s} {'v3 sd':>9s} {'v4 mean':>10s} {'v4 sd':>9s} "
                     f"{'v4/v3':>7s} {'z':>6s}")

        def row(label, f):
            v3 = [f(r) for r in by[("v3", m0)]]
            v4 = [f(r) for r in by[("v4", m0)]]
            (a, sa, na), (b, sb, nb) = _stats(v3), _stats(v4)
            se = np.sqrt(sa ** 2 / na + sb ** 2 / nb)
            z = (b - a) / se if se > 0 else 0.0
            flag = " *" if abs(z) > 2.5 else ""
            if flag:
                flags.append(f"{name} {label}: z {z:+.2f}")
            lines.append(f"{label:34s} {a:10.4f} {sa:9.4f} {b:10.4f} {sb:9.4f} {b / a if a else float('nan'):7.3f} "
                         f"{z:+6.2f}{flag}")
        for m in metrics:
            if all(m in r["res"]["none"] for r in by[("v3", m0)] + by[("v4", m0)]):
                row(f"none: {m}", lambda r, m=m: r["res"]["none"][m])
        for k in PACKAGES:
            if k == "none":
                continue
            for m in ("cases_all", "cases_u5", "deaths_all", "dalys_all"):
                # the package's relative reduction against no package, paired within a seed (common random numbers)
                row(f"{k}: {m} reduction", lambda r, k=k, m=m: 1 - r["res"][k][m] / r["res"]["none"][m])
            row(f"{k}: pfpr_u5_win", lambda r, k=k: r["res"][k]["pfpr_u5_win"])
    lines.append("\n(seeds are shared across units, so the units' initial populations are the same draws within a version)")
    cpu = {v: np.mean([r["cpu_s"] for vv, r in rows if vv == v]) for v in ("v3", "v4")}
    lines.append(f"\nCPU s per run_batch call (5 packages): v3 {cpu['v3']:.1f}, v4 {cpu['v4']:.1f} "
                 f"(v4/v3 {cpu['v4'] / cpu['v3']:.2f})")
    lines.append("flagged (|z| > 2.5): " + ("; ".join(flags) if flags else "none"))
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", action="store_true")
    ap.add_argument("--tree")
    ap.add_argument("--m0", type=float)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--agents", type=int, default=2000)
    ap.add_argument("--burnin", type=int, default=3)
    ap.add_argument("--seeds", type=int, default=8)
    ap.add_argument("--first-seed", type=int, default=1)
    ap.add_argument("--units", default=",".join(UNITS), help="comma-separated names from UNITS")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(HERE, "logs", "v3_v4.json"))
    ap.add_argument("--report", help="print the report of a saved --out file and stop")
    a = ap.parse_args(argv)
    if a.worker:
        print(json.dumps(worker(a.tree, a.m0, a.seed, a.agents, a.burnin)))
        return 0
    if a.report:
        print(report(json.load(open(a.report))))
        return 0
    if a.agents > 2000 or a.burnin + HORIZON["stop"] - HORIZON["start"] > 10:
        raise SystemExit("keep it small: <= 2,000 agents and <= 10 simulated years")
    jobs = [(ver, py, tree, UNITS[n], s, a) for n in a.units.split(",") for s in range(a.first_seed, a.first_seed + a.seeds)
            for ver, py, tree in (("v3", V3_PY, _v3_tree()), ("v4", V4_PY, ROOT))]
    t = time.time()
    with ThreadPoolExecutor(max(1, min(a.workers, 4))) as ex:
        rows = list(ex.map(_call, jobs))
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(rows, open(a.out, "w"), indent=1)
    print(f"{len(rows)} calls in {time.time() - t:.0f} s wall; versions "
          f"{sorted({(v, r['starsim'], r['engine']) for v, r in rows})}")
    print(report(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
