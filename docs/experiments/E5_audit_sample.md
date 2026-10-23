### crossmodel · CB1__p1#2 · ledger_mismatch · detected=False
- planted: Ledger claim `sc_delivery_cost360_n_changed` value changed 248 -> 340; its source artifact still says 248.
- reasons: None
- blocker: None

### crossmodel · CB1__p2#1 · sensitivity_contradiction · detected=False
- planted: results.md asserts sensitivity verdict ROBUST; models/sensitivity_analysis.yaml says UNSTABLE.
- reasons: None
- blocker: None

### crossmodel · CB1__p3#1 · prose_stale_number · detected=False
- planted: results.md line 398: 1,000 changed to 820; artifacts still support 1,000.
- reasons: None
- blocker: None

### crossmodel · CB1__p3#3 · budget_overspend · detected=True
- planted: models/allocation_lga.csv: `cost_usd` scaled x1.55 on 484 funded rows; total exceeds the budget.
- reasons: {'cites': ['models/allocation_lga.csv', 'cost_usd'], 'specific': [], 'generic': ['budget', 'exceed']}
- blocker: x-001 1 false high hard_blocker analyze the canonical allocation csv exceeds the $320m budget by approximately $176m and conflicts with its delivered copy. summing cost_usd gives $495,981,504.10 in models/allocation_lga.csv versus $319,988,065 in results/allocation_lga.csv. all 484 funded rows have approximately 1.55 times the cost in the canonical file, with identical packages and benefits. at line 20, gireri costs $846,202.30 versus $545,937; econ.py:325–328 reconstructs $545,936.82 from its population and registered prices. allocation_scenarios.yaml:5 nevertheless reports $319,988,066. reco

### crossmodel · CB2__p1#2 · ledger_mismatch · detected=False
- planted: Ledger claim `bm6_dual_ai_increment_vs_pyrethroid_only_observed` value changed 47.1 -> 64.5; its source artifact still says 47.1.
- reasons: None
- blocker: None

### crossmodel · CB2__p1#3 · sensitivity_contradiction · detected=True
- planted: results.md asserts sensitivity verdict ROBUST; models/sensitivity_analysis.yaml says UNSTABLE.
- reasons: {'cites': ['results.md', 'models/sensitivity_analysis.yaml'], 'specific': ['verdict is ROBUST'], 'generic': ['contradict', 'verdict', 'unstable']}
- blocker: x-006 1 false medium presentation write the report incorrectly declares the sensitivity analysis robust. results.md:31 states that the sensitivity verdict is robust. models/sensitivity_analysis.yaml:3934-3938 explicitly records unstable, with 44 of the top 50 policy assignments changing. models/model_rigor.py:313-320 classifies worst changes above 30 as unstable even when the dominant net-class recommendation never flips. results.md:30 also contradicts line 31. remove or correct the robust verdict. describe the unchanged dominant net class separately from the unstable lga package and smc decis

### crossmodel · CB2__p2#3 · prose_stale_number · detected=False
- planted: results.md line 11: 2,535 changed to 2,078; artifacts still support 2,535.
- reasons: None
- blocker: None

### crossmodel · CH161312__p2#1 · zero_width_ci · detected=False
- planted: 95% interval for `smc_parasitemia_rr` collapsed to the point estimate (0.38000869107221724) although draws vary.
- reasons: None
- blocker: None

### crossmodel · CH161312__p3#1 · renamed_verdict · detected=False
- planted: model_comparison_formal.yaml verdict `CLEAN` replaced by the invented `VALIDATED_WITH_NOTES`.
- reasons: None
- blocker: None

### crossmodel · CH161312__p3#2 · budget_overspend · detected=False
- planted: allocation_optimized.csv: `annual_cost` scaled x1.55 on 773 funded rows; total exceeds the budget.
- reasons: None
- blocker: None

### crossmodel · CH224202__p1#2 · citation_mismatch · detected=False
- planted: citations.md line 68: Chandramohan year 2020 changed to 2014.
- reasons: None
- blocker: None

