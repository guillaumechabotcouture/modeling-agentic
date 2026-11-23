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
- Passing the Coiled token by name is a separate change (#370).

**Fixes from Codex review of the reporting-only version.**
- **Probes:** they run at once on their own threads under one deadline. A probe still running from an earlier pass is not started again.
- **Evaluation:** rules are evaluated after all probes, so launches of one tag agree. A tag rule is definite only when every launch of the tag has a fresh or final reading.
- **Unknown:** an incomplete probe, a live run without a phase or heartbeat, or a probe that cannot be applied gives `unknown`. A held monitor reports `unknown`.
- **Ended launches:** they are evaluated on their final reading and late collections.
- **Identity:** a collected result counts only for its own launch id and invocation, and an assignment missing its invocation or run id binds nothing.
- **Spend:** it covers every id ever recorded for the launch, and is unread when one id is listed under another name.
- **History:** each launch keeps one history entry, under one tag.
- **Failure kinds:** `completed_no_report` is an eval outcome, and a run collected before `run_status` existed is unknown.
- **Inputs:** a refused request name is stored as a digest. Probe text is redacted and progress is kept as a digest. No ETA is made up without a launch time. Rules must be finite, and an unreadable `clusters.json` still collects.

**Self-tests:** SV25-SV39, SV41-SV55 (supervisor).
