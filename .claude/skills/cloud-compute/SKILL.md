---
name: cloud-compute
description: When and how to scale model execution to the cloud via
  modelops-calabaria (mops). Decision rule for local vs cloud based on
  outcome_fn cost and N draws. Azure AKS + Dask architecture with spot-
  instance node pools for 60-80% cost reduction. Template infra.yaml with
  spot config, budget guards, and auto-teardown. Specifies which stages
  benefit most (STAGE 5b UNCERTAINTY with N>100 draws, multi-structural
  LOO-CV for expensive full models, ensemble calibration). Use when the
  modeler is deciding whether to implement a cloud path, configuring
  infra.yaml, or planning a workload that exceeds 10 worker-hours of local
  compute. Trigger phrases include "cloud compute", "mops", "AKS", "spot
  instance", "Dask cluster", "cost control", "distributed calibration",
  "modelops infra".
---

# Cloud Compute via modelops-calabaria (`mops`)

## Overview

`modelops-calabaria` ships two CLIs: `cb` (local science) and `mops`
(cloud infrastructure on Azure AKS + Dask). See
`.claude/skills/modelops-calabaria/SKILL.md` for the full framework;
this skill focuses on the **decision rule** (when to go cloud) and
**cost control** via spot instances.

## When to use cloud

Rule of thumb, keyed off `outcome_fn` cost and the number of
evaluations required:

| Workload                                               | Local time          | Decision                     |
|--------------------------------------------------------|---------------------|------------------------------|
| UQ: 200 draws, outcome_fn < 2s                         | <10 min             | LOCAL                        |
| UQ: 200 draws, outcome_fn 2–30s                        | 10 min – 2 hr       | LOCAL if convenient          |
| UQ: 200 draws, outcome_fn > 30s                        | >2 hr               | **CLOUD** (or surrogate first)|
| Per-unit stability: 1000+ draws for 774 LGAs each      | Many hours–days     | **CLOUD**                    |
| Multi-structural LOO-CV: full model refit 22+ times    | Depends on model    | CLOUD if full > 5 min/fit    |
| Optuna calibration: 1000+ trials                       | Hours–days          | **CLOUD** (8–16 workers)     |
| Identifiability profile scan: 20 × N params × loss     | Usually fast local  | LOCAL                        |

**Rule of thumb**: if the total compute is >10 worker-hours, cloud.
If <2 worker-hours, local. In between is a judgment call based on
whether the modeler is iterating rapidly (local) or finalizing (cloud
is fine to wait for).

**Alternative to cloud**: build a surrogate (emulator on a sparse grid
of full-model runs). This is often the fastest path for UQ because it
turns a 200× ABM workload into 200× cheap emulator calls after ~30
full runs. See the `uncertainty-quantification` skill.

## Azure AKS + spot node pools (cost control)

Azure AKS supports two node-pool types:

- **On-demand pool**: standard VMs at full price. Used for the
  Dask scheduler and any must-not-interrupt workloads.
- **Spot pool**: preemptible VMs at 60–90% discount. Interrupted
  with ~30 seconds' notice when Azure reclaims capacity. Dask is
  interrupt-tolerant if each SimTask is idempotent and results are
  persisted — which they are under `mops`'s default job model.

For UQ, multi-structural, and Optuna workloads, **the workers belong
on spot**. The scheduler stays on on-demand (small, cheap, shouldn't be
interrupted).

### Template `infra.yaml` with spot workers

Drop this into `{run_dir}/infra.yaml` or the project root. Adjust the
subscription/resource_group/region fields to match your Azure account.

```yaml
# infra.yaml — Azure AKS with spot worker pool for cost-controlled
# distributed model execution. Tear down after every run with
# `mops infra down` to stop billing.

cluster:
  name: modeling-agentic-cluster
  subscription_id: ${AZURE_SUBSCRIPTION_ID}   # from environment
  resource_group: modeling-agentic-rg
  region: eastus2                              # highest spot availability
  kubernetes_version: "1.29"

node_pools:
  # Scheduler pool: small, always-on, not interruptible.
  - name: scheduler
    vm_size: Standard_D2s_v5      # 2 vCPU, 8 GB — sufficient for Dask scheduler
    node_count: 1
    spot: false
    auto_scale: false
    labels:
      role: scheduler
    taints:
      - "role=scheduler:NoSchedule"

  # Worker pool: spot instances, scale-to-zero when idle.
  - name: workers-spot
    vm_size: Standard_D8s_v5      # 8 vCPU, 32 GB per worker
    min_count: 0                   # scale down to 0 when cluster idle
    max_count: 16                  # cap at 16 workers
    spot: true
    spot_max_price: 0.20           # USD per hour per VM; spot VMs priced
                                   # against this ceiling, interrupted if
                                   # on-demand price exceeds it
    spot_eviction_policy: Delete
    auto_scale: true
    labels:
      role: worker
      spot: "true"

dask:
  scheduler:
    cpu: 2
    memory: 8Gi
    node_selector:
      role: scheduler
  workers:
    cpu: 8
    memory: 32Gi
    node_selector:
      role: worker
    # Ensure workers tolerate the non-spot taint / prefer spot nodes.
    tolerations:
      - key: "kubernetes.azure.com/scalesetpriority"
        value: "spot"
        effect: "NoSchedule"

# Budget guard: aborts the run if cumulative cost exceeds this ceiling.
budget:
  max_usd: 50                       # per-run cap; override with --budget CLI
  warn_at_usd: 30
  check_interval_seconds: 300

# Idle auto-teardown: if no jobs have run for this many minutes, tear
# down. Prevents forgotten clusters from burning budget.
auto_teardown:
  enabled: true
  idle_minutes: 30
```

