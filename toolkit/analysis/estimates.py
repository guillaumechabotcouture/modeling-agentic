#!/usr/bin/env python3
"""The analysis of an `estimates` deliverable: quantities with intervals, no allocation (design note v2d §2.3).

    python3 toolkit/analysis/estimates.py --self-test

Called by toolkit/analysis/run.py when the spec's deliverable kind is `estimates` (toolkit/spec/estimates.py). Every
number comes from the spec, the engine and the run's data:

1. The posterior problem. Its parameters are the spec's `calibration.priors` (engine parameters, or unit attributes
   the spec does not bind to data) and each observation source's own parameters (bias mechanisms, a declared
   dispersion), each with its declared prior. One engine run_batch() evaluates many points. The engine's incidence
   (`meta["incidence"]`, shares of N per step) times the population is the latent incidence; each source sees it
   through its measurement model: a declared reporting delay (convolution over steps), its bias mechanisms
   (toolkit/analysis/observation.py, e.g. under_ascertainment) and its noise (negbin or poisson). No source is the
   truth: every bias parameter is a prior-bounded mechanism.
2. The canonical joint sample (toolkit/analysis/calibration.py: importance sampling or adaptive Metropolis, fail
   closed below its floors), identifiability classes, and the posterior mode.
3. Uncertainty draws: rows of the canonical sample (with provenance), the engine re-run at each row's applied values;
   each declared output is an engine window metric. uncertainty_report.yaml (scalar_outputs) is written by the
   shared writer (toolkit/analysis/uncertainty.py).
4. What the data determine: each output's interval under the prior alone against the posterior (contraction).
5. The threshold rule (yes / no / undetermined) and an automatic prior_range test for every parameter the data do not
   determine: the decision recomputed with that parameter at each end of its declared range.
6. A posterior predictive check of every target, figures, the claims ledger and results.md.

Nothing here names a disease; the engine, the spec and the data carry the specifics.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.analysis import calibration as C  # noqa: E402
from toolkit.engines.contract import Horizon, Package, UnitSpec  # noqa: E402
from toolkit.spec.estimates import statements as _statements  # noqa: E402

PRODUCER = "toolkit/analysis/estimates.py"
EXPECTED_KINDS = ("engine", "calibration", "uncertainty", "estimates", "figures", "claims")
EXPECTED_ROLES = ("uncertainty_report", "claims_spec", "claims_ledger", "results")
PRIOR_DRAWS = 4000
PPC_DRAWS = 400


def thresholds() -> dict:
    """thresholds.yaml `estimates`: dependence_max (an output that moves by more than this share of its median across
    an undetermined parameter's posterior range is set by that parameter's declared range) and end_loglik_drop_max
    (an end of a declared range whose best fit is worse by more than this log-likelihood is rejected by the data)."""
    import yaml
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "thresholds.yaml")) as f:
        return {k: (dict(v) if isinstance(v, dict) else float(v)) for k, v in yaml.safe_load(f)["estimates"].items()}


def analyse(spec, eng, seed=0, n_draws=None, prior_range=True, log=print):
    raise NotImplementedError("estimates deliverable: tests first; the implementation is the next commit")


def run(run_dir, spec, S, eng, arts, data_in, eng_files, draws=None, log=print):
    raise NotImplementedError("estimates deliverable: tests first; the implementation is the next commit")


def headline(run_dir):
    raise NotImplementedError("estimates deliverable: tests first; the implementation is the next commit")


# --------------------------------------------------------------------------- self-test
def _synthetic(r0=1.8, rho=0.1, n=30, N=1e5, seed_share=3e-4, gi=(0.5, 0.5), k=50.0, rs=3):
    """A synthetic weekly series from the reference implementation in the renewal pack (not the engine's code)."""
    from toolkit.engines.renewal.benchmarks.oracles import renewal_reference
    inc, _ = renewal_reference(r0, list(gi), 1.0, seed_share, n)
    rng = np.random.default_rng(rs)
    mu = rho * np.asarray(inc) * N
    return [int(x) for x in rng.poisson(rng.gamma(k, mu / k))]


def _self_test() -> int:
    import tempfile
    import yaml
    from toolkit.analysis import contract
    from toolkit.analysis.run import run as run_analysis
    from toolkit.spec import estimates as ES, schema
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    rows = _synthetic()
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)          # run paths are recorded relative to the run dir: no symlinked temp root
        sp = ES.toy_dir(d, rows=rows)
        ok(schema.validate(schema.load(sp)) == [], "AE0 the toy estimates spec validates")
        r = run_analysis(d, unsigned=True, log=lambda *a: None)
        rep = yaml.safe_load(open(os.path.join(d, "uncertainty_report.yaml")))
        so = rep.get("scalar_outputs") or {}
        ok(set(so) == {"reff_next", "attack"} and all(so[k]["ci_low"] <= so[k]["median"] <= so[k]["ci_high"]
                                                     for k in so),
           f"AE1 uncertainty_report.yaml carries scalar_outputs for every declared output: {sorted(so)}")
        from toolkit.engines.renewal.benchmarks.oracles import renewal_reference
        inc, S_end = renewal_reference(1.8, [0.5, 0.5], 1.0, 3e-4, len(rows))
        true_reff, true_ar = 1.8 * S_end, sum(inc)
        ok(so["reff_next"]["ci_low"] <= true_reff <= so["reff_next"]["ci_high"],
           f"AE2 the reff interval covers the generator's value {true_reff:.3f}: "
           f"[{so['reff_next']['ci_low']:.3f}, {so['reff_next']['ci_high']:.3f}]")
        ok(so["attack"]["ci_low"] <= true_ar <= so["attack"]["ci_high"],
           f"AE3 the attack-rate interval covers the generator's value {true_ar:.3f}: "
           f"[{so['attack']['ci_low']:.3f}, {so['attack']['ci_high']:.3f}]")
        est = yaml.safe_load(open(os.path.join(d, "analysis", "estimates.yaml")))["data"]
        ok(est["decision"]["verdict"] in ("yes", "no", "undetermined") and est["decision"]["statement"],
           f"AE4 the threshold rule gives a verdict and its statement: {est['decision']['verdict']}")
        f = contract.gate_findings(d)
        kinds = sorted({x["kind"] for x in f})
        ok(kinds == ["analysis_contract_unsigned"], f"AE5 the contract checks pass on the estimates run (only the "
                                                    f"unsigned test-run finding): {kinds} {[x['claim'] for x in f][:2]}")
        txt = open(os.path.join(d, "results.md")).read()
        ok("[CLAIM:" not in txt and "What the data do and do not determine" in txt,
           "AE6 results.md is rendered with every claim resolved")
        man = json.load(open(os.path.join(d, "analysis", "manifest.json")))
        ok(man.get("deliverable_kind") == "estimates" and "allocation" not in {e["role"] for e in
                                                                               man["deliverables"].values()},
           "AE7 the manifest records the estimates kind and no allocation deliverable")
        hl = headline(d)
        ok("reff_next_median" in hl and "attack_hi" in hl, f"AE8 headline() reads the outputs: {sorted(hl)[:3]}")
        # draws are rows of the canonical sample: altering one applied value fails the contract check
        unc_p = os.path.join(d, "analysis", "uncertainty.yaml")
        doc = yaml.safe_load(open(unc_p))
        doc["data"]["draw_provenance"]["applied"][0]["values"]["r0"] *= 1.01
        from toolkit.analysis.contract import _posterior_provenance
        cal = yaml.safe_load(open(os.path.join(d, "analysis", "calibration.yaml")))["data"]
        ok(bool(_posterior_provenance("analysis/calibration.yaml", cal, doc["data"])),
           "AE9 a draw that is not a row of the canonical sample fails the provenance check")
        # the reporting fraction and s0 trade off: with s0 unknown, reporting is not determined
    with tempfile.TemporaryDirectory() as d:
        d = os.path.realpath(d)
        sp = ES.toy_dir(d, rows=rows)
        raw = yaml.safe_load(open(sp))
        raw["calibration"]["priors"]["s0"] = {"dist": "uniform", "range": [0.3, 1.0], "source": "toy: no serosurvey"}
        yaml.safe_dump(raw, open(sp, "w"), sort_keys=False)
        s2 = schema.load(sp)          # the analysis alone, with the automatic test of s0 only (self-test time)
        res = analyse(s2, s2.engine(), n_draws=400, prior_range=["s0"], log=lambda *a: None)
        est = {"determined": res["determined"], "decision_tests": res["tests"]}
        cls = res["classes"]["classes"]
        ok(cls.get("s0") == "prior_dominated" and cls.get("reporting") in ("prior_dominated", "partly_identified"),
           f"AE10 with s0 unknown, s0 and the reporting fraction are not determined by the series: {cls}")
        dd = est["determined"]
        ok(dd["attack"]["class"] == "set_by_declared_ranges" and "s0" in dd["attack"]["depends_on"]
           and dd["reff_next"]["class"] == "determined_by_data",
           f"AE11 with s0 unknown the attack rate is set by s0's declared range and reff is determined by the data "
           f"(attack {dd['attack']['class']} {dd['attack']['dependence']}; reff {dd['reff_next']['class']} "
           f"{dd['reff_next']['dependence']})")
        ok(any(t["parameter"] == "s0" and t["status"] == "run" and len(t["ends"]) == 2 for t in est["decision_tests"]),
           "AE12 an automatic prior_range test runs for s0 at both ends of its range")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
