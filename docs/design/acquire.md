# v2: an ACQUIRE state, so a run can start from a bare policy question (design note)

**Status:** written before any code (2026-11-23), at the owner's request. Nothing here is built. It extends
`v2c-orchestration.md` (the states), `v2-derived-inputs.md` (staged inputs, derivations, the sandbox) and the
evidence stage of v2 autonomy 1B-ii (`agents/stage_machine.py` `h_EVIDENCE`). File and function names are
proposals. The stage machine is being split into a package in parallel (branch `split-sm-pkg-v2`): if that lands
first, the handler moves with the other handlers, and the state names, `EDGES` and fields below stay the same.

## 1. The problem, and the guarantee to keep

Today a v2 run starts at SPEC. `stage_inputs` (`agents/stage_machine.py`) copies every repository file the
question names into `data/inputs/` and records each file's sha256 in `data/inputs_provenance.json`. Every number
the analysis uses then comes from harness code (the owner-signed spec, an engine, `toolkit/analysis/`) applied to
those hashed files, or to derived tables that the harness builds from them in a no-network sandbox
(`v2-derived-inputs.md` §3.1). The tasks (LLM sessions) have file tools only (`TOOLS` in
`agents/v2_sdk_tasks.py`; test S16). They cannot run code, reach the network or write `data/inputs/` or
`data/derived/` (`HARNESS_PREFIXES`).

A question with no data files ("How should country X allocate its measles budget across provinces next
year?") therefore has nothing to stage. The spec task has only engine cards and priors, and principle 3 (burden
from external data-driven estimates, effects from trials, current prices) cannot be met.

**ACQUIRE** is a new harness-controlled state before SPEC. A bounded task chooses public data to fetch from a
versioned list of allowed sources. The harness does the fetching, checks and hashes every file, and records where
it came from. It then places the files in `data/inputs/` exactly as if the question had named them. From SPEC on,
nothing changes.

**The guarantee, restated for acquired data.** Every number still comes from harness code applied to files
whose bytes are hashed and recorded. An acquired file is never written by a task, only by the harness fetcher,
and its record says where it came from (URL, time, query, source, licence). The difference from a staged file is
that the source is a public service, not the repository. A later re-fetch may return different bytes, so the
recorded bytes are the input, never the URL.

**Terms used here.**

| Term | Meaning |
|---|---|
| task | one bounded LLM session (`SdkTasks._session`), with a dollar cap and a turn cap |
| fetch tool | a tool that the harness implements and offers to the acquire task only; the harness, not the LLM, makes the HTTP request |
| quarantine | `acquire/quarantine/`: where fetched bytes wait for checks; only the fetch tool writes there |
| promotion | the harness step that checks a quarantined file and copies it into `data/inputs/acquired/` |
| allowlist | `.claude/orchestration/acquire_sources.yaml`: the public sources a run may fetch from, with licence notes |
| prompt injection | text inside fetched content that tries to instruct the model reading it |

## 2. Placement in the state machine

### 2.1 States and edges

```
ACQUIRE -> SPEC -> EVIDENCE -> SIGN -> ENGINE_CHOICE -> ...
   ^        |         |
   +--------+---------+      top-up back-edges, before any signature only
```

Changes to `agents/stage_machine.py`:

| Item | Change |
|---|---|
| `STATES` | `"ACQUIRE"` first |
| `EDGES` | new `"ACQUIRE": {"SPEC", "ACQUIRE", "FINAL"}`; `"SPEC"` gains `"ACQUIRE"`; `"EVIDENCE"` gains `"ACQUIRE"` |
| `LLM_STATES` | gains `"ACQUIRE"`, so `step()` checks the budget before the handler runs |
| `RunState` | new fields `acquire: str = "off"` (`off` or `auto`) and `acquired: dict` (status, counts, rounds) |
| `init_run` | new argument `acquire="off"`; when it is `auto`, the first state is `ACQUIRE`, otherwise `SPEC` as today |
| new constants | `MAX_ACQUIRE_ROUNDS = 2` (the first acquisition plus one top-up), `ACQUIRE_CAP_USD` (§6.2) |
| new pause | `AWAITING_INPUTS = "awaiting_inputs"`, printed as `paused:awaiting_inputs`, exit 2 |
| `main.py` | `--acquire` (off by default); recorded in `metadata.json` as `acquire: off|auto` on every run |

