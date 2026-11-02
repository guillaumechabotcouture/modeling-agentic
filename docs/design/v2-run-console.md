# v2: the run console, a local page for the owner of a run (design note)

**Status:** written before the code (2026-11-02), branch `run-console`, PR into `v2`. v0: one Python
module (`scripts/run_console.py`) and one HTML file (`scripts/run_console.html`). Nothing in the
stage machine, the gate or the sign-off code changes. The console reads run artifacts, and it changes
a run only through the existing sign-off commands.

## 1. Who it is for, and what it replaces

The owner of a v2 run (`main.py --v2`) is an epidemiological modelling expert. Today they follow a
run through four routes: `agents/stage_machine.py status`, the files in the run directory,
`cloud/run_cluster.py <config> brief|health|status`, and the sign-off CLIs. Their feedback reaches
the learning loop (finding → layer → change → re-test) as free text in a chat with a coding agent. The
console puts these in one place, in the form of a research assistant's conversation:

- **Conversation first.** The main pane is one chronological stream. The run narrates itself as event
  cards rebuilt from its artifacts. The owner's messages and Claude's replies are interleaved with the
  cards.
- **Artifact side panel.** A card or a citation opens the figure, results, spec section, blocker or
  decision test in a right-hand viewer.
- **Steering is a first-class action.** Besides questions, the owner sends guidance or corrections.
  These are recorded as feedback with an anchor, a kind and a layer.
- **Learning view.** All feedback across the runs the console knows, grouped by layer, with an export
  digest of candidate changes.
- **Process stepper, compute panel, projects sidebar** (§4-§6).

## 2. Constraints, and how the design meets them

| Constraint | Design |
|---|---|
| Local only; run outputs derive from licensed data | stdlib `http.server` bound to 127.0.0.1, one HTML page with vanilla JS and a small built-in markdown renderer, no CDN. The only outbound call is the Claude API, for chat. The server refuses a non-loopback bind address |
| No new heavy dependencies | one: `anthropic==2.3.0`, the official SDK the `claude-api` skill requires. It was not in `requirements.txt` (the pipeline uses `claude-agent-sdk`). It is imported only when a chat message is sent, so the self-test and CI need no key or network |
| Disease-agnostic | the code names artifacts and fields, never a disease, place, unit or intervention |
| Never read or print `.env` | the key comes from `ANTHROPIC_API_KEY` only. If it is absent the chat pane says so; the rest of the console works. The key is never logged |
| Fail closed | every reader returns `{error: ...}` for an unreadable file, and the page shows that card or panel with the error, never an empty panel |
| The console never edits the spec, code or harness files | writes go to `<run_dir>/owner/` (feedback, chat), to a draft directory (§6) and to the sign-off commands below, nothing else. A self-test checks the write set |

## 3. What it reads (artifact names from the code)

