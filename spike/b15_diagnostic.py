"""Diagnostic (not a fit): B15 interaction in the Muleba setting when the main effects are
strong enough to match the trial's single-intervention ORs. Uses the pack's runner."""
import sys
import yaml
sys.path.insert(0, sys.argv[1])
from toolkit.engines.benchmark import Runner, load_engine  # noqa: E402

if __name__ == "__main__":
    pack = yaml.safe_load(open(sys.argv[1] + "/toolkit/engines/malaria/benchmarks/pack.yaml"))
    b15 = next(b for b in pack["benchmarks"] if b["id"] == "B15")
    eng = load_engine(pack["engine"])
    out = {}
    for label, f in [("fitted", {}), ("strong_irs", {"irs_kill": 0.7}),
                     ("strong_both", {"irs_kill": 0.7, "itn_kill_max": 0.6})]:
        fitted = {k: v["value"] for k, v in yaml.safe_load(open(sys.argv[1] + "/toolkit/engines/malaria/benchmarks/fitted.yaml"))["parameters"].items()}
        fitted.update(f)
        r = Runner(pack, eng, 11, pack["seeds"])
        r.calibrate_settings(fitted, ["muleba"])
        e = r.evaluate(b15, fitted)
        import numpy as np
        arm = {k: float(np.exp(np.mean([np.log(x[k]) for x in e["arm_or"]]))) for k in ("a11", "a10", "a01")}
        out[label] = {"overrides": f, "interaction_or": e["computed"], "arm_or": arm, "m0": r.calibrated["muleba"]["value"]}
        print(label, out[label], flush=True)
    yaml.safe_dump(out, open(sys.argv[2], "w"))
