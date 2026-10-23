# E5: Critic effectiveness via planted defects (REGISTERED 2026-10-22, before any LLM-arm case ran)

**Drafted:** 2026-10-22. **Follows:** E1/E1b (`E1_context_diet.md`, `E1b_packet_gap_fix.md`). **Roadmap:** `ROADMAP.md` row E5.

## Question

Which critics catch which classes of defect, at what cost and noise? Five LLM-backed critics and the validator gates run at every CRITIQUE round. We don't know their marginal value: whether each one finds defects the others miss, or only adds cost and spurious blockers. E5 measures this directly. It plants known defects in frozen CRITIQUE-stage inputs and scores whether each critic reports them.

## Arms

All arms run on the same variants. Each LLM arm uses its production prompt, skills, and enforcement hooks (`agents/__init__.py`), via `scripts/replay.py` with stage `CRITIQUE`. Variant YAMLs are in `experiments/E5_critic_effectiveness/`.

| Arm | Agent | Caps |
|---|---|---|
| methods | critique-methods | claude-opus-5-5, 40 turns, $6, 30 min |
| domain | critique-domain | same |
| presentation | critique-presentation | same |
| redteam | critique-redteam | same |
| crossmodel | `scripts/cross_model_critic.py --round 1` (gpt-6.1-sol via codex) | 30 min; billed to OpenAI |
| deterministic | validators only: `validate_critique_yaml.py --spec-compliance --parameter-registry --rigor-artifacts`, no LLM | $0, 15 min |

The deterministic arm is the baseline: an LLM critic's value is what it catches *beyond* the validators.

## Fixtures and variants

**CRITIQUE fixtures** are built by `scripts/fixtures.py build --stage critique`.
- **What is included:** an evidence-based selection. It takes core rigor artifacts, then files the critic agents actually read in the source transcript, then processed data, then figures, within a 50 MB budget. Excluded files are listed with their sha256.
- **What is always removed:** prior critiques, reports, gate outputs, and harness state.

| Fixture | Source | Disease | Files | Size | Evidence basis |
|---|---|---|---|---|---|
| CB1 | B1 harvest | malaria | 612 | 36.4 MB | transcript |
| CB2 | B2 harvest | malaria | 227 | 36.6 MB | transcript |
| CH235631 | historical 2026-05-03_235631 | malaria | 62 | 3.7 MB | core only (no transcript) |
| CH161312 | historical 2026-04-29_161312 | malaria | 58 | 3.7 MB | core only |
| CH224202 | historical 2026-04-28_224202 | malaria | 62 | 3.8 MB | core only |
| CM213310 | MEASLES_213310 | measles | 851 | 10.1 MB | transcript |

T1 and T2 are excluded because their blinded expert review is pending.

**Variants** are built by `scripts/plant_defects.py build --k 3 --per-variant 3 --seed 7`.
- **Per fixture:** one `clean` variant plus 3 planted variants with 3 defects each. Within a variant, the defects are in non-overlapping files.
- **Total:** 24 variants and **54 planted defects per arm**, which meets the ≥40 target.
- **Answer keys** are in `_truth/<variant>.yaml`, outside the critic workspace.
- **Planting is deterministic:** each class is a minimal textual edit, and the script asserts that exactly one file changed per plant.

**Defect classes** (disease-agnostic; 12 implemented, 11 realised in this build):

| Class | Edit | Planted |
|---|---|---|
| budget_overspend | allocation CSV cost column ×1.55 on funded rows | 7 |
| prose_stale_number | one results.md number ×0.82 (years skipped) | 7 |
| citation_mismatch | cited year −6 | 6 |
| sensitivity_contradiction | results.md asserts the opposite of the sensitivity_analysis verdict | 6 |
| ledger_mismatch | claims_ledger value ×1.37 vs its source artifact | 5 |
| data_vintage | dataset year −14 | 5 |
| renamed_verdict | a verdict label changed to a non-schema value (`VALIDATED_WITH_NOTES`) | 5 |
| zero_width_ci | ci_low = ci_high = mean | 4 |
| ci_inverted | ci_low above the mean and ci_high | 4 |
| unit_error | a rate-like column ×100 | 4 |
| benefit_total_population | the benefit assignment uses total population instead of eligible population | 1 |
| double_counting | `(1-a)*(1-b)` → `1-(a+b)` | 0 (no realistic target) |

