---
phase: "v2"
commit: "vm_job storage account"
---

## Changelog

**vm_job passes the storage account name to the job VM.** A job's `--store` upload failed on the job VM with "no storage account" while the launcher had `$COILED_STORAGE_ACCOUNT` set: the VM does not inherit the launcher's environment and `start` sent `--store-account` only when given. `start` now resolves the name (`--store-account`, else the launcher's `$MA_STORAGE_ACCOUNT` / `$COILED_STORAGE_ACCOUNT`, the order `results_store.configured_account` reads them) and passes it as the store's account and as `$MA_STORAGE_ACCOUNT` in the job's environment, a variable `agents/enforcement.py`'s cloud-prefix filter does not blank. Only a bare account name passes (3-24 lowercase letters and digits); a connection string, URL or key is refused before anything is submitted, without echoing it. With no account anywhere nothing is added and the worker's own configuration applies as before. Self-tests SA1-SA6.
