#!/usr/bin/env bash
# Validate the malaria engine once: equilibration evidence and starting-guess sensitivity (v0.5), joint fit, full
# pack run, B15 mechanism scan, per-run cost (v0.5).
#   bash toolkit/engines/malaria/benchmarks/validate_once.sh [workers] [batch_workers]
# Run from the repo root (on a VM: cloud/vm_job.py start ... --tree . -- bash <this>).
# batch_workers (default: workers) bounds the processes that run shared burn-ins (run_batch): each holds the
# burn-in sim, its snapshot and a member sim, up to about 5 GB at 4,000-5,000 agents (v0.5 measurement), so a
# 32-vCPU VM with 64 GiB runs 12 at most.
# Writes equilibration.yaml, guess_sensitivity.yaml, fitted.yaml, results.yaml, b15_mechanisms.yaml and runtime.yaml
# beside the pack, and copies them (and the log) to $JOB_OUT when set.
set -euo pipefail
W="${1:-$(nproc 2>/dev/null || sysctl -n hw.ncpu)}"
WB="${2:-$W}"
D=toolkit/engines/malaria/benchmarks
PY="${PYTHON:-python3}"
"$PY" -W ignore "$D/equilibration.py" --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/equilibration.yaml" "$JOB_OUT/"
"$PY" -W ignore "$D/guess_sensitivity.py" --workers "$WB"
[ -n "${JOB_OUT:-}" ] && cp "$D/guess_sensitivity.yaml" "$JOB_OUT/"
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" validate
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" fit --workers "$WB"
[ -n "${JOB_OUT:-}" ] && cp "$D/fitted.yaml" "$JOB_OUT/"
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" run --workers "$WB"
[ -n "${JOB_OUT:-}" ] && cp "$D/results.yaml" "$JOB_OUT/"
"$PY" -W ignore "$D/b15_mechanisms.py" --workers "$WB"
[ -n "${JOB_OUT:-}" ] && cp "$D/b15_mechanisms.yaml" "$JOB_OUT/"
"$PY" -W ignore "$D/runtime.py" --workers 6
[ -n "${JOB_OUT:-}" ] && cp "$D/runtime.yaml" "$JOB_OUT/"
echo "validate_once: done"
