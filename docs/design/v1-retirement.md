# Retiring pipeline v1: inventory and deletion plan

The owner retired pipeline v1 on 2026-11-20. PR #292 (α) makes every path that would start or resume v1
refuse with "pipeline v1 is retired (owner, 2026-11-20); use v2". The refusal takes effect only when #292
lands: this tooling PR (β) is independent of it, and the orchestration PR (γ) is stacked on both. This document
lists what only v1 uses, what v2 shares, and what each delete PR removes.

Old v1 run directories stay readable. Their `metadata.json` says `pipeline: v1`; runs from before
2026-11-05 carry no `pipeline` field. `pipeline_id.label` reads those as `unknown`, and `resume_conflict`
judges them by their files: a directory without `run_state.json` is v1's. The gates and the review tools
still read their files.

## Method

- **Import graph.** Every tracked `.py` file was parsed with `ast`. Each import edge was resolved the way
  the harness resolves it: `agents.*`, `scripts/` and `scripts/lib/` on `sys.path`, `toolkit.*`. Imports
  inside `*self_test*` functions were kept apart. Reachability was then computed from two sets of roots:
  - **v2 roots:** `agents/stage_machine.py`, `v2_sdk_tasks.py`, `v2_tasks.py`, `fix_loop.py`,
    `findings_ledger.py`, `analysis_backend.py`, `world_model_stage.py`, `enforcement.py`, `run_budget.py`,
    `scripts/lib/pipeline_id.py`, `scripts/lib/eval_paths.py`, `toolkit/`, `cloud/`, `tools/`, and the
    validator the v2 legacy mode runs (`scripts/validate_critique_yaml.py`). The test roots are
    `scripts/golden_traces.py` and `gate_replay.py`, which invoke the validator themselves on stored runs.
  - **v1 roots:** `main.py` `run()`, the `agents/__init__.py` registry, the lead prompt, the hooks and
    the agent prompt modules.
- **Symbol level.** Some modules are imported by both pipelines: `main.py`, `agents/run_finish.py`,
  `agents/run_budget.py` and `agents/enforcement.py`. For each, every top-level name was matched against
  every other tracked `.py` file. A name counts as v1-only when every file that uses it is v1-only code.
- **Strings.** String literals that name a `.py` file were recorded as edges too, then checked by hand.
  Most are mentions in docstrings. The ones that actually run or load a file are listed in the tables:
  - the validator and the checks it invokes, run by the legacy gate (`stage_machine.legacy_gate`);
  - `scripts/golden_traces.py` and `scripts/gate_replay.py`, test drivers that invoke the validator on stored runs;
  - `replay.deterministic_critic`, called by `regression_eval.py`;
  - `replay_coiled.py`, loaded by `cloud/run_cluster.py` RC60f.
- **Launch flags.** `cloud/worker_side.py` passes `--max-sessions` to every run, and configs pass
  `--signoff-hours`. `main.py` keeps accepting every flag it accepts today, so no v2 launch command changes.

## Constraints from v2

- `scripts/validate_critique_yaml.py` `_recompute_action` is shared, together with everything it calls. The live call
  chain is `stage_machine.v2_gate` (around line 450), then `_incorporate_rigor_violations`, then `_recompute_action`.
  `stage_machine._owner_rule` also reads the function's source (AWAIT_OWNER), but only once `OWNER_LIFECYCLE_WIRED`
  is true; it is false today.
- The v2 legacy gate mode (`stage_machine.legacy_gate`) runs the STAGE 7 validator with the final gate's
  flags through `agents/run_finish.gate_command`. The validator, the checks it invokes, and `gate_command` are
  therefore shared. That covers `open_problems`, `spec_compliance`, `presentation_pass` and `data_schema`.
  `cross_model_critic` produces an artifact the validator reads (`critique_crossmodel.yaml`). The validator reports
  it missing only when the run's metadata enables `cross_model`, and says nothing for an ordinary run (XM1/XM1b).
- Importing any `agents.*` module runs `agents/__init__.py`. Today that file imports all eleven v1 prompt
  modules and puts `scripts/` on `sys.path`. The slimmed package keeps the `sys.path` line so that the import
  path v2 code sees is exactly what it was. Most v2 modules insert `scripts/` themselves (for example
  `run_finish.store_run_results`); keeping the line means none has to be audited for relying on it.
- **Fingerprint.** `scripts/compute.py` `code_fingerprint` hashes the code content of `scripts/` and `toolkit/` and
  the requirements files. It leaves out `scripts/lib/code_commit.txt` and, in `compute.py` itself, the designated
  dispatch changes. A PR that changes fingerprinted content gives a merged tree a new fingerprint, so its engine
  rows do not reuse rows computed on an older tree. This PR's deletions under `scripts/` do that, and so does #292.
  Merge timing is the owner's call: for example, after the F6 run's reuse window closes.
