#!/usr/bin/env bash
# Validate the malaria engine once: joint fit, full pack run, B15 mechanism scan.
#   bash toolkit/engines/malaria/benchmarks/validate_once.sh [workers]
# Run from the repo root (on a VM: cloud/vm_job.py start ... --tree . -- bash <this>).
# Writes fitted.yaml, results.yaml and b15_mechanisms.yaml beside the pack, and copies them
# (and the log) to $JOB_OUT when set.
set -euo pipefail
W="${1:-$(nproc 2>/dev/null || sysctl -n hw.ncpu)}"
D=toolkit/engines/malaria/benchmarks
PY="${PYTHON:-python3}"
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" validate
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" fit --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/fitted.yaml" "$JOB_OUT/"
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" run --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/results.yaml" "$JOB_OUT/"
"$PY" -W ignore "$D/b15_mechanisms.py" --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/b15_mechanisms.yaml" "$JOB_OUT/"
echo "validate_once: done"
