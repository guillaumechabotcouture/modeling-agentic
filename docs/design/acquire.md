# v2: an ACQUIRE state, so a run can start from a bare policy question (design note)

**Status:** written before any code (2026-11-23), at the owner's request. Nothing here is built. Revised the
same day after one Codex review round (§12). It extends `v2c-orchestration.md` (the states),
`v2-derived-inputs.md` (staged inputs, derivations, the sandbox) and the evidence stage of v2 autonomy 1B-ii
(`agents/stage_machine.py` `h_EVIDENCE`). File and function names are proposals. The stage machine is being split
into a package in parallel (branch `split-sm-pkg-v2`): if that lands first, the handler moves with the other
handlers, and the state names, `EDGES` and fields below stay the same.

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
the analysis path is the same; what changes is how later tasks may read these files (§3.3), because their content
comes from outside.

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
| inventory | every promoted file with its sha256: what the signature binds (§5) |
| prompt injection | text inside fetched content that tries to instruct the model reading it |

## 2. Placement in the state machine

### 2.1 States and edges

```
ACQUIRE -> SPEC -> EVIDENCE -> SIGN -> ENGINE_CHOICE -> ...
   ^        |         |
   +--------+---------+      top-up back-edges, before SIGN only, at most once
```

Changes to `agents/stage_machine.py`:

| Item | Change |
|---|---|
| `STATES` | `"ACQUIRE"` first |
| `EDGES` | new `"ACQUIRE": {"SPEC", "ACQUIRE", "FINAL"}`; `"SPEC"` gains `"ACQUIRE"`; `"EVIDENCE"` gains `"ACQUIRE"` |
| `LLM_STATES` | gains `"ACQUIRE"`, so `step()` checks the budget before the handler runs |
| `RunState` | new fields `acquire: str = "off"` (`off` or `auto`) and `acquired: dict` (`status`, `rounds`, `closed`, `spent_usd`, `fetches`, `bytes`, `inventory_sha256`) |
| `init_run` | new argument `acquire="off"`; when it is `auto`, the first state is `ACQUIRE`, otherwise `SPEC` as today |
| new constants | `MAX_ACQUIRE_ROUNDS = 2` (the first acquisition plus one top-up), `ACQUIRE_CAP_USD` (§6.2) |
| new pause | `AWAITING_INPUTS = "awaiting_inputs"`, printed as `paused:awaiting_inputs`, exit 2 |
| `main.py` | `--acquire` (off by default); recorded in `metadata.json` as `acquire: off|auto` on every run |
| `harness_owned()` | gains `acquire/*`, so no deliverable path or supplied dossier can overwrite the acquisition journal (this registry is separate from `HARNESS_FILES`) |

`RunState` defaults to `acquire="off"`. A state file saved before this change has no such field, so it loads as
`off` and resumes exactly as it does today. `metadata.json` readers treat a missing `acquire` as `off`, but the
grader does not rely on the field alone (§8).

### 2.2 Entry and exit

`h_ACQUIRE` (a thin handler that calls `agents/acquire.py`):

