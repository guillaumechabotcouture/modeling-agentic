#!/usr/bin/env python3
"""Synthetic units table for the amr_hospital_stewardship world-model trial: seeded, standard library only.

    python generate_units.py <out.csv>

Each of pop, cdi_rate, surgical_share is drawn independently of every other column (its own draw from its own distribution, no shared
term); id and region are labels.
"""
import csv
import math
import random
import sys


def main(out):
    rng = random.Random(2102)
    rows = []
    for i in range(20):
        row = {"id": "U%02d" % (i + 1), "region": "R%d" % (i % 4)}
        row["pop"] = format(rng.randint(5000, 60000), "d")
        row["cdi_rate"] = format(rng.uniform(0.001, 0.008), ".5f")
        row["surgical_share"] = format(rng.uniform(0.15, 0.55), ".3f")
        rows.append(row)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
