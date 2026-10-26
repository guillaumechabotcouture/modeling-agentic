#!/usr/bin/env python3
"""
A second opinion on a run's post-mortem from a different model family
(gpt-6.1-sol through `codex exec -s read-only`): given autopsy.md and the
run's stored evidence, the most likely root cause and what to change first.
Writes autopsy_review_codex.md next to autopsy.md.

    python3 scripts/autopsy_review.py <run_dir> [--dry-run]
    python3 scripts/autopsy_review.py --self-test
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

OUT = "autopsy_review_codex.md"
PROMPT = """You are reviewing the post-mortem of one run of a multi-agent modeling pipeline (a lead agent drives
planner, data, modeler, analyst, critics and a writer; a deterministic gate decides each round). In the current
directory: autopsy.md (an automatic post-mortem; its failure layers are infrastructure, harness, agent behaviour,
model quality), plus the run's own files (trace.jsonl, validator_history.jsonl, final_gate.json, budget_ledger.json,
resources.jsonl, status.json, progress.md, critique_*.yaml, models/, report.md if any).

Independently of autopsy.md, answer from the evidence:
1. The most likely ROOT cause of how this run ended (one paragraph), and which layer it belongs to.
2. Where autopsy.md is wrong or incomplete, if anywhere (cite the file and line or field).
3. The single change most likely to improve the next run, and how we would know it worked.
4. Anything in the evidence that suggests a different problem than the one being fixed.
Cite file names and quote evidence for every claim. Be brief: at most 400 words. Do not modify files."""


def review(run_dir: str, dry_run: bool = False) -> str:
    if not os.path.exists(os.path.join(run_dir, "autopsy.md")):
        raise SystemExit(f"no autopsy.md in {run_dir} (run scripts/run_autopsy.py first)")
    out = os.path.join(run_dir, OUT)
    if dry_run:
        return f"[dry-run] would run codex in {run_dir} and write {out}"
    subprocess.run(["codex", "exec", "-m", "gpt-6.1-sol", "-s", "read-only", "-o", out, PROMPT],
                   cwd=run_dir, capture_output=True, text=True, timeout=3600)
    return open(out).read() if os.path.exists(out) else "(no review produced)"


def _self_test() -> int:
    import tempfile
    fails = []
    with tempfile.TemporaryDirectory() as d:
        try:
            review(d, dry_run=True)
            fails.append("R1 a run without autopsy.md must refuse")
        except SystemExit:
            pass
        open(os.path.join(d, "autopsy.md"), "w").write("# Run autopsy\n")
        if "would run codex" not in review(d, dry_run=True):
            fails.append("R2 dry run describes the call")
        if "Do not modify files" not in PROMPT or "ROOT cause" not in PROMPT:
            fails.append("R3 the prompt asks for an independent, read-only root-cause review")
    if fails:
        print("SELF-TEST FAILURES:", *fails, sep="\n  - ")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.run_dir:
        ap.error("run_dir required")
    print(review(a.run_dir, a.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
