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


# group_code (0 AGYW, 1 MSM, 2 serodiscordant partners) is assigned by design, one unit per group per district,
# and incidence is drawn within each group's own range, so inc depends on group_code: neither is declared
# independent. pop and adherence are drawn independently of every other column.
rng = random.Random(3701)
GROUPS = [("agyw", 0, 0.005, 0.04), ("msm", 1, 0.015, 0.08), ("sdc", 2, 0.02, 0.06)]
rows = []
for d in range(8):
    for name, code, lo, hi in GROUPS:
        pop = round(rng.uniform(5000, 30000))                              # uniform, independent
        inc = math.exp(rng.uniform(math.log(lo), math.log(hi)))            # log-uniform within the group's range
        adherence = rng.uniform(0.3, 0.85)                                 # uniform, independent
        rows.append([f"D{d + 1}_{name}", f"D{d + 1}", pop, f"{inc:.5f}", code, f"{adherence:.3f}"])
with open(sys.argv[1], "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["id", "region", "pop", "inc", "group_code", "adherence"])
    w.writerows(rows)