## Outcome and scoring (`scripts/score_critiques.py`; no LLM judge)

**Detection rule.** A planted defect counts as detected if one blocker does both of the following:
1. **Cites the defect.** It cites the defect's file, an `also_files` entry, or the location key (claim id, column, or CI field).
2. **Gives evidence.** It contains at least one *specific* term, or at least two *generic* terms, from the answer key.
   - Specific terms: the planted value (also matched at 3–4 significant figures), or an identifier.
   - Generic terms: class vocabulary such as "inverted" or "exceeds".

Terms that only restate the cited file name or location key don't count. Neither do short common words. For single-number classes (prose_stale_number, citation_mismatch, data_vintage, ledger_mismatch), the blocker must quote the planted value; generic terms alone don't count.

**Matcher check on the deterministic arm (before / after tightening):**
- Before: the spurious-match rate on clean variants was 0.185. Typical false matches were figure-staleness blockers that cited the planted file, and the word "cost".
- After: the spurious-match rate is 0.00. Every remaining match in the full audit was a true detection.

**Primary outcome:** per-arm recall, overall, by class, and by severity, with Wilson 95% CIs.

**Secondary outcomes:**
- **Prose recall:** detection in the critique markdown but not in the YAML. This shows defects that were noticed but not filed as blockers.
- **Noise:** new unmatched blockers, planted vs clean of the same fixture.
- **Spurious-match rate** on clean variants.
- **Cost:** $ and turns per case.
- **Union recall:** recall of each subset of critics, to find the cheapest subset that reaches a given recall.

**Audit:** a random 20% of rows, plus every row in a pilot, goes to `e5_audit_sample.md` for a human check of the matcher. If the audit shows matcher disagreement above 10%, the matcher is revised before any comparison is reported.

## Power

- **Per-arm recall:** n = 54 gives a Wilson half-width of about ±0.13 at recall 0.5.
- **Arm-vs-arm comparison:** paired on the same defects, using McNemar. It detects recall differences of about 0.25 or more.
- **By class:** 4–7 instances per class is descriptive only. Class-level claims need the follow-up replication (more seeds or more fixtures).

## Cost estimate

| Item | Estimate |
|---|---|
| LLM critics: 4 arms × 24 cases = 96 invocations at ≈ $2–4 each (smoke: see below) | ≈ $190–385 |
| Batch cap | $6 per case (96 × $6 = $576 worst case) |
| crossmodel: 24 codex runs | OpenAI, billed separately |
| deterministic | $0 (already run) |
| Coiled VM time | ~24 small VMs × ≤30 min |

The $2–4 per invocation comes from E1/T1 critic telemetry. Wall time is about 30–40 min on Coiled with one VM per case.

**Smoke (2026-10-22):**
- **Run:** critique-methods on CH224202__p3, local, $1.76, 10 SDK turns, 275 s, 17 blockers (11 HIGH), schema OK.
- **Detection:** 1 of 3 planted defects detected in YAML.
  - benefit_total_population: caught in M-002 ("optimization.py:194 … total `population` instead of `u5`").
  - prose_stale_number: in prose only, not filed as a blocker (a checklist line notes "81,500 vs 66,830").
  - budget_overspend: missed. The deterministic arm catches it.
- **Matcher fix:** the smoke exposed one more spurious YAML match. M-011, on cost bases, cited results.md and contained "inconsistent". This led to the single-number rule above.
- **Cost:** $1.76 on a 3.8 MB core-only fixture puts the lower end at ≈ $170 for 96 invocations.

## Decision use (stated in advance)

1. **Retire or merge a critic** if it meets all three conditions:
   - its *unique* recall beyond the union of the other arms plus deterministic is < 0.05;
   - the CI upper bound is < 0.15;
   - its noise Δ is > 0.