`RunState` defaults to `acquire="off"`. A state file saved before this change has no such field, so it loads as
`off` and resumes exactly as it does today. `metadata.json` readers treat a missing `acquire` as `off`.

### 2.2 Entry and exit

`h_ACQUIRE` (a thin handler that calls `agents/acquire.py`):

1. **The off-switch.** Call `stage_inputs(run_dir, question)` first, as SPEC does. If it staged any file, the
   question supplies its inputs: record `acquired.status = "skipped_question_supplies_inputs"`, set `st.acquire =
   "off"` for the rest of the run, and go to SPEC. No task runs and no cost is incurred. The back-edges in §2.3
   are then closed too. Every eval question names its files under `data/evals/<q>/`, so on the eval suite
   ACQUIRE is never entered (the launcher does not pass `--acquire`), and even if it were, nothing would happen.
2. **Plan and fetch.** Run the bounded `acquire` task (§3) with a packet built by the harness: the question; the
   input kinds that the registered engines' cards declare (unit attributes, calibration-target kinds and decision
   inputs, from `toolkit/engines/decision_inputs.py`); the allowlist; and, on a top-up, the problems that sent the
   run back (§2.3). The task calls the fetch tool. Each call writes one quarantined file and one fetch record.
3. **Promote.** The harness checks, hashes and promotes each file that the task marked `keep` in
   `acquire/plan.yaml` (§4). Anything not marked `keep` is deleted with its reason recorded.
4. **Exit.**

| Outcome | Next state | Recorded |
|---|---|---|
| at least one file promoted | SPEC | `acquired.status = "promoted"`, `acquire/record.json` |
| nothing promoted | pause `awaiting_inputs` (owner question 3) | `acquire/inputs_request.md`: what was looked for, what failed and why |
| a promoted file's bytes differ from its quarantine hash, or a file under `data/inputs/acquired/` changed | FINAL, `artifact_tampered` (exit 1) | the mismatch |
| below the budget floor | pause `awaiting_budget`, as for any task | |
| task failed or crashed, round tries left | ACQUIRE (retry once) | the failure note |

### 2.3 Top-up back-edges (before sign-off only)

SPEC or EVIDENCE may find that an input is missing: a calibration target with no observed source, or a dossier
entry with no observation. Each check that can name a missing input adds `missing_input: <kind>:<name>` to its
problems. When `st.acquire == "auto"`, no signature exists (`spec_signed_once` is false) and `acquired.rounds <
MAX_ACQUIRE_ROUNDS`, the handler goes to ACQUIRE with those problems as the packet's `wanted` list, instead of
spending a SPEC try. After sign-off nothing more is fetched: a gap found later becomes a finding for the next run
("freeze and batch"), because the signature binds the input set.

### 2.4 Resume

- Every fetch has a key `ACQUIRE#<visit>:f<n>`. Its record (URL, sha256, status) is written to
  `acquire/record.json` before and after the request, through `lib.safe_io`, like the task journal. A resumed run
  never repeats a completed fetch. It re-checks the quarantined file's hash and either uses it or, on a mismatch,
  deletes it and marks the fetch `discarded_on_resume`.
- A partial download (`*.part`) is deleted on resume and never promoted.
- The task itself uses the existing journal (`st.journal`). A completed task is not re-run; an interrupted one is
  charged its allowance and retried once, as `_settle_interrupted` does now.
- Promotion is idempotent. A file already in `data/inputs/acquired/` whose hash matches its provenance entry is
  left as it is; one that does not match is tampering (exit 1).
- A run reused for a spec-only amendment (`--from`, reuse) copies the acquired bytes and their provenance. It
  never fetches again, so the amended run reads the same inputs.

## 3. The acquire task's powers

