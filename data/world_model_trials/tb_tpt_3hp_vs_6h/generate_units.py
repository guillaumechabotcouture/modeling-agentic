#!/usr/bin/env python3
"""Synthetic units table for the tb_tpt_3hp_vs_6h world-model trial: seeded, standard library only.

    python generate_units.py <out.csv>

Each of pop, tb_rate, hiv is drawn independently of every other column (its own draw from its own distribution, no shared
term); id and region are labels.
"""
import csv
import math
import random
import sys


def main(out):
    rng = random.Random(2105)
    rows = []
    for i in range(20):
        row = {"id": "U%02d" % (i + 1), "region": "R%d" % (i % 4)}
        row["pop"] = format(rng.randint(2000, 20000), "d")
        row["tb_rate"] = format(rng.uniform(0.004, 0.03), ".5f")
        row["hiv"] = format(rng.uniform(0.01, 0.3), ".3f")
        rows.append(row)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