2. **Move work into code.** For any class where deterministic recall is ≥ 0.8, stop prompting LLM critics for it and remove it from their duties.
3. **Fill coverage gaps.** For any class where union recall is < 0.5, add a targeted duty or a new gate, then re-test on held-out variants (new seed).
4. **Assess the cross-model arm.** If crossmodel adds unique recall ≥ 0.10, keep it in the default pipeline. Otherwise keep it opt-in.

## Limitations

- **Planted ≠ natural.** Planted defects are textual and local. Real defects, such as a wrong model structure or a misread paper, are harder. Recall here is an upper bound for those classes, and a lower bound for nothing.
- **Matcher validity.** It is lexical. A critic that explains a defect without the expected vocabulary is missed, and prose recall partly measures this. The audit sample is the check.
- **Natural defects in fixtures** add noise and can mask plants. For example, CH235631 already has zero-width CIs and renamed verdicts, so those classes weren't planted there. All fixtures produce 27–90 deterministic findings even when clean, mostly missing Phase-21 artifacts, so critics face a noisy baseline.
- **Some classes rarely plant.** double_counting (0) and benefit_total_population (1) rarely have a realistic generic target, so code-level classes are under-represented.
- **Historical fixtures are core-only.** They have no transcript evidence, so their file selection is the core list. Critics may lack files they would have read.
- **crossmodel on Coiled:** codex runs with a full-access sandbox on the VM, because the container can't run its OS sandbox. Workspace integrity is enforced by a before/after hash; a review that reads no files is UNAVAILABLE (commit b6ff936).
- **Local session environment.** Runs launched from inside a Claude Code session inherit `CLAUDE_CODE_*` env vars, which deny network Bash. Strip them, as the smoke command does, or use Coiled.
- **Single seed and round-1 only.** carried_forward dynamics and multi-round behaviour are out of scope.

## Execution (registered)

- **Code:** run from a clean worktree at the registering commit.
- **Backend:** Coiled, 24 workers (8 vCPU each), cases queued about 5 per worker.
- **Cases:** 5 LLM arms × 24 variants = 120. The deterministic arm was already run ($0).
- **Caps:** batch $600; per-case caps as listed in Arms.
- **Collection:** `collect --watch` runs until all are collected, then shuts the cluster down.
- **Matcher:** frozen at the registering commit.
- **Answer keys:** `_truth/` is never shipped to VMs.

**Pre-launch deviation (infra only, before any case ran):**
- **What happened:** the first launch attempt planned 0 cases (a zsh word-splitting error in my launch command). The second was refused by preflight: round-robin assignment uploaded each fixture 5×, 1.92 GB.
- **What changed:** worker assignment is now fixture-affine (commit 67ef501). Arms, matcher, fixtures and answer keys are unchanged.
- **Launch commit:** the commit that adds this note.

## Results (2026-10-22)

Two scorings are reported. **v1** is the matcher frozen at the registering commit. **v2** is the matcher revised after the audit (see *Matcher revision* below). Conclusions use v2. v1 is kept so the effect of the revision is visible.

### v1 (registered matcher)

**Run:** 120/120 LLM-arm cases ok, collected and sha256-verified; cluster shut down. Anthropic spend $172.85 (red-team $64.95, domain $52.99, methods $44.67, presentation $10.25); cross-model spend is on OpenAI. The deterministic arm was run during the build with unchanged validator code.

**Union of all 6 arms:** 32/54 = 0.59 (95% CI 0.46–0.71). Decision-critical defects: 11/16.

| Arm | Recall | Unique (no other arm) | Cost | $/detection |
|---|---|---|---|---|
| deterministic | 13/54 | 4 | $0 | 0 |
| red-team | 18/54 | 2 | $64.95 | 3.61 |
| methods | 11/54 | 0 | $44.67 | 4.06 |
| domain | 8/54 | 2 | $52.99 | 6.62 |
| presentation | 9/54 | 1 | $10.25 | 1.14 |
| cross-model | 12/54 | 1 | (OpenAI) | — |

**Best union by number of LLM critics** (validators always included):

