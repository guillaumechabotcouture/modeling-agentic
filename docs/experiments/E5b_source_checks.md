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

## Results (2026-10-22; 54 variants × 2 arms, local, $0)

**Variants.** Seed 11 planted 139 defects. 47 were identical to seed-7 defects, because some classes have few possible targets per fixture. They are excluded as registered, leaving **92 held-out defects**. Within seed 11, some defects repeat across variants of the same fixture; distinct counts are shown.

| Class | Held-out (distinct) | Baseline `72abe68` | New `5357991` | New 95% CI | Lost |
|---|---|---|---|---|---|
| data_vintage | 16 (12) | 0/16 | **10/16** | 0.39–0.81 | 0 |
| ledger_mismatch | 13 (13) | 0/13 | **6/13** | 0.23–0.71 | 0 |
| unit_error | 11 (8) | 0/11 | **2/11** | 0.05–0.48 | 0 |
| zero_width_ci | 12 | 3/12 | 4/12 | | 0 |
| ci_inverted | 8 | 4/8 | 4/8 | | 0 |
| citation_mismatch | 18 | 0/18 | 0/18 | | 0 |
| prose_stale_number | 12 | 0/12 | 0/12 | | 0 |
| benefit_total_population | 2 | 0/2 | 0/2 | | 0 |

budget_overspend, renamed_verdict and sensitivity_contradiction had no held-out instances: all of theirs repeated seed 7.

**Registered decision rules:**
1. **Gap filled (recall ≥ 0.5):**
   - data_vintage **passes** (0.63).
   - ledger_mismatch **fails** (0.46).
   - unit_error **fails** (0.18).
2. **No regression:** **passes.** No class lost a paired detection.
3. **Noise:**
   - Spurious-match rate is 0.014 vs 0.000 (≤ +0.02): **passes.**
   - New HIGH findings per clean fixture average **24.0** (limit 1): **fails.**
4. **No tuning:** the checks are not changed on these variants. The failing classes go to the structural fix.

**Diagnosis (post hoc; it explains the verdicts and does not change them):**
- **Ledger misses are mostly the matcher.**
  - 5 of the 7 uncredited plants *were* flagged with the right claim id and planted value (`sens_n_flips` 21 vs 15, `rm_n_scenarios`, `sa_n_params_r6`, `package_count`, `ex_n_wide`). The frozen matcher can't credit one- or two-digit values, because of its ≥3-character rule.
  - The other 2 were flagged as having an unresolvable source field, which is not a mismatch.
  - Read by gate output, 11 of 13 plants were flagged, 9 of them as wrong values.
- **The "noise" is real, unplanted defects.** All 24 new HIGHs per clean fixture are ledger-provenance findings that the round-1 early return had hidden.
  - Example: in CB1, 110 of 286 checkable claims contradict their source (`base_n_lgas_smc` 150 in the ledger vs 208 in `allocation_scenarios.yaml`).
  - Cause: the ledger was built at 07:01, and the scenarios were rewritten at 10:07 without rebuilding it.
  - By E5's definition (an unplanted finding on a clean variant) these count as noise. In substance they are true positives.
- **Unit misses have no reference to check against.** The missed columns have no `_lo`/`_hi` pair, no matching-key partner table, and no value quoted in a data document. Their names don't imply a bound. A heuristic can't see a ×100 change without some reference.

**What follows:**
- Both failing classes point to the same structural fix:
  - **Ledger:** code fills in ledger values from each claim's `source_field` and rebuilds them when a source changes, so a stale or mistyped ledger can't exist.
  - **Units:** the data stage declares each column's unit and plausible range at write time.
- The data_vintage check is kept (MEDIUM).
- The ungated ledger check is kept. Its findings are real, and the structural fix removes their cause.
