---
phase: "F6"
commit: "R3 γ–δ"
---

## Changelog

**Batch records copied out of a live run are not read as a live batch (F6 run 3).** F6 run 3 was copied out mid-run, and its `analysis/batch/.compute_progress/` records, `*.partial.jsonl` journals and `*.job.json` records, which describe the VM's process, misled supervision. They are kept, because `scripts/compute.py` reuses a journal's finished rows on resume and settles a progress record by its heartbeat. The in-repo copy paths now mark the copy: `cloud/run_cluster.py checkpoint` (fetch_checkpoint) and `cloud/vm_job.py`'s poll sync write `copied_run.json` in the copied run dir (`scripts/lib/copied_run.py`), listing each in-flight file with its sha256 and the capture time. `compute.py status` and `wait` report state `copied` (exit 4) for a job record the marker lists unchanged. Without a marker (a copy made outside the repo, such as run 3's `cp` loop), a job record from another host reads as copied, and so does an older record with neither host nor start token whose journal, log and record were not written for `PROGRESS_STALE_S`. New job records name their host. A batch whose summary was written after it started still reads as finished. A record that a new process rewrote is read as usual. Self-tests CR1-CR6, CP1-CP6, RC40b and CK10b failed first.