- This retirement stays out of `agents/stage_machine.py`, `toolkit/analysis/figures.py`,
  `toolkit/analysis/run.py` and `scripts/results_store.py`, which other work is changing now.

## Inventory

### Deleted in the tooling PR (v1 retirement β)

These are the v1 stage-replay lane (experiments E1, E2 and E5) and the lead session's SDK and watch tools.
Each is imported only by the others in this list, except where noted.

| File | Lines | What it was | Who used it |
|---|---:|---|---|
| `scripts/replay.py` | 1075 | runs one v1 stage agent (from the `agents/__init__.py` registry) on frozen fixtures | `regression_eval.py` used `deterministic_critic`, which moves there unchanged (sync instead of async); `replay_coiled.py` ran `replay.py case` on the workers by subprocess |
| `scripts/replay_coiled.py` | 766 | Coiled backend for `replay.py` | `replay.py`; `run_cluster.py` RC60f (checked its `WORKSPACE` copy) |
| `scripts/replay_compare.py` | 156 | paired comparison of replay variants | none |
| `scripts/stage_scorers.py` | 232 | scorers for replay outputs | `replay.py`, `replay_compare.py`, `score_critiques.py` |
| `scripts/score_critiques.py` | 426 | E5 critic-arm matcher | none (`regression_eval.py` names it only in its docstring) |
| `scripts/telemetry.py` | 259 | per-turn table from the lead and subagent transcripts | `replay.py` |
| `agents/output_caps.py` | 272 | E2 history-growth levers for `replay.py` | `replay.py` |
| `scripts/sdk_scenarios.py` | 598 | Claude CLI kill, resume and cap scenarios for the lead session | archived launchers: `docs/experiments/launchers/dev_vm.py`, and `e7r7_cluster.py`, which refuses to launch without it |
| `scripts/watch_run.py` | 154 | follows `status.json` that `agents/observability.py` (v1 heartbeat) uploads | archived: `docs/experiments/launchers/e7r7_cluster.py` requires it in its tarball |

Total: 9 files, 3,938 lines. The edits elsewhere are small:
- `regression_eval.py` holds `deterministic_critic` (R6);
- RC60f checks two workspace names instead of three;
- `self_tests.py` drops `output_caps` and the `replay_coiled` exemption;
- `write_audit.py` and `import_audit.py` drop the deleted files and recount the moved sites.

The experiment write-ups under `docs/experiments/` and `experiments/`, and the archived launchers, keep their
references: they record what ran. Reproducing E1, E2, E5, E7 or E8 (its Coiled replay command, `docs/experiments/E8_compute_service.md`) needs
a checkout from before the retirement.

### Deleted in the orchestration PR (v1 retirement γ, stacked on #292 and β): 14 files and 3,353 more lines, 9,010 lines in all

