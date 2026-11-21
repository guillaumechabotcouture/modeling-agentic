#!/usr/bin/env python3
"""Time one engine member under the Starsim of the interpreter that runs this: benchmarks/runtime.py's v05_shared
task (one run_batch() of 4 packages: none, pyr, dual+SMC, enhanced CM; 4,000 agents; the card's 50-year burn-in;
horizon 2016-2027), at one point, on one core.

    <python> spike/ssv4/timing.py --tree <repo root> [--m0 9] [--seed 1]

The v3 number comes from /software/conda/envs/modag/bin/python with --tree pointing at the unchanged code.
"""
import argparse
import json
import os
import sys

ap = argparse.ArgumentParser()
ap.add_argument("--tree", required=True)
ap.add_argument("--m0", type=float, default=9.0)
ap.add_argument("--seed", type=int, default=1)
a = ap.parse_args()
sys.path.insert(0, os.path.abspath(a.tree))
import starsim  # noqa: E402
from toolkit.engines.malaria.engine import VERSION  # noqa: E402
from toolkit.engines.malaria.benchmarks.runtime import _task  # noqa: E402

r = _task((a.m0, a.seed, "v05_shared"))
r.pop("outcomes")
print(json.dumps(dict(r, starsim=starsim.__version__, engine=VERSION)))
