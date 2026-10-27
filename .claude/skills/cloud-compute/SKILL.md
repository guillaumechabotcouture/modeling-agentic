---
name: cloud-compute
description: >-
  Parallel execution of model evaluations (UQ draws, calibration grids and
  trial batches, scenario sweeps, LOO-CV folds, sensitivity points). Primary:
  `scripts/compute.py map`, which fans a batch out over the run's Coiled
  compute workers (local process pool elsewhere). Legacy, opt-in: Azure Batch
  via scripts/cloud_batch.py (`--cloud`). Trigger phrases include "parallel",
  "calibration grid", "batch of runs", "parallel UQ", "distributed
  calibration", "cloud compute", "speed up", "too slow".
---

# Parallel model evaluations: the compute service (primary)

`scripts/compute.py map` runs one function over many parameter sets:

```bash
python3 scripts/compute.py map {run_dir} --fn models/<module>.py::<fn> \
    --inputs {run_dir}/models/<batch>_inputs.jsonl --out {run_dir}/models/<batch>_results.jsonl \
    --purpose "<finding IDs, or 'plan: <item>'>"
```

- **`--purpose` is required.** Name the critique finding IDs the batch
  addresses (`"D-019, R-021"`) or the plan item it serves
  (`"plan: calibration grid"`). It is stored with the call in
  `models/compute_ledger.json`, and `scripts/convergence_report.py` reports
  core-hours per purpose and per finding, with the share lost to failed rows.

- `<fn>(params: dict) -> dict` evaluates ONE parameter set. Keep it
  deterministic given its params (put seeds in the params).
- **Where it runs.** On Coiled, the launcher gives each run compute workers and
  sets `MA_COMPUTE_SCHEDULER`; each worker runs its share across all of its
  cores. Elsewhere, it uses a local process pool.
- **What workers see.** The run's `models/` and `data/` (and the repo's
  `scripts/`) are unpacked at the same absolute paths. Files over 100 MB are
  left out and listed in `<out>.summary.json`; pass what the function needs as
  params, or keep inputs small.
- **Memory and failures are handled by the service.** It measures each
  process's peak memory and runs as many processes per VM as fit (starting
  from the function's last measured peak, in `models/compute_ledger.json`).
  A process that dies (out of memory) or a lost VM: its inputs are rerun at
  lower concurrency. An input that kills its process even when run alone
  fails as a model error. The same model error repeating stops the batch
  early. `MA_COMPUTE_PROCS_PER_WORKER` remains as an upper limit, rarely needed.
- **Completing a batch:** rerun the same command. Rows already computed with
  the same code, data and params are kept; only missing or failed rows run
  (`--fresh` recomputes everything). No retry or merge scripts.
- **Failure kinds** in each failed row: `model` (the model raised or cannot
  run alone), `infra` (the machine failed after retries), `stopped` (cap,
  timeout, or the repeated-error stop). Results files with rows that have no
  result are a gate finding (`compute_results_incomplete`).
- **Results and accounting.** Results come back in input order; a failing row
  fails alone. Core-seconds are recorded in `models/compute_ledger.json`.
  Each call has an allowance (`--max-core-hours`, default 50) within the
  run-wide cap; rows not reached when it runs out fail with
  `stopped: compute cap reached`. Split a larger study into several calls,
  or raise `--max-core-hours` for one call.
- **Iterative calibration:** batch it. Ask K trials, evaluate them with one
  `map` call, tell K.

# Legacy: Cloud Compute via Azure Batch (opt-in, `--cloud`)

## What this skill chose and why

**Azure Batch** is the primary cloud surface for this project. It's the
right tool for every workload we have:

- Task-parallel execution of pre-built scripts / containers.
- Pool of VMs (on-demand or low-priority / spot).
- Auto-scales pool to zero when no jobs queued.
- Native "low-priority nodes" = ~60–80% cost reduction vs on-demand,
  with 30-second eviction notice (tasks are automatically re-queued).
- No cluster overhead: pool sits at zero nodes when idle, $0/hr.
- Dead-simple Python SDK (`azure.batch`).

**Azure Kubernetes Service (AKS) + Dask** is the escalation path. Better
for: persistent interactive clusters, Dask-distributed calabaria
workflows with fast iteration, workloads that benefit from a live Dask
scheduler managing state across tasks. The calabaria `mops` CLI
targets this architecture — use it when you have `mops` available and
the workload justifies the cluster overhead.

