#!/usr/bin/env python3
"""Synthetic units table for the amr_crp_delayed_prescribing world-model trial: seeded, standard library only.

    python generate_units.py <out.csv>

Each of pop, rx_rate, resp_share is drawn independently of every other column (its own draw from its own distribution, no shared
term); id and region are labels.
"""
import csv
import math
import random
import sys


def main(out):
    rng = random.Random(2101)
    rows = []
    for i in range(24):
        row = {"id": "U%02d" % (i + 1), "region": "R%d" % (i % 4)}
        row["pop"] = format(rng.randint(4000, 30000), "d")
        row["rx_rate"] = format(rng.uniform(0.15, 0.6), ".4f")
        row["resp_share"] = format(rng.uniform(0.2, 0.5), ".3f")
        rows.append(row)
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    main(sys.argv[1])
