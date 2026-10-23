# E2: conversation-history growth (MODEL stage), proposal

**Status:** proposal (2026-10-22). It must be pre-registered as `docs/experiments/E2_history_growth.md` before any case runs.
**Builds on:** E1b's refined contract-packet variant, called **V** below.

## Why

E1 cut the modeler's start context from 186K to 32K tokens, but cost per case fell only 26%. The rest is history growth: every turn re-reads everything earlier in the session. The measurements below come from `measure.py` plus transcript inspection, over E1's 11 modeler sessions of 60 turns each.

| | Per session |
|---|---|
| Context at turn 1 → turn 60 | ~32K → ~260K (variant); ~186K → ~410K (control). Growth of about **211–276K tokens**, about 3.5K per turn, never compacted |
| Tool results (chars/4) | ~52K: file `Read` 56% of tool re-read volume, `cat/head/tail/sed` 17%, inline `python -c` 14%, `grep/find/ls` 9%; long script stdout only ~2% |
| `Write` tool inputs (full file contents the agent writes) | ~29K |
| `Bash` command text | ~9K |
| Retained extended thinking | not visible in transcripts (only signatures, ~230K chars per session); it is the bulk of the remaining ~140K of growth |
| Injected reminders and attachments (excluding the start snapshot) | small, under ~10K |

**Single biggest re-read items:** whole-file Reads of `plan.md`, `data_quality.md`, `citations.md` and `pre_mortem.yaml` early in the session (5–12K tokens each, then re-read on every one of the following ~55 turns).

## Levers (all opt-in variant fields in `scripts/replay.py`)

| Field | What it does | Verified live |
|---|---|---|
| `tool_output_cap: {bash_chars: N, read_lines: M}` | PostToolUse `updatedToolOutput` replaces large Bash output with head + tail, and spills the full output to `.tool_outputs/<id>.txt` (path given to the agent). PreToolUse `updatedInput` adds `limit: M` to whole-file Reads of long text files. Enforcement runs first; a denied call is never rewritten. | Yes, haiku: Bash 50K→2.3K chars with spill file written; Read 12.8K→3.7K chars |
| `compaction: {window_tokens: N}` | sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW` for the session | **Partial.** Works for a top-level session (30K window: `compact_boundary` emitted, context 101K→63K; the compaction call itself cost ~$0.14). **Does not take effect inside a subagent:** context grew past 65K and stayed there. Replay runs the stage as a subagent, so this lever is inert for E2 as built. It becomes usable once the v2 orchestrator runs stage agents as top-level sessions. |
| `fresh_context_every: N` | runs the stage as segments of at most N turns. Each segment is a new session; segment 1 adds a working-log instruction (`models/WORKLOG.md`), and later segments get a deterministic handoff: original task + working log (capped at 12K chars) + sorted list of files written. Budget and turns are tracked across segments; stops early if the agent finishes. | Self-tests only, no paid run |
| `effort: low\|medium\|high\|max` | sets the stage AgentDefinition's reasoning effort, targeting retained thinking | Self-test only. Quality impact unknown; must be measured |

Defaults are unchanged: with none of these fields set, `sdk_runner` runs exactly one session with the unmodified task (self-test S3).

## Proposed arms (paired, same fixtures and seeds)

1. **V**: E1b refined variant (control for E2).
2. **V + cap**: `tool_output_cap: {bash_chars: 6000, read_lines: 300}`.
3. **V + fresh**: `fresh_context_every: 20`, i.e. 3 segments of 20 turns for a 60-turn cap.
4. **V + cap + fresh**: the stacked lever.
5. Optional: **V + effort=medium**, separate because it can trade quality for cost.

Fixtures: the 10 slim evidence-based fixtures (`fixtures_v2/`). Caps: 60 turns, $20, 90 min per case, as in E1.

## Metrics

- **Efficiency (primary):** cost per case, cost per turn, mean context per turn, max context, growth per turn.
- **Progress / quality, non-inferiority (primary):** `artifact_rate`, effort-floor HIGH violations *per evaluated floor*, framework OK, and compare/identifiability verdicts. All from `stage_scorers`.
- **Lever-specific secondary measures:**
  - cap: number of capped calls, spill-file reads afterwards (was the cap too tight?);
  - fresh: handoff size, rework (files rewritten in later segments), working-log quality;
  - effort: thinking tokens.

## Decision rule (to fix at pre-registration, using E1/E1b variance)

Adopt a lever if:
- cost per case falls by at least X% (CI excludes 0); **and**
- the `artifact_rate` CI lower bound is at least −δ; **and**
- effort-floor violations per evaluated floor do not increase.

X and δ are to be set from a power calculation on E1b's paired variance, not chosen in advance by hand.

## Files

- `experiments/E2_history_growth/measure.py`: growth and contributor measurement over a replay batch.
- `agents/output_caps.py`: caps, compaction env, handoff builder, hook wrapper (`--self-test`).
- `scripts/replay.py`: opt-in fields `tool_output_cap`, `compaction`, `fresh_context_every`, `effort`; self-tests S1–S4.
