---
phase: "v2 ACQUIRE (PR gamma)"
commit: "add ACQUIRE stage, bounded acquire task, read isolation, SIGN binding, quote verification, amendment reuse, and eval guards (AQ16-AQ21, S_ACQ_*, T_ACQ_*)"
---

## Changelog

**Third PR (`acquire-gamma`) of v2 ACQUIRE (`docs/design/acquire.md` §§3–6, §8–§9).**

- **Added `ACQUIRE` state and bounded `acquire` task** (`agents/stage_machine.py`, `agents/v2_sdk_tasks.py`, `agents/v2_tasks.py`, `agents/enforcement.py`, `main.py`), gated behind `--acquire` (default `off`).
- **Enforced untrusted-content isolation and no-prose feedback** (`agents/v2_sdk_tasks.py`, `scripts/pipeline.py`, `scripts/validate_critique_yaml.py`): `acquire` task restricted to `Read`/`Glob`/`Grep` + `mcp__acquire__*`, credential-blanked env, schema-only output; later tasks blocked from `acquire/` and raw `data/inputs/acquired/` (except `draft_evidence` on `.xml`/`.txt`); failing derivations on acquired inputs emit `derivation_failed_on_acquired` with exception class, script line, and input path only.
- **Bound acquired inventory into SIGN, preconditions, quotes, and amendment reuse** (`toolkit/spec/signoff.py`, `toolkit/spec/preconditions.py`, `toolkit/spec/evidence_dossier.py`, `toolkit/spec/amend.py`, `scripts/decision_brief.py`, `agents/acquire.py`): sheet renders acquired inputs, outgoing queries, and rejected fetches; sign-off records bind `acquired_inventory_sha256`; literature observations require quoted-span verification (`observation_quote_not_found`, `observation_acquired_unbound`); amendment children inherit acquired files, provenance, and metadata with acquisition closed.
- **Added eval comparability guards and §9.1 self-tests** (`cloud/run_cluster.py` AQ20, `scripts/eval_suite.py` AQ21, `agents/v2_sdk_tasks.py` `S_ACQ_*`, `agents/stage_machine.py` `T_ACQ_*` including `T_ACQ_OFF_GOLDEN`).