1. **The closed check.** If `acquired.closed` is true, ACQUIRE is refused (`FINAL`, `run_failed`, "acquisition
   is closed"); no edge should reach it then, so this is a guard against a bug.
2. **The off-switch.** Call `stage_inputs(run_dir, question)` first, as SPEC does. If it staged any file, the
   question supplies its inputs: record `acquired.status = "skipped_question_supplies_inputs"`, set `st.acquire =
   "off"` for the rest of the run, and go to SPEC. No task runs and no cost is incurred. The back-edges in §2.3
   are then closed too. Every eval question names its files under `data/evals/<q>/`, so on the eval suite
   ACQUIRE is never entered (the launcher does not pass `--acquire`), and even if it were, nothing would happen.
3. **Plan and fetch.** Run the bounded `acquire` task (§3) with a packet built by the harness: the question; the
   `wanted` vocabulary (§2.5); the allowlist; and, on a top-up, the `wanted` items that sent the run back. The task
   calls the fetch tool. Each call writes one quarantined file and one fetch record.
4. **Promote.** The harness checks, hashes and promotes each file whose fetch id the task listed in `keep` in its
   output (§3.1). Ids are the ones the fetch tool issued; a name or a path is never accepted. Anything not kept is
   deleted with its reason recorded.
5. **Exit.**

| Outcome | Next state | Recorded |
|---|---|---|
| at least one file promoted | SPEC | `acquired.status = "promoted"`, `acquire/record.json`, `evidence/acquired_manifest.json` |
| nothing promoted | pause `awaiting_inputs` (owner question 3) | `acquire/inputs_request.md`: the `wanted` items, what was tried and why each failed |
| a promoted file's bytes differ from the recorded hash, or the inventory has a missing or extra file | FINAL, `artifact_tampered` (exit 1) | the mismatch |
| below the budget floor, or the acquisition balance is spent | pause `awaiting_budget`, as for any task | |
| task failed or crashed, round tries left | ACQUIRE (retry once, inside the acquisition balance) | the failure note |

**Closing acquisition.** On the first exit from SIGN to ENGINE_CHOICE, in every sign-off mode including
`--signoff none`, the handler sets `acquired.closed = True` and saves the state before the transition. A run
restored from a copy whose state says it passed SIGN (`_signed_before`) is treated as closed even if the flag is
missing. Later back-edges to SPEC (an ANALYZE calibration refusal, a GATE rethink) therefore never fetch.

### 2.3 Top-up back-edges (before SIGN only)

SPEC or EVIDENCE may find that an input is missing: a calibration target with no observed source, or a dossier
entry with no observation. Each check that can name a missing input adds a problem in the form `missing_input:
<wanted kind>:<detail>` (§2.5). When `st.acquire == "auto"`, `acquired.closed` is false and `acquired.rounds <
MAX_ACQUIRE_ROUNDS`, the handler goes to ACQUIRE with those items as the packet's `wanted` list.

So that the new data can be used, a successful top-up grants one more SPEC try (`tries["spec_bonus"] = 1`,
counted once per run) and, when it came from EVIDENCE, one more evidence draft. Without that, a top-up after the
third SPEC try would land on `h_SPEC`'s `MAX_SPEC_TRIES` refusal. After SIGN nothing more is fetched: a gap found
later becomes a finding for the next run ("freeze and batch"), because the signature binds the inventory.

### 2.4 Resume

- **Fetch identity.** A fetch is identified by its request digest: sha256 of the canonical
  `(source_id, endpoint, sorted params, allowlist_sha256)`. `acquire/record.json` holds one entry per digest,
  written through `lib.safe_io` before the request (`status: started`) and after it (`ok`, with sha256, or the
  error). A replacement task that asks for the same request gets the stored result and nothing is fetched again;
  a different request gets a new entry. An ordinal never stands for a request. Fetch and byte quotas are counted
  from the record, so they survive a crash.
- A partial download (`*.part`) is deleted on resume and never promoted. A quarantined file whose hash differs from
  its record is deleted and its entry marked `discarded_on_resume`.
- **The task** uses the existing journal (`st.journal`). A completed task is not re-run; an interrupted one is
  charged its allowance (by `_settle_interrupted`, as now) and retried once, with the charge counted against the
  acquisition balance (§6.2).
- **Promotion** writes an intent entry (`promoting`: fetch digest, destination, expected sha256) before the copy.
  On resume, for each intent: if the destination exists with the expected hash, the provenance entry is written
  and the intent closed; if it exists with another hash, or exists without an intent, it is tampering (exit 1); if
  it does not exist, the copy is redone from quarantine. AQ15b stops the process between the copy and the
  provenance write.
- **Before any task reads**, every resume and every task start in SPEC and EVIDENCE run `verify_inventory(run_dir)`
  (§4) first. A mismatch is terminal (`artifact_tampered`), never feedback for a paid redraft.
- **Restores from a checkpoint** may reset file modes (`scripts/results_store.py` builds `TarInfo` entries without
  the mode). The hash, not the mode, is the tamper signal: on resume, a file whose bytes match its provenance entry
  is set back to `0444`.
- **Reuse.** A run reused for a spec-only amendment (`toolkit/spec/amend.py`) copies the acquired bytes, the
  provenance, `acquire/record.json` and the manifest, and carries `acquire`, `acquired` and the inventory digest
  into the child's `metadata.json` and state. It never fetches again.

### 2.5 What the task is asked to find: the `wanted` vocabulary

The engines' decision-input kinds (`toolkit/engines/decision_inputs.py` `KINDS`: `unit_attribute`, `parameter`,
`card_table`) are resolved only against a spec and a configured card, so they do not exist before SPEC. ACQUIRE
uses its own short, disease-agnostic vocabulary, kept in the allowlist file (`wanted_kinds`):

| Kind | Meaning | Typical endpoints |
|---|---|---|
| `population` | population by area, age and sex | World Bank; UN WPP when enabled |
| `burden` | incidence, prevalence or mortality estimates by area and year | WHO GHO; DHS indicators |
| `coverage` | coverage of an intervention by area and year | WHO GHO; DHS indicators |
| `trial_effect` | randomised-trial results for an intervention | ClinicalTrials.gov; Europe PMC; PubMed |
| `price` | unit procurement or delivery prices | none in v1 (§7) |
| `context` | income, health spending and similar covariates | World Bank |

Each allowlist endpoint lists the kinds it serves. A top-up maps a SPEC or EVIDENCE problem to a kind: a
calibration target without an observed source to `burden` or `coverage`; a dossier `parameter` entry without an
observation to `trial_effect`; a unit attribute to the kind its engine card names in a new optional field
`data_kind`, and to nothing (no top-up) when the card does not name one.

## 3. The acquire task

### 3.1 Its powers

| Power | Acquire task | Every other v2 task |
|---|---|---|
| file tools | `Read`, `Glob`, `Grep` (no `Write` or `Edit`) | as today |
| Bash, Agent, NotebookEdit | refused at PreToolUse (S16) | refused |
| built-in `WebFetch`, `WebSearch` | **refused**: not in its tool list, denied by the hook | refused |
| network | only through `mcp__acquire__list_sources`, `mcp__acquire__search`, `mcp__acquire__fetch`, served in-process by the harness (`claude_agent_sdk.create_sdk_mcp_server`) | none; the hook refuses `mcp__acquire__*` for every other task kind |
| writes | its result only, `_task_output.json`, which `_session` already requires; `OWNS["acquire"] = ()` | its own `OWNS` |
| result schema | `{"keep": [<fetch ids>], "wanted_map": {<fetch id>: [<wanted kinds>]}}`, validated by the harness; any other key, including notes, is dropped before the journal records the output | as today |
| may Read, Glob, Grep | `_task_input.json` only, plus the repository's `toolkit/engines/*/card.yaml` (an `_acquire_read_denied` like `_seed_read_denied`, confined by real path, refusing links and recursive searches outside those). Not `acquire/quarantine/`: fetched bytes reach the task only as the capped previews the tool returns | §3.3 |
| model, caps | `enforcement.MODEL_FAST`; `max_turns = 40`; allowance from the acquisition balance (§6.2) | as today |
| environment | a stricter `enforcement.acquire_env()`: `filtered_env(..., allow_cloud=False)` plus every variable whose name matches a credential pattern (`*_TOKEN`, `*_KEY`, `*SECRET*`, `*PASSWORD*`, `COILED_*`, `GH_*`, `GITHUB_*`) blanked, except what the SDK itself needs to authenticate | as today |