For this project as of Phase 2: **Batch first, AKS only if Batch falls short.**

## Workload → Batch mapping

| Workload | Pattern | Task shape | Typical cost (low-priority, Standard_D4s_v5) |
|---|---|---|---|
| **Single ABM run** | 1 task, 1 VM | Python script or container takes params CSV, writes output CSV to Blob | ~$0.02 / hr (1 VM × 1 hr × $0.025/hr) |
| **UQ propagation** | N tasks in parallel | Each task: sample params, run `outcome_fn`, write result to blob | 200 tasks × 30s × ~$0.001/task ≈ **$0.20** |
| **Optuna calibration** | M parallel workers, shared Optuna storage | Each worker pings storage for next trial, runs ABM, reports loss | 1000 trials × 1 min × 8 parallel × $0.025/hr = ~**$1.70** |
| **Multi-structural LOO-CV** | N × M tasks (N models × M leave-one-out partitions) | Each task refits one model with one partition held out | ~$0.30 for 22 targets × 3 models |
| **Sensitivity analysis** | Grid or Sobol in parallel | Same as UQ but with structured parameter draws | Same as UQ |

Every class reduces to "submit N tasks to a Batch pool, each task runs
a function, collect outputs." The `scripts/cloud_batch.py` helper
abstracts this.

## Spot / low-priority mechanics (cost control)

Azure Batch has two priority types for nodes in a pool:

- **Dedicated**: on-demand VMs. Full price. Never interrupted.
- **Low-priority**: preemptible VMs at ~60–80% discount. Can be
  reclaimed by Azure with 30-second notice. Batch automatically
  re-queues interrupted tasks on another node.

**For this project: all pools are low-priority unless a task literally
can't tolerate interruption** (e.g., a multi-hour ABM simulation with no
checkpointing). For most workloads — where each task is idempotent and
writes outputs when done — interruption is free: the task just runs
again. Dozens of interruptions across hundreds of tasks still come in
at 60–80% of on-demand cost.

### Typical per-hour costs (eastus2, late 2025)

| VM size | On-demand | Low-priority (typical) |
|---|---|---|
| Standard_A1_v2 (1 vCPU, 2 GB) | $0.043 | $0.010 |
| Standard_D2s_v5 (2 vCPU, 8 GB) | $0.096 | $0.020 |
| Standard_D4s_v5 (4 vCPU, 16 GB) | $0.192 | $0.040 |
| Standard_D8s_v5 (8 vCPU, 32 GB) | $0.384 | $0.080 |
| Standard_D16s_v5 (16 vCPU, 64 GB) | $0.768 | $0.160 |

Most malaria-style ABMs run well on D4s_v5. A 200-draw UQ in 8
parallel workers completes in ~12 min of wall time = **$0.07** at
low-priority pricing. Dozens of test runs per Free Trial $200 credit.

## Free Trial quota realities (READ THIS BEFORE CHOOSING A VM SIZE)

New personal Azure subscriptions are Free Trial by default. The quota
structure has a trap that bit us on first provisioning:

- **Total dedicated core quota**: usually 4 on Free Trial (look at
  `az batch account show -n <name> -g <rg> --query dedicatedCoreQuota`).
- **Low-priority / spot quota**: **0 on Free Trial**. Spot nodes are
  disabled until you upgrade to Pay-As-You-Go or request an increase.
- **Per-family quotas on top of the total**: Azure enforces separate
  quotas per VM SKU family. Even if your total quota is 4 cores, that
  doesn't mean you can run 1 × Standard_D4s_v5 (4 cores) — the
  **standardDSv5Family** quota may be separate and small. The first
  error you'll see is:
  `AccountVMSeriesCoreQuotaReached: The specified account has reached
   VM series core quota for standardDSv5Family`.

**The fix**: use the **A-series** on Free Trial. A1_v2 (1 vCPU) and
A2_v2 (2 vCPUs) live in a different family with looser quota. For
modeling-agentic's UQ workloads, **Standard_A2_v2** at 2 nodes = 4
total vCPUs fits comfortably in Free Trial and still runs surrogate-
backed outcome_fns in parallel. That's what our defaults are set to.

Upgrade to Pay-As-You-Go when you outgrow A-series:
- Upgrade process: portal → Subscriptions → "Upgrade" button. $0 to
  upgrade itself.
