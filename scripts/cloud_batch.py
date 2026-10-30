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
import contextlib
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

DEFAULT_VM_SIZE = "Standard_A2_v2"  # 2 vCPUs, fits Free Trial A-series quota.
                                     # Override to D-series on Pay-As-You-Go
                                     # for more RAM and faster CPUs.
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
    auto_delete: bool = True    # Phase 21 δ: delete the pool on exit of
                                # managed_pool() IF this invocation created
                                # it. Pre-existing pools are never deleted.
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


def models_tarball(models_dir: str) -> io.BytesIO:
    """models/ for worker-side import, with the repo's toolkit/ beside it (Phase 42 ε:
    run code imports toolkit.alloc, finding the repo root as the first parent of its
    file that holds toolkit/alloc; on a worker that is the task directory)."""
    import tarfile
    buf = io.BytesIO()
    toolkit = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "toolkit")
    skip = lambda ti: None if "__pycache__" in ti.name.split("/") else ti  # noqa: E731
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(models_dir, arcname="models")
        if os.path.isdir(toolkit):
            tar.add(toolkit, arcname="toolkit", filter=skip)
    return buf


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


class BudgetExceededError(RuntimeError):
    """Raised by preflight_budget() before any Azure resource is created."""


def preflight_budget(n_tasks: int, avg_task_seconds: float, vm_size: str,
                     max_nodes: int, low_priority: bool,
                     budget_usd_cap: float, force: bool = False) -> CostEstimate:
    """Phase 21 δ: pure budget guard that runs BEFORE provisioning.

    Pre-21 the only guard lived in submit_function_tasks(), i.e. after
    ensure_pool() had already created (billing) nodes, and it always
    priced at low-priority rates — ~5× too low for the default dedicated
    pool. This prices the node type that will actually be used."""
    est = estimate_cost(n_tasks, avg_task_seconds, vm_size, max_nodes,
                        low_priority=low_priority)
    print(f"[cloud_batch] Estimated cost: ${est.estimated_usd:.4f} "
          f"({est.n_tasks} tasks × {est.avg_task_seconds:.0f}s on "
          f"{vm_size} {'low-priority' if low_priority else 'dedicated'}, "
          f"~{est.n_nodes_effective} nodes)", file=sys.stderr)
    if est.estimated_usd > budget_usd_cap and not force:
        raise BudgetExceededError(
            f"Estimated cost ${est.estimated_usd:.4f} exceeds budget "
            f"cap ${budget_usd_cap}. Raise budget_usd_cap or pass "
            f"force=True to override.")
    return est


# Phase 21 δ: per-task wall-clock ceiling so a hung task cannot hold a
# billing node indefinitely, and transient-error retry policy for polling.
DEFAULT_TASK_MAX_WALL_MINUTES = 60
POLL_MAX_CONSECUTIVE_ERRORS = 5
SAS_DEFAULT_HOURS = 3


# ---------------------------------------------------------------------------
# Module-level test functions (pickle needs these importable by name)
# ---------------------------------------------------------------------------

def _square(x):
    return x * x