| Power | Acquire task | Every other v2 task (unchanged) |
|---|---|---|
| file tools | `Read`, `Glob`, `Grep`, `Write`, `Edit` | same |
| Bash, Agent, NotebookEdit | refused at PreToolUse (S16) | refused |
| built-in `WebFetch`, `WebSearch` | **refused**: not in its tool list, denied by the hook | refused |
| network | only through `mcp__acquire__list_sources`, `mcp__acquire__search`, `mcp__acquire__fetch`, served in-process by the harness (`claude_agent_sdk.create_sdk_mcp_server`) | none |
| may Write or Edit | `acquire/plan.yaml` only (`OWNS["acquire"]`) | its own `OWNS` |
| may Read | the run's `_task_input.json`, `acquire/` and the repository's `toolkit/` and `docs/` (a `_acquire_read_denied` like `_seed_read_denied`); never the home directory, other runs or `evals/` | as today |
| model, caps | `enforcement.MODEL_FAST`; `max_turns = 40`; `max_budget_usd = min(TASK_CAP_USD, left)` | as today |
| environment | `enforcement.filtered_env(..., allow_cloud=False)`: cloud credentials blanked even under `--cloud` | as today |

**Why the harness fetches.** If the task had a general web tool and wrote what it found into a file, a file the
model typed and a file a server returned would look the same afterwards. Here the task chooses what to fetch,
and harness code makes the request and writes the bytes. The task cannot place a byte in quarantine. The worst
that a misled task can do is choose the wrong files or waste its budget, and the owner sees the choice at SIGN.

**The fetch tool** (`agents/acquire.py`, class `Fetcher`):

- `list_sources()` returns the allowlist entries: id, what each holds, endpoints, licence summary.
- `search(source_id, endpoint, params)` calls the source's own search endpoint (for example Europe PMC search,
  ClinicalTrials.gov study search, an indicator catalogue). It returns at most 20 hits as structured fields, and
  stores the response bytes in quarantine like any fetch, so the query that found a file is on record.
- `fetch(source_id, endpoint, params, why, wanted)` builds the URL from the endpoint's template in the allowlist.
  The task supplies parameter values, never a URL. Each value is checked against the endpoint's parameter
  pattern, and the query string is at most 512 characters. The tool returns the fetch id, sha256, size, content
  type and a preview of at most 4 KB of text.
- HTTP rules: HTTPS only. The host must exactly match the endpoint's host, and so must each redirect (at most 3,
  each re-checked). Addresses that resolve to private, loopback or link-local ranges are refused. The client uses
  `trust_env=False`, so no proxy settings, `.netrc` or credentials apply. It sends no cookies and a generic
  User-Agent (`modeling-agentic-harness/<commit>`), with no personal or project details. Timeouts are 20 s to
  connect and 120 s in total. There is a per-source rate limit from the allowlist (for example, NCBI's limit for
  clients without a key). The body is streamed to `acquire/quarantine/<fetch_id>.part` and the transfer stops at
  the size cap (§6.2).
- A source marked `requires_key` is refused in v1 (owner question 5).

**Fetched content is untrusted.** Prompt injection is handled as follows:

1. Inside the acquire session, previews are returned as tool results wrapped as data (`<fetched source=... id=...>`
   ... `</fetched>`), with control characters removed. The system prompt says that fetched text is data and never
   instructions. That cannot be enforced, so the protection is structural: the session can reach only the
   allowlist and write only `plan.yaml`.
2. **Later tasks never get fetched text, or the acquire task's own prose, in a prompt.** The acquire task has
   read untrusted text, so its notes in `plan.yaml` (`why`, `notes`) count as untrusted too. The harness drops
   them. `draft_spec`, `draft_evidence`, the critics and the writer receive only the manifest that the harness
   builds (`evidence/acquired_manifest.json`): paths, source ids, endpoint ids, sizes, hashes, licence ids and the
   `wanted` labels, each checked against a fixed vocabulary. Titles that come from API metadata are kept in the
   manifest file as data, and the prompt points to the file without quoting it.
