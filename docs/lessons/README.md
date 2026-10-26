# Lessons ledger

**Purpose:** to show whether the pipeline is getting better as experts review its runs. Each lesson records:
- what went wrong;
- what we changed;
- how we will know whether the change worked;
- whether that class of error came back in later runs.

## Files
- `ledger.yaml`: the lessons, hand-edited. Its schema is in the header and checked by `python3 scripts/lessons.py validate`.
- `observations.jsonl`: one line per lesson per run, append-only. Each line is recurred, caught, absent or not applicable, with who observed it (`auto`, `expert`, `maintainer` or `replay`). Lines are written by `scripts/lessons.py record`, never by hand.

## How a lesson moves
1. **open:** an expert review, deep review, experiment or run failure finds an error class. The lesson needs either an automatic signal (`test.detect`) or a question an expert can answer (`test.review`).
2. **in_progress**, then **changed:** the fix is merged. Record the merge time (UTC) as `change.date`; observations are split at that point.
3. **verified:** this is set by hand, never automatically. `summary` proposes it once the error has not shipped in at least two runs since the change. For a lesson with a review question, at least one of those must be an expert or maintainer observation of `absent`.
4. **Reopened:** if the error class recurs after the change, `summary` says so.
5. **retired:** the error class can no longer occur, for example because the component was removed.

## Each run
- At finalize, `main.py` runs `lessons.py check`, which writes:
  - `lessons_check.json`: the automatic signals;
  - `lessons_check.md`: the expert checklist for this run, made of the review questions of the lessons that apply.
- The result is also added to the run's register row (`outputs/register/<run>.json`).
- After reviewing a run:

```bash
python3 scripts/lessons.py record --from-check <run_dir>/lessons_check.json      # automatic results
python3 scripts/lessons.py record --lesson L017 --run <run> --date <run start, UTC> \
    --result absent --by expert --note "targets and covariates are separate datasets"
python3 scripts/lessons.py summary --md
```

## What the automatic signals mean
- **`gate_kinds`:**
  - a kind raised in the last gate round is **recurred**: it shipped;
  - a kind raised only in earlier rounds is **caught**: the agents still make the error, but the gate stopped it.

  Both count as evidence. A falling *caught* rate shows a real improvement in agent behaviour; a falling *recurred* rate shows the gates working.
- **`autopsy_why`, `peak_mem_pct_over`, `file_absent`:** read from the run autopsy and the run directory.
- **Missing sources:** if a signal's source is missing, its result is **unknown**, never absent.
- **Older runs:** a run made before a signal's code existed can show **absent** only because nothing was recorded. For example, results-storage errors were only recorded from Phase 26 on. Treat absences in older runs as weak evidence.

## Limits
- **Most model-quality lessons have no automatic signal.** They depend on expert answers to the checklist. That is deliberate: a keyword match on critic prose would count false recurrences.
- **One run per version is anecdote.** For model-quality lessons, the stronger test is replaying the decision point from its stage snapshot several times, before and after the change (`scripts/replay.py`, recorded as `--by replay`).
- **The ledger describes error classes, not how to pass a gate.** Changes to agent prompts go in the prompts and skills themselves.