- Request quota increase for DSv5 family in the portal after upgrade.
- Set up a Budget alert in Cost Management BEFORE upgrade (Free Trial's
  spending limit turns off on upgrade).

**Check before first pool create**:
```bash
# Per-family quotas in your region
az vm list-usage -l eastus2 --query "[?limit>\`0\`].{family:name.value, used:currentValue, limit:limit}" -o table
```

Or before a specific pool attempt, check with a dry-run:
```bash
python3 scripts/cloud_batch.py --estimate-cost --vm-size Standard_A2_v2 \
    --n-tasks 200 --avg-seconds 30 --max-nodes 2
```

## Default VM sizes in modeling-agentic

- `DEFAULT_VM_SIZE` in `cloud_batch.py` = **Standard_A2_v2** (Free Trial safe).
- `propagate_uncertainty.py --cloud-vm-size` default = **Standard_A2_v2**.
- `propagate_uncertainty.py --cloud-max-nodes` default = **2**.

When you have Pay-As-You-Go + DSv5 quota, override with flags:
```bash
python3 scripts/propagate_uncertainty.py {run_dir} --cloud \
    --cloud-vm-size Standard_D4s_v5 --cloud-max-nodes 8
```

## The `scripts/cloud_batch.py` wrapper

Python API exposed by the wrapper:

```python
from scripts.cloud_batch import BatchRunner

runner = BatchRunner(
    subscription_id="048cd8f6-c126-46f7-919c-a895e9a8e2cd",
    batch_account="mymodelingruns",
    batch_account_url="https://mymodelingruns.eastus2.batch.azure.com",
    storage_account="mymodelingstorage",
    resource_group="modeling-agentic-rg",
)

# 1. Create a pool (autoscale 0 → max_nodes, low-priority).
runner.create_pool(
    pool_name="uq-pool",
    vm_size="Standard_D4s_v5",
    max_nodes=8,
    low_priority=True,
    dedicated_nodes=0,
    idle_minutes_before_shutdown=10,
)

# 2. Submit tasks. Each task runs a Python function against a set of args.
task_ids = runner.submit_function_tasks(
    pool_name="uq-pool",
    job_id="uq-run-2026-04-24",
    fn=my_outcome_fn,
    args_list=[{"params": draw} for draw in draws],
    container_image="python:3.12-slim",       # or custom image
    pip_deps=["numpy", "scipy", "starsim"],
    budget_usd_cap=10.0,                       # hard abort if est. cost > this
)

# 3. Wait for tasks to complete, collect outputs.
results = runner.wait_and_collect("uq-run-2026-04-24")

# 4. Teardown. Wrap steps 1-3 in try/finally so this runs on error too.
runner.delete_pool("uq-pool")
```

The wrapper handles:
- Auth via `DefaultAzureCredential` (picks up `az login` automatically)
- Serialization of function + args to JSON (pickle fallback)
- Output collection from Blob storage
- Budget guard (estimates cost from pool size × duration before submission)

It does NOT tear the pool down for you: the caller owns teardown (see
"Teardown discipline" below).

### Budget guard (pre-flight check)

Before submitting a batch of N tasks, the wrapper estimates:

    estimated_cost = (N_tasks × avg_task_duration_sec / 3600) × vcores / N_nodes × rate_per_node_hr

If > `budget_usd_cap`, the wrapper refuses to submit and prints the
estimate. Override with `--force` if you know what you're doing.

## The workflow (first-run recipe)

1. **Verify Azure auth is fresh and on the right subscription**:
   ```
   az account show
   ```
   If not on your personal sub:
   ```
   az account set --subscription 048cd8f6-c126-46f7-919c-a895e9a8e2cd
   ```

2. **Provision the Batch account once per project** (one-time setup):
   ```
   az group create -n modeling-agentic-rg -l eastus2
   az storage account create -n mymodelingstorage -g modeling-agentic-rg \
       -l eastus2 --sku Standard_LRS
   az batch account create -n mymodelingruns -g modeling-agentic-rg \
       -l eastus2 --storage-account mymodelingstorage
   ```
   Takes ~2 minutes. Stores ~$0/month at idle.

3. **Set environment variables** (so the wrapper picks them up):
   ```
   export AZ_SUBSCRIPTION_ID=048cd8f6-c126-46f7-919c-a895e9a8e2cd
   export AZ_RESOURCE_GROUP=modeling-agentic-rg
   export AZ_BATCH_ACCOUNT=mymodelingruns
   export AZ_STORAGE_ACCOUNT=mymodelingstorage
   ```