The SDK merges `options.env` into the inherited environment, so blanking is the only way to remove a variable.
The task has no shell, so its environment matters less than the fetcher's; the fetcher (§3.2) reads nothing from
the environment.

**Why the harness fetches.** If the task had a general web tool and wrote what it found into a file, a file the
model typed and a file a server returned would look the same afterwards. Here the task chooses what to fetch, and
harness code makes the request and writes the bytes. The task cannot write any file except its result. The worst
that a misled task can do is choose the wrong files, send poor queries or waste its budget, and the owner sees
every query and file at SIGN.

### 3.2 The fetch tool

`agents/acquire.py`, class `Fetcher`, running in the harness process:

- `list_sources()` returns the allowlist entries: id, what each holds, endpoints, `wanted_kinds`, licence summary.
- `search(source_id, endpoint, params)` calls a source's own search endpoint (for example Europe PMC search,
  ClinicalTrials.gov study search, an indicator catalogue). It returns at most 20 hits as structured fields, and
  stores the response like any fetch, so the query that found a file is on record.
- `fetch(source_id, endpoint, params, wanted)` builds the URL from the endpoint's template. The task supplies
  parameter values, never a URL.
- **Parameter values.** Each parameter in the allowlist has a type. Coded parameters (country codes, indicator
  codes, years, record ids) must match a pattern and, for codes, a value returned by an earlier catalogue fetch
  in the same run. Free text is allowed only in the `terms` parameter of literature search endpoints: at most 8
  words, letters, digits and spaces only, no more than 120 characters. The whole query string is at most 512
  characters. This limits what an allowed request can carry out of the run. It does not hide the question's
  subject (a search for an intervention and a country says what the run is about), so `--acquire` is a declaration
  that the question may be disclosed to public services in that form (owner question 12). Every outgoing query is
  listed at SIGN.
- **HTTP rules.** HTTPS only, standard port only. For each hop (the first request and each of at most 3
  redirects), the harness checks the full destination (scheme, host, port, path) against the endpoint's template,
  resolves the host once, refuses private, loopback, link-local, multicast and reserved addresses, and connects to
  the address it checked, keeping TLS hostname and certificate verification for the original host name. A
  redirect to another endpoint, port or host is refused. The client reads nothing from the environment
  (`trust_env=False`: no proxy settings, no `.netrc`), sends no `Authorization` header and no cookies, and uses a
  generic User-Agent (`modeling-agentic-harness/<commit>`) with no personal or project details. Timeouts are 20 s
  to connect and 120 s in total. Per-source rate limits come from the allowlist (for example, NCBI's limit for
  clients without a key).
- **Writing the bytes.** The body is streamed to `acquire/quarantine/<digest>.part` through directory-relative,
  no-follow operations (a directory descriptor for `acquire/quarantine/`, opened with `O_NOFOLLOW`, and
  `O_NOFOLLOW | O_EXCL` for the file), hashing as it goes. The transfer stops at the size cap (§6.2). Cleanup,
  hashing, renames and mode changes on resume use the same descriptor-relative calls, so a restored run in which
  `acquire/quarantine` is a link cannot write or delete outside the run. `lib.safe_io` gains these helpers.
- A source marked `requires_key` is refused in v1 (owner question 5).

### 3.3 Fetched content is untrusted

Fetched bytes can contain text written to steer a model. The design does not rely on the model ignoring it. It
limits who reads which bytes, and what a reader can do.

| Reader | What it may read of acquired content | What it can do afterwards |
|---|---|---|
| acquire task | previews returned by the tool: at most 4 KB per response, control characters removed, wrapped as `<fetched source=... id=...>` ... `</fetched>` | choose fetch ids; nothing else |
| `draft_spec` | **not the raw files.** For each tabular file, the harness writes a view, `data/inputs/acquired/_views/<id>.schema.json`: column names reduced to `[A-Za-z0-9_ .-]`, at most 64 characters each (others replaced by `col_<n>`), types, row count, and for each column its numeric range or its number of distinct values. No free-text cell is shown | write derivations, which run in the sandbox |
| derivations (harness-run scripts) | the raw files, as declared inputs | compute tables; no network, restricted file view (below) |
| `draft_evidence` | text files (abstracts, open-access article XML), because it must read a trial's result. This is the one deliberate exposure | write `evidence_dossier.yaml`, which the harness checks (quotes, §5) and the owner reviews at SIGN |
| critics, fix tasks, writer | the views and the dossier; not the raw files | their own outputs, as today |

The read hooks (`_search_denied` for all tasks, plus the per-task rules) refuse `acquire/` for every task except
the harness, and refuse raw files under `data/inputs/acquired/` for every task except `draft_evidence` (text
files only). Glob and Grep are refused when their root contains those paths.

**Feedback.** No harness message that goes back into a prompt quotes acquired bytes. A derivation that fails on
an acquired input returns its exception class, the script line number and the input path, never stderr text from
the data (`derivation_failed_on_acquired`). The acquire task's own result is reduced to fetch ids and kinds
(§3.1), so nothing it read can reach a later prompt through it. The manifest (`evidence/acquired_manifest.json`)
holds titles from API metadata as data; prompts point to it without quoting it, and its text fields pass the same
character filter as column names.

**The residual risk, stated.** `draft_evidence` reads untrusted text. An injected instruction could make it write
a misleading dossier entry. It cannot write anything else, run anything or reach the network. Each value it takes
from an acquired file must quote its span, which the harness checks (§5), and the owner reads the entries at SIGN.

**The derivation sandbox for acquired runs.** Derivations read acquired bytes, and the script that reads them was
written by a task that saw their schema. Two gaps in the current sandbox matter more here:
- `scripts/pipeline.py` `_bwrap_cmd` binds the whole root (`--ro-bind / /`), so a script can read files outside
  its declared inputs. For a run with acquired inputs, the bind list is narrowed to the interpreter, its
  libraries, the declared inputs and the output directory (DS1).