def _first_key(d):
    """Helper for the offline self-test — takes a single dict arg and
    returns the first key. Used to validate the fn(args) calling convention."""
    return next(iter(d.keys())) if d else None


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
    """Worker-side: run the pickled callable with the pickled args.

    Calling convention: args is passed as a single positional argument.
    Scientific functions like `outcome_fn(params)` where `params` is a
    dict match this naturally — the modeler passes
    args_list=[{"irs_rr": 0.35, ...}, ...] and each call is
    `fn({"irs_rr": 0.35, ...})`.

    If you genuinely want kwarg-unpacking (`fn(**args)`), wrap your
    function: `def wrapped(args): return fn(**args)`."""
    payload = pickle.loads(blob_bytes)
    return payload["fn"](payload["args"])


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
        # Phase 21 δ: pools created by THIS runner (name → low_priority).
        # Only these are eligible for teardown.
        self._created_pools: dict[str, bool] = {}
        self._delegation_key = None

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
                    dedicated_nodes: int = 0, auto_scale: bool = True) -> bool:
        """Create a pool if it doesn't exist. Returns True iff this call
        created it (Phase 21 δ ownership tracking — only created pools are
        torn down by managed_pool()).

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
            print(f"[cloud_batch] reusing pre-existing pool '{pool_name}' — "
                  f"it will NOT be deleted by this invocation; delete it "
                  f"yourself if nothing else uses it.", file=sys.stderr)
            return False  # Pool exists, reuse.
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
        self._created_pools[pool_name] = bool(use_low_priority)
        return True

    @contextlib.contextmanager
    def managed_pool(self, spec: "PoolSpec", use_low_priority: bool,
                     auto_scale: bool = False):
        """Phase 21 δ: provision `spec` and guarantee teardown.

        Yields True if the pool was created here. On exit — success,
        exception, timeout, or KeyboardInterrupt — deletes the pool iff
        this invocation created it and spec.auto_delete is set. A
        pre-existing pool is never deleted."""
        created = False
        try:
            created = self.ensure_pool(
                pool_name=spec.name, vm_size=spec.vm_size,
                max_nodes=spec.max_nodes, use_low_priority=use_low_priority,
                dedicated_nodes=(0 if use_low_priority else spec.max_nodes),
                auto_scale=auto_scale)
            yield created
        finally:
            if created and spec.auto_delete:
                try:
                    self.delete_pool(spec.name)
                    print(f"[cloud_batch] deleted pool '{spec.name}'",
                          file=sys.stderr)
                except Exception as e:  # noqa: BLE001 — must not mask
                    print(f"[cloud_batch] WARNING: failed to delete pool "
                          f"'{spec.name}' ({e}). DELETE IT MANUALLY — it "
                          f"is still billing.", file=sys.stderr)

    def pool_is_low_priority(self, pool_info) -> bool:
        """Infer node type of an existing pool for pricing. Unknown →
        False (price as dedicated, the conservative choice)."""
        name = getattr(pool_info, "id", None)
        if name in self._created_pools:
            return self._created_pools[name]
        if (getattr(pool_info, "target_low_priority_nodes", 0) or 0) > 0 and \
                not (getattr(pool_info, "target_dedicated_nodes", 0) or 0):
            return True
        formula = getattr(pool_info, "auto_scale_formula", None) or ""
        return "$TargetLowPriorityNodes = min" in formula

    def terminate_job(self, job_id: str) -> None:
        """Best-effort: stop a job's running tasks (used on timeout)."""
        try:
            self._batch_get().job.terminate(job_id)
        except Exception as e:  # noqa: BLE001
            print(f"[cloud_batch] WARNING: could not terminate job "
                  f"{job_id}: {e}", file=sys.stderr)

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

    def _generate_sas_url(self, blob_name: str, hours: int = SAS_DEFAULT_HOURS,
                          access: str = "r") -> str:
        """User-delegation SAS for one blob. Phase 21 δ: least privilege —
        access="r" for inputs the worker downloads, access="cw" for the
        output it uploads — and a short expiry (default 3 h, was 8 h with
        read+write on every blob)."""
        from azure.storage.blob import generate_blob_sas, BlobSasPermissions
        from datetime import datetime, timedelta, timezone
        blob_svc = self._blob_get()
        now = datetime.now(timezone.utc)
        if self._delegation_key is None:
            self._delegation_key = blob_svc.get_user_delegation_key(
                key_start_time=now - timedelta(minutes=5),
                key_expiry_time=now + timedelta(hours=hours),
            )
        perm = BlobSasPermissions(read="r" in access, create="c" in access,
                                  write="w" in access)
        sas_token = generate_blob_sas(
            account_name=self.storage_account,
            container_name=self.storage_container,
            blob_name=blob_name,
            user_delegation_key=self._delegation_key,
            permission=perm,
            expiry=now + timedelta(hours=hours),
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
            models_dir: Optional[str] = None,
            low_priority: Optional[bool] = None,
            task_max_wall_minutes: int = DEFAULT_TASK_MAX_WALL_MINUTES,
    ) -> str:
        """Submit N tasks to the pool, one per entry in args_list.

        Each task downloads the pickled (fn, args), runs fn(args), and
        uploads the result. Returns the job_id for later wait_and_collect.

        Pre-flight budget guard: if the estimated cost (based on
        len(args_list), avg_task_seconds, pool max_nodes, vm_size) exceeds
        `budget_usd_cap`, refuses to submit unless `force=True`.

        models_dir: optional path to a directory (typically `{run_dir}/models`)
            that will be tar'd and uploaded as a shared job blob. The task
            command extracts it alongside the worker script and adds its
            root to sys.path so custom user functions (e.g., outcome_fn)
            can be imported by the worker when unpickling.
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

        # Phase 21 δ: price the node type the pool actually uses (was
        # hardcoded low_priority=True → ~5× under-estimate on dedicated).
        if low_priority is None:
            low_priority = self.pool_is_low_priority(pool_info)
        preflight_budget(len(args_list), avg_task_seconds, vm_size,
                         max_nodes, low_priority, budget_usd_cap, force)

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

        # 2b. Optionally tar + upload models_dir for worker-side import.
        models_sas = None
        if models_dir:
            if not os.path.isdir(models_dir):
                raise FileNotFoundError(f"models_dir not a directory: {models_dir}")
            tar_buf = models_tarball(models_dir)
            tar_blob = f"{job_id}/models.tar.gz"
            self._upload_blob(tar_blob, tar_buf.getvalue())
            models_sas = self._generate_sas_url(tar_blob, access="r")

        # 3. Create the job.
        from datetime import timedelta
        n_waves = max(1, -(-len(args_list) // max(1, max_nodes)))
        job = batch_models.JobAddParameter(
            id=job_id,
            pool_info=batch_models.PoolInformation(pool_id=pool_name),
            on_all_tasks_complete="terminateJob",
            # Phase 21 δ: job-level ceiling = waves × per-task ceiling.
            constraints=batch_models.JobConstraints(
                max_wall_clock_time=timedelta(
                    minutes=task_max_wall_minutes * n_waves + 30)),
        )
        batch.job.add(job)

        # 4. Create tasks.
        worker_sas = self._generate_sas_url(worker_blob, access="r")
        pip_install = " ".join(pip_deps or [])
        tasks = []
        for i, blob_name in enumerate(blob_names):
            input_sas = self._generate_sas_url(blob_name, access="r")
            output_blob = f"{job_id}/task-{i:06d}/output.pkl"
            # Output SAS is create/write-only; we use upload at task end.
            output_sas = self._generate_sas_url(output_blob, access="cw")
            pip_line = (f"pip install --quiet {pip_install} && "
                        if pip_install else "")
            models_line = (
                f"curl -sSL \"{models_sas}\" -o models.tar.gz && "
                "tar xzf models.tar.gz && "
                if models_sas else ""
            )
            command = (
                "/bin/bash -c '"
                "set -e; "
                f"{pip_line}"
                f"curl -sSL \"{worker_sas}\" -o worker.py && "
                f"{models_line}"
                f"curl -sSL \"{input_sas}\" -o input.pkl && "
                "python3 worker.py input.pkl output.pkl && "
                f"curl -sSL -X PUT -T output.pkl -H \"x-ms-blob-type: BlockBlob\" \"{output_sas}\""
                "'"
            )
            tasks.append(batch_models.TaskAddParameter(
                id=f"task-{i:06d}",
                command_line=command,
                constraints=batch_models.TaskConstraints(
                    max_wall_clock_time=timedelta(minutes=task_max_wall_minutes),
                    max_task_retry_count=1),
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
        consecutive_errors = 0
        while time.time() < deadline:
            try:
                tasks = list(batch.task.list(job_id))
                consecutive_errors = 0
            except Exception as e:  # noqa: BLE001 — Phase 21 δ: transient
                consecutive_errors += 1
                if consecutive_errors >= POLL_MAX_CONSECUTIVE_ERRORS:
                    raise
                print(f"[cloud_batch] task.list error ({e}); retry "
                      f"{consecutive_errors}/{POLL_MAX_CONSECUTIVE_ERRORS}",
                      file=sys.stderr)
                time.sleep(poll_seconds * consecutive_errors)
                continue
            states = [t.state for t in tasks]
            if tasks and all(s == batch_models.TaskState.completed for s in states):
                break
            time.sleep(poll_seconds)
        else:
            self.terminate_job(job_id)
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


_REAL_BATCH_RUNNER = BatchRunner


_WORKER_SCRIPT = r"""#!/usr/bin/env python3
'''Worker-side script. Reads input.pkl, runs the pickled function, writes
output.pkl. If a `./models/` directory is present (extracted from
models.tar.gz by the task command before invoking this script), its path
is prepended to sys.path so pickled functions from user modules can be
resolved during unpickling.'''
import pickle, sys, os, traceback