4. **First test run**: submit a trivial "hello world" task, verify
   end-to-end:
   ```
   python3 scripts/cloud_batch.py --self-test-cloud
   ```
   Costs ~$0.01. Stands up a pool with 1 low-priority node, runs one
   task that prints "hello", tears down. Validates auth, quota, and
   the pool lifecycle.

5. **Real workload** (e.g., UQ with 200 draws):
   ```
   python3 scripts/propagate_uncertainty.py {run_dir} \
       --n-draws 200 --cloud --max-parallel 8
   ```
   Cost: ~$0.07. Wall time: ~12 min.

## Workload-specific patterns

### UQ propagation (automated via `--cloud` flag)

`scripts/propagate_uncertainty.py --cloud` submits N draws as tasks.
Each task pickles `outcome_fn` + params, uploads to Blob, the worker
runs the function and writes result to Blob. Master collects and
aggregates.

### Calibration (Optuna on Batch)

There is no dedicated cloud-calibration script in the repo yet; build
it on `BatchRunner.submit_function_tasks` from `scripts/cloud_batch.py`
(UQ uses the same wrapper via `scripts/propagate_uncertainty.py --cloud`).
Architecture:

- Master (local): Optuna study with SQLite or Azure PostgreSQL
  storage.
- N workers (Batch tasks, each a persistent task that loops): ask
  study for next trial, run ABM, report loss.

Shared Optuna storage is the key. For <500 trials, SQLite on
Azure Files works. For >500 or concurrent workers >16, use Azure
Database for PostgreSQL (Basic tier: ~$0.017/hr = $1.22 for a 3-day
calibration run).

### Multi-structural LOO-CV

`scripts/compare_models.py --loo-cloud` (to be added). For each
candidate model × each held-out target, submit one Batch task that
refits the model and predicts the held-out point.

### Single-model runs

For a single ABM invocation (e.g., reproducing a published baseline),
use the wrapper directly: 1 task, no parallelism, just offloading a
long compute from the laptop.

## When to escalate from Batch to AKS/Dask

- **Iterating rapidly** on a complex Dask-distributed calabaria
  workflow. AKS keeps the cluster warm; Batch's per-task startup
  overhead (~60–90 sec for pool creation) is wasted.
- **Fine-grained task graphs** (thousands of short dependencies).
  Batch doesn't have native DAG support; Dask does.
- **Shared in-memory state across tasks** (e.g., a calibrated
  emulator that's expensive to load). Dask's warm process pools amortize
  load cost; Batch re-loads per task.

For the modeling-agentic project today, we don't hit any of these.
Batch covers everything.

## Teardown discipline (critical)

Batch pools at low-priority, autoscaled to zero idle, cost ~$0 when
idle. But a Batch account itself costs $0 too. There's no background
fee to worry about.

HOWEVER:
- **A pool stuck at nonzero dedicated nodes** costs on-demand rates.
- **A forgotten Blob container with large files** costs ~$0.02/GB/month.
- **A PostgreSQL instance** for Optuna shared storage costs ~$0.017/hr
  continuously ($12/month). Delete when not actively calibrating.

Do not rely on `PoolSpec.auto_delete` to clean up. Any code that
creates a pool must delete it in a `finally:` block
(`runner.delete_pool(name)`), so a timeout, budget rejection, or
exception cannot leave dedicated nodes billing. Only delete pools your
own invocation created; a pre-existing shared pool is not yours to
remove. Also set a per-task wall-clock limit so a hung task cannot hold
a node indefinitely.

## Security notes

- Credentials via `DefaultAzureCredential` — picks up `az login`
  automatically. No secrets in the repo.
- Task code and args go to Blob; sensitive data should be encrypted
  if included.
- Spot eviction is normal; tasks must be idempotent.
- Never run a long expensive task as **dedicated** unless the cost of
  running it twice on low-priority outweighs the cost of running it
  once on-demand.

## Summary

- **Batch is the default.** One primitive covers single runs, UQ,
  calibration, LOO-CV, sensitivity.
- **Low-priority everywhere** unless demonstrated intolerance.
- **Budget guard + auto-delete + env-var-based auth.** Non-negotiable.
- **AKS/Dask is an escalation**, not the starting point.
- **Free Trial gets us through dozens of test runs** before upgrade.