3. Later tasks read acquired files with `Read`, the same way they read staged inputs today. Values reach the
   analysis only through derivations, which run in the sandbox with no network and read-only inputs. A file
   cannot make anything run. Derivations parse it as data, and XML is parsed with `defusedxml`.

## 4. Harness steps after the task: check, hash, record, promote, freeze

`agents/acquire.py` `promote(run_dir) -> dict`, run once the task ends:

| Step | Check | Fail closed as |
|---|---|---|
| 1 | the fetch record exists, its source and endpoint are in the allowlist version recorded at the start of the run, status `ok` | `acquire_unrecorded` (file deleted) |
| 2 | the size is above 0 and below the per-file cap; the run total is below the per-run cap | `acquire_oversized` |
| 3 | the content type from the response header and from sniffing the bytes agree, and the type is one the endpoint allows: `text/csv`, `application/json`, `application/xml`, `text/plain`. No archives, PDFs, HTML pages or executables in v1 | `acquire_type_mismatch` |
| 4 | the file parses: CSV with `csv` (UTF-8, consistent column count), JSON with `json`, XML with `defusedxml`. Nothing is converted: the bytes are kept as fetched | `acquire_unparseable` |
| 5 | the licence: the source's licence is in the allowlist's accepted list and, when the response carries a per-record licence (an open-access article, for example), that licence is accepted too | `acquire_licence_unclear` (not promoted) |
| 6 | the sha256 of the quarantined bytes equals the hash the fetcher computed while streaming | `artifact_tampered` (exit 1) |
| 7 | copy to `data/inputs/acquired/<source_id>/<fetch_id>.<ext>` with `lib.safe_io.write_nofollow` (no links, atomic), re-hash the destination, `chmod 0444` | `artifact_tampered` |
| 8 | write the provenance entry, then delete the quarantined copy | |

**Provenance** (`data/inputs_provenance.json`, a harness file). Acquired files join the existing `inputs` map, so
every consumer that checks `data/inputs/` (the derivation runner, `record_cited_inputs`,
`scripts/harness_metrics.py`) sees them without a second code path:

```json
{"inputs": {
   "data/inputs/acquired/<source_id>/<fetch_id>.json": {
     "origin": "acquired",
     "sha256": "<hex>", "bytes": 0, "content_type": "application/json",
     "url": "https://<host>/<path>?<query>", "retrieved_at": "<UTC ISO-8601>",
     "http": {"status": 200, "etag": "...", "last_modified": "..."},
     "source_id": "<allowlist id>", "endpoint": "<endpoint id>", "allowlist_version": "<n>",
     "allowlist_sha256": "<hex>",
     "licence": {"id": "<licence id>", "terms_url": "...", "record_licence": "<per-record, when any>"},
     "query": {"tool": "fetch", "params": {}, "found_by": "<search fetch_id or null>"},
     "wanted": ["<kind>:<name>"], "fetch_key": "ACQUIRE#1:f3"}},
 "cited": {}}
```

`query.params` is the parameter set as the task passed it, recorded by the tool, not described by the model.
Entries for staged files keep `source` and gain `origin: "repository"`. Two current functions change:

- `stage_inputs` rebuilds `inputs` on every SPEC try. It must keep the entries whose `origin` is `acquired` (test
  AQ12).
- `record_cited_inputs` compares a staged file with its repository source. For an acquired file there is no
  source to compare: it checks that the bytes still match the recorded sha256 and that the mode is still
  read-only.

**Freeze.** Promoted files are mode `0444` in a `0555` directory. `data/inputs/` is already in `HARNESS_PREFIXES`,
so a task's Write or Edit there is refused. `acquire/quarantine/` and `acquire/record.json` join
`HARNESS_PREFIXES` and `HARNESS_FILES`. The sign-off binds the files through the evidence dossier's input closure
(`toolkit/spec/evidence_dossier.py`), which already includes every file the spec and its derivations read, and
through the manifest's sha256 on the sheet (§5).

## 5. How later states use acquired inputs, and what the owner sees