# Make local files importable by pickle's module-resolution.
cwd = os.path.abspath(".")
sys.path.insert(0, cwd)
models_dir = os.path.join(cwd, "models")
if os.path.isdir(models_dir):
    sys.path.insert(0, models_dir)

input_path, output_path = sys.argv[1], sys.argv[2]
with open(input_path, 'rb') as f:
    payload = pickle.load(f)
fn = payload['fn']
args = payload['args']
try:
    # Always pass args as a single positional argument. Scientific
    # outcome_fn(params) matches this. If you want kwarg unpacking, wrap
    # your function yourself.
    result = fn(args)
    with open(output_path, 'wb') as f:
        pickle.dump({'ok': True, 'result': result}, f)
except Exception as e:
    with open(output_path, 'wb') as f:
        pickle.dump({'ok': False, 'error': repr(e),
                     'traceback': traceback.format_exc()}, f)
"""


# ---------------------------------------------------------------------------
# Phase 21 δ: in-memory fake of the azure SDK surface BatchRunner touches.
# Used by the offline self-tests (here and in propagate_uncertainty.py) to
# prove pool teardown / budget ordering without any network call.
# ---------------------------------------------------------------------------

class _FakeAzure:
    """Context manager installing fake `azure.batch` / `azure.storage.blob`
    modules. `calls` records every mutating Azure operation in order.
    task_mode: "complete" (tasks finish and write outputs), "hang" (never
    complete), "flaky" (task.list raises twice, then completes)."""

    def __init__(self, existing_pools=(), task_mode="complete"):
        self.existing = set(existing_pools)
        self.task_mode = task_mode
        self.calls: list[tuple] = []
        self.blobs: dict[str, bytes] = {}
        self._saved: dict = {}
        self._flaky_left = 2

    # -- module construction --
    def _modules(self):
        import types
        fake = self

        class BatchErrorException(Exception):
            pass

        class _Obj:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        models = types.ModuleType("azure.batch.models")
        for n in ("PoolAddParameter", "VirtualMachineConfiguration",
                  "ImageReference", "JobAddParameter", "PoolInformation",
                  "JobConstraints", "TaskConstraints", "TaskAddParameter"):
            setattr(models, n, type(n, (_Obj,), {}))
        models.BatchErrorException = BatchErrorException

        class TaskState:
            completed = "completed"
            running = "running"
        models.TaskState = TaskState

        class Pools:
            def get(self, name):
                if name in fake.existing:
                    return _Obj(id=name, vm_size=DEFAULT_VM_SIZE,
                                target_low_priority_nodes=0,
                                target_dedicated_nodes=2,
                                auto_scale_formula=None)
                raise BatchErrorException("PoolNotFound")

            def add(self, pool):
                fake.calls.append(("pool.add", pool.id))
                fake.existing.add(pool.id)

            def delete(self, name):
                fake.calls.append(("pool.delete", name))
                fake.existing.discard(name)

        class Jobs:
            def add(self, job):
                fake.calls.append(("job.add", job.id))

            def terminate(self, job_id):
                fake.calls.append(("job.terminate", job_id))

        class Tasks:
            def __init__(self):
                self.by_job: dict[str, list] = {}

            def add_collection(self, job_id, tasks):
                fake.calls.append(("task.add_collection", len(tasks)))
                for t in tasks:
                    self.by_job.setdefault(job_id, []).append(t.id)
                    if fake.task_mode != "hang":
                        idx = int(t.id.split("-")[-1])
                        inp = fake.blobs[f"{job_id}/task-{idx:06d}/input.pkl"]
                        try:
                            res = {"ok": True, "result": deserialize_and_run(inp)}
                        except Exception as e:  # noqa: BLE001
                            res = {"ok": False, "error": repr(e)}
                        fake.blobs[f"{job_id}/task-{idx:06d}/output.pkl"] = \
                            pickle.dumps(res)

            def list(self, job_id):
                if fake.task_mode == "flaky" and fake._flaky_left > 0:
                    fake._flaky_left -= 1
                    raise ConnectionError("transient")
                state = ("running" if fake.task_mode == "hang" else "completed")
                return [_Obj(id=i, state=state)
                        for i in self.by_job.get(job_id, [])]

        class BatchClient:
            def __init__(self):
                self.pool, self.job, self.task = Pools(), Jobs(), Tasks()

        batch_mod = types.ModuleType("azure.batch")
        batch_mod.models = models
        batch_mod.BatchServiceClient = lambda *a, **k: BatchClient()

        class Container:
            def upload_blob(self, name, data, overwrite=True):
                fake.blobs[name] = data

            def download_blob(self, name):
                data = fake.blobs[name]
                return _Obj(readall=lambda: data)

        class BlobSvc:
            def get_container_client(self, c):
                return Container()

            def get_user_delegation_key(self, **kw):
                return "key"

        class BlobSasPermissions:
            def __init__(self, read=False, create=False, write=False):
                self.read, self.create, self.write = read, create, write

        def generate_blob_sas(**kw):
            perm = kw["permission"]
            fake.calls.append(("sas", kw["blob_name"],
                               "r" * perm.read + "c" * perm.create + "w" * perm.write))
            return "sig"

        blob_mod = types.ModuleType("azure.storage.blob")
        blob_mod.BlobSasPermissions = BlobSasPermissions
        blob_mod.generate_blob_sas = generate_blob_sas
        blob_mod.BlobServiceClient = lambda **k: BlobSvc()
        az = types.ModuleType("azure")
        storage = types.ModuleType("azure.storage")
        self.client = BatchClient()
        self.blob_svc = BlobSvc()
        return {"azure": az, "azure.batch": batch_mod,
                "azure.batch.models": models, "azure.storage": storage,
                "azure.storage.blob": blob_mod}

    def runner(self) -> "BatchRunner":
        # _REAL_BATCH_RUNNER: tests may monkeypatch the BatchRunner name.
        r = _REAL_BATCH_RUNNER(subscription_id="s", batch_account="b",
                               batch_account_url="https://b", storage_account="st")
        r._batch_client = self.client
        r._blob_client = self.blob_svc
        return r

    def __enter__(self):
        mods = self._modules()
        for k, v in mods.items():
            self._saved[k] = sys.modules.get(k)
            sys.modules[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        return False


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
    # Calling convention: fn(args) — args is a single positional arg. For
    # outcome_fn(params), pass the params dict as args.
    payload = serialize_function_and_args(_square, 9)
    ok(deserialize_and_run(payload) == 81,
       f"serialize+run _square(9) = 81, got {deserialize_and_run(payload)}")
    payload2 = serialize_function_and_args(_first_key, {"a": 3, "b": 4})
    ok(deserialize_and_run(payload2) == "a",
       f"dict arg passed as single param: got {deserialize_and_run(payload2)}")

    # --- Phase 21 δ: pricing + lifecycle against the in-memory fake ---
    lp = estimate_cost(200, 30.0, "Standard_A2_v2", 2, low_priority=True)
    dd = estimate_cost(200, 30.0, "Standard_A2_v2", 2, low_priority=False)
    ok(abs(dd.estimated_usd / lp.estimated_usd - VM_DEDICATED_MULTIPLIER) < 1e-9,
       f"P1: dedicated priced at {VM_DEDICATED_MULTIPLIER}× low-priority; got "
       f"{dd.estimated_usd / lp.estimated_usd:.2f}×")

    spec = PoolSpec(name="p-new", max_nodes=2)
    # L1: success path deletes the pool this invocation created.
    with _FakeAzure() as fa:
        r = fa.runner()
        with r.managed_pool(spec, use_low_priority=False):
            job = r.submit_function_tasks("p-new", _square, [2, 3],
                                          budget_usd_cap=100.0)
            res = r.wait_and_collect(job, timeout_minutes=1, poll_seconds=0)
        ok([x.get("result") for x in res] == [4, 9],
           f"L1: results collected; got {res}")
        ok(("pool.delete", "p-new") in fa.calls,
           f"L1: created pool deleted on success; calls {fa.calls}")
        sas = [c for c in fa.calls if c[0] == "sas"]
        ok(all(c[2] == "r" for c in sas if not c[1].endswith("output.pkl"))
           and all(c[2] == "cw" for c in sas if c[1].endswith("output.pkl")),
           f"L1b: least-privilege SAS (r inputs, cw outputs); got {sas}")
    # L2: exception inside the session still deletes.
    with _FakeAzure() as fa:
        r = fa.runner()
        try:
            with r.managed_pool(spec, use_low_priority=False):
                raise ValueError("boom")
        except ValueError:
            pass
        ok(("pool.delete", "p-new") in fa.calls,
           f"L2: created pool deleted on exception; calls {fa.calls}")
    # L3: timeout deletes the pool and terminates the job.
    with _FakeAzure(task_mode="hang") as fa:
        r = fa.runner()
        timed_out = False
        try:
            with r.managed_pool(spec, use_low_priority=False):
                job = r.submit_function_tasks("p-new", _square, [2],
                                              budget_usd_cap=100.0)
                r.wait_and_collect(job, timeout_minutes=0.0001, poll_seconds=0)
        except TimeoutError:
            timed_out = True
        ok(timed_out and ("pool.delete", "p-new") in fa.calls
           and any(c[0] == "job.terminate" for c in fa.calls),
           f"L3: timeout → job terminated + pool deleted; calls {fa.calls}")
    # L4: a pre-existing pool is reused and never deleted.
    with _FakeAzure(existing_pools={"p-new"}) as fa:
        r = fa.runner()
        with r.managed_pool(spec, use_low_priority=False) as created:
            pass
        ok(created is False and not any(c[0] == "pool.delete" for c in fa.calls)
           and not any(c[0] == "pool.add" for c in fa.calls),
           f"L4: pre-existing pool untouched; calls {fa.calls}")
    # L5: auto_delete=False keeps even a created pool.
    with _FakeAzure() as fa:
        r = fa.runner()
        with r.managed_pool(PoolSpec(name="keep", auto_delete=False),
                            use_low_priority=False):
            pass
        ok(not any(c[0] == "pool.delete" for c in fa.calls),
           f"L5: auto_delete=False honored; calls {fa.calls}")
    # L6: budget guard raises before ANY create call.
    with _FakeAzure() as fa:
        r = fa.runner()
        raised = False
        try:
            preflight_budget(10_000, 600.0, "Standard_D32s_v5", 4,
                             low_priority=False, budget_usd_cap=1.0)
            with r.managed_pool(spec, use_low_priority=False):
                pass
        except BudgetExceededError:
            raised = True
        ok(raised and fa.calls == [],
           f"L6: budget rejected before provisioning; calls {fa.calls}")
    # L7: submit on a dedicated pool prices dedicated (was low-priority).
    with _FakeAzure(existing_pools={"ded"}) as fa:
        r = fa.runner()
        raised = False
        try:
            # 200 × 30s on 2 dedicated A2_v2 ≈ $0.27; low-priority ≈ $0.05.
            r.submit_function_tasks("ded", _square, list(range(200)),
                                    budget_usd_cap=0.10)
        except BudgetExceededError:
            raised = True
        ok(raised, "L7: dedicated pool priced at dedicated rate in submit")
    # L8: transient task.list errors are retried, not fatal.
    with _FakeAzure(task_mode="flaky") as fa:
        r = fa.runner()
        with r.managed_pool(spec, use_low_priority=False):
            job = r.submit_function_tasks("p-new", _square, [5],
                                          budget_usd_cap=100.0)
            res = r.wait_and_collect(job, timeout_minutes=1, poll_seconds=0)
        ok(res and res[0].get("result") == 25,
           f"L8: flaky task.list retried; got {res}")
    # L9 (Phase 42 ε): the worker archive carries toolkit/ beside models/, so the
    # prescribed `from toolkit.alloc import ...` resolves in the task directory.
    import tarfile as _tarfile
    import tempfile as _tempfile
    with _tempfile.TemporaryDirectory() as _d:
        os.makedirs(os.path.join(_d, "models"))
        open(os.path.join(_d, "models", "outcome_fn.py"), "w").write("x = 1\n")
        _names = _tarfile.open(fileobj=io.BytesIO(models_tarball(os.path.join(_d, "models")).getvalue())).getnames()
    ok("models/outcome_fn.py" in _names and "toolkit/alloc/costs.py" in _names
       and not any("__pycache__" in n for n in _names),
       f"L9: the worker archive has models/ and toolkit/: {sorted(_names)[:8]}")

    if failures:
        print(f"FAIL: {len(failures)} case(s)", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("OK: all self-test cases passed.", file=sys.stderr)
    return 0


def _run_cloud_module_test() -> int:
    """Second live cloud test: validate the models_dir tarball upload
    path. Creates a scratch module with a user function, submits a job
    that pickles that function by qualified name, worker extracts the
    tarball and unpickles + runs. Proves custom outcome_fn modules work."""
    import tempfile
    import subprocess
    print("[cloud_batch] Live cloud module-upload self-test. Validates "
          "that worker can import a user-provided module from the tarball.",
          file=sys.stderr)
    try:
        runner = BatchRunner()
        runner._require_config()
    except RuntimeError as e:
        print(f"[cloud_batch] ERROR: {e}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        models_dir = os.path.join(tmp, "models")
        os.makedirs(models_dir)
        # Write a scratch user module with a toy outcome_fn. Note: we import
        # from a top-level module name (scratch_outcome); worker's
        # sys.path insertion of ./models/ lets it find this.
        with open(os.path.join(models_dir, "scratch_outcome.py"), "w") as f:
            f.write(
                "def compute(params):\n"
                "    # Exercises dict-unpacking and simple math so we know the\n"
                "    # worker really called our code, not a builtin.\n"
                "    x = params.get('x', 0)\n"
                "    y = params.get('y', 0)\n"
                "    return {'sum': x + y, 'prod': x * y, 'label': 'scratch'}\n"
            )
        # Now import that function the same way a modeler would, so the
        # pickle stores its qualified name.
        sys.path.insert(0, models_dir)
        import scratch_outcome
        user_fn = scratch_outcome.compute
        # Sanity-check locally before cloud submission.
        local_result = user_fn({"x": 3, "y": 4})
        assert local_result == {"sum": 7, "prod": 12, "label": "scratch"}, \
            f"local sanity failed: {local_result}"

        pool_name = f"module-test-{uuid4().hex[:8]}"
        try:
            print(f"  creating pool {pool_name}...", file=sys.stderr)
            runner.ensure_pool(pool_name, vm_size="Standard_A1_v2",
                               max_nodes=1, use_low_priority=False,
                               auto_scale=False, dedicated_nodes=1)
            print("  submitting 2 tasks via models_dir tarball...",
                  file=sys.stderr)
            job_id = runner.submit_function_tasks(
                pool_name=pool_name,
                fn=user_fn,
                args_list=[{"x": 3, "y": 4}, {"x": 10, "y": 20}],
                budget_usd_cap=0.10,
                avg_task_seconds=30.0,
                pip_deps=[],
                models_dir=models_dir,
            )
            print(f"  job {job_id} submitted, waiting...", file=sys.stderr)
            results = runner.wait_and_collect(job_id, timeout_minutes=20)
            print(f"  results: {results}", file=sys.stderr)
            expected = [
                {"ok": True, "result": {"sum": 7, "prod": 12, "label": "scratch"}},
                {"ok": True, "result": {"sum": 30, "prod": 200, "label": "scratch"}},
            ]
            ok = results == expected
            if ok:
                print("OK: live cloud module-upload test passed.",
                      file=sys.stderr)
                return 0
            print(f"FAIL: expected {expected}, got {results}", file=sys.stderr)
            return 1
        finally:
            try:
                runner.delete_pool(pool_name)
            except Exception:
                pass


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
    p.add_argument("--self-test-cloud-module", action="store_true",
                   help="Live cloud test of user-module upload path "
                        "(~$0.01, requires AZ_* env vars). Validates "
                        "models_dir tarball packaging and worker-side "
                        "sys.path insertion.")
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
    if args.self_test_cloud_module:
        return _run_cloud_module_test()
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
