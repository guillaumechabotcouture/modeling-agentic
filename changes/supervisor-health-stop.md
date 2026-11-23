---
phase: "v2 autonomy"
commit: "W0-J θ"
---

## Changelog

**Supervisor run monitor, reporting only (`cloud/supervisor.py`).** Launches stay on the laptop. The supervisor reports on them, and the laptop acts on the report. The VM never deletes, stops or checkpoints anything.

**Request.** `enqueue-collect <config>` now also carries the launch's recorded cluster ids from `clusters.json`, but only when that record is this launch's: the invocation and launch id of every assignment. Helpers are included only under the config's own names. A legacy record without an invocation gets collection only. The request can also carry stop rules:
- `--stop-tag-usd`
- `--stop-run-usd`
- `--stop-no-progress-min`
- `--stop-same-failure K`
- `--tag`, whose default is the eval tag, or else the whole config name.

A launch is identified by its launch id and invocation, and must agree with the run id every assignment records.

**Every 5 minutes, per watched launch.** Each launch is probed on its own, under a deadline; a `SystemExit` from a refused connect is caught per launch. The monitor records:
- **For each run:** state, phase, heartbeat age, last progress, ETA and failure kind. A failure kind is a collected run's status, except eval outcomes and `awaiting_*` / `paused:*`; or a dead run; or exit 1, whether finished or collected.
- **For the launch:** its USD at the scopes file's floor rate (`budget._cluster_cost` per recorded id, over the full listing), unrounded.
- **For each declared rule:** its evaluation, `would_trip` true or false with its reason, computed only from that fresh probe. A failed or late probe, or a run whose inputs could not all be read, is reported as `unknown`, never as "no progress".

**Tag history.** Tag spend and failures are kept per launch, so a reused name loses nothing, and a re-tagged launch's history moves with it.

**Output.** Everything goes to `status.json`, redacted: run_health's patterns, Coiled's `ApiToken` header and the VM's own secret values. `supervisor.py status` prints it as tables.

**Other changes.**
- Only a request file named `<generated id>.json` is read.
- `tick` updates the state in place, so a failed `status.json` write loses nothing.
- The loop's sleep is bounded by the next probe or collection retry, with no 1-second polling while the monitor is held.
- `vm_job --secret-env` passes `GH_TOKEN` and `DASK_COILED__TOKEN` by name, reading them from the keychain and from dask's config files. `supervisor start` strips both from every `vm_job` process's environment, even when they are exported in the shell. It checks only that each is present; it never reads the values.

**Self-tests:** SV25-SV42 (supervisor), VS6-VS7b (vm_job).