| LLM critics | Union | Arms | Cost |
|---|---|---|---|
| 0 | 0.24 | deterministic only | $0 |
| 1 | 0.50 | + red-team | $65 |
| 2 | 0.56 | + domain | $118 |
| 3 | 0.57 | + presentation | $128 |
| 4 | 0.59 | + cross-model | $128 |
| 5 | 0.59 | + methods | $173 |

**Never detected by any arm:** ledger_mismatch 5, citation_mismatch 5, data_vintage 5, unit_error 3, zero_width_ci 2, prose_stale_number 2.

**Pre-registered decision rules (v1, superseded by v2 below):**
1. **Retire/merge.**
   - Domain meets all three conditions (unique 0.037, CI upper 0.125, noise Δ +1.22): **merge candidate**.
   - Methods has unique 0/54 (CI upper 0.066), but its noise Δ is −0.33, so it fails the noise condition by the letter. It is redundant in practice.
   - Red-team and presentation are kept.
2. **Move into code** (deterministic recall ≥ 0.8): budget_overspend (6/7) and renamed_verdict (5/5) come off LLM critic duties.
3. **Fill gaps** (union recall < 0.5): citation_mismatch, data_vintage, ledger_mismatch, unit_error. All four require checking a claim against its source or data rather than reading prose, which argues for deterministic cross-checks.
4. **Cross-model:** unique 1/54 < 0.10, so it **stays opt-in**.

### Matcher revision (v2)

