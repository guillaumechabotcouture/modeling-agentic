---
phase: "v2 autonomy"
commit: "W0-J ι"
---

## Changelog

**Supervisor secrets by name only (`cloud/vm_job.py`, `cloud/supervisor.py`).** Owner, 2026-11-23: no secret is copied into a `vm_job` process's environment.

- **`vm_job --secret-env`** now allows `DASK_COILED__TOKEN` as well as `GH_TOKEN`. When the launcher's environment does not hold a token, vm_job reads it where it lives: `GH_TOKEN` from the login keychain (`modeling-agentic-supervisor-github`), the Coiled token from dask's config. Neither is copied into vm_job's own environment.
- **Presence checks:**
  - `keychain_has` asks for the keychain item's attributes only, never its secret.
  - `coiled_token_configured` reads the dask config files only, so a token exported only in the shell does not count.
- **`supervisor start`** checks only that each token is present, without reading either value. It passes them with `--secret-env`, and strips every spelling of either variable from each `vm_job` process's environment (`secret_free_env`). Every spelling matters because Dask lower-cases what follows `DASK_`.
- **Approved use:** the owner approved sending both tokens to the supervisor job, for the merge train and the run monitor.

Self-tests: SV60-SV61 (supervisor), VS6-VS7b (vm_job).
