---
phase: "v2"
commit: "N5"
status_group: "In review (v2 compute)"
---

## Changelog

**One cluster runs a whole engine batch and stops itself once its outputs are verified in storage.** The Nigeria re-run ran its batch on six Coiled clusters, one per shard. Each had one 8-vCPU worker and its own 2-vCPU scheduler, took about 5 minutes to start and idled for 20 minutes after its shard ended; a watcher on the laptop shut each one down. `cloud/vm_job.py up --workers N` now makes one cluster with one scheduler and N workers; it is created only after any stopping cluster of that name has stopped, and it waits for the first worker only, because compute.py gives work at once to workers that join later. `start --cluster-compute` gives the job this cluster's scheduler and TLS credentials (`compute.export_worker_env`, loaded by path from the shipped tree), so `compute.run_map` spreads the batch over the cluster's workers. compute.py gives chunks only to idle workers, and the job's own worker is busy with the job, so N workers give N - 1 compute VMs. `--store PREFIX` uploads `$JOB_OUT` when the job ends, skipping links and sensitive paths (`_truth/`, credentials), and reads every file back to compare sha256. `--shutdown-when-stored` then stops the cluster from inside the job, but only when the job is done and every output is verified in storage. A failed job, a copy that differs, or a tree without `results_store` leaves the cluster to its idle timeout, so the outputs can still be collected. `start` returns at once, and nothing waits on the laptop. The guarded scheduler reads (`worker_side.scheduler_workers`), the attach guard and `wait_for_workers=False` on attach are unchanged. Self-tests OC1-OC6 (`vm_job.py`) were written first and failed before the change. Live smoke on the IDM default workspace: a 240-job toy-engine batch ran through `run_map` on a 2-worker cluster (dask backend, 0 failed, 25 s), both outputs were verified in storage, and the cluster stopped about 2 minutes after `start` with no laptop action; 0.48 Coiled credits (about US$0.03).

## Status

- **A5:** one cluster with N workers runs a whole batch through `compute.run_map` and stops itself once its outputs are verified in storage (`cloud/vm_job.py --workers`, `--cluster-compute`, `--store`, `--shutdown-when-stored`).
