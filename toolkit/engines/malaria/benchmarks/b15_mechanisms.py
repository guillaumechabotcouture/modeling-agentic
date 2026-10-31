#!/usr/bin/env python3
"""B15 mechanism scan (a diagnostic, not a fit): the Muleba 2x2 factorial under each v0.2
redundancy mechanism alone and together, at the fitted engine parameters.

    python3 toolkit/engines/malaria/benchmarks/b15_mechanisms.py [--workers N] [--out PATH]

Every configuration re-calibrates Muleba's transmission to the standard-LLIN arm (55% at
9 months) and runs the four arms on the pack's seeds. Only NON-fitted parameters and the
IRS product are changed; fitted values come from fitted.yaml. Writes b15_mechanisms.yaml.
"""
from __future__ import annotations

import argparse
import copy
import os
import sys

import numpy as np
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(HERE)))))
from toolkit.engines.benchmark import Runner, load_engine  # noqa: E402

OFF = {"hh_net_corr": 0.0, "cov_corr": 0.0, "exp_corr": 0.0, "irs_divert": 0.0}
# name -> (non-fitted parameter overrides, IRS product)
CONFIGS = [
    ("hut coefficients only (no household, no diversion, short IRS)", dict(OFF), "short"),
    ("+ IRS long-lasting product decay (alone)", dict(OFF), "pm_cs"),
    ("+ IRS diversion to outdoor biting 0.5 (alone)", dict(OFF, irs_divert=0.5), "short"),
    ("+ household net-use clustering 0.5 and net x IRS correlation 0.3 (alone)", dict(OFF, hh_net_corr=0.5, cov_corr=0.3), "short"),
    ("+ exposure correlated with net non-use 0.2 (alone)", dict(OFF, exp_corr=0.2), "short"),
    ("all together (engine defaults)", {}, "pm_cs"),
    ("all together, high correlations (0.9, 0.9, 0.6)", {"hh_net_corr": 0.9, "cov_corr": 0.9, "exp_corr": 0.6}, "pm_cs"),
    ("all together, full diversion (1.0)", {"irs_divert": 1.0}, "pm_cs"),
    ("all together, high correlations and full diversion", {"hh_net_corr": 0.9, "cov_corr": 0.9, "exp_corr": 0.6,
                                                            "irs_divert": 1.0}, "pm_cs"),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    ap.add_argument("--out", default=os.path.join(HERE, "b15_mechanisms.yaml"))
    a = ap.parse_args(argv)
    pack0 = yaml.safe_load(open(os.path.join(HERE, "pack.yaml")))
    fitted = {k: v["value"] for k, v in (yaml.safe_load(open(os.path.join(HERE, "fitted.yaml")))["parameters"]).items()}
    eng = load_engine(pack0["engine"])
    b15 = next(b for b in pack0["benchmarks"] if b["id"] == "B15")
    rows = []
    for name, over, product in CONFIGS:
        pack = copy.deepcopy(pack0)
        s = pack["settings"]["muleba"]
        s["params"] = dict(s.get("params") or {}, **over)
        s["unit"]["irs_product"] = product
        r = Runner(pack, eng, a.workers, pack["seeds"])
        r.calibrate_settings(fitted, ["muleba"])
        e = r.evaluate(b15, fitted)
        row = {"config": name, "overrides": over, "irs_product": product,
               "m0": round(r.calibrated["muleba"]["value"], 3),
               "interaction_or": round(e["computed"], 3), "seed_min": e["seed_min"], "seed_max": e["seed_max"],
               "or_pbo_alone": e["arm_or_geomean"]["a10"], "or_irs_alone": e["arm_or_geomean"]["a01"],
               "or_both": e["arm_or_geomean"]["a11"], "prevalence": e["arm_prevalence"],
               "single_arms_in_trial_ci": bool(0.21 <= e["arm_or_geomean"]["a10"] <= 0.65
                                               and 0.19 <= e["arm_or_geomean"]["a01"] <= 0.55)}
        rows.append(row)
        print(row, flush=True)
    yaml.safe_dump({"fitted_used": fitted, "seeds": pack0["seeds"],
                    "trial": {"or_pbo_alone": 0.37, "or_irs_alone": 0.33, "or_both": 0.29, "interaction_or": 2.43,
                              "interaction_ci": [1.19, 4.97],
                              "prevalence": {"a00": 0.55, "a10": 0.31, "a01": 0.29, "a11": 0.26},
                              "source": "Protopopoff 2018 Lancet, Table 2 (9 months)"},
                    "configs": rows}, open(a.out, "w"), sort_keys=False)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
