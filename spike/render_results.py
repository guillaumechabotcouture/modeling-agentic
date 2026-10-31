#!/usr/bin/env python3
"""Render the spike's comparison tables from artifacts into spike/RESULTS.md (between the
`<!-- tables:start -->` and `<!-- tables:end -->` markers), so no table number is typed.

    python3 spike/render_results.py [--analysis spike/out/analysis.json] [--spend spike/out/spend.json]
"""
import argparse
import glob
import json
import os
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
A8_RUN = "~/modeling-agentic-keep/e7r8_cloud/R1/runs/2026-10-28_200327_build-an-agent-based-model-of-malaria-tr"


def loc(paths):
    n = 0
    for p in paths:
        for f in glob.glob(os.path.join(ROOT, p), recursive=True):
            if os.path.isfile(f):
                n += sum(1 for line in open(f, encoding="utf-8") if line.strip())
    return n


def fmt(x, d=3):
    if x is None:
        return "n/a"
    if isinstance(x, (int, float)):
        if abs(x) >= 1e5:
            return f"{x / 1e6:.2f}M"
        return f"{x:.{d}g}" if abs(x) < 1000 else f"{x:,.0f}"
    return str(x)


def bench_table(res, a8):
    rows = ["| ID | Benchmark | Target (band) | Spike engine | In band | Role | Attempt 8 |",
            "|---|---|---|---|---|---|---|"]
    for b in res["benchmarks"]:
        band = f"{fmt(b['target'])} ({fmt(b['band'][0])}–{fmt(b['band'][1])})" if b.get("band") else "rules"
        comp = fmt(b["computed"])
        if b.get("points"):
            comp = f"{b['rules_met']}/{b['rules_checked']} rules met; points (EIR, PfPR2-10): " + \
                   ", ".join(f"({x:g}, {y:.2f})" for x, y in b["points"])
        a = a8.get(b["id"])
        a_s = "n/a" if a is None else f"{fmt(a['computed'])} ({'in' if a['in_band'] else 'out'})"
        rows.append(f"| {b['id']} | {b['metric']} | {band} | {comp} | {'yes' if b['in_band'] else '**no**'} | "
                    f"{b['role']} | {a_s} |")
    s = res["summary"]
    rows.append("")
    rows.append(f"In band: {s['in_band']} of {s['total']} (held out: {s['held_out_in_band']} of {s['held_out_total']}). "
                f"{res['n_sims']} engine runs, {len(res['seeds'])} seeds per arm (common random numbers).")
    return "\n".join(rows)


