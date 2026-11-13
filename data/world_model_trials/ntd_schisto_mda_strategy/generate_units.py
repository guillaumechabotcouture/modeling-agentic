#!/usr/bin/env python3
"""Synthetic units table for the ntd_schisto_mda_strategy world-model trial: seeded, standard library only.

    python generate_units.py <out.csv>

Each of pop, prev_sac, prev_adult is drawn independently of every other column (its own draw from its own distribution, no shared
term); id and region are labels.
"""
import csv
import math
import random
import sys


def main(out):
    rng = random.Random(2103)
    rows = []
    for i in range(20):
        row = {"id": "U%02d" % (i + 1), "region": "R%d" % (i % 4)}
        row["pop"] = format(rng.randint(80000, 600000), "d")
        row["prev_sac"] = format(rng.uniform(0.02, 0.15), ".4f")
        row["prev_adult"] = format(rng.uniform(0.01, 0.12), ".4f")
        rows.append(row)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
