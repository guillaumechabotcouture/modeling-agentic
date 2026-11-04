---
phase: "v2"
commit: "N6"
status_group: "In review (v2 compute)"
---

## Changelog

**One command launches a VM job, waits for it and stops the cluster.** On 2026-11-03 an agent ran `cloud/vm_job.py up`, ended its turn to wait for the cluster, and never sent `start`; the VM idled out after 20 minutes with no job submitted, and about two hours were lost. `vm_job.py run <name> <job_id>` takes every `up` and `start` option. It creates the cluster, submits the job as soon as the first worker is up, prints a status line every `--every` seconds (default 60; outputs saved locally on each poll), then stops the cluster. A failed, timed-out, lost or unverified-storage job exits non-zero with the reason after its last poll, and the cluster is stopped unless `--keep-on-failure`. A cluster whose first worker never arrives is stopped too. A cluster of that name that is already running is refused, never reused or stopped, and a stopping one is waited out before creation. Polls and the final stop go through the guarded attach (`rc.attach`), so no step calls `coiled.Cluster(name=...)` on a stopping cluster. `--store`, `--store-account` and `--shutdown-when-stored` keep their meaning. Under `run`, the job's own shutdown waits two poll intervals plus a minute (`worker_side.job_task(shutdown_grace_s=)`), so `run` reads the final state first; with the laptop gone, the job still stops the cluster. A cluster that stops before its final state is read is reported as a failure. Coiled 1.135.6 counts `idle_timeout` from cluster start and has no "after the first task" setting, so `up` now warns that the cluster stops after its idle timeout unless `start` follows. The `cloud-compute` skill, README and `vm_job.py` docstring make `run` the default. Self-tests RN1-RN10 (`vm_job.py`) were written first and failed before the change. No cloud run.

## Status

- **A7:** `cloud/vm_job.py run` launches, waits and stops in one command; `up` warns about the idle timeout.
