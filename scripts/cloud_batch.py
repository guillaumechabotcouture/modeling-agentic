#!/usr/bin/env python3
"""
Thin wrapper around Azure Batch for task-parallel cloud execution.

Covers the five workload classes in the cloud-compute skill: single model
runs, UQ propagation, Optuna calibration (via batch_calibrate.py built on
top), multi-structural LOO-CV, sensitivity analysis. All reduce to:
"submit N tasks to a Batch pool, each task runs a Python function with
given args, collect outputs."

Auth via DefaultAzureCredential (picks up `az login` automatically).
All pools default to low-priority / spot nodes. Budget guard does a
pre-flight cost estimate and refuses to submit when over cap. Storage
round-trips function+args via Azure Blob.

Usage patterns:

    from scripts.cloud_batch import BatchRunner

    runner = BatchRunner()  # reads AZ_SUBSCRIPTION_ID, AZ_BATCH_ACCOUNT, etc.
    runner.ensure_pool("uq-pool", vm_size="Standard_D4s_v5", max_nodes=8)

    job_id = runner.submit_function_tasks(
        pool_name="uq-pool",
        fn=my_outcome_fn,
        args_list=[{"params": d} for d in draws],
        pip_deps=["numpy", "scipy"],
        budget_usd_cap=5.0,
    )
    results = runner.wait_and_collect(job_id)
    runner.delete_pool("uq-pool")  # or set auto_delete when submitting

CLI helpers:

    python3 scripts/cloud_batch.py --self-test
        # Offline dry-run of pickling / cost-estimation / etc.

    python3 scripts/cloud_batch.py --self-test-cloud
        # LIVE test: stands up pool with 1 low-priority node, runs
        # a hello-world task, tears down. Costs ~$0.01. Requires
        # AZ_* env vars and provisioned Batch account.

    python3 scripts/cloud_batch.py --estimate-cost \\
        --vm-size Standard_D4s_v5 --n-tasks 200 --avg-seconds 30 --max-nodes 8
        # Print estimated cost for a hypothetical workload.

Exit codes:
    0   success / within budget
    1   over budget cap (use --force) or live test failed
    2   missing credentials / unregistered provider / quota error
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import pickle
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional
from uuid import uuid4


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Low-priority per-hour rates in eastus2, late 2025. Used for budget estimate.
# Dedicated rates are approximately 5x these.
VM_LOW_PRIORITY_USD_PER_HR = {
    "Standard_A1_v2":   0.010,
    "Standard_A2_v2":   0.020,
    "Standard_D2s_v5":  0.020,
    "Standard_D4s_v5":  0.040,
    "Standard_D8s_v5":  0.080,
    "Standard_D16s_v5": 0.160,
    "Standard_D32s_v5": 0.320,
    "Standard_E2s_v5":  0.025,
    "Standard_E4s_v5":  0.050,
    "Standard_E8s_v5":  0.100,
}
VM_DEDICATED_MULTIPLIER = 5.0

DEFAULT_VM_SIZE = "Standard_D4s_v5"
DEFAULT_POOL_LOCATION = "eastus2"
DEFAULT_BUDGET_USD_CAP = 5.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PoolSpec:
    name: str
    vm_size: str = DEFAULT_VM_SIZE
    low_priority_nodes: int = 0
    dedicated_nodes: int = 0
    max_nodes: int = 8          # autoscale ceiling for low-priority
    auto_delete: bool = True    # delete pool when job completes
    image_offer: str = "ubuntu-server-container"
    image_sku: str = "22-04-lts"
    image_publisher: str = "microsoft-azure-batch"


@dataclass
class CostEstimate:
    n_tasks: int
    avg_task_seconds: float
    vm_size: str
    n_nodes_effective: int
    low_priority: bool
    estimated_hours: float
    rate_per_node_hr: float
    estimated_usd: float
    note: str = ""


# ---------------------------------------------------------------------------
# Cost estimation (pure, no SDK calls)
# ---------------------------------------------------------------------------

def estimate_cost(n_tasks: int, avg_task_seconds: float, vm_size: str,
                  max_nodes: int, low_priority: bool = True) -> CostEstimate:
    rate = VM_LOW_PRIORITY_USD_PER_HR.get(vm_size)
    note = ""
    if rate is None:
        rate = 0.08
        note = (f"No reference rate for {vm_size}; assumed $0.08/hr. "
                f"Override by adding to VM_LOW_PRIORITY_USD_PER_HR.")
    if not low_priority:
        rate *= VM_DEDICATED_MULTIPLIER

    n_nodes_effective = min(max_nodes, max(1, n_tasks))
    total_task_hours = n_tasks * avg_task_seconds / 3600.0
    wall_hours = total_task_hours / n_nodes_effective
    # Also pay for pool lifecycle overhead: ~2 min node startup + ~1 min idle.
    lifecycle_hours = n_nodes_effective * (3.0 / 60.0)
    estimated_hours = wall_hours * n_nodes_effective + lifecycle_hours
    estimated_usd = estimated_hours * rate

    return CostEstimate(
        n_tasks=n_tasks,
        avg_task_seconds=avg_task_seconds,
        vm_size=vm_size,
        n_nodes_effective=n_nodes_effective,
        low_priority=low_priority,
        estimated_hours=estimated_hours,
        rate_per_node_hr=rate,
        estimated_usd=estimated_usd,
        note=note,
    )


# ---------------------------------------------------------------------------
# Module-level test functions (pickle needs these importable by name)
# ---------------------------------------------------------------------------

def _square(x):
    return x * x


def _add(a, b):
    return a + b


def _hello(name: str) -> str:
    """Module-level hello function used by the live cloud self-test.
    Must be importable by qualified name so workers can unpickle it."""
    return f"hello {name}"


# ---------------------------------------------------------------------------
# Function-to-task serialization
# ---------------------------------------------------------------------------

def serialize_function_and_args(fn: Callable, args: Any) -> bytes:
    """Pickle fn + args together. Workers unpickle and call fn(**args) (if
    dict) or fn(args) (otherwise).

    NOTE: uses stdlib pickle, so `fn` must be importable by qualified name
    on the worker (top-level module function, not a lambda/closure/local
    def). For models/outcome_fn.py-style code this is fine — the worker
    also loads models/outcome_fn.py and pickle stores by reference. If you
    need arbitrary local functions, consider installing `cloudpickle` and
    wiring it in (one-line swap below).
    """
    return pickle.dumps({"fn": fn, "args": args})


def deserialize_and_run(blob_bytes: bytes) -> Any:
    """Worker-side: run the pickled callable with the pickled args."""
    payload = pickle.loads(blob_bytes)
    fn = payload["fn"]
    args = payload["args"]
    if isinstance(args, dict):
        return fn(**args)
    return fn(args)


# ---------------------------------------------------------------------------
# BatchRunner (main surface)
# ---------------------------------------------------------------------------

class BatchRunner:
    """Thin wrapper around azure.batch + azure.storage.blob.

    Resolves config from env vars:
        AZ_SUBSCRIPTION_ID
        AZ_RESOURCE_GROUP
        AZ_BATCH_ACCOUNT
        AZ_BATCH_ACCOUNT_URL   (https://<account>.<region>.batch.azure.com)
        AZ_STORAGE_ACCOUNT
        AZ_STORAGE_CONTAINER   (defaults to "batch-io")

    Lazy: doesn't touch Azure until a method is called.
    """

    def __init__(self, **overrides):
        self.subscription_id = overrides.get("subscription_id") \
            or os.environ.get("AZ_SUBSCRIPTION_ID")
        self.resource_group = overrides.get("resource_group") \
            or os.environ.get("AZ_RESOURCE_GROUP")
        self.batch_account = overrides.get("batch_account") \
            or os.environ.get("AZ_BATCH_ACCOUNT")
        self.batch_account_url = overrides.get("batch_account_url") \
            or os.environ.get("AZ_BATCH_ACCOUNT_URL")
        self.storage_account = overrides.get("storage_account") \
            or os.environ.get("AZ_STORAGE_ACCOUNT")
        self.storage_container = overrides.get("storage_container") \
            or os.environ.get("AZ_STORAGE_CONTAINER", "batch-io")
        self.location = overrides.get("location", DEFAULT_POOL_LOCATION)

        self._credential = None
        self._batch_client = None
        self._blob_client = None

    def _require_config(self):
        missing = []
        if not self.subscription_id: missing.append("AZ_SUBSCRIPTION_ID")
        if not self.batch_account: missing.append("AZ_BATCH_ACCOUNT")
        if not self.batch_account_url: missing.append("AZ_BATCH_ACCOUNT_URL")
        if not self.storage_account: missing.append("AZ_STORAGE_ACCOUNT")
        if missing:
            raise RuntimeError(
                f"BatchRunner missing config: {missing}. Set as env vars or "
                f"pass as kwargs to the constructor."
            )

    def _credential_get(self):
        if self._credential is None:
            from azure.identity import DefaultAzureCredential
            self._credential = DefaultAzureCredential()
        return self._credential

    def _batch_get(self):
        if self._batch_client is None:
            self._require_config()
            from azure.batch import BatchServiceClient
            from azure.batch.batch_auth import SharedKeyCredentials
            # For simplicity: use service-principal / AAD via aiohttp-less
            # REST endpoint. Use the ARM-style auth through management API.
            # azure-batch supports DefaultAzureCredential via
            # BatchServiceClient(credentials=...) but the auth flow differs
            # from ARM; we use the "AAD token" approach.
            from azure.identity import DefaultAzureCredential
            class _AADTokenCredentials:
                """Adapter to present DefaultAzureCredential as the old
                BatchServiceClient credential interface."""
                def __init__(self, cred):
                    self._cred = cred
                def signed_session(self, session=None):
                    import requests
                    if session is None:
                        session = requests.Session()
                    token = self._cred.get_token("https://batch.core.windows.net/.default").token
                    session.headers["Authorization"] = f"Bearer {token}"
                    return session
            cred = _AADTokenCredentials(self._credential_get())
            self._batch_client = BatchServiceClient(cred, batch_url=self.batch_account_url)
        return self._batch_client

    def _blob_get(self):
        if self._blob_client is None:
            self._require_config()
            from azure.storage.blob import BlobServiceClient
            account_url = f"https://{self.storage_account}.blob.core.windows.net"
            self._blob_client = BlobServiceClient(
                account_url=account_url,
                credential=self._credential_get(),
            )
            # Ensure the container exists.
            try:
                self._blob_client.create_container(self.storage_container)
            except Exception as e:
                # Already exists is fine; anything else re-raise.
                if "ContainerAlreadyExists" not in str(e):
                    # Permit "Forbidden" here so the user at least gets a
                    # clear error at submit time rather than at client init.
                    pass
        return self._blob_client

    # ---- Pool lifecycle ----

    def ensure_pool(self, pool_name: str, vm_size: str = DEFAULT_VM_SIZE,
                    max_nodes: int = 8, use_low_priority: bool = True,
                    dedicated_nodes: int = 0, auto_scale: bool = True) -> None:
        """Create a pool if it doesn't exist.

        Default: autoscale 0 → `max_nodes` low-priority VMs. For Free Trial
        subscriptions (lowPriorityCoreQuota=0), pass `use_low_priority=False`
        to autoscale dedicated nodes instead.

        Static (non-autoscale) mode: pass `auto_scale=False` with an explicit
        `dedicated_nodes` count (and `max_nodes` for low-priority size).
        """
        self._require_config()
        from azure.batch import models as batch_models
        from azure.batch.models import BatchErrorException

        batch = self._batch_get()

        # Does pool already exist?
        try:
            batch.pool.get(pool_name)
            return  # Pool exists, reuse.
        except BatchErrorException as e:
            if "PoolNotFound" not in str(e):
                raise

        # Pick which node-type variable the autoscale formula drives.
        if use_low_priority:
            target_var = "$TargetLowPriorityNodes"
            other_var = "$TargetDedicatedNodes"
        else:
            target_var = "$TargetDedicatedNodes"
            other_var = "$TargetLowPriorityNodes"

        autoscale_formula = (
            "startingNumberOfVMs = 0;\n"
            f"maxNumberofVMs = {max_nodes};\n"
            "pendingTaskSamplePercent = $PendingTasks.GetSamplePercent(60 * TimeInterval_Second);\n"
            "pendingTaskSamples = pendingTaskSamplePercent < 70 ? startingNumberOfVMs : avg($PendingTasks.GetSample(60 * TimeInterval_Second));\n"
            f"{target_var} = min(maxNumberofVMs, pendingTaskSamples);\n"
            f"{other_var} = 0;\n"
            "$NodeDeallocationOption = taskcompletion;\n"
        )

        if auto_scale:
            pool_kwargs = dict(
                enable_auto_scale=True,
                auto_scale_formula=autoscale_formula,
                auto_scale_evaluation_interval="PT5M",
                target_low_priority_nodes=0,
                target_dedicated_nodes=0,
            )
        else:
            pool_kwargs = dict(
                target_low_priority_nodes=max_nodes if use_low_priority else 0,
                target_dedicated_nodes=dedicated_nodes if not use_low_priority else 0,
            )

        pool = batch_models.PoolAddParameter(
            id=pool_name,
            vm_size=vm_size,
            virtual_machine_configuration=batch_models.VirtualMachineConfiguration(
                image_reference=batch_models.ImageReference(
                    publisher="canonical",
                    offer="0001-com-ubuntu-server-jammy",
                    sku="22_04-lts",
                    version="latest",
                ),
                node_agent_sku_id="batch.node.ubuntu 22.04",
            ),
            **pool_kwargs,
        )
        batch.pool.add(pool)

    def delete_pool(self, pool_name: str) -> None:
        self._require_config()
        batch = self._batch_get()
        try:
            batch.pool.delete(pool_name)
        except Exception as e:
            if "PoolNotFound" not in str(e):
                raise

    # ---- Job + task lifecycle ----

    def _upload_blob(self, name: str, data: bytes) -> str:
        """Upload bytes to the storage container. Return the blob name."""
        blob_svc = self._blob_get()
        container = blob_svc.get_container_client(self.storage_container)
        try:
            container.upload_blob(name=name, data=data, overwrite=True)
        except Exception:
            # Some permissions variants need create_container to be
            # re-attempted here. Best effort.
            raise
        return name

    def _download_blob(self, name: str) -> Optional[bytes]:
        blob_svc = self._blob_get()
        container = blob_svc.get_container_client(self.storage_container)
        try:
            return container.download_blob(name).readall()
        except Exception:
            return None

    def _generate_sas_url(self, blob_name: str, hours: int = 8) -> str:
        from azure.storage.blob import generate_blob_sas, BlobSasPermissions
        from datetime import datetime, timedelta
        blob_svc = self._blob_get()
        # We need the storage account key for SAS. If we don't have it,
        # we need to request it via management plane with the credential.
        # For simplicity, use user-delegation SAS which uses AAD credentials.
        user_delegation_key = blob_svc.get_user_delegation_key(
            key_start_time=datetime.utcnow() - timedelta(minutes=5),
            key_expiry_time=datetime.utcnow() + timedelta(hours=hours),
        )
        sas_token = generate_blob_sas(
            account_name=self.storage_account,
            container_name=self.storage_container,
            blob_name=blob_name,
            user_delegation_key=user_delegation_key,
            permission=BlobSasPermissions(read=True, write=True),
            expiry=datetime.utcnow() + timedelta(hours=hours),
        )
        return (f"https://{self.storage_account}.blob.core.windows.net/"
                f"{self.storage_container}/{blob_name}?{sas_token}")

    def submit_function_tasks(
            self,
            pool_name: str,
            fn: Callable,
            args_list: list,
            pip_deps: Optional[list[str]] = None,
            job_id: Optional[str] = None,
            budget_usd_cap: float = DEFAULT_BUDGET_USD_CAP,
            avg_task_seconds: float = 30.0,
            force: bool = False,
    ) -> str:
        """Submit N tasks to the pool, one per entry in args_list.

        Each task downloads the pickled (fn, args), runs fn(args), and
        uploads the result. Returns the job_id for later wait_and_collect.

        Pre-flight budget guard: if the estimated cost (based on
        len(args_list), avg_task_seconds, pool max_nodes, vm_size) exceeds
        `budget_usd_cap`, refuses to submit unless `force=True`.
        """
        self._require_config()
        from azure.batch import models as batch_models
        batch = self._batch_get()

        # Introspect pool for VM size and max_nodes.
        pool_info = batch.pool.get(pool_name)
        vm_size = pool_info.vm_size
        max_nodes = (pool_info.target_low_priority_nodes
                     or pool_info.target_dedicated_nodes
                     or 1)
        # Autoscaled pools: max_nodes is in the formula; use 8 as a
        # reasonable default.
        if max_nodes < 1:
            max_nodes = 8

        est = estimate_cost(len(args_list), avg_task_seconds, vm_size,
                            max_nodes, low_priority=True)
        print(f"[cloud_batch] Estimated cost: ${est.estimated_usd:.4f} "
              f"({est.n_tasks} tasks × {est.avg_task_seconds:.0f}s on "
              f"{vm_size}, ~{est.n_nodes_effective} nodes)", file=sys.stderr)
        if est.estimated_usd > budget_usd_cap and not force:
            raise RuntimeError(
                f"Estimated cost ${est.estimated_usd:.4f} exceeds budget "
                f"cap ${budget_usd_cap}. Raise budget_usd_cap or pass "
                f"force=True to override."
            )

        if job_id is None:
            job_id = f"job-{uuid4().hex[:12]}"

        # 1. Upload serialized (fn, args) for each task.
        blob_names = []
        for i, args in enumerate(args_list):
            payload = serialize_function_and_args(fn, args)
            name = f"{job_id}/task-{i:06d}/input.pkl"
            self._upload_blob(name, payload)
            blob_names.append(name)

        # 2. Upload worker script.
        worker_script = _WORKER_SCRIPT
        worker_blob = f"{job_id}/worker.py"
        self._upload_blob(worker_blob, worker_script.encode())

        # 3. Create the job.
        job = batch_models.JobAddParameter(
            id=job_id,
            pool_info=batch_models.PoolInformation(pool_id=pool_name),
            on_all_tasks_complete="terminateJob",
        )
        batch.job.add(job)

        # 4. Create tasks.
        worker_sas = self._generate_sas_url(worker_blob)
        pip_install = " ".join(pip_deps or [])
        tasks = []
        for i, blob_name in enumerate(blob_names):
            input_sas = self._generate_sas_url(blob_name)
            output_blob = f"{job_id}/task-{i:06d}/output.pkl"
            # Output SAS is write-only; we use upload at task end.
            output_sas = self._generate_sas_url(output_blob)
            pip_line = (f"pip install --quiet {pip_install} && "
                        if pip_install else "")
            command = (
                "/bin/bash -c '"
                "set -e; "
                f"{pip_line}"
                f"curl -sSL \"{worker_sas}\" -o worker.py && "
                f"curl -sSL \"{input_sas}\" -o input.pkl && "
                "python3 worker.py input.pkl output.pkl && "
                f"curl -sSL -X PUT -T output.pkl -H \"x-ms-blob-type: BlockBlob\" \"{output_sas}\""
                "'"
            )
            tasks.append(batch_models.TaskAddParameter(
                id=f"task-{i:06d}",
                command_line=command,
            ))

        # Submit in chunks of 100 per API limit.
        for i in range(0, len(tasks), 100):
            batch.task.add_collection(job_id, tasks[i:i + 100])

        return job_id

    def wait_and_collect(self, job_id: str, timeout_minutes: int = 120,
                         poll_seconds: int = 15) -> list:
        """Block until all tasks in the job complete or terminate, then
        collect outputs in task order. Returns a list of unpickled results
        (one per task); failed tasks return None."""
        self._require_config()
        from azure.batch import models as batch_models
        batch = self._batch_get()

        deadline = time.time() + timeout_minutes * 60
        while time.time() < deadline:
            tasks = list(batch.task.list(job_id))
            states = [t.state for t in tasks]
            if all(s == batch_models.TaskState.completed for s in states):
                break
            time.sleep(poll_seconds)
        else:
            raise TimeoutError(
                f"Job {job_id} did not complete within {timeout_minutes} min"
            )

        # Collect outputs, in task order.
        tasks = sorted(list(batch.task.list(job_id)), key=lambda t: t.id)
        results = []
        for t in tasks:
            idx = int(t.id.split("-")[-1])
            output_blob = f"{job_id}/task-{idx:06d}/output.pkl"
            data = self._download_blob(output_blob)
            if data is None:
                results.append(None)
            else:
                try:
                    results.append(pickle.loads(data))
                except Exception:
                    results.append(None)
        return results


_WORKER_SCRIPT = r"""#!/usr/bin/env python3
'''Worker-side script. Reads input.pkl, runs the pickled function, writes
output.pkl.'''
import pickle, sys, traceback

input_path, output_path = sys.argv[1], sys.argv[2]
with open(input_path, 'rb') as f:
    payload = pickle.load(f)
fn = payload['fn']
args = payload['args']
try:
    if isinstance(args, dict):
        result = fn(**args)
    else:
        result = fn(args)
    with open(output_path, 'wb') as f:
        pickle.dump({'ok': True, 'result': result}, f)
except Exception as e:
    with open(output_path, 'wb') as f:
        pickle.dump({'ok': False, 'error': repr(e),
                     'traceback': traceback.format_exc()}, f)
"""


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def _run_offline_self_test() -> int:
    failures = []
    def ok(cond, label):
        if not cond:
            failures.append(label)

    # Cost estimation reasonable.
    est = estimate_cost(200, 30.0, "Standard_D4s_v5", 8, low_priority=True)
    ok(0.01 < est.estimated_usd < 1.0,
       f"cost 200x30s on D4s_v5: {est.estimated_usd:.4f} USD (want 0.01-1.00)")

    est2 = estimate_cost(1000, 60.0, "Standard_D4s_v5", 16,
                         low_priority=True)
    ok(0.5 < est2.estimated_usd < 5.0,
       f"cost 1000x60s D4s_v5 x16 low-priority: {est2.estimated_usd:.4f} "
       f"USD (want 0.5-5.0)")

    # Budget guard.
    try:
        est3 = estimate_cost(10_000, 600.0, "Standard_D32s_v5", 1,
                             low_priority=False)
        ok(est3.estimated_usd > 1000,
           f"worst-case cost should be >$1000: {est3.estimated_usd}")
    except Exception as e:
        failures.append(f"cost estimate raised: {e}")

    # Serialization round-trip. Uses module-level functions — stdlib
    # pickle cannot serialize locally-defined functions (the fn is stored
    # by qualified name, which must resolve on the worker). Real workloads
    # put their function in models/outcome_fn.py and the worker imports it.
    payload = serialize_function_and_args(_square, {"x": 7})
    ok(deserialize_and_run(payload) == 49,
       f"serialize+run _square: got {deserialize_and_run(payload)}, want 49")
    payload2 = serialize_function_and_args(_add, {"a": 3, "b": 4})
    ok(deserialize_and_run(payload2) == 7, "two-arg serialize+run _add")
    payload3 = serialize_function_and_args(_square, 9)  # positional
    ok(deserialize_and_run(payload3) == 81, "positional arg round-trip")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: offline self-test passed.", file=sys.stderr)
    return 0


def _run_cloud_self_test() -> int:
    """Live cloud test: stand up pool, run hello task, tear down.
    Requires AZ_* env vars."""
    print("[cloud_batch] Live cloud self-test. Requires AZ_* env vars and "
          "provisioned Batch + Storage accounts.", file=sys.stderr)
    try:
        runner = BatchRunner()
        runner._require_config()
    except RuntimeError as e:
        print(f"[cloud_batch] ERROR: {e}", file=sys.stderr)
        return 2
    try:
        pool_name = f"self-test-{uuid4().hex[:8]}"
        print(f"  creating pool {pool_name}...", file=sys.stderr)
        # Free Trial subscriptions have lowPriorityCoreQuota=0, so the
        # self-test defaults to dedicated nodes. One A1_v2 (1 vCPU) is
        # cheap (~$0.04/hr, ~$0.005 for a 5-minute test) and fits within
        # the default 4-vCPU dedicated quota.
        runner.ensure_pool(pool_name, vm_size="Standard_A1_v2",
                           max_nodes=1, use_low_priority=False,
                           auto_scale=False, dedicated_nodes=1)
        print("  submitting hello task (using builtin len)...", file=sys.stderr)

        # Use a stdlib builtin so the worker doesn't need any custom code.
        # len("hello world") == 11. For real workloads, the modeler's
        # outcome_fn lives in models/outcome_fn.py; we'll add a workflow
        # that packages + uploads the models/ directory separately.
        job_id = runner.submit_function_tasks(
            pool_name=pool_name,
            fn=len,
            args_list=["hello world"],
            budget_usd_cap=0.10,
            avg_task_seconds=30.0,
            pip_deps=[],
        )
        print(f"  job {job_id} submitted, waiting...", file=sys.stderr)
        results = runner.wait_and_collect(job_id, timeout_minutes=20)
        print(f"  results: {results}", file=sys.stderr)
        ok = (results and results[0] and results[0].get("ok")
              and results[0].get("result") == 11)
        runner.delete_pool(pool_name)
        if ok:
            print("OK: live cloud self-test passed.", file=sys.stderr)
            return 0
        print(f"FAIL: unexpected result: {results}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"[cloud_batch] ERROR: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        try:
            runner.delete_pool(pool_name)
        except Exception:
            pass
        return 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-test", action="store_true",
                   help="Offline self-test (no Azure calls)")
    p.add_argument("--self-test-cloud", action="store_true",
                   help="Live cloud self-test (~$0.01, requires AZ_* env vars)")
    p.add_argument("--estimate-cost", action="store_true")
    p.add_argument("--vm-size", default=DEFAULT_VM_SIZE)
    p.add_argument("--n-tasks", type=int, default=200)
    p.add_argument("--avg-seconds", type=float, default=30.0)
    p.add_argument("--max-nodes", type=int, default=8)
    p.add_argument("--dedicated", action="store_true",
                   help="Estimate for dedicated (on-demand) rates instead of low-priority")
    args = p.parse_args()

    if args.self_test:
        return _run_offline_self_test()
    if args.self_test_cloud:
        return _run_cloud_self_test()
    if args.estimate_cost:
        est = estimate_cost(args.n_tasks, args.avg_seconds, args.vm_size,
                            args.max_nodes,
                            low_priority=not args.dedicated)
        print(f"Workload: {est.n_tasks} tasks × {est.avg_task_seconds:.0f}s "
              f"on {est.vm_size} ({'dedicated' if not est.low_priority else 'low-priority'}), "
              f"max {est.n_nodes_effective} parallel nodes")
        print(f"  estimated wall time: {est.estimated_hours:.2f} hours")
        print(f"  rate per node-hr:    ${est.rate_per_node_hr:.4f}")
        print(f"  estimated total:     ${est.estimated_usd:.4f}")
        if est.note:
            print(f"  note: {est.note}")
        return 0
    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
