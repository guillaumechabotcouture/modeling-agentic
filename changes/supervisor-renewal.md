---
phase: "v2 autonomy"
commit: "W0-J ζ"
---

## Changelog

**Supervisor self-renewal through a lease (`cloud/supervisor.py`).** The off-laptop supervisor's job ended at `--max-hours` and nothing started the next one, so between-run work stopped until someone ran `start` from the laptop. One loop now holds `outputs/supervisor/<name>/lease.json`. Every write is a compare-and-swap on the blob's etag (Azure `If-None-Match` / `If-Match`), so two loops never both hold it, and a lease that has not been renewed for 15 minutes is free. Thirty minutes before `--max-hours` the loop starts its successor through `vm_job start` on the same name and state prefix, with its shipped tree (`--dir`) and the same secrets by name (`--secret-env GH_TOKEN`, `--secret-env DASK_COILED__TOKEN`, `--anthropic-key` when present) and a `--holder` id. It then hands the lease to the successor, writes `status.json:renewal`, and exits. A failed successor start keeps the lease and is retried every 5 minutes. A loop that finds another loop's live lease does nothing, and a named successor waits for the handover. `start` copies the owner-signed `sv`/`ev` budget records (no secret) to `budgets/`, and each loop installs them where `cloud/budget.py` reads them. `vm_job` allow-lists `DASK_COILED__TOKEN` for `--secret-env`. Without a `.env` file (the supervisor's VM), `run_cluster.read_keys` reads `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` from the environment. `status` prints the lease and the renewal. Self-tests: SV37-SV40 and SV41a (supervisor), VS6 (vm_job), RK1-RK2 (run_cluster).
