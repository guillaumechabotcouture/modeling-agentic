# E5b: Held-out re-test of the E5 source checks (pre-registered)

**Registered:** 2026-10-22, before any E5b variant was planted or scored. **Follows:** E5 rule 3 ("fill coverage gaps … then re-test on held-out variants, new seed").

## Question

Do the Phase 22 α checks (`5f812dc`) raise deterministic recall on the three E5 gap classes on defect instances that weren't used to design them? The gap classes are ledger_mismatch, unit_error and data_vintage. The re-test also checks that the new checks don't lose other detections or add noise on clean inputs.

## Arms (deterministic only, $0, local)

- **Baseline:** validators at `72abe68`, before Phase 22.
- **New:** validators at this registering commit (Phase 22 α code from `5f812dc`; this commit adds only this document).

Both arms run `replay.py --stage CRITIQUE` with the `deterministic` variant, each from a clean worktree at its commit. The LLM critics are not re-run: E5b tests the gates, not the critics.

## Variants (held out)

- **Build:** `scripts/plant_defects.py build --k 8 --per-variant 3 --seed 11` on the same 6 CRITIQUE fixtures as E5.
- **Per fixture:** one clean variant plus 8 planted variants with 3 defects each. Seed 11 never ran in E5.
- **Why "held out" is only partial.** Only these 6 fixtures exist; T1/T2 stay excluded for the pending blinded review. The instances are new: different claims, columns and lines. The defect *generator* is the same one, so E5b tests generalization across instances, not across defect forms.
- **Contamination check.** Any seed-11 defect identical to a seed-7 defect (same file, location and value) is excluded from the primary analysis and reported.

## Scoring

- **Matcher:** `scripts/score_critiques.py` v2 at `72abe68`, frozen.
- **No adjudications:** E5's adjudications were for LLM critics, and none apply to the deterministic arm.

## Outcomes

**Primary:** per-arm recall on each gap class, with Wilson 95% CIs.

**Secondary:**
- **Paired regression check:** defects detected by baseline and missed by new, in every class.
- **Clean-variant noise:** HIGH findings in the new arm that aren't in the baseline, per clean fixture.
- **Spurious-match rate** on clean variants.

## Decision rules (stated in advance)

1. **Gap filled.** A gap class is filled if new-arm recall is ≥ 0.5.
2. **No regression.** No class may lose more than one paired detection (new misses, baseline caught), and any loss must be explained.
3. **Noise.**
   - The new arm adds on average ≤ 1 new HIGH finding per clean fixture.
   - Its spurious-match rate is ≤ baseline + 0.02.
4. **No tuning on held-out data.** Whatever the result, the checks are **not** tuned on these variants. A failing class is reported and handled by the structural fix planned next (code-derived ledger values, declared data schema), not by more heuristics fitted to seed 11.

## Limitations

- **Same generator and fixtures:** see Variants above.
- **The checks were designed after reading the seed-7 instances.** This is the reason for E5b.
- **Deterministic arm only:** the effect on union recall with LLM critics is inferred, not measured.