**No change downstream.** SPEC, the derivations, EVIDENCE, CONFIGURE and ANALYZE treat
`data/inputs/acquired/...` exactly like `data/inputs/<staged>`: the spec cites derived tables, derivations read
inputs, and the derivation record checks input hashes against provenance. The prompt for `draft_spec` already
tells the task to read staged inputs. It gains one sentence pointing to `evidence/acquired_manifest.json`.

**One added check, for literature values.** When a dossier observation's `where` is an acquired file (an
abstract or an open-access article), the entry must carry `quote`: the exact text span holding the value. The
harness checks that the span occurs in the file and that the entry's `value` appears in it
(`observation_quote_not_found`, HIGH, not declarable). This is the quoted-span rule that `v2d-compartmental-engines.md`
(benchmark FS7) already expects of the evidence service. It does not make a transcription right, but it makes every typed
value traceable to bytes the harness fetched (owner question 6).

**At SIGN.** The sign-off sheet (`toolkit/spec/signoff.py` `sheet`) gains a subsection inside the evidence
dossier section, "Acquired inputs (fetched by the harness)", built from `evidence/acquired_manifest.json`:

| Column | Content |
|---|---|
| file | `data/inputs/acquired/<source>/<id>.<ext>` |
| source | allowlist id and endpoint, with the full URL |
| retrieved | UTC time |
| sha256 | first 12 characters (the full hash is in the manifest) |
| size, type | bytes, content type |
| licence | licence id and terms URL; "redistribution not allowed" where that applies |
| wanted | the input kinds it was fetched for |
| used by | the derivations that read it and the spec paths built from it; "unused" otherwise |

A second table lists what was fetched and not promoted, with each reason. `sheet_sources` adds the manifest's
sha256, so a change to the acquired set after signing makes the signature stale. A spec that cites an acquired
file the manifest does not list is refused before SIGN (`acquired_input_unlisted`).

## 6. Failure modes, routing and cost

### 6.1 Failures

| Failure | Where it is caught | Routing |
|---|---|---|
| nothing relevant found | the task's plan keeps no file, or every kept file fails §4 | pause `awaiting_inputs`: `acquire/inputs_request.md` lists the wanted inputs and what was tried; the owner adds files to the question and relaunches, or resumes after placing them (owner question 3) |
| source down (5xx, timeout, DNS) | fetch tool: 2 retries with backoff, then an error result to the task | the task may try another allowed source; recorded as `source_unavailable` |
| host or redirect outside the allowlist, or a private address | fetch tool | refused before any byte is read; `fetch_refused` |
| licence unclear, or a per-record licence not accepted | promotion step 5 | not promoted; listed on the sheet; never a pause on its own |
| source needs a key, login or registration | the allowlist marks it `requires_key` | refused in v1 |
| file over the per-file cap | the streaming cap | download aborted, `.part` deleted, `acquire_oversized`; the task may ask for a narrower slice (one country, fewer years) |
| run total over the per-run cap | fetch tool | further fetches refused |
| wrong content type, HTML error page served with status 200, file does not parse | promotion steps 3 and 4 | not promoted |
| hash mismatch, or a promoted file changed | promotion step 6 or 7; `record_cited_inputs`; resume | `artifact_tampered`, exit 1 |
| budget below the floor | `_task` | `paused:awaiting_budget` |
| a gap found at SPEC or EVIDENCE | `missing_input` problems | one top-up (§2.3); after that, the run goes on as it does today (SPEC tries, dossier `no_data` with a borrowing rule) |
| a gap found after SIGN | critics | a finding for the next run; nothing is fetched |

### 6.2 Cost model

| Item | Cap (proposed) | Expected |
|---|---|---|
| acquire task | `TASK_CAP_USD` ($3) per task; at most 2 tasks per run (first acquisition plus one top-up), so `ACQUIRE_CAP_USD = 6` | most of a typical run's acquisition spend; to be measured in the δ smoke |
| retries of a crashed task | 1 per round, inside the cap | |
| fetches | 40 per round, 80 per run | |
| bytes | 25 MB per file, 200 MB per run | small JSON and CSV slices; national-scale population tables can approach the per-file cap |
| compute | none: the fetches run in the harness process on the agent VM, with no Coiled workers | |
| when the question supplies inputs | $0, no task | all eval questions |

