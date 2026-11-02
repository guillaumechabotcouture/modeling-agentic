#!/usr/bin/env bash
# Validate the malaria engine once: equilibration evidence (v0.5), joint fit, full pack run, B15 mechanism scan,
# per-run cost (v0.5).
#   bash toolkit/engines/malaria/benchmarks/validate_once.sh [workers]
# Run from the repo root (on a VM: cloud/vm_job.py start ... --tree . -- bash <this>).
# Writes equilibration.yaml, fitted.yaml, results.yaml, b15_mechanisms.yaml and runtime.yaml beside the pack, and
# copies them (and the log) to $JOB_OUT when set.
set -euo pipefail
W="${1:-$(nproc 2>/dev/null || sysctl -n hw.ncpu)}"
D=toolkit/engines/malaria/benchmarks
PY="${PYTHON:-python3}"
"$PY" -W ignore "$D/equilibration.py" --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/equilibration.yaml" "$JOB_OUT/"
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" validate
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" fit --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/fitted.yaml" "$JOB_OUT/"
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" run --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/results.yaml" "$JOB_OUT/"
"$PY" -W ignore "$D/b15_mechanisms.py" --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/b15_mechanisms.yaml" "$JOB_OUT/"
"$PY" -W ignore "$D/runtime.py" --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/runtime.yaml" "$JOB_OUT/"
echo "validate_once: done"
