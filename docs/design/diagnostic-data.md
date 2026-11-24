# Diagnostic data: inventory and contract

Status: reviewed by the owner on 2026-11-24 (decisions in §11). Nothing here is built yet: PR 1
(`events.jsonl`) and PR 2 (the read-only MCP server) follow.

## 1. Purpose

Claude Desktop is the front end for diagnosing the harness. Our job is to decide what data it can see and in
what shape. This document:

1. lists the questions a person diagnosing the harness asks (§3, seeded with the owner's own questions);
2. maps each question to the data that answers it, where that data lives today, and what is missing (§4);
3. defines stable, versioned record shapes for what the tools return (§6), including the new run event
   stream, `events.jsonl` (§7);
4. sets the privacy rules (§2) and the review loop that changes this contract from real use (§9).

It does not replace the review workbench (`docs/design/review-workbench.md`), which reviews a run's science.
This contract covers operations: what is running, what it costs, why it stopped, and what needs the owner.

## 2. Rules

**Read-only by default.** The PR 2 server has no tool that changes a run, a launch, a cluster, a PR or a budget. Its only
writes are its own usage log and the feedback log (§9), outside the repository, through `lib.safe_io`. A
failed log write never fails a call. Actions (§8a) come later, in a separate server entry with its own safeguards.

**No network.** The server reads local files only. Anything remote (cloud checkpoints, cluster listings,
`gh` output, the supervisor's status) reaches it as a local cache that `tools/diagnostics/follow.py`
refreshes. Every cached record carries the time it was fetched, and every tool says when its data is stale.

**Confined reads.** Every read goes through `lib.safe_io` (no link followed at any component, one no-follow
descriptor per open), confined to the roots in §5. `run_artifact` reads only allow-listed paths (§8).

**Privacy.**

- Never read, list or return anything under a path with a `_truth` or `evals_private` component (the
  refusal `scripts/harness_metrics.py` already makes, `REFUSED_PARTS`), and never the decrypted eval bundle.
- No secrets: no environment variables, keychain items, storage keys, SAS tokens, GitHub tokens or budget
  signing material. The budget tool returns the verified record's fields (scope, amount, window, note), never
  the key. Strings that look like credentials are redacted in every result.
- Run values (estimates, costs, finding text) are fine for the owner on this machine. They are never
  written into the public repository: not in docs, commits, fixtures, PR text or change fragments.
  `tools/workbench/leak_check.py` stays the push-time check. The usage and feedback logs live outside the
  repository for the same reason.
- Results pass through Claude Desktop to the model provider, exactly as Claude Code sessions in this
  repository already do. No other party receives them.

**Fail closed, never empty.** A source that cannot be read returns `{"available": false, "why": ...}`,
never an empty list that reads as "nothing happened".

## 3. The questions

Grouped by what the person is trying to find out. Each has an id used in §4. The owner's own questions so far
are marked (owner) and quoted.

**R. One run**

- R1 (owner) "status?" / "what's running?": what is run X doing now, and since when?
- R2 (owner) "check its status closely, don't assume": the run's state from its own records, with the time
  each record was written, not from an agent's summary.
- R3 Why did it pause or fail?
- R4 Which finding cost rounds?
- R5 What did each stage cost (model spend and cloud compute)?
- R6 What does the owner need to answer for this run, and what exactly is the question?
- R7 Is a long step still alive, and when will it end?
- R8 What did the gate decide in each round, and by which rule?
- R9 Which inputs, code commit and engine produced this run (provenance)?
- R10 (owner) "is that ridiculous for flu, should we restart?" / "cancel, it's wasteful": is this run's
  time and spend out of line with comparable past runs, and how far is it from the end?
- R11 (owner) "eta to a successful run start to finish?": how long does a run of this kind take from
  launch to an accepted report, and where is this one on that path?

**C. Launches and clusters**

- C1 (owner) "what Coiled clusters are running?"
- C2 Is any cluster or launch over its stop rule (cap, alert level, max hours)?
- C3 Which labels of a launch are done, collected, or stuck?

**S. Supervisor and merge queue**

- S1 What is in the merge queue, and why was a PR skipped?
- S2 Is the supervisor alive (heartbeat), and what did it collect?

**P. Development**

- P1 Which PRs are open, and what is their CI state?
- P2 Which fixes' smoke markers fired in the last baseline?
- P3 (owner) "where are we with harness development, goals, how do PRs build toward them?"
- P4 (owner) "are we moving towards a checklist approach?": how much of what the critics find comes from the
  structural checklist versus free-form review, baseline to baseline?
- P5 (owner) "do you need anything from me / can I unblock you?": every item waiting on the owner, across
  runs, PRs, budgets and the work queue.

**B. Baselines and check value**

- B1 How did the last baseline compare with the one before (completion, interventions, owner wait)?
- B2 (owner) "which tests are most useful or productive?": which checks are productive, which are noise,
  which never fire.
- B3 What is the root cause of each non-accepted run in a baseline?

**M. The operator's machine and agents**

- M1 (owner) "are you using multiple agents?": which agent sessions and worktrees are active, and on what.
- M2 (owner) "why so many active shells?" / "it's overheating": what is running on the owner's laptop, and
  is it load the work needs.

## 4. Inventory: question to data

"Today" says where the answer lives now. "Missing" is what this plan adds; its PR is in brackets.

| Q | Data that answers it | Today | Missing |
|---|---|---|---|
| R1 | current state, round, status, paused reason; the time it entered the state; running task and its start | `run_state.json`: `state`, `round`, `status`, `paused`, `history[-1].at`; `journal[*]` with `status: started` | a single "now" record and a stream of what happened when [PR 1: `events.jsonl`; PR 2: `run_status`] |
| R2 | the same, each with the file's modification time and the record's own time | the files, read directly | `run_status` returns `as_of` per source and the age of the last event [PR 2] |
| R3 | the last history entry's `why`, the pause entry, the terminal status, the gate's rationale, the failed step's error | `run_state.json` `history` (`paused`, `why`), `metadata.json` `status`, `gate_r*.json`, `models/pipeline_report.json` row `error`, `models/logs/<step>.log` | one `status` event with the reason, and the paused question's request file [PR 1] |
| R4 | per finding: first and last round seen, status changes by round, the fix tasks planned for it, rounds it stayed open | `findings.jsonl` (`first_seen_round`, `last_seen_round`, `events`), `fix_plan_r*.json` (task to finding ids), `impact_ledger.jsonl`, `validator_history.jsonl` keys; `scripts/stall_rule.py` | a `finding` event per status change [PR 1]; spend per finding is not recorded: `run_findings` joins fix-plan tasks to journal keys by round and labels it `inferred` [PR 2] |
| R5 | model spend per task and per stage; cloud compute per step | `journal[*].usd`, `history[*].spent_usd` (cumulative at each transition), `budget_ledger.json`, `models/compute_ledger.json` (core-hours per call) | the model per task is not recorded; per-stage totals are computed, not stored [PR 1 records `model` on `task_end`; PR 2 `run_spend` sums by stage] |
| R6 | pending owner question: kind, items, fingerprints, the command that answers it | `scope_signoff_request.json` (`pending`, `items`), `spec_rethink_request.json`, `decision_spec.signoff_sheet.md`, the `paused` history entry | `owner_question` and `owner_answer` events [PR 1]; `owner_inbox` across runs [PR 2] |
| R7 | heartbeat age; a long step's elapsed time, projection and its basis | `pipeline_progress.json` (written only after a step passes its soft deadline, every 15 min) | a `heartbeat` event every N seconds while a task or pipeline step runs, with progress or ETA when known [PR 1] |
| R8 | per gate: round, post-write, action, rule, rounds remaining, open blocking count | `gate_r<N>[_post].json`, `final_gate.json`, `validator_history.jsonl`, `run_state.json` `last_gate` | a `gate` event [PR 1] |
| R9 | pipeline, commit, launcher, invocations; spec hash and signer; engine; input and figure provenance | `metadata.json` (`pipeline`, `commit`, `launcher`, `invocations`), `decision_spec.signoff.json`, `engine_choice.json`, `data/inputs_provenance.json`, `data/derivations_record.json`, `figures/*.provenance.json`, `harness_manifest.json` | none; `run_artifact` returns them [PR 2] |
| R10 | elapsed and spend so far, by stage, against past runs of the same question; remaining stages | this run's history and journal; past runs' `harness_metrics` rows (supervisor `metrics.json`, `ops/hb*_triage_data/hb*_metrics.json`) | a per-question reference (median elapsed and spend by stage over collected runs) [PR 2 `run_compare`] |
| R11 | per completed chain: launch to accepted report; per stage elapsed | `scripts/harness_metrics.py` (chains, completion, owner wait); history times | stage elapsed medians are not summarised; `run_compare` computes them and says how many runs they rest on [PR 2] |
| C1 | live clusters: name, id, state, age, instance types, spend | `cloud/budget.py status` (text lines only), `clusters.json` per launch out_dir | `budget.py status --json`, cached by `follow.py` with its fetch time [PR 2] |
| C2 | per launch: cap, alert level, burn rate, hours to each level; per scope: budget, used, remaining, expiry | `<out_dir>/cap_projection_<token>.json`, `~/.config/modeling-agentic/spend_alerts.jsonl`, `budget_<scope>.json` | none beyond C1's cache; `launches` joins them [PR 2] |
| C3 | per label: assigned, done, stored, collected, latest checkpoint | `assignments.json`, `<label>.result.json`, `<out_dir>/<label>/checkpoint/` (`run_cluster.py checkpoint`) | a local mirror of each label's latest checkpoint [PR 2 `follow.py`] |
| S1 | queued, merged, skipped PRs with reasons | supervisor `status.json` and `state.json` in blob storage; `supervisor.py pull` copies them to `~/modeling-agentic-keep/supervisor` | `follow.py` pulls on a schedule; `supervisor_status` reads the copy [PR 2] |
| S2 | supervisor heartbeat, collection by launch and label, errors | the same `status.json` | as S1 |
| P1 | open PRs: number, title, branch, head sha, checks state, review decision, updated time | `gh pr list` (network) | a cached `gh pr list --json ...` output with its fetch time [PR 2 `open_prs`] |
| P2 | per PR: its markers; per collected run: which markers are present | markers are prose in PR descriptions and `ops/hb*_smoke_markers.md`; `scripts/smoke_check.py` checks them | markers in machine-readable form: a `markers:` list in each change fragment's front matter, read by a `smoke_markers` tool that runs `smoke_check` logic over the mirrored runs [fragment format: owner yes 2026-11-24, small PR; `smoke_markers` tool in PR 2] |
| P3 | goals and their status; open and merged PRs mapped to them | `docs/design/improvement-plan.md` Status, `changes/*.md`, `ops/work_queue.md` | a goal id per change fragment (front matter `goal:`), so PRs group under the ids of `docs/design/goals.yaml` [owner yes 2026-11-24, same small PR] |
| P4 | per baseline: findings by source (checklist rule vs free-form critic), share accepted, share productive | critique YAMLs (`checklist` key, `toolkit/spec/critique_checklist.py`), `findings.jsonl` `source` and `kind` | a per-baseline count by origin; `check_value` gains an `origin` column [proposed, with B2] |
| P5 | every owner item: paused runs and their questions, PRs waiting on the owner, budgets near expiry, work-queue items marked OWNER | R6's files across runs; P1's cache; budget records; `ops/work_queue.md` | one `owner_inbox` tool [PR 2] |
| B1 | per baseline: completion, interventions I1-I9, owner wait | `scripts/harness_metrics.py` output (`ops/hb*_triage_data/hb*_metrics.json`, supervisor `metrics.json`); triage prose `ops/baseline_hb*_triage.md` | `baseline_summary` reads the metrics files and says which baseline each came from [PR 2] |
| B2 | per check: fired, productive, noise, never fired, cost in rounds | `harness_metrics.py --check-value` (open PR, branch `check-value-table`); `ops/check_value_analysis.md` | its JSON output cached per baseline; `check_value` reads it [PR 2, after that PR merges] |
| B3 | per non-accepted run: root cause, whether right | triage prose `ops/baseline_hb*_triage.md` §5 | prose only; kept as prose (§9 says when to promote it) |
| M1 | active agent sessions, their names and tasks; git worktrees and their branches | `git worktree list`; `.claude/worktrees/`; agent names exist only inside the lead's session | worktrees from git; the lead's `agents.jsonl` (§9) [owner yes 2026-11-24] |
| M2 | process counts by kind (shells, python, node, caffeinate, git), load average, CPU and memory share, thermal state | the OS (`ps`, `uptime`, `pmset -g therm`) | a `machine_status` tool that reads the process table and returns counts and the top consumers by command name, never arguments or environments [PR 2] |

## 5. Roots

The server reads only under these roots. Each is configurable by environment variable; none is a path inside
the public repository except `runs/`, which is gitignored.

| Root | Default | Holds |
|---|---|---|
| runs | `<repo>/runs` | local runs |
| mirror | `~/modeling-agentic-keep/mirror` | `follow.py`'s copies of cloud runs: `<launch>/<label>/` (each a run dir) |
| supervisor | `~/modeling-agentic-keep/supervisor` | the pulled `status.json`, `state.json`, `metrics.json` |
| ops | `~/modeling-agentic-keep/ops` | `work_queue.md`, `hb*_triage_data/*_metrics.json`, triage and check-value notes |
| cache | `~/modeling-agentic-keep/diagnostics/cache` | `budget_status.json`, `gh_prs.json`, `launches/<config>.json`, `check_value/<baseline>.json` |
| logs | `~/modeling-agentic-keep/diagnostics` | `usage.jsonl`, `feedback.jsonl` (the server's only writes) |
| budget | `~/.config/modeling-agentic` | `budget_<scope>.json` (verified fields only), `spend_alerts.jsonl` |

`follow.py` is the only part that touches the network: a read-only download of each label's latest checkpoint
through `cloud/run_cluster.py`'s checkpoint helpers (storage account from `$MA_STORAGE_ACCOUNT` /
`$COILED_STORAGE_ACCOUNT`), `cloud/budget.py status --json`, `cloud/supervisor.py pull` and
`gh pr list --json`. It runs every N minutes (default 10) and writes only under `mirror` and `cache`.

## 6. Record shapes

Every tool returns one JSON object:

```json
{"schema": "diag.<tool>/1", "as_of": "<UTC ISO>", "sources": [{"path": "...", "mtime": "<UTC ISO>",
 "fetched_at": "<UTC ISO, cached sources>", "stale": false}], "data": {...},
 "notes": ["..."], "derived": {"<field>": "record | inferred | unavailable"}}
```

- `schema` names the tool and its major version. A field is added within a version; renaming or removing one
  is a new version, and the server keeps the old one for one baseline.
- `stale` is true when a cached source is older than its refresh interval x 3, or a run's last event is
  older than its heartbeat interval x 3 while the run is not FINAL or paused. The tool says so in `notes`.
- `derived` follows `harness_metrics.py`'s convention: a field a record states is `record`; one reconstructed
  from other records is `inferred`; one that cannot be computed is `unavailable`.
- Times are UTC ISO 8601. Money is USD with its source (`billed`, `estimated`, `allowance`).

Per tool, the `data` shape (version 1):

- `run_status`: `{run, pipeline, commit, state, round, max_rounds, status, paused, since, running_task:
  {key, task, started, elapsed_s} | null, last_heartbeat_age_s, eta: {s, basis} | null, spent_usd,
  cap_usd, open_findings, owner_question: {kind, request, items} | null}`.
- `run_events`: `{events: [<§7 event>], next_seq, total, dropped_before}`, filtered by `kind`, `since`,
  `limit` (default 200). For a run with no `events.jsonl`, events are rebuilt from `history` and `journal`
  and marked `derived: inferred`.
- `run_findings`: `{findings: [{id, kind, severity, status, blocking, owner_state, first_seen_round,
  last_seen_round, rounds_open, claim, fix_requires, events, fix_tasks: [{round, task_key, usd}]}]}`.
- `run_spend`: `{cap_usd, spent_usd, by_stage: {<state>: {usd, tasks, elapsed_s}}, by_task: [...],
  compute: {core_hours, calls}}`.
- `run_compare`: `{question_key, n_runs, this: {elapsed_s, usd, stage}, reference: {<state>: {median_s,
  median_usd, n}}, remaining_stages}`.
- `launches`: `{launches: [{config, launch_id, labels: [{label, state, stored, collected,
  checkpoint_at}], cap_usd, level, burn_usd_per_h, hours_to_cap}], scopes: [{scope, budget_usd, used_usd,
  remaining_usd, expires}], clusters: [{name, id, state, age_h, usd, source}]}`.
- `supervisor_status`, `open_prs`, `baseline_summary`, `check_value`: the cached document's own fields, in a
  documented subset, plus `fetched_at`.
- `owner_inbox`: `{items: [{where: run|pr|budget|queue, ref, question, since, how_to_answer}]}`.
- `machine_status`: `{load: [1, 5, 15], thermal, processes: {<command name>: count}, top: [{command, cpu,
  mem}], worktrees: [{path, branch}]}`.

## 7. `events.jsonl` (PR 1)

Each v2 run appends one JSON object per line to `events.jsonl` in its run directory.

**Common fields.** `{"v": 1, "at": "<UTC ISO>", "seq": <int>, "pid": <int>, "kind": "<kind>",
"state": "<machine state>", "round": <int>}`. `seq` increases within a process; `pid` separates the run
from a signing command or a resume writing to the same file.

| kind | fields | written where (one place per kind) |
|---|---|---|
| `run_start` | `pipeline`, `commit`, `resume` | `Machine.run()` entry |
| `stage` | `from`, `to`, `why`, `spent_usd`, `elapsed_in_from_s`, `status` (on `to: FINAL`) | `Machine._go` (every transition: one event is both the leave and the enter) |
| `round` | `from`, `to` | `Machine._go`, when the round differs from the previous transition's |
| `task_start` | `key`, `task`, `allowance_usd` | `Machine._task`, after the journal records `started` |
| `task_end` | `key`, `task`, `ok`, `elapsed_s`, `usd`, `spent_usd`, `note` (300 chars), `model` (when the task runner reports it) | `Machine._task`, after the journal records `completed` |
| `heartbeat` | `step`, `elapsed_s`, `progress` (e.g. "rows 40 of 120"), `eta_s`, `basis` | a timer while `_task` or the analysis pipeline runs (default every 300 s); ETA from `pipeline_progress.json`'s projection or the batch's rows, else null |
| `gate` | `post_write`, `action`, `rule`, `rounds_remaining`, `blocking` (count), `waiting` (count awaiting the owner) | `h_GATE`, where it sets `last_gate` |
| `finding` | `id`, `finding_kind`, `severity`, `from`, `to`, `by` | `findings_ledger.save`, from the difference with the ledger on disk |
| `owner_question` | `question` (`spec_signoff`, `scope_signoff`, `spec_amendment`, `budget`), `request`, `items` (ids) | `Machine.run()` exit, when it ends paused |
| `owner_answer` | `question`, `paused`, `by` (the spec's signer, for a sign-off) | `Machine._go`, when the run leaves the state it paused in (owner and lead answers alike, whichever command signed) |
| `status` | `status`, `paused`, `terminal` | `Machine.run()` exit |

**Rules.**

- Appended through `lib.safe_io.write_nofollow(..., append=True)`, one `O_APPEND` write per line, so writers
  in several processes do not interleave within a line on a local file system; a line is capped at 4 KB, with
  long text cut.
- Never fatal: an append that fails is counted in the process and reported in the next event that succeeds
  (`dropped_before`) and in `run_state.json` (`events_dropped`).
- Outside every hashed or protected record: not in the integrity record, not an input of any pipeline step,
  not under `models/` or `data/` (so compute and pipeline reuse keys do not change). Agents may not write it:
  it joins `agents/enforcement.py`'s harness-owned list beside `pipeline_progress.json`.
- No secrets and no `_truth` content; finding text is the ledger's claim, cut to 300 characters.
- Smoke markers for PR 1: `events.jsonl~"kind": "stage"`, `events.jsonl~"kind": "task_end"`,
  `events.jsonl~"kind": "status"`, and `run_state.json:events_dropped=0`.

## 8. MCP tools (PR 2)

`tools/diagnostics/mcp_server.py`, stdio, for Claude Desktop. Read tools: `list_runs` (roots runs and mirror;
state, status, last event age), `run_status`, `run_events`, `run_findings`, `run_spend`, `run_compare`,
`run_artifact` (allow-listed: `metadata.json`, `run_state.json`, `findings.jsonl`, `gate_r*.json`,
`final_gate.json`, `validator_history.jsonl`, `fix_plan_r*.json`, `impact_ledger.jsonl`,
`scope_signoff_request.json`, `spec_rethink_request.json`, `decision_spec.yaml`,
`decision_spec.signoff_sheet.md`, `engine_choice.json`, `budget_ledger.json`, `pipeline_progress.json`,
`models/pipeline_report.json`, `models/compute_ledger.json`, `models/logs/*.log` (tail), `report.md`,
`data/inputs_provenance.json`, `analysis/*.yaml`), `launches`, `clusters`, `supervisor_status`, `open_prs`,
`owner_inbox`, `baseline_summary`, `check_value`, `smoke_markers`, `machine_status`. One write tool, `note_feedback` (§9).

It reuses the workbench's confined readers (`lib.safe_io`, the run-folder confinement of
`tools/workbench/workbench.py`). `tools/diagnostics/README.md` shows the `claude_desktop_config.json` entry.

## 8a. Actions (design only; levels 2 to 4 come in a later PR 3)

The modeller should be able to act from Claude Desktop, not only read. Actions come in four levels. PR 2's server
stays read-only (level 1). Levels 2 to 4 come in PR 3, as a separate server entry (`--actions`), so the owner
chooses in the Claude Desktop config whether a session can act at all.

**Safeguards for every level.**

- Nothing edits a run in place. An action either runs an existing signed command, which writes only its own
  records, or works on a copy.
- Each write is its own tool call, and the owner approves it in Claude Desktop. No tool chains a write after
  another write. Preparing a write (`prepare_*`) is read-only and returns the exact command, what it will change
  and a `prepared_sha256` that binds both. The write tool (`apply_*`) takes that hash and refuses if the run's
  files changed since it was prepared.
- Spend goes through the existing guards: the run's `--max-budget-usd` and, for cloud, `cloud/budget.py`'s
  launch check and `launch_watch.py`'s cap. No action has its own spending path.
- Every prepare and apply call is recorded in `usage.jsonl` with its arguments, the command, `prepared_sha256`,
  the exit code and the records it wrote (§9).
- The model can never sign. A signature is the owner's keychain prompt (the Ed25519 owner key, as
  `cloud/budget.py` uses it today). The tool can only start the command that raises the prompt.

| Level | What the modeller does | Tools | Must exist first |
|---|---|---|---|
| 1 Understand | read everything in §4 | §8 | PR 1 and PR 2 |
| 2 Decide at the gate | answer owner-inbox items | `prepare_answer`, `apply_answer`, `prepare_resume`, `apply_resume` | owner-key signing on the sign commands (below) |
| 3 What-if on a finished run | ask what the model would say with other inputs | `whatif` | a v2 what-if path over stored rows or the emulator (below) |
| 4 Change a step and re-run | propose a spec amendment and launch it | `prepare_amendment`, `apply_amendment` | after #371 and the same-run amendment redesign; #373 (`motivated_by`) |

**Level 2: decide at the gate.** These are the items `owner_inbox` lists: plan-changing decision tests, stability
results, degenerate intervals, scope declarations, spec amendments and spec sign-offs.

- `prepare_answer(run, item, decision: accept | decline | declare, reason)` returns `diag.prepare_answer/1`:
  `{run, item, finding_fingerprint, decision, reason, command: [argv], effect: "what the gate will read next",
  shown_sha256: {file: sha}, prepared_sha256}`. The command is the existing one: `scripts/scope_signoff.py sign
  <run> --by owner --answers <file>` for findings and declarations, and `agents/stage_machine.py sign <run>
  --by owner --expect-sha256 <spec> --expect-sheet-sha256 <sheet>` for the spec. The answers file is written to
  the diagnostics folder, never into the run.
- `apply_answer(prepared_sha256)` runs that command and returns its exit code, the record it wrote and the
  finding's status after it.
- `prepare_resume(run)` / `apply_resume(prepared_sha256)` is a separate, explicit action. It runs `main.py --v2
  --resume <run>` with the run's recorded cap, locally, or prepares the cloud relaunch command and does not run
  it. Answering never resumes a run on its own.
- **Must exist first: owner-key signing on the two sign commands.** Today they refuse only inside the agents'
  environment. A process started from Claude Desktop is not in that environment, so as they stand a tool call
  could sign as the owner with no key. Level 2 ships only after `scope_signoff.py sign` and `stage_machine.py
  sign --by owner` require the owner's Ed25519 signature over the answer, prompted from the keychain on every
  use, and the harness checks it against the tracked public key.

**Level 3: what-if on a finished run.**

- `whatif(run, set: {input: value}, outputs?)` returns `diag.whatif/1`, shaped like `run_compare`:
  `{run, set, refused: null | reason, source: stored_rows | emulator | outcome_fn, original: {headline,
  plan}, changed: {headline, plan}, change: {...}, interval, cost_estimate_usd}`.
- It wraps `scripts/ask.py <run> --set input=value`, which refuses anything outside the model card's valid
  domain. It runs on a copy, with `--no-log`, so the run's `whatif_log.jsonl` is not touched; the query goes to
  `usage.jsonl` instead.
- No engine runs. When neither the stored rows nor a fitted emulator cover the setting, the tool returns
  `refused` with the estimated cost of the engine runs it would need, and stops.
- **Must exist first:** `ask.py` loads a run's `models/outcome_fn.py`, which v1-era runs have. v2 runs need a
  what-if path over the analysis engine's stored rows and emulator, under the same domain refusal.

**Level 4: change a step and re-run** (after #371 and the same-run amendment redesign).

- `prepare_amendment(run, patch, motivated_by: [finding ids], reason)` returns `diag.prepare_amendment/1`:
  `{run, patch, motivated_by, spec_diff, reuse: {steps reused, steps rerun}, cost_estimate_usd, budget_check:
  {scope, remaining_usd, ok}, command: [argv], prepared_sha256}`. The command is the existing amendment path
  (`toolkit/spec/amend.py`), with the motivating finding ids recorded per #373's `motivated_by`.
- `apply_amendment(prepared_sha256)` runs the amendment. The owner then signs the amended spec through level 2's
  signed command. Launching is a separate prepare and apply through `cloud/launch_watch.py` and the budget
  check. Results come back through `run_compare`, with the parent run as the reference.

## 9. Learning from use

- **Question log.** The server appends each call to `~/modeling-agentic-keep/diagnostics/usage.jsonl`:
  `{"v": 1, "at", "tool", "args" (summarised: run name, filters; no free text over 200 characters),
  "records" (count returned), "error"}`. It is the server's only automatic write, through `lib.safe_io`; a
  failed append never fails the call.
- **Feedback.** `note_feedback(question, useful: yes|no|partly, missing: text)` appends
  `{"v": 1, "at", "question", "useful", "missing"}` to `~/modeling-agentic-keep/diagnostics/feedback.jsonl`.
- **Review at each baseline triage.** The lead reads both logs and updates this contract: add data that was
  missing, promote an analysis repeated three or more times into a standing summary tool, and retire a tool
  no call used across two baselines. Each change is a row in §11.
- **Agent log.** The lead appends one line per agent it spawns, and one when it ends, to
  `~/modeling-agentic-keep/diagnostics/agents.jsonl` (local, outside the repository):
  `{"v": 1, "at", "event": "start" | "end", "name", "task" (one line, no run values), "worktree" (path or
  null), "branch" (or null), "pr" (number or null), "outcome" (on `end`: done | stopped | failed, else
  null)}`. An agent is active when its last line is `start`. `machine_status` reads it beside
  `git worktree list` to answer M1.
- **Triage template.** Every baseline triage (`ops/baseline_hb<N>_triage.md`) ends with:

  ```markdown
  ## diagnostics_review
  - Calls since last triage: <count by tool> (usage.jsonl)
  - Feedback: <count yes / partly / no>; the "missing" items, grouped
  - Questions with no tool that answered them: <list>
  - Contract changes: added <...>; promoted <...>; retired <...> (rows in diagnostic-data.md §11)
  ```

## 10. Open items and gaps

| Item | Owner | Date |
|---|---|---|
| Owner review of this contract | owner | done 2026-11-24 |
| PR 1: `events.jsonl` (§7) | lead | PR open by 2026-11-26 |
| PR 2: MCP server, `follow.py`, README; `budget.py status --json` | lead | PR open by 2026-11-28 |
| `check_value` tool depends on the open `check-value-table` PR merging | lead | with PR 2 |
| P2 and P3: `markers:` and `goal:` in change-fragment front matter, CI checks the format, old fragments stay prose; goal ids in `docs/design/goals.yaml` (small separate PR) | lead | PR open by 2026-11-25 |
| M1 agent log `agents.jsonl` (shape in §9), written by the lead | lead | from 2026-11-24 |
| Level 2 prerequisite: owner-key (Ed25519, keychain) signing on `scope_signoff.py sign` and `stage_machine.py sign` (§8a) | lead | design with PR 3 |
| Level 3 prerequisite: a v2 what-if path over stored rows and the emulator (§8a) | lead | design with PR 3 |
| PR 3: action tools, levels 2 and 3; level 4 after #371 and the same-run amendment redesign | lead | after PR 2 |
| B3 root causes stay prose until the review loop shows they are asked for repeatedly | lead | next triage |

Tell us what is missing from §3, or which questions you would phrase differently, and we will use your version.

## 11. Contract changes

| Date | Change | Why |
|---|---|---|
| 2026-11-24 | First draft | owner decision: Claude Desktop as the diagnostic front end |
| 2026-11-24 | Owner yes: §8a Actions in four levels (understand; decide at the gate; what-if; change a step and re-run), design only; levels 2-4 in a later PR 3 | owner decision |
| 2026-11-24 | Owner review: yes to `markers:` and `goal:` in change fragments (CI-checked, old fragments stay prose, goals in `docs/design/goals.yaml`); yes to the lead's `agents.jsonl` (§9); keep `run_compare`, `owner_inbox` and `machine_status` | owner decisions on #381 |
