#!/usr/bin/env python3
"""Synthetic units table for the early_growth_doubling_time world-model trial: seeded, standard library only.

    python generate_units.py <out.csv>

Each of pop, seed_share, s0 is drawn independently of every other column (its own draw from its own distribution, no shared
term); id and region are labels.
"""
import csv
import math
import random
import sys


def main(out):
    rng = random.Random(2109)
    rows = []
    for i in range(24):
        row = {"id": "U%02d" % (i + 1), "region": "R%d" % (i % 4)}
        row["pop"] = format(rng.randint(10000, 300000), "d")
        row["seed_share"] = format(10 ** rng.uniform(-6, -4), ".3e")
        row["s0"] = format(rng.uniform(0.5, 1.0), ".3f")
        rows.append(row)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