### crossmodel · CH224202__p3#1 · prose_stale_number · detected=False
- planted: results.md line 278: 81,500 changed to 66,830; artifacts still support 81,500.
- reasons: None
- blocker: None

### crossmodel · CH224202__p3#2 · budget_overspend · detected=True
- planted: models/allocation_detail.csv: `cost` scaled x1.55 on 774 funded rows; total exceeds the budget.
- reasons: {'cites': ['models/allocation_detail.csv'], 'specific': [], 'generic': ['budget', 'exceed']}
- blocker: x-002 1 false high data data the designated canonical allocation exceeds the budget and contradicts the other allocation artifacts. models/sanity_schema.yaml:48 designates models/allocation_detail.csv as canonical. its cost column totals $495,994,629.781, versus $319,996,534.944 in allocation.csv for identical lga-package assignments. every detail cost is approximately 1.55 times the corresponding package-evaluation cost. for kura, models/allocation_detail.csv:2 records $746,510.4544, while allocation.csv:396 and models/package_evaluation.csv:2527 record $481,619.648. recomputed detail cost pe

### crossmodel · CM213310__p3#1 · zero_width_ci · detected=False
- planted: 95% interval for `cfr_age_lt1` collapsed to the point estimate (0.03027644490211293) although draws vary.
- reasons: None
- blocker: None

### deterministic · CB1__p1#1 · renamed_verdict · detected=True
- planted: model_comparison_formal.yaml verdict `CLEAN` replaced by the invented `VALIDATED_WITH_NOTES`.
- reasons: {'cites': ['model_comparison_formal.yaml'], 'specific': ['VALIDATED_WITH_NOTES'], 'generic': ['verdict', 'unknown', 'invented', 'edited']}
- blocker: det-008 high model_comparison_formal_unknown_verdict multi_structural/model_comparison_formal_unknown_verdict: `model_comparison_formal.yaml` has verdict: 'validated_with_notes', which is not in the producing script's vocabulary ['clean', 'degenerate_fit_detected', 'insufficient_structures']. hand-edited or invented verdicts are treated as failing; re-run the producing script instead of editing its output. {"reviewer": "rigor-artifacts", "id": "rig-008", "category": "methods", "target_stage": "model", "first_seen_round": 1, "kind": "model_comparison_formal_unknown_verdict", "scope_declarable":

### deterministic · CB1__p1#3 · zero_width_ci · detected=False
- planted: 95% interval for `smc_clinical_incidence_rr` collapsed to the point estimate (0.2698368365980352) although draws vary.
- reasons: None
- blocker: None

### deterministic · CB2__p3#2 · budget_overspend · detected=False
- planted: models/round1_closed/lga_allocation.csv: `cost` scaled x1.55 on 773 funded rows; total exceeds the budget.
- reasons: None
- blocker: None

### deterministic · CH161312__p3#1 · renamed_verdict · detected=True
- planted: model_comparison_formal.yaml verdict `CLEAN` replaced by the invented `VALIDATED_WITH_NOTES`.
- reasons: {'cites': ['model_comparison_formal.yaml'], 'specific': ['VALIDATED_WITH_NOTES'], 'generic': ['verdict', 'unknown', 'invented', 'edited']}
- blocker: det-003 high model_comparison_formal_unknown_verdict multi_structural/model_comparison_formal_unknown_verdict: `model_comparison_formal.yaml` has verdict: 'validated_with_notes', which is not in the producing script's vocabulary ['clean', 'degenerate_fit_detected', 'insufficient_structures']. hand-edited or invented verdicts are treated as failing; re-run the producing script instead of editing its output. {"reviewer": "rigor-artifacts", "id": "rig-002", "category": "methods", "target_stage": "model", "first_seen_round": 1, "kind": "model_comparison_formal_unknown_verdict", "scope_declarable":

### deterministic · CH161312__p3#3 · prose_stale_number · detected=False
- planted: results.md line 27: 184,800 changed to 151,536; artifacts still support 184,800.
- reasons: None
- blocker: None