- `--derive-unsandboxed` (the macOS development flag) is refused when the run has acquired inputs: such a run
  needs a real sandbox (DS2).

XML is not parsed by derivations: `IMPORT_ALLOW` is the standard library, numpy, pandas and pyyaml, and v1 does
not add to it. Data endpoints are chosen to return CSV or JSON. XML is accepted only for literature text, which the
harness parses itself (with `defusedxml`, to check well-formedness and to extract plain text for the quote check);
no task script parses it.

## 4. Harness steps after the task: check, hash, record, promote, freeze

`agents/acquire.py` `promote(run_dir) -> dict`, run once the task ends:

| Step | Check | Fail closed as |
|---|---|---|
| 1 | the fetch id is one the tool issued in this run, its record says `ok`, and its source and endpoint are in the allowlist version recorded at the start of the run | `acquire_unrecorded` (file deleted) |
| 2 | the size is above 0 and below the per-file cap; the run total is below the per-run cap | `acquire_oversized` |
| 3 | the content type from the response header and from sniffing the bytes agree, and the type is one the endpoint allows: `text/csv` and `application/json` for data, `application/xml` and `text/plain` for literature text. No archives, PDFs, HTML pages or executables in v1 | `acquire_type_mismatch` |
| 4 | the file parses: CSV with `csv` (UTF-8, consistent column count), JSON with `json`, XML with `defusedxml`. Nothing is converted: the bytes are kept as fetched | `acquire_unparseable` |
| 5 | the licence: the source's licence is in the allowlist's accepted list. For a source marked `redistribution: per_record`, **every record** in the response must carry a licence on the accepted list; a missing record licence fails the file, as does a multi-record response with any record that lacks one | `acquire_licence_unclear` (not promoted) |
| 6 | the sha256 of the quarantined bytes equals the hash the fetcher computed while streaming | `artifact_tampered` (exit 1) |
| 7 | write the promotion intent; copy to `data/inputs/acquired/<source_id>/<digest>.<ext>` with `lib.safe_io.write_nofollow` (no links, atomic); re-hash the destination; `chmod 0444` | `artifact_tampered` |
| 8 | write the provenance entry and the view (§3.3), close the intent, delete the quarantined copy | |
| 9 | recompute the inventory and its digest (§5), write `evidence/acquired_manifest.json` | |

**Provenance** (`data/inputs_provenance.json`, a harness file). Acquired files join the existing `inputs` map, so
the consumers that check `data/inputs/` (the derivation runner, `record_cited_inputs`,
`scripts/harness_metrics.py`) see them:

```json
{"inputs": {
   "data/inputs/acquired/<source_id>/<digest>.json": {
     "origin": "acquired",
     "sha256": "<hex>", "bytes": 0, "content_type": "application/json",
     "url": "https://<host>/<path>?<query>", "retrieved_at": "<UTC ISO-8601>",
     "http": {"status": 200, "etag": "...", "last_modified": "..."},
     "source_id": "<allowlist id>", "endpoint": "<endpoint id>", "allowlist_version": "<n>",
     "allowlist_sha256": "<hex>",
     "licence": {"id": "<licence id>", "terms_url": "...", "record_licences": ["<per record, when any>"]},
     "query": {"tool": "fetch", "params": {}, "found_by": "<search digest or null>"},
     "wanted": ["<wanted kind>"], "request_digest": "<hex>"}},
 "acquired_inventory_sha256": "<hex>",
 "cited": {}}
```

`query.params` is the parameter set as the task passed it, recorded by the tool, not described by the model.
Entries for staged files are unchanged: they carry no `origin` field, and readers treat a missing `origin` as
`repository`. That keeps the provenance file of a run with acquisition off byte-identical to today's (§8). Two
current functions change:

- `stage_inputs` rebuilds `inputs` on every SPEC try. It must keep the entries whose `origin` is `acquired`, and
  `acquired_inventory_sha256` (test AQ12).
- `record_cited_inputs` compares a staged file with its repository source. For an acquired file there is no
  source to compare: it calls `verify_inventory`.

**`verify_inventory(run_dir)`** lists every file under `data/inputs/acquired/` (excluding `_views/`, which it
rebuilds and compares), checks each against its provenance entry by sha256, and fails on a missing file, an extra
file, a changed file or a link. It is called at the start of every SPEC and EVIDENCE task, at SIGN (by every
signer), on resume, and by the preconditions at ENGINE_CHOICE.

**Freeze.** Promoted files are mode `0444`. Directories stay writable by the harness, so a top-up can add files
next to earlier ones (an atomic write needs a temporary sibling); the protection is the hash check, not the mode.
`data/inputs/` is already in `HARNESS_PREFIXES`, so a task's Write or Edit there is refused. `acquire/` joins
`HARNESS_PREFIXES` and `harness_owned()`.

## 5. How later states use acquired inputs, and what the owner sees

**The analysis path is unchanged.** SPEC, the derivations, EVIDENCE, CONFIGURE and ANALYZE treat
`data/inputs/acquired/...` like `data/inputs/<staged>`: the spec cites derived tables, derivations read inputs,
and the derivation record checks input hashes against provenance. What differs is the read rules of §3.3. The
`draft_spec` prompt gains one sentence pointing to the manifest and the views, **only when the manifest exists**,
so the prompt of a run with acquisition off is unchanged.

