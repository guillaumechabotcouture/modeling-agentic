#!/usr/bin/env bash
# Fit and run the pack only (no mechanism scans): re-stamps fitted.yaml and results.yaml.
#   bash toolkit/engines/malaria/benchmarks/fit_and_run.sh [workers]
set -euo pipefail
W="${1:-$(nproc 2>/dev/null || sysctl -n hw.ncpu)}"
D=toolkit/engines/malaria/benchmarks
PY="${PYTHON:-python3}"
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" validate
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" fit --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/fitted.yaml" "$JOB_OUT/"
"$PY" -W ignore -m toolkit.engines.benchmark "$D/pack.yaml" run --workers "$W"
[ -n "${JOB_OUT:-}" ] && cp "$D/results.yaml" "$JOB_OUT/"
echo "fit_and_run: done"