| File or part | Lines | Why it is v1-only |
|---|---:|---|
| `agents/planner.py`, `data.py`, `modeler.py`, `analyst.py`, `writer.py`, `red_team.py`, `critique_domain.py`, `critique_methods.py`, `critique_premortem.py`, `critique_presentation.py`, `critique_sufficiency.py` | 3,524 | the eleven specialist prompts; imported only by `agents/__init__.py` (and by `main.py`'s self-test) |
| `agents/lead_prompt.py` | 951 | the lead's prompt; imported by `agents/__init__.py` |
| `agents/hooks.py` | 742 | the lead session's PreToolUse and SubagentStop hooks; imported by `__init__`, `run_finish`, `run_session` and `main` |
| `agents/observability.py` | 440 | heartbeat and `status.json` of the lead session; imported by `main.run` and by `main.py`'s OB self-test cases, which go with it |
| `agents/__init__.py` registry | 452 lines removed net; 17 lines left | `build_agents`, `_registry`, skill selection, the lead prompt and hook re-exports; the package keeps its docstring and the `scripts/` `sys.path` line |
| `agents/run_finish.py`, v1 part | 738 lines removed net; 99 lines left | `finalize_run`, `final_writeup`, `presentation_fix_pass`, `run_final_gate`, `determine_status`, report restore, verdict banner, model card write. Kept: `gate_command`, `VALIDATOR`, `exit_code_for` and its status sets, `store_run_results`, `_store_blob_name`. (`detect_current_round` also went: `gate_replay.py` has its own copy.) `agents/run_session.py` keeps only `LEAD_TOOLS`, for the preflight. |
| `main.py`, v1 part | 1,883 lines removed net (about 640 of runner code, the rest self-test cases) | `run()` and its helpers (resume context, sign-off waits, the lead's brief answers), and the self-test cases of the code above, including PV9 (it tested `_restore_protected_meta`, the v1 finalizer's helper, which goes). Kept: `main()` with every flag, `main_v2`, `run_v2_and_store`, `eval_refusal`, `create_run_dir`, the other PV cases, the EV and ST cases, and the cases that test kept shared modules (enforcement, integrity, run budget) |
| `scripts/validate_skill_attachments.py` | edit | it parses the attachment matrix in `agents/__init__.py`. With the registry gone it checks frontmatter and the skills a module under `agents/` loads by path (`decision-spec`) |
| `scripts/write_audit.py` | edit | WA3 counts write sites exactly and fails on stale entries. The `hooks.py` and `observability.py` entries go, and `run_finish.py` is recounted |
| README.md, CLAUDE.md | edit | the v1 description (agents table, STAGE 7 walkthrough, CLI rows) becomes a short historical note |

This PR is mostly whole-file deletions. Its in-place edits (main.py, `__init__`, `run_finish`,
validate_skill_attachments, docs) are what reviewers need to read.

### v1-flavoured, kept for now (follow-ups, owner decision)

| File or part | Why it stays |
|---|---|
| `agents/integrity.py` (1,319) | the hash record of protected run artifacts. At run time only v1 code writes to it: the hooks; `run_finish`; `main.run`, which closes the session; `run_session.log_message`, which records task starts and ends; `run_budget.write_budget_ledger`, which credits ledger writes through the object it is passed; and
`scripts/derived_rebuild.py` and `scripts/pipeline.py`, which credit rebuilt sidecars and pipeline outputs through
`integrity.harness_wrote` when a v1 caller (`run_finish._rebuild_derived`) hands them the object. But `scripts/pipeline.py` (shared: v2 calls `pipeline.load` and `run`) takes an `integrity` argument, and its `harness_build` and PO self-tests are written against `ArtifactIntegrity`. Removing it means re-specifying `pipeline.py`'s grant model. |
| `scripts/pipeline.py` `harness_build` | v1-only callers (`hooks.py`, `run_finish._rebuild_derived`). It goes with `integrity.py`. |
| `agents/enforcement.py` `check_tool_use` | the v1 agents' permission entry point; its only runtime caller is `hooks.py`. Everything it reaches through `bash_reason` is shared: the owner-command and kill policies (`owner_command_reason`, `kill_reason`) and their helpers, because `scripts/pipeline.py` checks every shell step through `_bash_refusal`, which calls `bash_reason`, which calls them. `lock_harness` and `unlock_harness` are v1 runtime code too (the lead's setup in `main.run` and its finalizer in
`run_finish`), and so are `diff_harness` and `harness_changed` (the lead's baseline in `main.run` and its finalizer); their other callers are self-tests, which stay with them. The rest of the module is shared: v2 uses `deny_output`, `filtered_env`, `MODEL_*`, `REPO_ROOT` and `HARNESS_FILES`, and `pipeline.py` uses `SCRIPT_OUTPUTS` and `OWNED_BY`. Its tests live in `main.py`'s self-test and stay with it. |
| `agents/run_preflight.py`, `agents/run_session.py` | **removed in γ** (owner decision, 2026-11-20). `main.py --preflight-only` ran the v1 lead session's live preflight (CLI, models, PATH, imports, a subagent Write probe); the flag is kept in the parser and refuses with a message (PV10, PV11e). `--skip-preflight` is accepted and unused. v2's own checks (the stage machine's preconditions, the launchers' preflights) are not touched. |
| `agents/run_budget.py` | shared. v2 uses the ledger, `charge_session`, `RECONCILE_PROMPT` and `writeup_reserve`; its own single, capped read-back is `v2_sdk_tasks` around line 496. `crashed_session_cost` is v1 runtime code (`main.run`), and v2 names it only in a docstring (`v2_sdk_tasks` around line 411, which describes the shared read-back loosely) and a self-test (around line 663), so a cleanup must keep v2's capped read-back. The other v1-only names (`MAX_USD_PER_HOUR`, `TRANSCRIPT_MARGIN`) are small. |
| `scripts/decision_brief.py`, `scripts/scope_signoff.py`, `scripts/done_when.py`, `scripts/open_problems.py`, `scripts/run_autopsy.py`, `scripts/lessons.py`, `scripts/model_card.py` | used by v2 code (`stage_machine`, `enforcement`), by the shared validator, or by standalone tools (`ask.py`, the lessons ledger) |
| All 58 skills under `.claude/skills/` | v2 loads `decision-spec`, `tools/run_console` cites `critique-blockers-schema` in its glossary tooltips, and Claude Code sessions in this repository load the rest directly. No skill is deleted. Once γ deletes the attachment matrix, 40 local skills lost their pipeline attachment (the registry's 41st, `decision-spec`, is loaded by v2 by path; 17 were never attached; 3 registry names were external MCP skills). All remain project skills. |
| `.claude/orchestration/*.yaml` | read by the shared validator and by `expert_priors` |
| `cloud/run_health.py` stall check on `status.json` | a generic monitor rule. v2 runs write no `status.json`. Whether v2 needs a heartbeat is a separate decision. |
| `docs/experiments/launchers/*` | archived launchers, kept as records |

## Owner decisions (2026-11-20)

1. `--preflight-only` and the v1 lead-session preflight are removed (in γ).
2. The kept v1-flavoured code is removed in a follow-up PR after the F6 report. The plan is below; it is not built
   yet.

## Follow-up plan: integrity, harness_build and the enforcement agent policy (after the F6 report)

**What goes.**
- `agents/integrity.py`: `ArtifactIntegrity`, `harness_scripts_run`, `SCRIPT_PRODUCES` and its self-test.
- `scripts/pipeline.py` `harness_build`.
- In `agents/enforcement.py`: `check_tool_use` and the helpers only it reaches (the write/agent path policy,
  `_OUTPUT_CARVE_OUTS`, `LEAD_PROTECTED`, `_AGENT_TOOLS`), plus `lock_harness`, `unlock_harness`, `diff_harness` and
  `harness_changed`. `hash_harness` stays: `scripts/eval_suite.py` uses it.

**What replaces the write grants in `scripts/pipeline.py`.** Nothing at run time. Every v2 caller already calls
`pipeline.run(run_dir)` without an `integrity` argument (`stage_machine` around lines 1279 and 1514,
`analysis_backend` around line 1221). The grant branches are therefore dead in v2. They are:
- `integrity.check_now` / `is_open(STATE)` (drop distrusted state);
- `integrity.harness_pipeline(grant)` around step runs and `_finish_granted`;
- `suspect=integrity.is_open` in `_run_one`;
- `integrity.harness_wrote` in `_finish`.

The follow-up:
1. Removes the `integrity` parameter from `pipeline.run`, `_run_one`, `_finish` and `_finish_granted`, and from
   `scripts/derived_rebuild.py` (`_commit`, `_write_sidecar`, `_invalidate`, `rebuild`).
2. Deletes `harness_build`.
3. Keeps `pipeline.py`'s own refusals of agent writes. These are `_bash_refusal` through `enforcement.bash_reason`,
   and the protected-output set built from `SCRIPT_OUTPUTS`, `SIGNED_FILES` and `OWNED_BY` (around line 157).
   v2 protects its run artifacts by other means:
   - the bounded tasks' own path checks (`v2_sdk_tasks` PreToolUse hook: `TOOLS`, owned paths);
   - the signed spec's sha256 binding (`spec_signoff_findings`);
   - the stage machine owning every transition.

   No v2 code reads an integrity record.
4. Keeps `scripts/validate_critique_yaml.py` `_check_artifact_integrity`: it reads `artifact_integrity.json` from
   stored v1 runs and returns nothing when the file is absent, which is the case for every v2 run.

**Which tests are rewritten.**
- `main.py`:
  - delete IN1-IN12, IC1-IC9, IR1-IR5 (integrity) and HL1 (lock);
  - keep T1 (hash and diff, restated on `hash_harness` alone);
  - rewrite E1-E7 and CS1/XM2-3, which test `check_tool_use`'s path policy: drop the agent-type write cases, and
    move the shell cases to `bash_reason` (as OC1-OC2 already do);
  - CR1-CR11, KL1-KL3 and BG1-background exercise `bash_reason` through `check_tool_use`: call `bash_reason`
    directly.
- `scripts/pipeline.py`:
  - delete PO0's integrity import and the PO cases that build an `ArtifactIntegrity` (grants, `harness_build`,
    distrusted state, `_Probe` around line 2825);
  - keep the staleness and hashing cases with `integrity=None`;
  - add one case that `run()` has no `integrity` parameter, so a grant cannot be reintroduced unnoticed.
- `scripts/derived_rebuild.py`: drop the integrity-crediting cases.
- `scripts/write_audit.py` and `scripts/import_audit.py`: drop the `agents/integrity.py` entries and recount
  `pipeline.py`.

**Check before building.** Grep for every `integrity=` call site, the `harness_wrote`, `harness_pipeline`,
`check_now` and `is_open` attribute uses, and every `enforcement.<name>` this plan removes, across `agents/`,
`scripts/`, `toolkit/`, `cloud/` and `tools/`. Then do the usual: self-tests, `regression_eval`, `gate_fuzz`, and a
Codex review. It touches `scripts/`, so the fingerprint changes: schedule it outside any run's reuse window.

## Open questions for the owner

1. (Decided 2026-11-20: one follow-up after the F6 report; plan above.)
2. (Decided 2026-11-20: retired, in γ.)
3. When should these PRs merge, given the fingerprint change for runs that reuse engine rows?