**Values taken from literature.** A dossier observation whose evidence is an acquired file carries
`acquired: {id: <request digest>, sha256: <file sha256>, locator: "<section, table or paragraph>", quote: "<exact
span>"}`. The harness checks that the id is in the manifest, that the sha256 matches, that the quote (at most 300
characters) occurs in the file's harness-extracted text, and that the observation's `value` appears in the quote
(`observation_quote_not_found`, HIGH, not declarable). The rule also applies when `where` is a URL on an allowlisted
host or a phrase like "Table 2" while the manifest holds a file from that source: such an observation must use the
`acquired` block (`observation_acquired_unbound`). This makes every observed value traceable to bytes the harness
fetched. It does not bind the value the run uses: `chosen` remains the signer's choice, and the sheet already shows
the chosen value next to the observations and next to what execution will use, so a quote of one number and a
chosen value of another is visible at SIGN (owner question 6). This matches the quoted-span rule that
`v2d-compartmental-engines.md` (benchmark FS7) already expects of the evidence service.

**What the signature binds.** The signed record (`decision_spec.signoff.json`) gains `acquired_inventory_sha256`:
the sha256 of the sorted list of (path, sha256) of every promoted file, and of the manifest's bytes. Every signer
(`toolkit/spec/signoff.py` `sign`), `status`, the registered preconditions (`toolkit/spec/preconditions.py`) and the
resume check recompute it with `verify_inventory` and compare. A missing, extra or changed file, or a changed
manifest field (a URL, a licence), makes the signature stale, also for a run whose spec needs no dossier. The
dossier's input closure (`evidence_dossier.input_closure`) hashes derivation outputs, not the raw files that fed
them, so the inventory digest is what covers raw acquired bytes, including files no derivation used.

**At SIGN.** The sign-off sheet (`toolkit/spec/signoff.py` `sheet`) gains a subsection inside the evidence
dossier section, "Acquired inputs (fetched by the harness)", rendered from the same manifest bytes the digest
covers:

| Column | Content |
|---|---|
| file | `data/inputs/acquired/<source>/<digest>.<ext>` |
| source | allowlist id and endpoint, with the full URL |
| retrieved | UTC time |
| sha256 | first 12 characters (the full hash is in the manifest) |
| size, type | bytes, content type |
| licence | licence id and terms URL; "redistribution not allowed" where that applies |
| wanted | the kinds it was fetched for |
| used by | the derivations that read it, the spec paths built from it and the dossier entries that quote it; "unused" otherwise |

Two more tables follow: every outgoing query (source, endpoint, parameters), and what was fetched and not
promoted, with each reason. A spec that cites an acquired file the manifest does not list is refused before SIGN
(`acquired_input_unlisted`).

## 6. Failure modes, routing and cost

### 6.1 Failures

| Failure | Where it is caught | Routing |
|---|---|---|
| nothing relevant found | the task keeps no file, or every kept file fails §4 | pause `awaiting_inputs`. Recovery in v1 is a relaunch: a new run whose question names the files (ACQUIRE is then off). A resumable "supply inputs" command is not in v1 (owner question 3) |
| source down (5xx, timeout, DNS) | fetch tool: 2 retries with backoff, then an error result to the task | the task may try another allowed source; recorded as `source_unavailable` |
| destination outside the template, a private address, a redirect off the endpoint | fetch tool, at each hop | refused before any byte is read; `fetch_refused` |
| a parameter outside its type, or free text where none is allowed | fetch tool | refused; `fetch_param_refused` |
| licence unclear, or a record licence missing or not accepted | promotion step 5 | not promoted; listed on the sheet; never a pause on its own |
| source needs a key, login or registration | the allowlist marks it `requires_key` | refused in v1 |
| file over the per-file cap | the streaming cap | download aborted, `.part` deleted, `acquire_oversized`; the task may ask for a narrower slice (one country, fewer years) |
| run total over the per-run cap | fetch tool, from the record | further fetches refused |
| wrong content type, HTML error page served with status 200, file does not parse | promotion steps 3 and 4 | not promoted |
| hash mismatch, missing or extra file | promotion, `verify_inventory` (task start, SIGN, resume, preconditions) | `artifact_tampered`, exit 1; never a redraft |
| derivation fails on an acquired input | the derivation runner | `derivation_failed_on_acquired` with no data text (§3.3) |
| budget below the floor, or acquisition balance spent | `_task` | `paused:awaiting_budget` |
| a gap found at SPEC or EVIDENCE | `missing_input` problems | one top-up, with one more SPEC try (§2.3); after that, the run goes on as today (SPEC tries, dossier `no_data` with a borrowing rule) |
| a gap found after SIGN | critics | a finding for the next run; acquisition is closed |

### 6.2 Cost model

| Item | Cap (proposed) | Expected |
|---|---|---|
| acquisition balance | `ACQUIRE_CAP_USD = 6` per run, held in `acquired.spent_usd`. Every acquire call's allowance is `min(TASK_CAP_USD, ACQUIRE_CAP_USD - spent)`, and its crash read-back allowance comes out of the same balance. An interrupted call's charge (`_settle_interrupted`) is added before any retry | most runs: one task; to be measured in the δ smoke |
| tasks | at most 2 rounds (first acquisition plus one top-up), each with at most one retry, all inside the balance | |
| fetches | 40 per round, 80 per run, counted from `acquire/record.json` | |
| bytes | 25 MB per file, 200 MB per run | small JSON and CSV slices; national-scale population tables can approach the per-file cap |
| compute | none: the fetches run in the harness process on the agent VM, with no Coiled workers | |
| when the question supplies inputs | $0, no task | all eval questions |

The run's `--max-budget-usd` still caps the total. ACQUIRE's spend is a separate line in `budget_ledger.json`
(`stage: ACQUIRE`), so per-stage costs stay readable.

## 7. The source allowlist