### Workflow with spot

```bash
# 1. Set credentials (once).
export AZURE_SUBSCRIPTION_ID=...
export AZURE_CLIENT_ID=...            # service principal
export AZURE_CLIENT_SECRET=...
export AZURE_TENANT_ID=...
az login --service-principal \
    -u $AZURE_CLIENT_ID \
    -p $AZURE_CLIENT_SECRET \
    --tenant $AZURE_TENANT_ID

# 2. Stand up the cluster (scheduler + 0 spot workers initially).
mops infra up --config infra.yaml

# 3. Package the model (OCI bundle).
mops bundle push models/ --tag malaria-v1.0

# 4. Submit the UQ job. Workers auto-scale up on spot.
mops jobs submit uq \
    --bundle malaria:v1.0 \
    --n-draws 200 \
    --workers 8 \
    --outcome-fn models/outcome_fn.py::outcome_fn \
    --registry-path citations.md

# 5. Monitor.
mops jobs status uq
mops jobs logs uq --tail

# 6. Pull results.
mops jobs results uq --output uncertainty_report.yaml

# 7. Tear down — CRITICAL. Spot nodes still cost money when idle.
mops infra down
```

## Estimated costs (USD, rough)

Eastus2 spot prices as of late 2025 (always verify before a run):

| VM size           | On-demand | Spot (typical) | Spot ceiling |
|-------------------|-----------|----------------|--------------|
| Standard_D2s_v5   | $0.096/hr | —              | —            |
| Standard_D8s_v5   | $0.384/hr | $0.05-0.12/hr  | $0.20/hr     |
| Standard_D16s_v5  | $0.768/hr | $0.10-0.24/hr  | $0.40/hr     |
| Standard_D32s_v5  | $1.536/hr | $0.20-0.50/hr  | $0.80/hr     |

Example workload: 200 UQ draws × 30s each × 8 workers = ~12 minutes
wall time. Scheduler on-demand ($0.096 × 0.5h) + 8 spot workers at
~$0.10/hr for 12 min = ~$0.21 + $0.16 = **<$0.50**.

Example heavy: 1000 Optuna trials × 60s each × 16 workers = ~1 hour.
Cluster total: ~$0.10 (scheduler) + 16 × $0.10/hr × 1h = **~$1.70**.

The `budget.max_usd` guard in `infra.yaml` is a hard abort. Always set
it before running; default to **$50 per run** unless the workload
demands more.

## When NOT to use cloud

- **Exploratory iteration**: the modeler is changing outcome_fn code
  every 5 minutes. Cloud turnaround is too slow.
- **Small N (<100) and fast fn (<5s)**: trivially local.
- **Credential / access barriers**: if the Azure setup isn't in place,
  don't hack around it — either request credentials or build a
  surrogate for local runs.
- **Interactive debugging**: run the outcome_fn on a single sample
  locally first to catch bugs. Cloud amplifies latency on debugging.

## Modeler's workflow: "when to cloud-ify"

1. First, run `outcome_fn` on a single parameter draw locally. Verify
   it returns the expected dict shape and finishes in reasonable time.
2. Run 10 draws locally with `propagate_uncertainty.py --n-draws 10`
   to catch shape/unit bugs cheaply.
3. Estimate total cost: 200 draws × (measured per-call time). If >2
   hours, decide: surrogate (build one, re-run local) or cloud.
4. For cloud: verify credentials are in place. Bring up the cluster,
   verify with a tiny job (`mops jobs submit uq --n-draws 10`), then
   scale up.
5. Tear down when done. Check `az` or Azure Portal to confirm no
   lingering resources.

## Security notes

- Never commit Azure credentials to the repo. Use environment
  variables (`.env` is git-ignored) or Azure Managed Identity.
- The budget guard protects against runaway spend, but a forgotten
  cluster at idle (spot pool scaled to 0, scheduler alone) still
  costs ~$0.10/hour — $72/month if left running. Always tear down.
- Spot nodes are preemptible. If a job can't tolerate interruption
  (e.g., a single long-running simulation with no checkpointing),
  run it on on-demand instead. Dask's default behavior re-runs an
  interrupted SimTask on another worker, so Optuna/UQ workloads are
  interruption-tolerant by design.

## Integration points in the pipeline

- **STAGE 5b UNCERTAINTY**: `propagate_uncertainty.py` doesn't call
  `mops` directly. The modeler decides at outcome_fn design time
  whether to build a cloud-backed callable (which would
  `mops jobs submit` internally and block on results) or a local
  one. The skill documents both patterns.
- **Multi-structural comparison**: full-model LOO-CV is often cloud-
  sized (22 refits × full-model cost). Modeler can use `mops jobs
  submit loocv --bundle ...` as the refit engine, or build a
  surrogate fit-from-summary.
- **Optuna calibration**: well-supported out of the box by `mops jobs
  submit calibrate`. This is the canonical calabaria use case.

## Summary recommendation

- **Default local**. Build a surrogate before reaching for cloud.
- **Use spot**. Workers on spot, scheduler on on-demand. The 60–80%
  cost savings apply to the bulk of the workload.
- **Budget guard + auto-teardown**. Both in `infra.yaml`. Non-negotiable.
- **Keep credentials out of git**. Env vars only.
- **Validate with a 10-draw pilot job** before scaling to 200+.
