## Paired replay comparison: `contract-packet` vs `control`

6 paired cases (fixture × seed). Delta = B − A; 95% bootstrap CI over pairs.

| Metric | n | A | B | Δ (B−A) | 95% CI | B better? |
|---|---|---|---|---|---|---|
| artifact_rate | 6 | 0.381 | 0.5 | +0.119 | [-0.31, +0.5] | yes (CI spans 0) |
| compare_clean | 6 | 0.167 | 0.5 | +0.333 | [+0, +0.667] | yes (CI spans 0) |
| completed_ok | 6 | 1 | 0.833 | -0.167 | [-0.5, +0] | no (CI spans 0) |
| cost_usd ↓ | 5 | 9.93 | 7.31 | -2.62 | [-4.56, -0.698] | yes |
| effort_high_violations ↓ | 6 | 0 | 0.667 | +0.667 | [+0.333, +1] | no |
| eligibility_high ↓ | 6 | 0.833 | 0.667 | -0.167 | [-0.667, +0.333] | yes (CI spans 0) |
| framework_ok | 6 | 1 | 1 | +0 | [+0, +0] |  |
| identifiability_clean | 6 | 0 | 0 | +0 | [+0, +0] |  |
| input_tokens ↓ | 6 | 1.94e+07 | 1.11e+07 | -8.31e+06 | [-8.58e+06, -8.04e+06] | yes |
| start_context | 6 | 1.81e+05 | 3.21e+04 | -1.49e+05 | [-1.54e+05, -1.44e+05] | no |
| turns ↓ | 6 | 60 | 59.7 | -0.333 | [-1, +0] | yes (CI spans 0) |
| wall_s ↓ | 6 | 2.1e+03 | 2.62e+03 | +514 | [-529, +1.8e+03] | no (CI spans 0) |

↓ = lower is better.