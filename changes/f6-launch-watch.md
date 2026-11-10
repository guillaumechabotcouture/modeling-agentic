---
phase: "F6"
commit: "S2-LW"
status_group: "F6 Stage 2"
---

## Changelog

**A tracked launch watchdog (F6 S2-LW).** The F5 launches were watched by a local, untracked bash script with the config's names written into it, and launched through a hold wrapper made obsolete by S1-8 (its patch of `run_cluster._submit` had the wrong signature). `cloud/launch_watch.py <config>` replaces both, with every name taken from the config. It runs `run_cluster.py <config> launch` with `MA_LAUNCH_INVOCATION` set to its own token. Exit 0, 3, 4 or 5 means something was submitted: nothing is stopped, and on 5 (or 3 with no keeper recorded) it runs `keeper`. Exit 1 or any other code stops clusters only when `clusters.json` carries this token with status `failed`, and then exactly the ids it names, by id; the old script stopped everything on any non-zero exit. When every run is done, or the run cluster has stopped itself, it runs `collect` and confirms that every label is collected locally for this launch (`result.json` with the launch id of this invocation's assignments) before it stops anything. The old script shut down first, while `status` could print `stored=None` with the agent VM holding the only copy; unconfirmed now means exit 4 with the run cluster left up. Further guards: a credit cap (`--cap`, default 650: billed credits of the recorded ids plus a projection for live instances; at the cap one collect, then a stop by id, exit 3); a stray sweep (a live cluster with this launch's names, unrecorded, created after the launch, stopped by id on its second sighting); a refusal (exit 7) of configs without `run_timeout_hours` or `cluster_timeout` unless `--allow-no-cloud-limits`; network errors retried and never read as "not running"; one `relaunch` of a run still missing after `--start-minutes` (exit 2 if it still does not start). Exit codes 0-7 are in the module doc. Self-tests LW1-LW8 (36 cases, fakes for the runner, the Coiled listing, billing and the clock) were written first and failed against a stub.

## Status

- **S2-LW:** `cloud/launch_watch.py`, a config-agnostic launch watchdog (invocation-bound stop on a failed launch, collect and confirm before shutdown, credit cap, stray sweep, cloud-side limits required); self-tests only, real smoke pending.
