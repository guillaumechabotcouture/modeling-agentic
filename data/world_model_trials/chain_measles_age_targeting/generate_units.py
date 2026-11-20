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


# susc_9_59m and susc_5_14y are drawn independently of each other and of the population; mean_susceptible is
# derived from them and the group sizes (not declared independent). The age shares are fixed.
rng = random.Random(20269032)
G = ["u9m", "m9_59", "y5_14", "a15p"]
SH = {"u9m": 0.022, "m9_59": 0.123, "y5_14": 0.27}
SH["a15p"] = 1 - sum(SH.values())
rows = []
for i in range(24):
    n = round(rng.uniform(150000, 700000))                                 # uniform, independent
    s_young = rng.uniform(0.15, 0.55)                                      # uniform, independent
    s_school = rng.uniform(0.01, 0.20)                                     # uniform, independent
    s = [1.0, round(s_young, 4), round(s_school, 4), 0.0]
    N = [round(n * SH[g]) for g in G]
    rows.append({"district": f"D{i + 1:02d}", "pop": sum(N), "pop_9_59m": N[1], "pop_9m_14y": N[1] + N[2],
                 "n_groups": " ".join(map(str, N)), "s0_groups": " ".join(f"{x:.4f}" for x in s),
                 "susc_9_59m": s[1], "susc_5_14y": s[2],
                 "mean_susceptible": round(sum(a * b for a, b in zip(N, s)) / sum(N), 5), "r0_target": 13.0})
with open(sys.argv[1], "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)