### deterministic · CH224202__p1#1 · data_vintage · detected=False
- planted: data_provenance.md line 42: NMIS data year 2021 changed to 2007.
- reasons: None
- blocker: None

### deterministic · CH224202__p1#2 · citation_mismatch · detected=False
- planted: citations.md line 68: Chandramohan year 2020 changed to 2014.
- reasons: None
- blocker: None

### deterministic · CH224202__p2#1 · zero_width_ci · detected=True
- planted: 95% interval for `cost_per_daly` collapsed to the point estimate (62.28368098461769) although draws vary.
- reasons: {'cites': ['cost_per_daly'], 'specific': [], 'generic': ['degenerate', 'interval', 'point estimate']}
- blocker: det-001 high ci_degenerate uq/ci_degenerate: cost_per_daly 95% ci [62.2837, 62.2837] is 0.000% of mean (62.2837) after 200 draws. either the sampled parameters never reach this output (frozen in outcome_fn, surrogate flattening, parameters not wired through), or it is deterministic by design — then declare it in models/uq_declarations.yaml `deterministic_outputs:` with a reason and report it as a point estimate. any interval quoted elsewhere for this output is unsupported. {"reviewer": "rigor-artifacts", "id": "rig-001", "category": "methods", "target_stage": "model", "first_seen_round": 1, "k

### deterministic · CH224202__p3#2 · budget_overspend · detected=True
- planted: models/allocation_detail.csv: `cost` scaled x1.55 on 774 funded rows; total exceeds the budget.
- reasons: {'cites': ['models/allocation_detail.csv'], 'specific': [], 'generic': ['budget', 'envelope', 'exceed', 'more than']}
- blocker: det-001 high budget_exceeded budget_exceeded: sum(cost) from models/allocation_detail.csv = $495,994,630 = 155.0% of $320m envelope ($320,000,000). an allocation that spends more than the stated budget is not implementable; the optimizer's budget constraint is missing or not binding. {"reviewer": "spec-compliance", "id": "obj-001", "category": "structural", "target_stage": "model", "first_seen_round": 1, "kind": "budget_exceeded", "scope_declarable": true}

### deterministic · CM213310__p1#1 · unit_error · detected=False
- planted: data/kenya_confirmed_cases_2003_2016_kisangau2018_table2.csv: `incidence_per_million` multiplied by 100 on 183 rows (unit change, header unchanged).
- reasons: None
- blocker: None

### deterministic · CM213310__p1#2 · prose_stale_number · detected=False
- planted: results.md line 332: 50,000 changed to 41,000; artifacts still support 50,000.
- reasons: None
- blocker: None

### deterministic · CM213310__p3#2 · sensitivity_contradiction · detected=False
- planted: results.md asserts sensitivity verdict ROBUST; models/sensitivity_analysis.yaml says SENSITIVE.
- reasons: None
- blocker: None

### domain · CB1__p3#2 · citation_mismatch · detected=False
- planted: citations.md line 14: ID year 2023 changed to 2017.
- reasons: None
- blocker: None

### domain · CB2__p2#3 · prose_stale_number · detected=False
- planted: results.md line 11: 2,535 changed to 2,078; artifacts still support 2,535.
- reasons: None
- blocker: None

### domain · CB2__p3#2 · budget_overspend · detected=False
- planted: models/round1_closed/lga_allocation.csv: `cost` scaled x1.55 on 773 funded rows; total exceeds the budget.
- reasons: None
- blocker: None

### domain · CB2__p3#3 · unit_error · detected=False
- planted: data/processed/lga_master_774.csv: `map_pfpr2024` multiplied by 100 on 774 rows (unit change, header unchanged).
- reasons: None
- blocker: None

### domain · CH161312__p1#2 · data_vintage · detected=False
- planted: data_provenance.md line 14: NMIS data year 2021 changed to 2007.
- reasons: None
- blocker: None

### domain · CH161312__p2#1 · zero_width_ci · detected=False
- planted: 95% interval for `smc_parasitemia_rr` collapsed to the point estimate (0.38000869107221724) although draws vary.
- reasons: None
- blocker: None

### domain · CH161312__p2#2 · prose_stale_number · detected=False
- planted: results.md line 48: 184,800 changed to 151,536; artifacts still support 184,800.
- reasons: None
- blocker: None

### domain · CH161312__p2#3 · citation_mismatch · detected=False
- planted: citations.md line 51: Thwing year 2024 changed to 2018.
- reasons: None
- blocker: None

### domain · CH161312__p3#1 · renamed_verdict · detected=False
- planted: model_comparison_formal.yaml verdict `CLEAN` replaced by the invented `VALIDATED_WITH_NOTES`.
- reasons: None
- blocker: None

### domain · CH161312__p3#3 · prose_stale_number · detected=False
- planted: results.md line 27: 184,800 changed to 151,536; artifacts still support 184,800.
- reasons: None
- blocker: None

### domain · CH224202__p1#3 · ci_inverted · detected=False
- planted: `total_cost` ci_low set to 367999952.25572461, above both the mean and ci_high (inverted interval).
- reasons: None
- blocker: None

### domain · CH224202__p3#1 · prose_stale_number · detected=False
- planted: results.md line 278: 81,500 changed to 66,830; artifacts still support 81,500.
- reasons: None
- blocker: None

### domain · CH235631__p1#3 · sensitivity_contradiction · detected=True
- planted: results.md asserts sensitivity verdict ROBUST; models/sensitivity_analysis.yaml says SENSITIVE.
- reasons: {'cites': ['results.md', 'models/sensitivity_analysis.yaml'], 'specific': [], 'generic': ['contradict', 'verdict', 'sensitive']}
- blocker: d-015 low presentation analyze 1 the executive summary contradicts itself on the sensitivity verdict (sensitive vs robust). results.md lines 9-10. state a single verdict consistent with sensitivity_analysis.yaml. false

### domain · CH235631__p2#1 · budget_overspend · detected=False
- planted: models/allocation_optimized.csv: `cost` scaled x1.55 on 406 funded rows; total exceeds the budget.
- reasons: None
- blocker: None

### domain · CH235631__p3#2 · budget_overspend · detected=False
- planted: models/allocation_optimized.csv: `cost` scaled x1.55 on 406 funded rows; total exceeds the budget.
- reasons: None
- blocker: None

### domain · CH235631__p3#3 · ledger_mismatch · detected=False
- planted: Ledger claim `nmis_nw_pfpr` value changed 51.6 -> 70.7; its source artifact still says 51.6.
- reasons: None
- blocker: None

### domain · CM213310__p1#3 · citation_mismatch · detected=False
- planted: citations.md line 342: RF year 2008 changed to 2002.
- reasons: None
- blocker: None

### domain · CM213310__p2#2 · ledger_mismatch · detected=False
- planted: Ledger claim `h5_d022_res_pct` value changed 30.0 -> 41.1; its source artifact still says 30.0.
- reasons: None
- blocker: None

### methods · CB1__p1#3 · zero_width_ci · detected=False
- planted: 95% interval for `smc_clinical_incidence_rr` collapsed to the point estimate (0.2698368365980352) although draws vary.
- reasons: None
- blocker: None

### methods · CB2__p3#3 · unit_error · detected=False
- planted: data/processed/lga_master_774.csv: `map_pfpr2024` multiplied by 100 on 774 rows (unit change, header unchanged).
- reasons: None
- blocker: None

### methods · CH161312__p1#1 · sensitivity_contradiction · detected=False
- planted: results.md asserts sensitivity verdict ROBUST; models/sensitivity_analysis.yaml says SENSITIVE.
- reasons: None
- blocker: None

### methods · CH224202__p2#1 · zero_width_ci · detected=True
- planted: 95% interval for `cost_per_daly` collapsed to the point estimate (62.28368098461769) although draws vary.
- reasons: {'cites': ['uncertainty_report.yaml', 'cost_per_daly'], 'specific': [], 'generic': ['degenerate', 'ci_low', 'ci_high', 'interval']}
- blocker: m-007 high hard_blocker model 1 the headline cost-per-daly interval is invalid. uncertainty_report.yaml cost_per_daly has ci_low = ci_high = mean = 62.28 (degenerate), and the "$31-$120" in results.md cannot be traced to any artifact. calibration/baseline-pfpr uncertainty is not propagated.
 uncertainty_report.yaml scalar_outputs.cost_per_daly; results.md executive summary fix the uq summarizer, regenerate the ci from draws, add calibration-replicate and within-archetype pfpr uncertainty. false

### methods · CH235631__p1#3 · sensitivity_contradiction · detected=True
- planted: results.md asserts sensitivity verdict ROBUST; models/sensitivity_analysis.yaml says SENSITIVE.
- reasons: {'cites': ['results.md', 'models/sensitivity_analysis.yaml'], 'specific': [], 'generic': ['verdict', 'sensitive']}
- blocker: m-009 high methods analyze 1 sensitivity verdict is misreported. at llin or 0.32, smc lgas fall from 245 to 110, and at smc irr 0.35, 58 lgas switch package. these are changes to the primary deliverable (which lga gets which package) but are logged as primary_recommendation_changes=false. results.md also says both "robust" and "sensitive".
 models/sensitivity_analysis.yaml sa-001/sa-002; results.md lines 9-10, §sensitivity reclassify package switches as recommendation changes; remove the robust claim; extend sa to pbo or, costs, coverage, multipliers. false

### methods · CH235631__p3#3 · ledger_mismatch · detected=False
- planted: Ledger claim `nmis_nw_pfpr` value changed 51.6 -> 70.7; its source artifact still says 51.6.
- reasons: None
- blocker: None

### methods · CM213310__p3#3 · renamed_verdict · detected=True
- planted: model_comparison_formal.yaml verdict `CLEAN` replaced by the invented `VALIDATED_WITH_NOTES`.
- reasons: {'cites': ['model_comparison_formal.yaml'], 'specific': ['VALIDATED_WITH_NOTES'], 'generic': ['verdict']}
- blocker: m-005 medium methods analyze 1 residual diagnostics are over-interpreted. the residuals show systematic runs (2015-2017 over-predicted 3-6x; 2018/2022 under-predicted ~3.5-4x), and an n=12 acf bound of ±0.566 has almost no power. results.md calls the model-comparison verdict clean, but the file says validated_with_notes. figures/residuals_combined.png; results.md §2.3, table 3 caption; model_comparison_formal.yaml verdict describe the runs as misspecification of post-outbreak dynamics, add a runs test or acf power statement, and correct the verdict label. false

### presentation · CB1__p1#1 · renamed_verdict · detected=False
- planted: model_comparison_formal.yaml verdict `CLEAN` replaced by the invented `VALIDATED_WITH_NOTES`.
- reasons: None
- blocker: None

### presentation · CB1__p1#2 · ledger_mismatch · detected=False
- planted: Ledger claim `sc_delivery_cost360_n_changed` value changed 248 -> 340; its source artifact still says 248.
- reasons: None
- blocker: None

### presentation · CB1__p2#2 · unit_error · detected=False
- planted: data/state_targets.csv: `pfpr_rdt_2015_lo` multiplied by 100 on 44 rows (unit change, header unchanged).
- reasons: None
- blocker: None

### presentation · CB2__p1#2 · ledger_mismatch · detected=False
- planted: Ledger claim `bm6_dual_ai_increment_vs_pyrethroid_only_observed` value changed 47.1 -> 64.5; its source artifact still says 47.1.
- reasons: None
- blocker: None

### presentation · CB2__p1#3 · sensitivity_contradiction · detected=True
- planted: results.md asserts sensitivity verdict ROBUST; models/sensitivity_analysis.yaml says UNSTABLE.
- reasons: {'cites': ['results.md'], 'specific': [], 'generic': ['contradict', 'verdict', 'unstable']}
- blocker: p-008 medium presentation analyze 1 internal contradictions and undefined abbreviations: key messages say sensitivity is unstable then robust; h5 is refuted while table 5 shows loao passing; abm, paar, pi, uq, wmr, ilp, anc/epi, cfr, hbhi, list are not defined. results.md lines 30-31, h5 verdict and table 5, abbreviations paragraph remove the contradictory sentence, reconcile the h5 verdict wording, and extend the abbreviation list. false

### presentation · CH161312__p3#3 · prose_stale_number · detected=True
- planted: results.md line 27: 184,800 changed to 151,536; artifacts still support 184,800.
- reasons: {'cites': ['results.md'], 'specific': ['151,536'], 'generic': []}
- blocker: p-007 medium presentation analyze 1 numerical inconsistencies in results.md: deaths 184,800 vs 151,536; pbo $/daly ~$105 vs $144 vs figure $58-187; rmse 1.12 vs 2.99; approximate '~' values in key tables; 'supported' verdicts for h3/h4 vs sensitivity flip; eight redundant appendix eda figures. results.md lines on introduction, section 3, 5, 12, 16; appendix figures 10-18 reconcile numbers, replace '~' with computed values, soften verdicts, and trim/merge appendix figures. false

### presentation · CH235631__p1#2 · data_vintage · detected=False
- planted: data_provenance.md line 60: NMIS data year 2021 changed to 2007.
- reasons: None
- blocker: None

### presentation · CH235631__p2#1 · budget_overspend · detected=False
- planted: models/allocation_optimized.csv: `cost` scaled x1.55 on 406 funded rows; total exceeds the budget.
- reasons: None
- blocker: None

### presentation · CH235631__p2#2 · ledger_mismatch · detected=False
- planted: Ledger claim `dalys_uq_ci_high` value changed 2900144 -> 3973197; its source artifact still says 2900144.
- reasons: None
- blocker: None

### presentation · CH235631__p3#1 · citation_mismatch · detected=False
- planted: citations.md line 77: Thwing year 2024 changed to 2018.
- reasons: None
- blocker: None

### presentation · CM213310__p2#3 · data_vintage · detected=False
- planted: data_provenance.md line 50: survey data year 2018 changed to 2004.
- reasons: None
- blocker: None

### redteam · CH161312__p2#1 · zero_width_ci · detected=True
- planted: 95% interval for `smc_parasitemia_rr` collapsed to the point estimate (0.38000869107221724) although draws vary.
- reasons: {'cites': ['uncertainty_report.yaml', 'smc_parasitemia_rr'], 'specific': [], 'generic': ['collapsed', 'ci_low', 'ci_high']}
- blocker: r-012 medium methods analyze 1 the uncertainty ci is too narrow. attack_rate_mult (registered ci 1.0-2.0, linear in dalys, ±33%) is not sampled. the smc_parasitemia_rr prior collapsed to a point (ci_low = ci_high = 0.38). smc_clinical_rr is sampled but unused. lga pfpr and population are not sampled.
 uncertainty_report.yaml parameter_samples; citations.md registry attack_rate_mult; outcome_fn.py:114-135 propagate attack_rate_mult, a working smc prior, and pfpr/population uncertainty. report the resulting ci.
 false

### redteam · CH161312__p2#2 · prose_stale_number · detected=True
- planted: results.md line 48: 184,800 changed to 151,536; artifacts still support 184,800.
- reasons: {'cites': ['results.md'], 'specific': ['151,536'], 'generic': ['inconsistent']}
- blocker: r-011 medium hard_blocker model 1 further cross-file inconsistencies. (a) within_zone_heterogeneity.yaml cases_averted 8.39m vs 44.25m in the csv, plus an unsupported "shift to smc in ne". (b) calibration_results.json was not produced by the current model_run.py (key semantics and fields differ). (c) the pbo or 0.169 derivation has three inconsistent formulas (0.168, 0.225, "0.163 rounded to 0.169"). (d) nigeria deaths given as 184,800 and 151,536 in the same document. (e) the map legend totals 784 > 774 polygons (pbo 696 vs csv 691), so some lgas show the wrong package. (f) data/ is absent fr

### redteam · CH161312__p3#1 · renamed_verdict · detected=False
- planted: model_comparison_formal.yaml verdict `CLEAN` replaced by the invented `VALIDATED_WITH_NOTES`.
- reasons: None
- blocker: None

### redteam · CH161312__p3#3 · prose_stale_number · detected=True
- planted: results.md line 27: 184,800 changed to 151,536; artifacts still support 184,800.
- reasons: {'cites': ['results.md'], 'specific': ['151,536'], 'generic': []}
- blocker: r-016 medium hard_blocker analyze 1 several report numbers do not match their sources. (a) h2's "smc $89-95/daly" does not appear in the matrix; marginal smc is $3,349/daly. (b) the package table lists irs "+0.35m (marginal)", but the true marginal is 0.106m. (c) wmr 2024 nigeria deaths are given as 151,536 (line 27) and 184,800 (line 48). (d) the h1 figure title shows +5.5% while the text says +5.0%. (e) decision_rule.md claims "accuracy vs optimizer: 100%" for a population-priority rule that does not describe the optimizer (see r-007).
 results.md:27,48,56,69,167; figures/h1_targeting_compar

### redteam · CH224202__p2#1 · zero_width_ci · detected=True
- planted: 95% interval for `cost_per_daly` collapsed to the point estimate (62.28368098461769) although draws vary.
- reasons: {'cites': ['uncertainty_report.yaml', 'cost_per_daly'], 'specific': [], 'generic': ['ci_low', 'ci_high']}
- blocker: r-007 high hard_blocker analyze 1 the robustness evidence is contradictory, physically impossible, or unreproducible: (a) figure 8 shows more cases averted when costs rise 20% and fewer when they fall 20%, in every zone. under a fixed budget the direction must be the reverse, and sa-005 itself reports a lower objective at +10%. (b) h7 claims spearman rho = 1.00 under ±20%, yet sensitivity_analysis.yaml reports rank_change = 43 for sa-005 at ±20%. h7's "proof" ignores the budget constraint. (c) results.md says the sa verdict is unstable with 3 flips; sensitivity_analysis.yaml says sensitive wit

### redteam · CH224202__p3#1 · prose_stale_number · detected=True
- planted: results.md line 278: 81,500 changed to 66,830; artifacts still support 81,500.
- reasons: {'cites': ['results.md'], 'specific': ['66,830'], 'generic': ['discrepan']}
- blocker: r-005 high hard_blocker analyze 1 the aggregate impact claims fail external sanity checks. 81,525 deaths averted per year is 44% of all malaria deaths in nigeria (who wmr 2024: 30.9% of 597k, about 184.5k; c16: 184,800), from one donor line. 26.1m cases is 38% of all national cases. the model's own baseline mortality, 203.8k deaths, already exceeds who by 10%. the scott 2017 benchmark is a category error: scott's 84k deaths over 5 years is the incremental gain from reallocating a fixed budget, not total deaths averted by that budget. the "scott ~26,800/yr at ~$235m" figure behind the "2.2x per

### redteam · CH235631__p3#1 · citation_mismatch · detected=False
- planted: citations.md line 77: Thwing year 2024 changed to 2018.
- reasons: None
- blocker: None

### redteam · CM213310__p1#2 · prose_stale_number · detected=False
- planted: results.md line 332: 50,000 changed to 41,000; artifacts still support 50,000.
- reasons: None
- blocker: None

### redteam · CM213310__p2#2 · ledger_mismatch · detected=False
- planted: Ledger claim `h5_d022_res_pct` value changed 30.0 -> 41.1; its source artifact still says 30.0.
- reasons: None
- blocker: None

### redteam · CM213310__p3#2 · sensitivity_contradiction · detected=False
- planted: results.md asserts sensitivity verdict ROBUST; models/sensitivity_analysis.yaml says SENSITIVE.
- reasons: None
- blocker: None