| Panel / card | Source (writer) |
|---|---|
| question, state, round, status, spend vs cap, paused | `run_state.json` through `stage_machine.load_state` (the harness's reader) |
| stage changes, fix-loop rounds | `run_state.json` `history` (`Machine._go`, `h_FIX_LOOP`) |
| core-hours used vs cap | `models/compute_ledger.json` (`scripts/compute.py`); the cap is the run's `MA_COMPUTE_MAX_CORE_HOURS`, which the run dir does not record, so the console shows `compute.RUN_CAP_CORE_HOURS` (the default) unless `--core-hours-cap` is given |
| engine-batch progress, active compute call | ledger `reservations` (fn, n, purpose, started) joined with `.compute_progress/*.json` (done_tasks, core_s, t). ETA from the observed rate: (n − done) × elapsed / done |
| figures | `figures/*.png` with `<png>.provenance.json` (`toolkit/analysis/figures.py`) and the `shows:` captions in `analysis/figures.yaml` |
| results, report | `results.md`, `manuscript.md`, `report.md`, rendered as markdown |
| blockers | `critique_*.yaml` (`blockers`: id, severity, category, target_stage, claim, fix_requires, done_when, resolved; `round`) |
| gate decisions | `gate_r*.json` (action, rule_matched, rationale, unresolved_high, done_when_status, post_write) |
| decision tests | `analysis/decision_tests.yaml` and `models/decision_tests/*.yaml` (question, decision_changes, criterion, recommendation with / without) |
| spec sign-off | `toolkit/spec/signoff.status` and `decision_spec.signoff_sheet.md` |
| scope decisions | `scope_signoff_request.json` (pending items with fingerprints), rendered with `scope_signoff._test_md`; `scope_signoff.status` |
| headline, engine (sidebar) | first paragraph under `## Recommendation` in `results.md`; `engine_choice.json` `engine.ref`, else `run_state.engine` |

The stage list is `stage_machine.STATES`, imported, not copied.

## 4. Event cards and the stepper

The stream is rebuilt on every refresh. Each card has a time (the history entry's `at`, else the
file's mtime), a stage and an anchor `{kind, id, file}`:

| Card | Stage | Inline actions |
|---|---|---|
| stage changed: from → to, why, spend | the `to` stage | comment |
| batch progress: fn, purpose, done/n, ETA | ANALYZE | |
| figure produced: thumbnail, caption | ANALYZE | open, comment |
| blocker raised: id, severity, category, target stage, claim | CRITIQUE | agree / disagree / comment |
| gate decided: action, rule, rationale, unresolved HIGH count | GATE | comment |
| decision test: one line ("changes the plan" or not) | ANALYZE | open, comment |
| **your decision needed**: spec sign-off or scope answers | SIGN / OWNER_DECISION | answer controls, explicit confirm |
| owner message, Claude reply | the run's state when sent | |
| unreadable artifact: the error | the artifact's stage | |

**Stepper.** A strip across the top shows each state in order. A state is *done* when the history
left it, *current* when it is `state`, *waiting* when it is current and `paused` is set, *failed* when
the run ended in a failure status (`run_failed`, `completed_gate_error`, `completed_gate_not_accepted`,
`completed_scope_declined`) and this was the state FINAL was entered from, and *skipped* when it comes
before the current state in order and the history never entered it. FIX_LOOP, OWNER_DECISION and
PRESENTATION are left out when they were never entered. The strip is a thin line of dots with short
labels: done is filled, current is ringed, a failed state is a small red dot with the reason on hover,
and a state not reached is muted. It shows the number of fix-loop rounds (history entries into
FIX_LOOP) when there were any. Clicking a state filters the stream to that stage's cards.

**Status line.** At most four items, in plain words: the state and round, the engine batch
("Engine batch 184/256", with an ETA only while the run is live), spend, and compute used. A run is
finished when it is FINAL, has a status (from `run_state.json` or `metadata.json`), or
`metadata.json` records `finished`. A progress record is live only while the run is unfinished and
the record was updated within `STALE_S`; otherwise the batch reads "interrupted" and has no ETA.
Compute used is the ledger's settled `core_seconds` plus the `core_s` of live progress records.

## 5. Owner inbox: the only writes that change a run

| Question | Path (unchanged harness code) | Refusals the console adds |
|---|---|---|
| spec sign-off, local run | `python3 agents/stage_machine.py sign <run_dir> --by owner --note <note>` (calls `toolkit/spec/signoff.sign` through the backend) | no confirm; empty note; `signoff.status` is not `awaiting`; run is a mirror |
| scope answers, local run | `python3 scripts/scope_signoff.py sign <run_dir> --answers <tmp.yaml> --note <note>` | no confirm; empty note; no pending request; a pending item without accept/decline; run is a mirror |
| either, remote run | `python cloud/run_cluster.py <config> sign <answers.yaml> <note>` (worker side: scope answers when `scope_signoff.py status` is `awaiting`, else `decision_brief.py sign`) | same, plus no `--cloud-config` |

The answers file has the format `render_md` prints: one line per finding, `"<finding>": accept` or
`decline`, or `{decision, note}`. The console builds it from the controls and never writes
`scope_signoff.json` or `decision_spec.signoff.json` itself. The command's stdout, stderr and exit
code are shown on the card. The self-test round-trips an answer through `scope_signoff.load_answers`
and the check `h_OWNER_DECISION` makes (`answers` non-empty).

Known limits, recorded as open items:
- `stage_machine.OWNER_LIFECYCLE_WIRED` is `False` on `v2`, so a v2 run does not reach
  OWNER_DECISION today. The card is built from the request file whenever one is pending, so it will work
  once the lifecycle is wired.
- Remote spec sign-off: `worker_side.sign_side` signs `decision_brief.py` when no scope request is
  pending. A v2 run with a spec and no brief needs `stage_machine.py sign` on the worker. That is a
  harness change, not made here; until then the console shows the CLI's refusal.

## 6. Projects, chat persistence and "Build on this run"

**Discovery.** `--roots` (default: the repo's `runs/`) is scanned to depth 4 for directories with
`run_state.json`. The sidebar labels each run by its path under the root without `runs` folders and
without the timestamped run folder (`F1/runs/2026-..._question` is "F1"), and under the label shows a
state chip, the headline number ("12.34M DALYs", from "averts ... " in the recommendation) and the
start date. Group headers (the root's name) appear only when there is more than one root.

**Projects and lineage.** Runs with the same `question` are one project, titled with a short question
title. Inside it the runs form a lineage ordered by spec sign-off time, then engine-choice time, then
history; the sidebar shows the newest on top. Each row shows a short label (the folder's first token:
`F2_baseline` is "F2" when that is unique), a status chip (completed, failed, stopped, running, waiting),
the headline number, and one line: the relation to the run before it and the scenario. The relation is
*amended + re-signed* when the signed spec's sha differs, *resumed* when the history has a FINAL → stage
transition on the same spec, and *relaunched* otherwise. The scenario is a structured diff of the spec's
lineage fields (`horizon`, `objective.horizon`, `objective.delivery`, `decision_units.engine_attributes`,
`decision_units.defaults_accepted` keys, `constraints`, `analysis.decision_tests` ids), shown as a short
phrase; the sign-off note is its tooltip. The selected run expands to its engine and version (from
`engine_choice.json`), why it ended, outcome with interval and the largest package-component shift
against the nearest earlier run with an `allocation.csv`, model spend, spec sha, sign-off time and
signer, launch commit when `metadata.json` records one, folder and path. A status counts from
`metadata.json` only when its `finished` is not older than the last history entry (a resumed copy keeps
its source's metadata).

**Staging copies.** A directory is a draft, listed under "Drafts and staging copies", not a run, when it
never got past sign-off (state SPEC or SIGN and no `engine_choice.json`), or when its `run_state.json`
and signed spec are byte-identical to another run's; the copy whose label sorts first is the run.

**Compare.** Shift-clicking a second run opens a compare view in the main column (older run on the
left): status, engine versions, outcome and interval, spend, the spec diff, units per package
component, and each decision test's "changes the plan?" side by side.

**Reader support (ζ).** Data the owner compares is a table, never JSON: the gate's done-when status
joined with each blocker's done-when checks (criterion, what it checks, pass or fail, detail, mechanical
or judged, grouped by blocker); one table per critique round of blockers (id, severity, reviewer, target
stage, one-line claim, status; a row expands to the claim, evidence and fix); one table of decision tests
(the spec's question text, values tested, plan changes, share of units changed, re-solved objective vs
the point plan); and the compare view, runs as columns. The stream opens with a summary of the run's key
numbers, each with its unit and its source file: outcomes averted (the headline first, with its 95% UI
and median from `analysis/uncertainty.yaml`), envelope (`decision_spec.yaml`), allocatable and allocated
cost and units with no package (`analysis/point.yaml`). Every card and table says what it is and whether
it needs the owner. Terms the pipeline uses are marked and defined on hover from one `GLOSSARY` in the
module, each with the file it is defined in; benchmark ids are defined from the run's engine card. An
unfinished run that is not moving says where it stopped and which journal task never finished; the run
does not record who stopped it, so the page says so and shows the latest operator note in `progress.md`.
Compute used counts this run's core-hours and names engine-batch points reused from an earlier run in the
lineage (same batch output and code fingerprint).

**Figures.** A Stream / Figures toggle above the stream. Figures is a grid of thumbnails with the caption
from `analysis/figures.yaml` and the producer and time from the provenance; a click opens the figure in
the side panel with a comment button anchored to it. With no figures, one line says why (the run has not
reached ANALYZE, ANALYZE is running, the run ended in ANALYZE before it wrote any). `--run-dir` adds and selects one run.

**Persisted conversation.** `<run_dir>/owner/chat.jsonl` (one line per message: ts, role, text, anchor,
stage, usd) and `<run_dir>/owner/feedback.jsonl`. Selecting a run loads both and rebuilds the cards.

**Build on this run.** What the harness supports today (checked in `main.py`, `stage_machine.py`):
- `main.py --v2 --resume <run_dir>` continues the same run from `run_state.json`. That is the only reuse
  path today.
- A new v2 run always starts at SPEC, and `h_SPEC` always drafts a new spec through `draft_spec`. There
  is no seed-spec input, and `stage_inputs` refuses files under `runs/`, so a new run cannot be pointed
  at an old run's spec or derived data.
- There is no `evidence_planner` module on `v2` at 1412f26 (one stored run has such a directory from a
  development build, but no code reads it).
- Batch reuse is decided by `scripts/compute.py`: an earlier `--out` is reused when its `.summary.json`
  has the same `fn` and `code_fp`, and `code_fp` is `code_fingerprint(run_dir, fn)` over the run's
  `models/`, `data/` and the repo's `scripts/`, `toolkit/`.

So v0 writes a draft directory, `<first root>/_drafts/<ts>_from_<run>/`, and launches nothing:
`build_on.json` (source run, question, spec sha256 and sign-off status, engine choice, derivations,
and for each stored batch whether `compute.code_fingerprint` with today's code matches its `code_fp`,
that is, whether it would be reused) plus copies of the signed spec, `analysis_settings.yaml`,
`engine_choice.json`, `data/derivations.yaml`, `data/derive/`, `data/derivations_record.json` and
`data/derived/`. The draft is under `runs/` (gitignored), so licensed derived data never reaches git.
The page shows the exact commands: `main.py --v2 --resume <source run>` to continue, and
`main.py --v2 "<question>"` for a new run, which today drafts its own spec. Reusing the signed spec,
derivations and batch in a new run needs harness support: a seed spec input to `h_SPEC` (re-validated
and re-signed by the owner, since the sign-off binds the run's own hashes), and a staging rule that
accepts a draft's files. Proposed as follow-ups, not built.

## 7. Chat

- Model `claude-opus-5-5` through the official SDK, streamed (`client.beta.messages.stream(...)`,
  `get_final_message()`), adaptive thinking, effort `medium` set explicitly, `max_tokens` 8000, and the
  server-side refusal fallback (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`) as the
  skill recommends.
- System prompt in two blocks: the console's role and rules (fixed text), then the run context (state,
  timeline tail, `results.md`, the latest gate, unresolved blockers, decision tests, pending owner
  questions; each part truncated to a stated character budget, and the truncation stated). Both carry
  `cache_control`, so turns on an unchanged run read the cache. The question goes in `messages`, after
  the last 12 turns of `chat.jsonl`.
- Rules in the prompt: cite every number and claim as `[[file#anchor]]` (a file in the run dir plus a
  blocker id, heading or key); do not invent numbers; say when the context does not contain the answer.
  The page renders citations as links that open the side panel, and marks a sentence that has a digit
  and no citation as "uncited".
- Cost: priced from `usage` at Opus 5.5 rates ($4 / $20 per MTok, cache writes $5, cache reads $0.20).
  A call is refused before it is made when the session's spend plus its worst case (prompt estimate plus
  `max_tokens` of output) would pass the cap (default $2, `--chat-cap-usd`). The running spend is shown.
  If the fallback model served a reply, its rates differ; the reply is marked "priced at Opus 5.5 rates".
- Steering: a message marked guidance or correction is appended to `feedback.jsonl` first. Claude is
  then asked to acknowledge it: what it would change and in which layer. It cannot change anything.

## 8. Feedback for the learning loop

`<run_dir>/owner/feedback.jsonl`, one JSON object per line: `id`, `ts`, `anchor {kind, id, file}`,
`kind` (question | correction | guidance | approval), `layer` (engine | analysis_harness | critique |
writer | spec | data | process), `text`, optional `proposed_change`, and `verdict` (agree | disagree)
for blocker reactions. Unknown kinds or layers are refused.

**Can an agent forge it?** v2 tasks (`agents/v2_sdk_tasks.py`) have no shell and may write only the
files in their `OWNS` entry, so `owner/` is already refused; a self-test checks this for every task. The
v1 lead path is not a target of the console. **Should the harness hash it?** Not in v0: the console
appends to it while the run is live, from outside the run's process, so a hash taken by the harness
would be stale at once, and no run step reads it. If a later step feeds feedback into a run (for
example the fix loop reading owner guidance), it should be recorded the way `scope_signoff` records
answers (a harness ledger entry under a nonce), not trusted as a file.

**Export.** One digest across all runs the console knows, written to `<first root>/_owner/`
(`feedback_digest_<ts>.md`): per layer, each item with its run, anchor, kind, text and proposed change,
ready to become a learning-loop PR. The console never edits code or the spec.

## 9. Remote runs (Coiled)

- `--mirror <dir> --sync-cmd "<command>"`: the console runs the owner's command every `--sync-s`
  seconds (default 120) and reads only the mirror. The command must keep `owner/` (for example rsync
  without `--delete`, or excluding `owner/`).
- `--cloud-config <config.py>` enables the remote sign path and the compute panel: `run_cluster.py
  <config> status`, `health` (one shot) and `report-cost`, run as subprocesses. Those commands are
  read-only and already attach only after checking that a cluster is running
  (`worker_side.attach_existing`). The console never creates, scales or stops a cluster. It refreshes
  only while the "live" toggle is on, and the server refuses a refresh less than 60 s after the
  last. Without a config, or when `coiled` cannot be imported, the panel says "not available".
  The outputs are shown as text; the first status line is parsed into cluster states. Structured
  fields (workers, target, quota refusals) need a `--json` output on `run_cluster.py` (open item).

## 10. Tests (written first, `python3 scripts/run_console.py --self-test`)

A synthetic run dir with toy numbers (no licensed data):

| Case | Checks |
|---|---|
| RC1 | state, stepper (done, current, waiting, skipped, failed) and fix-loop count from `run_state.json` |
| RC2 | blockers and gates parsed; an unreadable critique or gate file gives an error card, not nothing |
| RC3 | figures with provenance and captions; ledger and progress give done/n and an ETA |
| RC4 | feedback append validates kind and layer; export groups by layer across two runs |
| RC5 | scope answers go through `scope_signoff.py sign`; `load_answers` reads them back; refusals (no confirm, no note, missing answer, mirror) |
| RC6 | spec sign-off refusals (no confirm, no note, absent spec, already signed) and a confirmed sign through `stage_machine.py sign` |
| RC7 | chat context assembly (truncation stated, cached blocks) and the cost cap with a mocked client; the key is never in a log line |
| RC8 | v2 tasks cannot write `owner/`; the console writes only under `owner/`, the draft dir and through the sign commands |
| RC9 | run discovery under roots; build-on draft (copies, batch reuse verdict from `compute.code_fingerprint`) |
| RC10 | the HTTP server binds 127.0.0.1 only and serves the page and `/api/run` on a toy run |
| RC15 | lineage on a toy lineage of three runs plus two staging copies: one project, order, drafts, relations, the spec diff and its phrase, engine version, chip and end reason, package shift, compare, `/api/runs` projects and `/api/compare` |
| RC16 | every term the page marks, every gate action, rule and severity has a definition and a source; JSON only for anchors and request bodies; done-when rows join gate status and blocker; the summary's order, intervals and sources; reused batch points |
| RC14 | live vs finished: an ETA and live core-seconds only for a fresh progress record on an unfinished run; a progress record left on a finished run, or a stale one, is interrupted; the headline number |

Plus one real end-to-end check, not part of the self-test: start the server on a toy run, hit the
endpoints, and send one real chat message (spend capped at $1, reported in the PR).
