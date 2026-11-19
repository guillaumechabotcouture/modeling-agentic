---
phase: "F6"
commit: "rerun-α"
---

## Changelog

**One CPU family and a platform record for a run's cluster (F6 rerun prep).** A CI run showed decision-test outputs that differ across machines, and `vm_job.py --cpu/--memory` resolves to a list of priced types across VM families (for 8 vCPU and 64 GiB: Esv5, Edsv5 and the AMD Easv5), from which Coiled may pick any. `vm_job.py up|run --pin-vm-type T` now pins every worker to exactly one priced type, refusing an unpriced type or one whose vCPUs differ from `--cpu` before any cluster is listed or reserved (VT6a-d). New `cloud/platform_probe.py record <out.json> [--every S --until F]` records, for the job's host and every worker of its Dask scheduler, the CPU model and vector flags, the thread settings (OMP/OpenBLAS/MKL), the loaded BLAS and its thread count (threadpoolctl), library versions and the Azure VM size, merging workers that join later and flagging more than one platform (`one_platform`); it reads only and fails soft (PP1-9). Neither touches `scripts/` or `toolkit/`, so no run fingerprint or stored row changes.
