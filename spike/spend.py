#!/usr/bin/env python3
"""Coiled credits used by this spike's clusters (billing API, filtered client-side by name).

    python3 spike/spend.py <start ISO UTC> [--prefix spike-v2-] [--batch-wall-min N] [--out spike/out/spend.json]

Coiled bills an instance when it stops, so run this after the clusters are down. The
credit price is an estimate from attempt 7 (2,457 credits ~ $150 of Azure VM time).
No pipeline LLM calls (critics, writer) were made by the spike: model_calls_usd is 0.
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "cloud"))
import run_cluster as rc  # noqa: E402

USD_PER_CREDIT = 0.061


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("start")
    ap.add_argument("--prefix", default="spike-v2-")
    ap.add_argument("--batch-wall-min", type=float, default=None)
    ap.add_argument("--out", default=os.path.join(HERE, "out", "spend.json"))
    a = ap.parse_args()
    end = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    ev = [e for e in rc.billing_events(a.start, end) if str(e.get("cluster") or "").startswith(a.prefix)]
    credits = round(-sum(float(e.get("amount_credits") or 0) for e in ev), 2)
    out = {"cluster_prefix": a.prefix, "window": [a.start, end], "credits": credits,
           "instances": len({e.get("instance") for e in ev}), "clusters": sorted({e.get("cluster") for e in ev}),
           "usd_per_credit": USD_PER_CREDIT, "usd_est": round(credits * USD_PER_CREDIT, 2), "model_calls_usd": 0.0,
           "batch_wall_min": a.batch_wall_min}
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(out, open(a.out, "w"), indent=1)
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
