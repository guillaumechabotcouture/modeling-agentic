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


rng = random.Random(2301)
N = 24
rows = []
for i in range(N):
    pop = round(math.exp(rng.uniform(math.log(60000), math.log(400000))))  # log-uniform, independent
    prev = math.exp(rng.uniform(math.log(0.0015), math.log(0.009)))         # log-uniform, independent
    hiv = rng.uniform(0.01, 0.25)                                           # uniform, independent
    rows.append([f"U{i + 1:02d}", f"R{i % 4}", pop, f"{prev:.6f}", f"{hiv:.3f}"])
with open(sys.argv[1], "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["id", "region", "pop", "prev", "hiv"])
    w.writerows(rows)
