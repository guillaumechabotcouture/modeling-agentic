#!/usr/bin/env python3
"""Synthetic units table for this world-model-trial template (describes no real place).

    python generate_units.py <out.csv>

Deterministic (seeded, standard library only). Each column listed in the spec's
analysis.emulator.independent_features is drawn from its own distribution, independently of every
other column; columns derived from others are noted below and are not declared independent.
"""
import csv
import math
import random
import sys


# pop and seed_share are drawn independently of each other and of prior_immune_share; s0 = 1 - prior_immune_share
# is derived (and is the calibration target column, which the design's swept s0 overrides).
rng = random.Random(1113)
rows = []
for i in range(24):
    pop = round(math.exp(rng.uniform(math.log(250000), math.log(3000000))))  # log-uniform, independent
    seed_share = 10 ** rng.uniform(-6.5, -4.5)                                 # log-uniform, independent
    imm = rng.uniform(0.0, 0.35)                                               # uniform, independent
    rows.append([f"C{i + 1}", pop, f"{seed_share:.4g}", f"{imm:.3f}", f"{1 - imm:.3f}"])
with open(sys.argv[1], "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["city", "pop", "seed_share", "prior_immune_share", "s0"])
    w.writerows(rows)