`.claude/orchestration/acquire_sources.yaml`, versioned (`version: <n>`), reviewed like `expert_priors.yaml`, and
disease-agnostic: it names sources, endpoints and `wanted_kinds`, never a disease, a country or a unit. Each entry
has: `id`, `holds` (one line), `endpoints` (host, path template, typed parameters, allowed content types, kinds
served), `rate_limit`, `licence` (`id`, `terms_url`, `checked` date, `redistribution:
allowed|not_allowed|per_record`, accepted record licences), `requires_key`, and `status: enabled|excluded` with a
reason.

Proposed first version. The licence column states what we expect the terms to be. Each one is to be read and
recorded, with the date, when the file is written in step β (§9). Nothing is enabled until that is done.

| id | holds | expected licence or terms (to verify) | v1 status |
|---|---|---|---|
| `who_gho` | WHO Global Health Observatory OData API: indicators by country and year | WHO terms; CC BY-NC-SA 3.0 IGO for most content | enabled |
| `worldbank` | World Bank Indicators API: population, income, health spending | CC BY 4.0 for most indicators; per-dataset terms | enabled |
| `dhs_api` | DHS Program indicator API (aggregated indicators, not microdata) | DHS terms of use, with citation; microdata needs registration and is refused | enabled for the aggregated-indicator endpoints only, if they work without a key; otherwise excluded |
| `europepmc` | Europe PMC REST: search, abstracts, open-access full-text XML | metadata open; full text promoted only when every record's licence is on the accepted list | enabled |
| `pubmed` | NCBI E-utilities: search, abstracts | NCBI usage policy and rate limit; abstracts may be under publisher copyright: used inside the run only | enabled |
| `ctgov` | ClinicalTrials.gov API v2: registered trials and posted results | NLM terms | enabled |
| `un_wpp` | UN World Population Prospects: population by age and sex | CC BY 3.0 IGO; the bulk files are large archives, and the data-portal API may need a token | excluded in v1 unless an endpoint gives a small, non-archive slice without a key |
| `ihme_gbd` | IHME Global Burden of Disease results | the IHME user agreement (non-commercial use, with restrictions on redistribution); the results tool needs a login | excluded in v1 (needs a login, and the terms need the owner's decision) |
| price lists (for example pooled-procurement reference prices) | current procurement prices (principle 3) | terms to be checked per list | candidates for version 2 of the file |

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

**Eval runs are unchanged, and that is tested, not assumed.**
- ACQUIRE is off unless `--acquire` is passed, and it does nothing when the question names a file. The eval
  questions name theirs.
- With acquisition off, nothing a task sees changes: no new prompt sentence (§5), no new provenance field (§4), the
  same tools and the same state trace. Test T_ACQ_OFF_GOLDEN runs an eval question's SPEC and EVIDENCE with fake
  tasks and compares the task packets, the tool lists, `data/inputs_provenance.json` and the `run_state.json`
  history with a fixture captured before the change.
- `cloud/configs/_eval.py` does not pass `--acquire`. `cloud/run_cluster.py` refuses an eval config whose flags
  contain it (AQ20).
- `scripts/eval_suite.py grade` refuses, in every grading mode, a run that holds any acquired content, whatever
  its metadata says: a `data/inputs/acquired/` tree, a provenance entry with `origin: acquired`, an
  `acquired_inventory_sha256`, an `acquire/` directory, or `acquire: auto` in metadata (AQ21). The answer-key match
  (`select_truth`, `input_hash_problem`) compares only `data/evals/<q>/` entries, so an extra acquired file would
  not change the match; that is why the grader checks for acquired content directly.

**Comparing runs that acquired data.** Two fingerprints, recorded in `metadata.json`:

| Fingerprint | What it is | Compare runs with equal values when |
|---|---|---|
| `acquired_inventory_sha256` | the sorted (path, sha256) of every promoted file | holding the data fixed: same bytes, different harness or prompts |
| `allowlist_version` + `allowlist_sha256` | the policy the run fetched under | comparing acquisition behaviour: which sources and slices the task chose |

The same allowlist version does not give the same inputs (sources revise data; tasks choose different years).
`harness_metrics.py` groups by the inventory fingerprint for results and by the allowlist for acquisition
behaviour.

## 9. Tests first, smoke markers, rollout

### 9.1 Self-tests to write first (they fail until the code exists)

In `agents/acquire.py --self-test` (new module, run by `scripts/self_tests.py`):

| ID | Case |
|---|---|
| AQ1 | an allowlist entry missing `licence.terms_url`, `checked`, `status` or typed parameters fails to load (fail closed) |
| AQ2 | `fetch` builds the URL from the template only; a value that breaks its type, a full URL as a value, or free text outside a literature `terms` parameter is refused |
| AQ2b | a coded value not returned by an earlier catalogue fetch in the run is refused |
| AQ3 | a redirect to another host, port or endpoint is refused before the body is read |
| AQ4 | a resolver that returns a public address when checked and a private one afterwards: the connection goes to the checked address, or is refused; TLS verification still uses the host name |
| AQ5 | the client sends no `Authorization` header or cookies and ignores `.netrc` and proxy settings (`trust_env=False`) |
| AQ6 | a body over the per-file cap is cut off, the `.part` file is deleted, and the record says `oversized` |
| AQ7 | an HTML error page served as `200 application/json` is not promoted (type sniff) |
| AQ8 | malformed CSV, JSON or XML is not promoted; an XML entity-expansion payload is rejected by `defusedxml` |
| AQ9 | a quarantined file changed after its fetch is refused at promotion with `artifact_tampered` |
| AQ10 | a promoted file is mode 0444, its provenance entry has every field in §4, its view exists, and the quarantined copy is gone |
| AQ11 | per-record licences: one record with an unaccepted licence, one with none, and a multi-record response with one unlicensed record are all not promoted |
| AQ12 | `stage_inputs` on a later SPEC try keeps the `origin: acquired` entries and the inventory digest |
| AQ13 | `verify_inventory` passes an untouched inventory and fails a changed, missing or extra file and a link |
| AQ14 | a replacement task that repeats a completed request gets the stored result with no new fetch; a different request is a new entry; quotas survive a crash; a `.part` file is deleted on resume |
| AQ15 | promotion run twice gives the same result (idempotent) |
| AQ15b | a process stopped between the copy and the provenance write resumes to a complete, consistent state |
| AQ16 | `requires_key` and `excluded` sources are refused |
| AQ17 | `acquire/quarantine` replaced by a link to a scratch directory: no write or delete happens outside the run |
| AQ18 | a checkpoint restore that resets modes to 0644 passes `verify_inventory` and is set back to 0444 |
| DS1 | a derivation in a run with acquired inputs cannot read a file outside its declared inputs |
| DS2 | `--derive-unsandboxed` is refused for a run with acquired inputs |

In `agents/v2_sdk_tasks.py --self-test`:

| ID | Case |
|---|---|
| S_ACQ_TOOLS | the acquire task's hook allows the three `mcp__acquire__*` tools and `Read`, `Glob`, `Grep`; it refuses `Write`, `Edit`, `WebFetch`, `WebSearch`, `Bash` and `Agent`; no other task kind is allowed `mcp__acquire__*` |
| S_ACQ_RESULT | through `_session` with a stub: a valid `_task_output.json` result completes; any key other than `keep` and `wanted_map` is dropped before the journal records it; a fetch id the tool did not issue is refused |
| S_ACQ_READ | the acquire task cannot Read, Glob or Grep `~/.aws/`, `~/.netrc`, another run, `evals/` or `acquire/quarantine/`, also through a link or a recursive search |
| S_ACQ_ENV | with synthetic `COILED_TOKEN`, `GH_TOKEN` and `X_API_KEY` set, the acquire session's environment holds them blank |
| S_ACQ_NOPROSE | a planted marker in a CSV cell, an API title and a dropped result note: run `draft_spec`, a critic and the writer with recording fakes; the marker is absent from every prompt, every tool result they receive (Read of views, the manifest) and every feedback string, including a failing derivation's |
| S_ACQ_RAWREAD | `draft_spec`, critics and the writer are refused a Read of a raw acquired file; `draft_evidence` may read acquired text files and not tabular ones |

In `agents/stage_machine.py --self-test`:

| ID | Case |
|---|---|
| T_ACQ_OFF | with `acquire="off"` the first state is SPEC and `run_state.json:history` never names ACQUIRE |
| T_ACQ_OFF_GOLDEN | the eval-baseline comparison of §8 |
| T_ACQ_SUPPLIED | with `acquire="auto"` and a question that names a file: ACQUIRE goes to SPEC, no task, $0, back-edges closed |
| T_ACQ_PROMOTE | a fake task and the fixture server: files promoted, SPEC receives the manifest and views |
| T_ACQ_EMPTY | nothing promoted: pause `awaiting_inputs`, `inputs_request.md` written, exit 2 |
| T_ACQ_TOPUP | a `missing_input` problem at SPEC after the third try goes to ACQUIRE once, and SPEC then gets one more try; a second gap does not go back |
| T_ACQ_CLOSED | with `--signoff none`, after SIGN, an ANALYZE calibration refusal back to SPEC never reaches ACQUIRE; a restored state past SIGN without the flag is treated as closed |
| T_ACQ_TAMPER_PRESPEC | a promoted file changed between ACQUIRE and SPEC ends the run `artifact_tampered` before `draft_spec` is called |
| T_ACQ_BALANCE | an interrupted call, its retry and a top-up cannot together exceed `ACQUIRE_CAP_USD`, read-backs included |
| T_ACQ_OLD_STATE | a `run_state.json` without `acquire` loads as `off` and resumes unchanged |
| T_ACQ_SIGN | the sheet shows the acquired-inputs tables; changing a raw file, adding a file or changing a manifest URL after signing makes the signature stale through `sign`, `status`, the preconditions and resume, also with no dossier |
| T_ACQ_QUOTE | an observation that cites an acquired file without a matching quote, or that names an allowlisted URL without the `acquired` block, is a HIGH finding |
| T_ACQ_DELIVERABLE | a deliverable path under `acquire/` is refused |
| T_ACQ_AMEND | an amendment child carries the acquired bytes, provenance, record, manifest and metadata fields |

Launchers and grader: AQ20 (`run_cluster.py` refuses `--acquire` in an eval config), AQ21 (`eval_suite.py grade`
refuses any run with acquired content, in every mode, with or without the metadata field). `scripts/gate_fuzz.py`
gains the new codes.

### 9.2 Smoke markers (real run)

```bash
python3 scripts/smoke_check.py <collected run dir> --pipeline v2 --commit <launched sha> \
  --expect 'metadata.json:acquire=auto' \
  --expect 'run_state.json~"ACQUIRE"' \
  --expect 'acquire/record.json' \
  --expect 'data/inputs_provenance.json~"origin": "acquired"' \
  --expect 'data/inputs_provenance.json:acquired_inventory_sha256' \
  --expect 'evidence/acquired_manifest.json' \
  --expect 'decision_spec.signoff.json:acquired_inventory_sha256' \
  --expect 'decision_spec.signoff_sheet.md~Acquired inputs'
```

The eval-suite smoke expects `metadata.json:acquire=off`, and the collected run is checked to hold no `acquire/`
directory and no `data/inputs/acquired/` tree (the same check the grader makes).

### 9.3 Rollout

Each step is a PR under 800 lines, with a Codex review before merge.

| Step | Content | Exit criterion |
|---|---|---|
| 0 | this note; the owner's answers to §10 | owner decisions recorded in this file |
| α | the failing self-tests of §9.1 | they fail for the right reason |
| β | `agents/acquire.py` (allowlist loader, fetcher, promotion, provenance, `verify_inventory`), the `lib.safe_io` descriptor helpers, and the allowlist with licence terms read and dated; no LLM, no state | AQ1-AQ18 pass against the fixture server |
| β2 | the narrowed derivation sandbox for acquired runs (DS1, DS2) | DS1, DS2 pass on Linux; existing derivation tests unchanged |
| γ | the acquire task, `h_ACQUIRE`, edges, closing at SIGN, read rules and views, sheet tables, signature binding and quote check, behind `--acquire` (off by default) | every §9.1 test passes, T_ACQ_OFF_GOLDEN included |
| δ | one real run of a question that is not in the eval suite and names no files, on a Coiled VM, `--signoff owner`, run cap agreed beforehand | §9.2 markers present; the owner reviews the acquired set at SIGN; acquire cost measured |
| ε | after δ: whether `--acquire` becomes the default for questions that name no files | owner decision |

## 10. Open questions for the owner

| # | Question | Recommended default |
|---|---|---|
| 1 | May the acquire task use a general web search to discover sources, with fetching still limited to the allowlist? | No. Discovery only through the allowed sources' own search endpoints. A general search returns arbitrary pages into the session, which widens prompt injection for little gain |
| 2 | When a question names some files but not all that are needed, may ACQUIRE top up? | No in v1: any supplied file turns ACQUIRE off for the run, which keeps the eval-suite path unchanged. Revisit after δ |
| 3 | Nothing acquired: pause or end, and how does the owner supply inputs? | Pause `awaiting_inputs` under both `owner` and `lead` sign-off; recovery is a relaunch whose question names the files. A resumable supply command can follow if relaunches prove costly |
| 4 | PDFs, archives and HTML pages? | Not in v1: CSV and JSON for data, XML and plain text for literature. A PDF path needs text extraction inside the sandbox, which can be a later step |
| 5 | Sources that need a free key or registration (a DHS API key, a UN data-portal token, IHME)? | Excluded in v1. Later: a dedicated variable that only the fetcher reads, blanked for every task, and a licence decision per source |
| 6 | Must a dossier observation taken from an acquired file quote its span? | Yes (`observation_quote_not_found`, `observation_acquired_unbound`, HIGH, not declarable) |
| 7 | Should the owner approve the acquired set before SPEC (an extra pause)? | No. One review at SIGN, where the sheet lists every query, every acquired file and what used it; the spec can still be refused there |
| 8 | Cache acquired files across runs (content-addressed, on the cloud store)? | Not in v1. Reuse is through the existing amendment path, which copies bytes and provenance |
| 9 | Which model runs the acquire task? | `MODEL_FAST`: the task chooses sources and slices; judgement about evidence stays in SPEC and EVIDENCE |
| 10 | Caps: $6 acquisition balance (at most $3 per call), 40 fetches per round, 25 MB per file, 200 MB per run? | As proposed; re-measure after δ |
| 11 | Which sources are enabled in allowlist version 1? | The six marked `enabled` in §7, each only after its terms are read and dated in step β; `un_wpp` and `ihme_gbd` excluded |
| 12 | Passing `--acquire` sends search terms derived from the question to public services. Is that acceptable for every question, or only for questions marked public? | `--acquire` is the owner's statement that the question's subject may be disclosed in that form; queries are limited to typed codes and short literature terms (§3.2) and listed at SIGN. The launcher refuses `--acquire` for a question file marked `private: true` |
| 13 | Is `draft_evidence` reading acquired literature text (the one deliberate exposure, §3.3) acceptable, given the quote check and SIGN review? | Yes for v1. The alternative, a separate extraction task whose only output is quoted spans, adds a task per run and can follow if δ shows a problem |

## 11. Not in this note

Per-run engine code (engines stay a fixed registry built and validated by the team); other changes to
derivations; parsing PDFs; a cross-run evidence store; and the evidence service's parameter matching (V5), which
would read the acquired files once they exist.

## 12. Review log

**Codex round 1 (2026-11-23), design gaps in security, provenance and comparability.** 16 P1 and 10 P2; the
author checked the code claims it rested on, and all 26 are folded in:
- **Security:** later tasks no longer read raw acquired files, except `draft_evidence` for literature text, stated
  as a residual risk (§3.3); derivation errors carry no data text; the acquire task writes only its validated
  result, which `_session` already requires (`_task_output.json`), and reads only its packet and engine cards;
  queries are typed, with a disclosure question for the owner (§3.2, question 12); connections are pinned to the
  checked address, with every hop checked; quarantine uses descriptor-relative, no-follow operations; credential
  patterns are blanked for the task; every record in a per-record-licence response needs an accepted licence; the
  derivation sandbox is narrowed and required for acquired runs (it bound the whole root); XML is never parsed by
  derivations (`IMPORT_ALLOW` has no XML library).
- **Provenance:** the signature binds an inventory digest over every raw file and the manifest bytes, checked by
  every signer, status, the preconditions and resume (the dossier closure covered only derivation outputs);
  acquisition closes on leaving SIGN in every mode; fetches are keyed by request digest; promotion records its
  intent; restores re-freeze by hash; tampering is checked before any task reads; `acquire/` is in
  `harness_owned()`; directories stay writable so a top-up works; a top-up grants one more SPEC try; the
  acquisition balance includes retries and read-backs; ACQUIRE has its own `wanted` vocabulary, since the engines'
  kinds exist only after SPEC; the quote check binds an observation to a manifest id and hash, and the note says
  what it does not bind.
- **Comparability:** the grader checks for acquired content directly; amendment children carry the acquisition
  record; results are compared by inventory fingerprint, not by allowlist version; the prompt sentence and the
  provenance field appear only when acquisition ran, and a golden test checks that the off path is unchanged.
