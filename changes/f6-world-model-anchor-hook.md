---
phase: "F6"
commit: "WM-H"
status_group: "F6 progressive steps"
---

## Changelog

**Every transition records the hypotheses registration ledger's head (F6 WM-H).** `agents/stage_machine.py` `registry_head()` reads `analysis/hypotheses_registry.jsonl` with no link followed. When the run has one, each `run_state.json` history entry (written at every transition, a step's start) gains `registry_head: {at, lines, head}`: the ledger's line count and last line sha256, with the time. An unreadable ledger records an error, never a head. `run_state.json` is a harness file in `agents/integrity.py`'s hash record, so these heads are the anchor outside the ledger that the world model (`toolkit/analysis/world_model.py`, F6 WM-L) requires before it counts a hypothesis as pre-registered. Self-test RH1-4.

## Status

- **WM-H:** the stage machine records the hypotheses registration ledger's head in run_state.json at every transition.