**Deviation (logged).** The first-pass audit of `E5_audit_sample.md` (Claude, on the owner's instruction) found the v1 matcher was systematically under-crediting LLM critics. The owner approved revising the matcher and re-scoring every case at $0; no case was re-run. I couldn't recover the exact first-pass disagreement rate, so this is logged as an owner-approved deviation rather than one triggered by the >10% clause.

**What changed in `scripts/score_critiques.py`:**
1. **Short planted values count.** v1 only credited specific terms that looked like identifiers (≥6 chars plus a digit or symbol), so planted values like "2,078", "2017" and "1.55" could never match. v2 credits any value with a digit and ≥3 characters, or an identifier.
2. **Years need the entry's name.** v2's first rescore matched "Protopopoff 2018" against a planted "Thwing … 2018", in clean variants and planted ones alike. A year now counts only if the blocker also names the entry as a whole word. Names under 3 characters ("ID", "JA", "RF") can't anchor, so for those the year still stands alone.
3. **Vocabulary.** "breach" was added for budget_overspend.
4. **Adjudications.** `E5_adjudication.yaml` overrides the automatic call where the matcher can't decide, with the reason recorded and an `adjudicated` flag on the row:
   - **A1** (methods, CM213310__p3#3) → *not* detected. The critic adopted the invented verdict as true.
   - **A2** (methods, CH161312__p1#1) → detected. "calls the verdict both robust and unstable".
   - **A5** (domain, CH235631__p3#1) → detected. It names the entry by citation key ("c10 header says 2018 but the paper is 2024").

   A3 was judged not a catch, and A4 is now matched automatically. All of these are Claude's first-pass calls, and the owner may reverse any of them.

**Checks.** Self-tests V2a–V2f pass. Spurious-match rates on clean variants equal v1's in every arm (deterministic 0.000, red-team 0.019, domain 0.037, presentation 0.037, cross-model 0.037, methods 0.074). The deterministic arm is unchanged (13/54).

### v2 results (used for decisions)

**Union of all 6 arms:** 40/54 = 0.74 (95% CI 0.61–0.84), up from 0.59 in v1. Decision-critical defects: 11/16, unchanged.

| Arm | Recall v2 (v1) | Unique v2 (v1) | Unique 95% CI | Noise Δ | Cost | $/detection |
|---|---|---|---|---|---|---|
| deterministic | 13/54 (13) | 5 (4) | 0.040–0.199 | +0.06 | $0 | 0 |
| red-team | 24/54 (18) | 4 (2) | 0.029–0.176 | −4.22 | $64.95 | 2.71 |
| methods | 12/54 (11) | 0 (0) | 0.000–0.066 | −0.39 | $44.67 | 3.72 |
| domain | 12/54 (8) | 4 (2) | 0.029–0.176 | +1.06 | $52.99 | 4.42 |
| presentation | 9/54 (9) | 1 (1) | 0.003–0.098 | −0.11 | $10.25 | 1.14 |
| cross-model | 14/54 (12) | 1 (1) | 0.003–0.098 | +0.72 | (OpenAI) | — |

Red-team vs each other critic, counting defects caught by one arm and missed by the other (red-team-only vs other-only): methods 14 vs 2, domain 17 vs 5, presentation 17 vs 2, cross-model 16 vs 6.

**Best union by number of LLM critics** (validators always included):

| LLM critics | Union v2 (v1) | Arms | Cost |
|---|---|---|---|
| 0 | 0.24 (0.24) | deterministic only | $0 |
| 1 | 0.61 (0.50) | + red-team | $65 |
| 2 | 0.70 (0.56) | + domain | $118 |
| 3 | 0.72 (0.57) | + cross-model | $118 |
| 4 | 0.74 (0.59) | + presentation | $128 |
| 5 | 0.74 (0.59) | + methods | $173 |

**Union recall by class:**

| Class | v2 union recall |
|---|---|
| budget_overspend | 7/7 |
| renamed_verdict | 5/5 |
| sensitivity_contradiction | 6/6 |
| ci_inverted | 4/4 |
| benefit_total_population | 1/1 |
| prose_stale_number | 6/7 |
| citation_mismatch | 5/6 |
| zero_width_ci | 2/4 |
| data_vintage | 2/5 |
| unit_error | 1/4 |
| ledger_mismatch | 1/5 |

**Pre-registered decision rules, v2 (changes from v1 in bold):**
1. **Retire/merge.**
   - **Domain is no longer a merge candidate.** Its unique recall is 4/54 = 0.074 (≥ 0.05) and the CI upper bound is 0.176 (≥ 0.15). This reverses v1. Its 4 unique catches are 3 citation_mismatch (one via adjudication A5) and 1 sensitivity_contradiction. The result is fragile. Without A5 it is 3/54 = 0.056 with CI upper 0.151, which still passes but only just. If citation checks move into code, most of domain's unique value goes with them.
   - Methods: unique 0/54 (CI upper 0.066), but noise Δ −0.39, so it fails the noise condition by the letter. Unchanged from v1. It is redundant in practice: adding it to the other five arms gains nothing for $45.
   - Presentation: unique 0.019 and CI upper 0.098, but noise Δ −0.11, so it is kept by the letter.
   - Red-team is kept. Its unique catches are 2 data_vintage, 1 budget_overspend and 1 ledger_mismatch.
   - Cross-model technically meets all three retire conditions (unique 0.019, CI upper 0.098, noise +0.72). It is already opt-in, so nothing changes.
2. **Move into code** (deterministic recall ≥ 0.8): budget_overspend (6/7) and renamed_verdict (5/5). Unchanged from v1.
3. **Fill gaps** (union recall < 0.5): **data_vintage (2/5), ledger_mismatch (1/5), unit_error (1/4).**
   - **citation_mismatch leaves the gap list** (5/6, v1 0/6).
   - zero_width_ci is at 2/4 = 0.50, on the threshold, so not a gap by the letter.
4. **Cross-model:** unique 1/54 < 0.10, so it **stays opt-in**. Unchanged from v1.

**Interpretation.**
- Red-team alone gets 0.61. Adding domain gets 0.70 for $118, which is 68% of the full-panel cost.
- Methods adds nothing that another arm misses, so it is the clearest saving: about $45 per round, 26% of LLM critic spend.
- The remaining gap classes all require checking a value against its source or data (ledger vs artifact, units vs data, data year vs decision year). That argues for deterministic cross-checks rather than more prompting.
- **Lower-bound caveat:** the matcher is still lexical, so LLM recall here remains a lower bound.

**Exploratory (post hoc, not pre-registered):** validators re-run at round 3 instead of round 1 reach 14/54 (+1 ledger_mismatch). The round-1 reproduction matched the original 13/54 exactly. Round gating is not the main reason for the validator gaps. Why the provenance check catches only 1/5 planted ledger mismatches needs a focused look.
