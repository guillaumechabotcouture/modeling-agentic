#!/usr/bin/env python3
"""The trajectory step: modelled monthly series for a stratified sample of units (docs/design/v2-figures.md §5).

    python -m toolkit.analysis.trajectories run <run_dir> [--per-region 3] [--seeds 3] [--metrics a,b] [--workers N]
    python3 toolkit/analysis/trajectories.py --self-test

For a finished run: per region, the units at the lowest, median and highest comparator burden
(analysis/figure_data.yaml `burden`; fewer when the region has fewer units) are sampled and recorded. For each
sampled unit and seed, one compute task runs the comparator and the unit's plan package through the engine's
`run_series` hook (toolkit/engines/hooks.py) at the point parameters and the unit's calibrated attribute, through
scripts/compute.py `run_map` (the same path as the batch, so a real run sends it to Coiled; rows are kept per
input, so a stopped step resumes). It writes analysis/trajectories.yaml (the analysis envelope; the manifest is
not touched): the sample, the series per unit (mean over seeds), and population-weighted series per region and in
total, with the calibration target per region where the metric is the calibration metric. An engine without the
hook gives `status: not_offered` with the reason, and the figure is recorded as not produced.

Cost: tasks = sampled units x seeds, each one shared-history run with two arms; at 3 units per region and 3 seeds
that is 9 tasks per region. Nothing here names a disease, component or unit.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys

import numpy as np
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PRODUCER = "toolkit/analysis/trajectories.py"
OUT = os.path.join("analysis", "trajectories.yaml")
ROWS = os.path.join("analysis", "trajectories", "series.jsonl")


def _data(run_dir, kind):
    raise NotImplementedError("figures β")


def sample_units(units: dict, burden: dict, per_region: int) -> dict:
    """{region: [unit ids]}: per region, `per_region` units spread over the comparator-burden order (lowest, median,
    highest for 3), deterministic. `units`: {id: region}."""
    raise NotImplementedError("figures β")


def tasks(run_dir: str, per_region: int = 3, seeds: int = 3, metrics=None) -> tuple[list[dict], dict]:
    """(compute inputs, plan of the step) for a finished run. Raises hooks.HookUnsupported without the hook."""
    raise NotImplementedError("figures β")


def run_task(task: dict) -> dict:
    """The compute.py function: one unit and seed, both arms, through the engine's run_series hook."""
    raise NotImplementedError("figures β")


def aggregate(run_dir: str, step: dict, results: list[dict]) -> dict:
    """Seed means per unit and arm, then population-weighted means per region and in total."""
    raise NotImplementedError("figures β")


def write(run_dir: str, data: dict, inputs: list[str]) -> str:
    raise NotImplementedError("figures β")


def run(run_dir: str, per_region: int = 3, seeds: int = 3, metrics=None, log=print, **compute_kw) -> dict:
    raise NotImplementedError("figures β")


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    import tempfile
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    smp = sample_units({f"u{i}": f"r{i % 2}" for i in range(10)}, {f"u{i}": float(i) for i in range(10)}, 3)
    ok(smp == {"r0": ["u0", "u4", "u8"], "r1": ["u1", "u5", "u9"]},
       f"TJ1 per region: the lowest, median and highest burden units ({smp})")
    ok(sample_units({"a": "r", "b": "r"}, {}, 3) == {"r": ["a", "b"]}, "TJ2 a small region gives all its units")
    from toolkit.analysis import run as RUN
    from toolkit.spec import schema, signoff
    q = lambda *a, **k: None  # noqa: E731
    keep = os.environ.get("TRAJ_SELFTEST_KEEP")          # a directory to keep the rendered figures in (optional)
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as figs:
        figs = keep or figs
        bench, v_ok, v_bad = RUN._toy_bench(d)
        schema.toy_spec(d, n=12, bench=bench, dt_values=(v_ok, v_bad))
        signoff.sign(d, "lead", note="self-test")
        RUN.run(d, log=q, compute_kw={"max_workers": 2})
        res = run(d, log=q)
        ok(res["status"] == "not_offered" and "run_series()" in res["reason"]
           and os.path.exists(os.path.join(d, OUT)),
           f"TJ3 an engine without the series hook records not_offered with the reason: {res.get('reason')}")
        from toolkit.analysis import figures as FG
        r = FG.render(d, os.path.join(figs, "a"))
        why = {x["figure"]: x["reason"] for x in r["not_produced"]}.get("trajectories", "")
        ok("needs trajectory runs" in why and "run_series()" in why,
           f"TJ4 the figure is not produced and says why: {why}")
        # the same run with an engine that offers the hook (the toy engine plus hooks)
        sp = os.path.join(d, "decision_spec.yaml")
        raw = yaml.safe_load(open(sp))
        raw["engine"]["ref"] = "toolkit.engines.hooks:ToyScheduleEngine"
        yaml.safe_dump(raw, open(sp, "w"), sort_keys=False)
        res = run(d, per_region=2, seeds=2, log=q, max_workers=2)
        tot = res["series"]["total"]
        n_units = sum(len(v) for v in res["sample"].values())
        ok(res["status"] == "ok" and n_units == 6 and len(tot["t"]) == 36
           and set(tot["arms"]) == {"comparator", "plan"},
           f"TJ5 with the hook: {n_units} sampled units, monthly series for both arms, per region and in total")
        pre = [k for k, tt in enumerate(tot["t"]) if tt < res["delivery"][0]]
        same_before = all(abs(tot["arms"]["comparator"]["prev"][k] - tot["arms"]["plan"]["prev"][k]) < 1e-12
                          for k in pre)
        after = [k for k, tt in enumerate(tot["t"]) if tt >= res["delivery"][0]]
        lower_after = all(tot["arms"]["plan"]["prev"][k] <= tot["arms"]["comparator"]["prev"][k] + 1e-12 for k in after)
        ok(same_before and lower_after, "TJ6 the arms agree before delivery and the plan is not above the comparator after")
        ok(res["series"]["total"]["target"].get("value") is not None, "TJ7 the calibration target is carried per panel")
        r2 = FG.render(d, os.path.join(figs, "b"))
        ok(any(f["figure"] == "trajectories" for f in r2["figures"]), "TJ8 the trajectories figure is produced")
    print("OK: all self-test cases passed." if not fails else f"FAILED: {len(fails)} case(s)")
    return 1 if fails else 0


def main(argv=None) -> int:
    raise NotImplementedError("figures β")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    sys.exit(main())