def a8_bench(run_dir):
    p = os.path.join(os.path.expanduser(run_dir), "models", "benchmark_computed.yaml")
    if not os.path.exists(p):
        return {}
    out = {}
    for b in yaml.safe_load(open(p))["benchmarks"]:
        v = b.get("computed_value")
        if v is None:
            continue
        if "tolerance_abs" in b:
            lo, hi = b["target_value"] - b["tolerance_abs"], b["target_value"] + b["tolerance_abs"]
        else:
            lo, hi = b["target_value"] / b["tolerance_factor"], b["target_value"] * b["tolerance_factor"]
        out[b["id"]] = {"computed": v, "in_band": lo <= v <= hi}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--analysis", default=os.path.join(HERE, "out", "analysis.json"))
    ap.add_argument("--spend", default=os.path.join(HERE, "out", "spend.json"))
    a = ap.parse_args()
    spec = yaml.safe_load(open(os.path.join(HERE, "decision_spec.yaml")))
    ref = spec["attempt8_reference"]
    res = yaml.safe_load(open(os.path.join(ROOT, "toolkit/engines/malaria/benchmarks/results.yaml")))
    fitted = yaml.safe_load(open(os.path.join(ROOT, "toolkit/engines/malaria/benchmarks/fitted.yaml")))
    parts = ["### Benchmark pack (engine fitted once; held-out rows were not used in fitting)", "",
             bench_table(res, a8_bench(A8_RUN)), "",
             "Fitted engine-level parameters (toolkit/engines/malaria/benchmarks/fitted.yaml):", "",
             "| Parameter | Value | Range used for uncertainty | Fitted to | All its targets in band |", "|---|---|---|---|---|"]
    for k, v in fitted["parameters"].items():
        parts.append(f"| {k} | {v['value']} | {v['lo']}–{v['hi']} | {', '.join(v['benchmarks'])} | "
                     f"{'yes' if v.get('all_targets_in_band_at_value') else 'no'} |")
    if os.path.exists(a.analysis):
        A = json.load(open(a.analysis))
        o = spec["objective"]["outcome"]
        pt, uq = A["point"], A["uncertainty"]["fixed_plan"][o]
        mix = pt["mix"]
        loo = A["loo"]["metrics"]
        a8mix = ref["package_mix_gf_states"]
        parts += ["", "### Decision results (13 GF states, 309 LGAs)", "",
                  "| Quantity | Spike (v2) | Attempt 8 |", "|---|---|---|",
                  f"| DALYs averted 2024–2026 vs CF1, point | {fmt(pt['averted'][o])} | {fmt(ref['dalys_averted_gf_states_point'])} (same 13 states) |",
                  f"| DALYs averted, median (95% UI), fixed plan over {A['uncertainty']['draws']} draws | "
                  f"{fmt(uq['median'])} ({fmt(uq['lo95'])}–{fmt(uq['hi95'])}) | national, all 774 LGAs: "
                  f"{fmt(ref['dalys_averted_national_median_ui'][0])} ({fmt(ref['dalys_averted_national_median_ui'][1])}–"
                  f"{fmt(ref['dalys_averted_national_median_ui'][2])}); GF states are 98.6% of its point value |",
                  f"| Deaths averted, point | {fmt(pt['averted'].get('deaths'))} | n/a for the subset |",
                  f"| Cost allocated (excl. CM floor) | ${fmt(pt['cost_allocated'])} of ${fmt(A['budget_allocatable'])} | $240.9M + $15M reserve |",
                  f"| PBO nets (LGAs) | {mix.get('net_pbo', 0)} | {a8mix['net_pbo']} |",
                  f"| Dual-AI nets | {mix.get('net_dual', 0)} | {a8mix['net_dual']} |",
                  f"| Pyrethroid nets | {mix.get('net_pyr', 0)} | 0 |",
                  f"| SMC | {mix.get('smc', 0)} | {a8mix['smc']} |",
                  f"| IRS | {mix.get('irs', 0)} | {a8mix['irs']} |",
                  f"| Enhanced CM | {mix.get('cm', 0)} | {a8mix['cm']} |",
                  f"| No GF package | {mix.get('no_package', 0)} | 0 (state-wide campaign rule imposed) |",
                  f"| Leave-one-archetype-out worst-fold rank r | {fmt(loo['rank_correlation_worst_fold'])} "
                  f"({loo['folds_with_rank_corr']} folds with ≥5 LGAs; verdict {A['loo']['verdict']}) | {ref['loo_worst_fold_rank_r']} (UNSTABLE) |",
                  f"| LOO package concordance, mean / worst fold | {fmt(loo['package_concordance_pct_mean'])}% / "
                  f"{fmt(loo['package_concordance_pct_worst_fold'])}% | 86.7% / 30.8% |",
                  f"| LOO package concordance, worst fold with ≥5 LGAs | {fmt(loo.get('package_concordance_pct_worst_fold_ge5'))}% | n/a |",
                  f"| LOO DALY gap in the held-out LGAs, mean / worst fold (skill metric) | {fmt(loo['benefit_gap_pct_mean'])}% / {fmt(loo['benefit_gap_pct_worst_fold'])}% | 4.0% / 35.9% (cases) |",
                  f"| LOO DALYs lost over all LGAs (fold plan judged with full calibration), mean / worst | "
                  f"{fmt(loo.get('total_benefit_loss_pct_mean'))}% / {fmt(loo.get('total_benefit_loss_pct_worst_fold'))}% | n/a |",
                  f"| Max calibrated annual EIR (B16: 120, band 40–200) | {fmt(A['calibration']['max_eir_annual'])} | 106–168 |",
                  f"| Units clamped at the calibration range | {A['calibration']['clamped_units']} of {A['calibration']['units']} | n/a |",
                  f"| Emulator held-out accuracy | reduction R² median {fmt(A['emulator_accuracy'].get('reduction_r2_median'))}, "
                  f"10th pct {fmt(A['emulator_accuracy'].get('reduction_r2_p10'))}; calibration logit RMSE "
                  f"{fmt(A['emulator_accuracy'].get('cal_logit_rmse'))} | hand-built emulators, validated per round |",
                  f"| Plan stability over draws | {A['uncertainty']['units_same_package_in_90pct_of_draws']} of 309 LGAs keep "
                  f"their package in ≥90% of re-optimised draws | n/a |"]
        for dt in A.get("decision_tests") or []:
            parts += ["", f"Decision test {dt['id']}: {dt['text']}", ""]
            if dt.get("results"):
                parts += [f"Engine interaction OR in the trial setting: {fmt(dt['engine_interaction'])}.", "",
                          "| Interaction set to | IRS-on-net increment scale | LGAs changing package | LGAs with IRS (point plan) |",
                          "|---|---|---|---|"]
                for r in dt["results"]:
                    parts.append(f"| {r['trial_value']} | {fmt(r['increment_scale'])} | {r['units_changed']} | "
                                 f"{r['units_with_component']} ({r['units_with_component_point_plan']}) |")
            else:
                parts.append(dt.get("status", ""))
    code = [("Reusable engine (toolkit/engines: contract, benchmark runner, malaria engine)", loc(["toolkit/engines/**/*.py"])),
            ("Engine benchmark pack (YAML)", loc(["toolkit/engines/malaria/benchmarks/pack.yaml"])),
            ("Reusable analysis engine (spike/analysis)", loc(["spike/analysis/*.py"])),
            ("Question-specific code (spike/units.py)", loc(["spike/units.py"])),
            ("Question-specific spec (spike/decision_spec.yaml)", loc(["spike/decision_spec.yaml"]))]
    parts += ["", "### Code", "", "| Part | Non-blank lines |", "|---|---|"] + [f"| {k} | {v:,} |" for k, v in code]
    parts.append("| Attempt 8: per-run code, models/*.py (42 files; 5,850 lines with blanks) | 5,316 |")
    parts.append("| Attempt 8: per-run driver scripts at the run root (12 files) | 2,426 |")
    if os.path.exists(a.spend):
        sp = json.load(open(a.spend))
        parts += ["", "### Spend and time", "", "| Item | Spike | Attempt 8 |", "|---|---|---|",
                  f"| Coiled credits (clusters {sp['cluster_prefix']}*) | {sp['credits']} ({sp['instances']} VMs) | 5,074 |",
                  f"| Estimated VM cost at {sp['usd_per_credit']} $/credit | ${sp['usd_est']:.2f} | about ${5074 * sp["usd_per_credit"]:.0f} at the same rate |",
                  f"| Pipeline model-call spend (critics, writer) | ${sp['model_calls_usd']:.2f} | $442 |",
                  f"| Batch wall time | {sp.get('batch_wall_min', 'n/a')} min | 44 h total run |"]
    text = "\n".join(parts)
    p = os.path.join(HERE, "RESULTS.md")
    body = open(p).read() if os.path.exists(p) else "<!-- tables:start -->\n<!-- tables:end -->\n"
    a_, b_ = body.index("<!-- tables:start -->"), body.index("<!-- tables:end -->")
    body = body[:a_] + "<!-- tables:start -->\n" + text + "\n" + body[b_:]
    open(p, "w").write(body)
    print(f"rendered tables into {p}")


if __name__ == "__main__":
    sys.exit(main())
