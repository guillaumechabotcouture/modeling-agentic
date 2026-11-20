#!/usr/bin/env python3
"""Synthetic units table for the typhoid_tcv_age_targeting world-model trial: seeded, standard library only.

    python generate_units.py <out.csv>

Each of pop, inc_u5, inc_5_14, inc_adult is drawn independently of every other column (its own draw from its own distribution, no shared
term); id and region are labels.
"""
import csv
import math
import random
import sys


def main(out):
    rng = random.Random(2110)
    rows = []
    for i in range(20):
        row = {"id": "U%02d" % (i + 1), "region": "R%d" % (i % 4)}
        row["pop"] = format(rng.randint(100000, 900000), "d")
        row["inc_u5"] = format(rng.uniform(0.0001, 0.0015), ".6f")
        row["inc_5_14"] = format(rng.uniform(0.0002, 0.003), ".6f")
        row["inc_adult"] = format(rng.uniform(0.00005, 0.0008), ".6f")
        rows.append(row)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
