# Plan: gate inputs the harness produces itself (replaces integrity inference)

**Status:** approved by the owner 2026-10-24; build after E7 ends. **Scope:** `agents/integrity.py`, the Bash write-target scanner in `agents/enforcement.py`, and the STAGE 7 hand-off in `main.py`.

## Problem

The final gate trusts files that agents can also write: gate outputs such as `identifiability.yaml`, `uncertainty_report.yaml` and `coherence_audit.yaml`, and the critique YAMLs.

Today we try to infer, from each Bash command, whether a sanctioned producer wrote a file. That means parsing `sh -c`, wrappers (`timeout`, `nice`, `nohup`), `cd`/`pushd`/`popd`, background grants with time-to-live, and live-process checks.

That inference can never be complete:
- every review found a new command shape;
- every gap either let a forgery through or flagged legitimate work;
- this work produced PR #14's ζ rounds and PRs #15–18, and stalled E7 twice;
- the code is about 430 lines in `integrity.py`, plus the subshell and write-target tracking in `enforcement.py`.

## Design

**Don't infer who wrote a file. Make the gate read only files the harness itself produced.**

1. **Harness-run producers.** Before STAGE 7 runs (each round, and at finalize), `main.py` runs each gate producer from the `SCRIPT_PRODUCES` list in a subprocess. It writes the outputs into `{run_dir}/.harness/gate_outputs/`, and `validate_critique_yaml.py` reads only that directory.
   - Agents keep running the same scripts to see results while they work. Their copies in the run folder are working files that the gate ignores.
   - Nothing an agent writes can change what the gate sees, so detecting tampering is no longer needed.
2. **Critique snapshots.** When a critic subagent finishes (`SubagentStop`), the harness copies that critic's own `critique_<name>.yaml` into `.harness/critiques/` and records its hash. The gate reads only the snapshots, so a later edit by another agent has no effect.
3. **Agents can't write into `.harness/`.** One prefix in the existing Write/Edit/Bash deny rules covers it.
4. **The `.harness/` copies are checked.** At finalize, one hash check compares the `.harness/` contents with the record; a mismatch makes the run `artifact_tampered`.

## What gets deleted

- `agents/integrity.py`: intents, grants, time-to-live, `_live_harness_scripts`, and shell parsing (`harness_scripts_run`, `_WRAPPERS`, the `-c` handling). About 380 lines; about 50 remain for the hash record.
- `enforcement.py`: `bash_write_targets_cwd`, subshell and directory tracking, and `iter_segments_with_cwd`, which only the integrity code used. The simple deny rules stay.
- Validator checks `artifact_integrity_violation` and `artifact_integrity_resolved`. They become one check: snapshot hash mismatch → `artifact_tampered`.
- Prompt text telling the modeler that gate outputs must come only from producers. It's no longer needed.
- Their self-tests (IN, IC, IR, BG, AI series) are replaced by about 6 tests of the new path.

## Cost and risk

- **Runtime:** the harness re-runs producers once per gate. Most take seconds. `identifiability.py` profile likelihoods and `propagate_uncertainty.py` can take minutes; they go through the compute service. To check before building: producer runtimes in the E7 runs.
- **Producer inputs** still come from agent files, such as the model code and `models/identifiability.yaml` specs. That's by design: the gate checks the model the agents built. What changes is that the result can no longer be hand-written.
- **A producer that fails under the harness** is a gate finding (fail closed), the same as today's missing-output finding.

## Plan and dates

| Step | What | Owner | When |
|---|---|---|---|
| 1 | Measure producer runtimes in the E7 runs (decides which producers go through the compute service) | Claude | after E7 ends |
| 2 | Build steps 1–4 above; delete the inference code; ~6 new tests | Claude | ~half a day after approval |
| 3 | Real check first: replay one finished E7 run's STAGE 7 with harness-run producers and compare the gate decision | Claude | same day, before any review |
| 4 | One scoped review: does the gate see only harness-produced files, and does a hand-written file change the decision? Other findings are logged, not fixed. At most 2 rounds. | Codex/Copilot | same day |
| 5 | Merge | Owner approves | after step 4 |

## Decision

Approved 2026-10-24 (harness-run producers and critique snapshots).
