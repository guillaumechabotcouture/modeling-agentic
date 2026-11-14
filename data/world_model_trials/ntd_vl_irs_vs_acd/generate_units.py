#!/usr/bin/env python3
"""Synthetic units table for the ntd_vl_irs_vs_acd world-model trial: seeded, standard library only.

    python generate_units.py <out.csv>

Each of pop, vl_rate, mud_share is drawn independently of every other column (its own draw from its own distribution, no shared
term); id and region are labels.
"""
import csv
import math
import random
import sys


def main(out):
    rng = random.Random(2104)
    rows = []
    for i in range(24):
        row = {"id": "U%02d" % (i + 1), "region": "R%d" % (i % 4)}
        row["pop"] = format(rng.randint(40000, 200000), "d")
        row["vl_rate"] = format(rng.uniform(0.00005, 0.0012), ".6f")
        row["mud_share"] = format(rng.uniform(0.2, 0.9), ".3f")
        rows.append(row)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
