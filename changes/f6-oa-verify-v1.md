---
phase: "F6"
commit: "OA verify v1 α–γ"
---

## Changelog

**Option A verification round v1 (owner, 2026-11-20).** Refit b2 (f6-refit-optiona-b2) chose a point in band on all 10 targets, but 1 of its 12 verified sample points narrowly missed B9g, so its sample was invalid under the 14 Nov rule.

`net_effect_fit.py run --verify-v1` (`OPTION_A["verify_v1"]`) runs the owner's one pre-declared verification round on b2's checkpoint, pinned by its sha256: every calibration, design, extension and wave-1 chosen-point task comes from that checkpoint. The round uses new candidate and pick seeds (41, 43) and 12 new sample points, keeps the 14 Nov acceptance rule unchanged, requires `--margin-loo`, and has its own job (f6-refit-optiona-v1), store and $20 cap. The output records the round under `waves.verify_v1`. Its result is final: a failure is recorded and the owner decides the fallback.

`plan --verify-v1` prices the round at 1,248 arm runs (the re-chosen point plus 12 sample points), about 1.1–1.5 h and $6.6–9 on 48 vCPUs.

The round verifies exactly 12 sample points: a smaller `--verify` is refused, and fewer than 12 in the region fail the round. It stops if any stored task is missing from the checkpoint before the choice. New self-tests: FV1–FV5.
