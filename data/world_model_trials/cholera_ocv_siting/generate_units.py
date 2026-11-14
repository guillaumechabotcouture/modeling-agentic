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


rng = random.Random(1101)
N = 24
rows = []
for i in range(N):
    pop = round(math.exp(rng.uniform(math.log(60000), math.log(400000))))  # log-uniform, independent
    inc = math.exp(rng.uniform(math.log(0.0005), math.log(0.008)))            # log-uniform, independent
    wash = rng.uniform(0.2, 0.85)                                            # uniform, independent
    rows.append([f"U{i + 1:02d}", f"R{i % 4}", pop, f"{inc:.6f}", f"{wash:.3f}"])
with open(sys.argv[1], "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["id", "region", "pop", "inc", "wash"])
    w.writerows(rows)