The run's `--max-budget-usd` still caps the total. ACQUIRE's spend is a separate line in `budget_ledger.json`
(`stage: ACQUIRE`), so per-stage costs stay readable.

## 7. The source allowlist

`.claude/orchestration/acquire_sources.yaml`, versioned (`version: <n>`), reviewed like `expert_priors.yaml`, and
disease-agnostic: it names sources and endpoints, never a disease, a country or a unit. Each entry has: `id`,
`holds` (one line), `endpoints` (host, path template, parameter patterns, allowed content types), `rate_limit`,
`licence` (`id`, `terms_url`, `checked` date, `redistribution: allowed|not_allowed|per_record`),
`requires_key`, and `status: enabled|excluded` with a reason.

Proposed first version. The licence column states what we expect the terms to be. Each one is to be read and
recorded, with the date, when the file is written in step β (§9). Nothing is enabled until that is done.

| id | holds | expected licence or terms (to verify) | v1 status |
|---|---|---|---|
| `who_gho` | WHO Global Health Observatory OData API: indicators by country and year | WHO terms; CC BY-NC-SA 3.0 IGO for most content | enabled |
| `worldbank` | World Bank Indicators API: population, income, health spending | CC BY 4.0 for most indicators; per-dataset terms | enabled |
| `dhs_api` | DHS Program indicator API (aggregated indicators, not microdata) | DHS terms of use, with citation; microdata needs registration and is refused | enabled for the aggregated-indicator endpoints only, if they work without a key; otherwise excluded |
| `europepmc` | Europe PMC REST: search, abstracts, open-access full-text XML | metadata open; full text promoted only when the per-article licence is accepted | enabled |
| `pubmed` | NCBI E-utilities: search, abstracts | NCBI usage policy and rate limit; abstracts may be under publisher copyright: used inside the run only | enabled |
| `ctgov` | ClinicalTrials.gov API v2: registered trials and posted results | NLM terms | enabled |
| `un_wpp` | UN World Population Prospects: population by age and sex | CC BY 3.0 IGO; the bulk files are large archives, and the data-portal API may need a token | excluded in v1 unless an endpoint gives a small, non-archive slice without a key |
| `ihme_gbd` | IHME Global Burden of Disease results | the IHME user agreement (non-commercial use, with restrictions on redistribution); the results tool needs a login | excluded in v1 (needs a login, and the terms need the owner's decision) |
| price lists (for example pooled-procurement reference prices) | current procurement prices (principle 3) | terms to be checked per list | candidates for v2 of the file |

**Refused, whatever the list says:** any source that needs a login, registration, a key or a paid licence;
microdata; anything marked confidential or internal; and private connectors such as foundation document stores.
The Claude.ai connectors available in an interactive session (DHS, PubMed, ClinicalTrials.gov, IHME) are not used:
a v2 run on a VM does not have them, and their outputs cannot be hashed at the source.

**The public repository.** Acquired bytes stay in the run directory (`runs/` is gitignored) and in the run's
private cloud store. They are never committed, even under an open licence, because the project rule is no run
values in the repository. Self-tests use synthetic bytes served by a local test server (`AcquireFixtureServer`
in `agents/acquire.py`), never real responses. A file whose terms say `redistribution: not_allowed` is marked on
the sheet, and `tools/workbench/leak_check.py` treats it like any licensed run input.

## 8. Comparability with baselines

- ACQUIRE is off unless `--acquire` is passed, and it does nothing when the question names a file. The eval
  questions name theirs, so eval runs take the same path as before and cost the same.
- `cloud/configs/_eval.py` does not pass `--acquire`. `cloud/run_cluster.py` refuses an eval config whose flags
  contain it (test AQ20).
- `metadata.json` records `acquire` on every run, and `scripts/eval_suite.py grade` refuses a run recorded as
  `acquire: auto` (test AQ21). The grader's answer keys match on input hashes, so an acquired input would not
  match a key anyway.
- A run with acquired inputs is compared only with runs that acquired from the same allowlist version.
  `harness_metrics.py` groups runs by `allowlist_version`.

## 9. Tests first, smoke markers, rollout

### 9.1 Self-tests to write first (they fail until the code exists)

In `agents/acquire.py --self-test` (new module, run by `scripts/self_tests.py`):

| ID | Case |
|---|---|
| AQ1 | an allowlist entry missing `licence.terms_url`, `checked` or `status` fails to load (fail closed) |
| AQ2 | `fetch` builds the URL from the template only; a parameter that breaks its pattern, or a full URL passed as a parameter, is refused |
| AQ3 | a redirect to a host not in the allowlist is refused before the body is read |
| AQ4 | a host that resolves to a loopback or private address is refused |
| AQ5 | the client sends no `Authorization` header or cookies, and ignores `.netrc` and proxy settings (`trust_env=False`) |
| AQ6 | a body over the per-file cap is cut off, the `.part` file is deleted, and the record says `oversized` |
| AQ7 | an HTML error page served as `200 application/json` is not promoted (type sniff) |
| AQ8 | malformed CSV, JSON or XML is not promoted; an XML entity-expansion payload is rejected by `defusedxml` |
| AQ9 | a quarantined file changed after its fetch is refused at promotion with `artifact_tampered` |
| AQ10 | a promoted file is mode 0444, its provenance entry has every field in §4, and the quarantined copy is gone |
| AQ11 | a per-record licence outside the accepted list is not promoted, and it is listed in the not-promoted table |
| AQ12 | `stage_inputs` on a later SPEC try keeps the `origin: acquired` entries |
| AQ13 | `record_cited_inputs` passes an untouched acquired file and fails one whose bytes or mode changed |
| AQ14 | a resumed run does not repeat a completed fetch, and deletes a `.part` file |
| AQ15 | promotion run twice gives the same result (idempotent) |
| AQ16 | `requires_key` and `excluded` sources are refused |

In `agents/v2_sdk_tasks.py --self-test`:

| ID | Case |
|---|---|
| S_ACQ_TOOLS | the acquire task's hook allows the three `mcp__acquire__*` tools and the file tools; it refuses `WebFetch`, `WebSearch`, `Bash` and `Agent`; no other task kind is allowed the `mcp__acquire__*` tools |
| S_ACQ_OWNS | the acquire task can write `acquire/plan.yaml` and nothing else; a Write to `acquire/quarantine/` or `data/inputs/` is refused |
| S_ACQ_READ | the acquire task cannot Read `~/.aws/`, `~/.netrc`, another run or `evals/` |
| S_ACQ_NOPROSE | the `draft_spec` and `draft_evidence` prompts contain no text from `acquire/plan.yaml` or from a fetched file (a planted marker string in both is absent from every prompt) |

In `agents/stage_machine.py --self-test`:

| ID | Case |
|---|---|
| T_ACQ_OFF | with `acquire="off"` the first state is SPEC and `run_state.json:history` never names ACQUIRE |
| T_ACQ_SUPPLIED | with `acquire="auto"` and a question that names a file: ACQUIRE goes to SPEC, no task, $0, back-edges closed |
| T_ACQ_PROMOTE | a fake task and the fixture server: files promoted, SPEC receives the manifest path |
| T_ACQ_EMPTY | nothing promoted: pause `awaiting_inputs`, `inputs_request.md` written, exit 2 |
| T_ACQ_TOPUP | a `missing_input` problem at SPEC goes to ACQUIRE once; a second one does not |
| T_ACQ_NO_AFTER_SIGN | after a signature, a `missing_input` problem never goes to ACQUIRE |
| T_ACQ_OLD_STATE | a `run_state.json` without `acquire` loads as `off` and resumes unchanged |
| T_ACQ_SHEET | the sheet shows the acquired-inputs table; changing the manifest after signing makes the signature stale |
| T_ACQ_QUOTE | a dossier observation that cites an acquired file without a matching `quote` is a HIGH finding |

Launchers and grader: AQ20 (`run_cluster.py` refuses `--acquire` in an eval config), AQ21 (`eval_suite.py grade`
refuses an `acquire: auto` run). `scripts/gate_fuzz.py` gains the new codes.

### 9.2 Smoke markers (real run)

```bash
python3 scripts/smoke_check.py <collected run dir> --pipeline v2 --commit <launched sha> \
  --expect 'metadata.json:acquire=auto' \
  --expect 'run_state.json~"ACQUIRE"' \
  --expect 'acquire/record.json:status=promoted' \
  --expect 'data/inputs_provenance.json~"origin": "acquired"' \
  --expect 'evidence/acquired_manifest.json' \
  --expect 'decision_spec.signoff_sheet.md~Acquired inputs'
```

The eval-suite smoke expects `metadata.json:acquire=off` and no `acquire/` directory in the collected run.

### 9.3 Rollout

Each step is a PR under 800 lines, with a Codex review before merge.

| Step | Content | Exit criterion |
|---|---|---|
| 0 | this note; the owner's answers to §10 | owner decisions recorded in this file |
| α | the failing self-tests of §9.1 | they fail for the right reason |
| β | `agents/acquire.py` (allowlist loader, fetcher, promotion, provenance) and the allowlist with licence terms read and dated; no LLM, no state | AQ1-AQ16 pass against the fixture server |
| γ | the acquire task, `h_ACQUIRE`, edges, sheet section and quote check, behind `--acquire` (off by default) | every §9.1 test passes; the eval-suite self-tests are unchanged |
| δ | one real run of a question that is not in the eval suite and names no files, on a Coiled VM, `--signoff owner`, run cap agreed beforehand | §9.2 markers present; the owner reviews the acquired set at SIGN; acquire cost measured |
| ε | after δ: whether `--acquire` becomes the default for questions that name no files | owner decision |

## 10. Open questions for the owner

| # | Question | Recommended default |
|---|---|---|
| 1 | May the acquire task use a general web search to discover sources, with fetching still limited to the allowlist? | No. Discovery only through the allowed sources' own search endpoints. A general search returns arbitrary pages into the session, which widens prompt injection for little gain |
| 2 | When a question names some files but not all that are needed, may ACQUIRE top up? | No in v1: any supplied file turns ACQUIRE off for the run, which keeps the eval-suite path unchanged. Revisit after δ |
| 3 | Nothing acquired: pause or end? | Pause `awaiting_inputs` under both `owner` and `lead` sign-off. Nothing can be computed without inputs, and a pause can be resumed once files are added |
| 4 | PDFs, archives and HTML pages? | Not in v1: CSV, JSON, XML and plain text only. Papers come as abstracts or open-access XML. Adding a PDF path needs text extraction inside the sandbox, which can be a later step |
| 5 | Sources that need a free key or registration (a DHS API key, a UN data-portal token, IHME)? | Excluded in v1. If you want one later: a dedicated environment variable that only the fetcher reads, never in `filtered_env`'s task environment, and a licence decision per source |
| 6 | Must a dossier observation taken from an acquired paper quote its span? | Yes (`observation_quote_not_found`, HIGH, not declarable) |
| 7 | Should the owner approve the acquired set before SPEC (an extra pause)? | No. One review at SIGN, where the sheet lists every acquired file and what used it; the spec can still be refused there |
| 8 | Cache acquired files across runs (content-addressed, on the cloud store)? | Not in v1. Reuse is through the existing run-reuse path, which copies bytes and provenance |
| 9 | Which model runs the acquire task? | `MODEL_FAST`: the task chooses sources and slices; the judgement about evidence stays in SPEC and EVIDENCE |
| 10 | Caps: $3 per task, 2 tasks, 40 fetches per round, 25 MB per file, 200 MB per run? | As proposed; re-measure after δ |
| 11 | Which sources are enabled in allowlist version 1? | The six marked `enabled` in §7, each only after its terms are read and dated in step β; `un_wpp` and `ihme_gbd` excluded |

## 11. Not in this note

Per-run engine code (engines stay a fixed registry built and validated by the team); changes to derivations or
the sandbox; parsing PDFs; a cross-run evidence store; and the evidence service's parameter matching (V5), which
would read the acquired files once they exist.
