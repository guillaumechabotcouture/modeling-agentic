#!/usr/bin/env python3
"""Fixture runs for the review workbench (docs/design/review-workbench.md §9, §3a).

    python3 tools/workbench/fixtures.py <dir> [--method M] [--figures SET] [--kind K] [--other NAME] [--defect NAME] [--upto STAGE]
    python3 tools/workbench/fixtures.py --list                                               # every planted edit
    python3 tools/workbench/fixtures.py --self-test

`build(dir, upto=None, defect=None, method="history_matching")` writes a small synthetic run: three groups, twelve
units, two survey rounds, three shared parameters, components A and B. It holds every file the Steps view (§5.2)
and the Fit view (§5.5) read, with the key names and nesting of a stored v2 run for that calibration method:

- `history_matching`: analysis/joint_calibration.yaml (and the small analysis/calibration.yaml a real run writes
  beside it). Keys the views do not read (figure_data.points, pairs, transmission, design_share; the full copy of
  the report under each sensitivity setting) are left out or shortened; nothing is renamed.
- `posterior`: analysis/calibration.yaml with `data.method: posterior`, and analysis/estimates.yaml with the
  predictive band of a fit over time (a stored run writes that file for the `estimates` deliverable; the fixture
  carries it beside its allocation so the Fit view's `series` table has a case).

It also holds every file the science register (§5.8, §6) is derived from, shaped as the producers write them
(agents/findings_ledger.py for findings.jsonl; a stored run for the rest): findings.jsonl (critics' findings and one
raised by a harness check), engine_choice.json, the signed spec's uncertainty.excluded, decision_spec.signoff_sheet.md
(no row marked HIGH on the clean run; defect sheet_high marks one), evidence_dossier.yaml with no-data entries, and
decision tests of each verdict. On the clean run every science row has a disposition, the one plan-changing test is
linked to a row, and the signed spec holds no comment line that starts a departure, limitation or assumption (such a
line is itself something the register raises, so it is planted: defect prose_only).

The clean run holds nothing declared (§9): no engine departure, no declared calibration miss, no scope declaration
and no finding with the status declared, and no two of its files disagree. Every declared case is a variant: defect
`declared` is the run with one of each (a signed departure for benchmark B2, which is then out of band, and one for
component B; a declared calibration miss for history matching; scope_declaration.yaml declaring finding R-001), each
naming a decision test that ran; the edits that change a declaration are made on top of it.

It also holds every file the Model view (§5.4) and the Figures view (§5.12) read, shaped as the producers write them:
- analysis/engine.yaml (toolkit/analysis/run.py): the card with four benchmarks, each in band at the centre of its band
  on the log scale (status words of toolkit/engines/benchmark.py), the engine's parameters and the one value the spec
  sets; engine_choice.json with three candidates (agents/analysis_backend.py), the chosen one reporting B4 without
  letting it gate. The signed spec says `engine: auto`, so every analysis header holds the hash of the engine choice,
  as the harness writes it only then. The clean run has no departure: an engine choice records one only for a
  benchmark that is not in band or a component with no benchmark (toolkit/engines/benchmark_coverage.py gate()). Defect
  C4_out_of_band_departure puts B2 out of band and adds the signed departure that covers it, to the signed spec and
  to the choice record with the benchmark's own status.
- analysis/figures.yaml in the shape of either producer (`figures="standard"`: toolkit/analysis/figures.py, a subset of
  its figure set under its own names, in its three tiers; `figures="estimates"`: toolkit/analysis/estimates.py, for the
  posterior method), every figure produced, each with a one-pixel PNG, a vector copy, its table and its provenance
  record (scripts/figure_validator.py); the manifest lists each image; report.md names each decision figure by path or
  file name, manuscript.md one more, and report_render.json holds the report's hash.
It also holds every file the Outcomes view (§5.7) reads, shaped as the producers write them and in the order
toolkit/analysis/run.py writes them: analysis/point.yaml (the plan over the twelve units: packages A+B and A+C, which
differ in the option of one group of components, the cheaper option B in nine units; mix, per-unit rows, cost from the
signed spec's per-person cost items, the budget and its reserve), uncertainty.yaml (the headline's median, equal to
the point value, and its interval; what was drawn and what was held at one value), decision_tests.yaml with one result
file per test under models/decision_tests/ (every test ran, with no change and clear of its three thresholds),
stability.yaml (both verdicts ROBUST), value_of_information.yaml (computed on 500 draws; two inputs above their noise
floor, neither prior-dominated), plan_variants.yaml (one variant, within budget), burden_validation.yaml (four
references inside their bands, two of them report-only, one of those named by decision test DT10), emulator.yaml and
emulator_boundary.yaml (rows that hold every component), and allocation.csv. A plan-changing test, a near miss and a
test that did not run are variants, as every declared case is. `kind="estimates"` (posterior method, estimates figure
set) writes the run of an `estimates` deliverable instead: no plan, tests file or allocation; analysis/estimates.yaml
holds the outputs, the decision rule's verdict and the decision tests. `other=<name>` (OTHERS) writes a second run of
the same question for the cross-run comparison: `second` differs in one spec key (constraints.envelope_usd) and what
follows from it; the rest are the runs the planted comparisons are read against (DEFECTS entries with `against`).
Not the producers' shape, and said here: analysis/emulator.yaml records the spec and the units table as its inputs
(a stored run records its batch files, which the fixture does not write).
It also holds every file the Spec and evidence view (§5.3) and the Scenarios view (§5.6) read, shaped as the producers
write them: a signed spec that names who decides, a decision date and a delivery period the producer's own rule accepts
(toolkit/spec/spec_critique.py), no outcome adjustment (each is a variant: _adjust), one engine parameter value whose
source the spec itself marks, and the dossier it names; evidence_dossier.yaml, whose observations record the optional `follow_up_years` the producer reads
and a `checked` mark (no producer writes that mark today: gap H2; the key is the fixture's, and said so in _obs); the
sign-off sheet with every section toolkit/spec/signoff.py writes for any spec, its spec critique and its last line of
source hashes; two archived sign-offs under evidence_planner/superseded_signoff_<date>/; analysis/batch.yaml;
analysis/figure_data.yaml with the stored schedule (toolkit/analysis/figure_data.py; no arm, no `charged`) and
figures/timeline.csv; analysis/trajectories.yaml with its producer's own header, which has no spec hash and which the
manifest does not list (toolkit/analysis/trajectories.py), and the dynamics checks where the producer stores them, on
the trajectories figure's entry in analysis/figures.yaml (toolkit/analysis/figures.py trajectory_checks). The estimates
kind has no schedule, batch or trajectory record. A run stopped before the outcome batch is `upto="calibration"`.
The clean run holds no analysis/benchmarks_at_spec_values.yaml: that file has no artifact header and sits outside the
manifest (gap H13), which the Steps view raises; variant spec_benchmarks_recomputed adds it with its record file.

Names are neutral on purpose (§3a): units U01.., groups G1.., parameters p1.., components A and B, and a grouping
column called `stratum` so that a view which hard-codes a column name fails.

The files agree with each other as a stored run's do: a change between rounds in survey_changes.pairs is computed
from the run's own target rows (so a planted change moves those rows), and a planted block also shows in the table
the block is about (the offset's own row).

On the clean run every check is `ok` or `not_available`. `defect` names one entry of DEFECTS and applies that one
edit; files that record a hash of an edited file (later analysis files, the manifest, the sign-off record) follow
from it, as they would in a real run. `upto` names a stage of STAGES and leaves out every file a later stage writes.
The launch commit recorded is this checkout's HEAD, so the Steps `pipeline` check is ok on a clean fixture.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import zlib

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
_SCRIPTS = os.path.join(REPO, "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
from lib.safe_io import write_nofollow  # noqa: E402

METHODS = ("history_matching", "posterior")
STAGES = ("launch", "sign-off", "calibration", "analysis", "final")
GROUPING = "stratum"
GROUPS = ("G1", "G2", "G3")
UNITS = tuple(f"U{i:02d}" for i in range(1, 13))
PARAMS = ("p1", "p2", "p3")
PRIOR = {"p1": [0.2, 0.8], "p2": [1.0, 5.0], "p3": [10.0, 50.0]}
RANGE = {"p1": [0.42, 0.58], "p2": [1.6, 4.4], "p3": [14.0, 46.0]}
BEST = {"p1": 0.5, "p2": 3.1, "p3": 28.0}
CUTOFF, Z_MAX, UNIT_LO, UNIT_HI = 3.0, 3.0, -1.0, 6.0
SD_OBS, SD_BIAS, SD_MD = 0.3, 0.15, 0.5                # a target's error terms; the discrepancy is the largest, so a
#                                                        level can be matched while the change between rounds is not
CHANGE_PARTS = {"sampling": 0.31, "offset": 0.15, "run": 0.06}     # the sd of a change leaves the discrepancy out
OFFSET = "S2|G2"                                       # the offset key the declared variants name
JC, CAL, UNC, DT = ("analysis/joint_calibration.yaml", "analysis/calibration.yaml", "analysis/uncertainty.yaml",
                    "analysis/decision_tests.yaml")
EST = "analysis/estimates.yaml"                        # the posterior fixture's fit over time (a predictive band)
SPEC_FILES = ["decision_spec.yaml", "decision_brief.signed/decision_spec.yaml"]
# What the science register reads beside the spec and the decision tests (§6).
LEDGER, SCOPE, CHOICE, SHEET, DOSSIER = ("findings.jsonl", "scope_declaration.yaml", "engine_choice.json",
                                         "decision_spec.signoff_sheet.md", "evidence_dossier.yaml")
OLD_DECLARED, OLD_GATE = "declared_findings.yaml", "final_gate.json"       # where an older run keeps dispositions
GATES = ("gate_r1.json", "gate_r2.json")               # agents/stage_machine.py: one gate record per round
# What the Model view (§5.4) and the Figures view (§5.12) read beside those.
ENGINE_REC, FIGS = "analysis/engine.yaml", "analysis/figures.yaml"
BSV, BSV_REC = "analysis/benchmarks_at_spec_values.yaml", "analysis/benchmarks_at_spec_values.record.json"
REPORT, MANUSCRIPT, RESULTS, RENDER, LABELS = "report.md", "manuscript.md", "results.md", "report_render.json", "presentation.yaml"
FIGURE_SETS = ("standard", "estimates")                # the two producers of analysis/figures.yaml (§5.12)
FIG_PRODUCER = {"standard": "toolkit/analysis/figures.py", "estimates": "toolkit/analysis/estimates.py"}
# The producers' own words. toolkit/engines/benchmark.py: STATUSES and the two reasons a band cannot be met;
# toolkit/engines/benchmark_coverage.py gate(): why a benchmark is reported and does not gate; toolkit/analysis/figures.py:
# the reasons a figure is not produced.
BENCH_STATUSES = ("in_band", "out_of_band", "not_assessable", "band_mis_based")
MIS_BASED = ("price_floor", "unreachable_in_fitted_range")
REPORTED_WHY = "the spec prices the component itself, not through this engine cost output"
NO_TRAJECTORIES = "needs trajectory runs (python -m toolkit.analysis.trajectories run <run_dir>)"
NO_TESTS_FILE = "needs analysis/decision_tests.yaml, which this run does not have"
ENGINE_REF = "toolkit.engines.toy.engine:ToyEngine"
HELD = "c1"                                            # a scenario value the signed spec excludes from the draws by name
CAL_DECLARED = {"target": "grp_r1:G1", "decision_test": "DT6",
                "reason": "the earlier-round value for G1 is matched only at the primary error setting; tested by DT6"}
ENGINE = {"name": "toy-engine", "version": "0.1", "card_sha256": hashlib.sha256(b"toy card").hexdigest()}
# The engine departures a variant signs ({engine, benchmark | component, reason, decision_test}: benchmark_coverage.py).
DEPARTURES = {
    "B2": {"engine": ENGINE["name"], "benchmark": "B2", "decision_test": "DT4",
           "reason": "B2 is held out; where the engine sits outside its band, DT4 runs the plan across the band"},
    "B": {"engine": ENGINE["name"], "component": "B", "decision_test": "DT5",
          "reason": "no benchmark compares component B with its absence; DT5 runs its effect from none to the top"}}
SCOPE_WHY = "The data cannot narrow p1 further; DT3 runs the plan at both ends of its accepted range."
MECHANISMS = ("components A and B on one outcome", "one value per unit sets its level before any package",
              "the two components combine on the ratio scale")
HARNESS_INPUT = {"../../toolkit/engines/toy/priors.yaml": hashlib.sha256(b"toy priors").hexdigest()}
# Which stage writes each file. run_state.json and metadata.json are written at every stage.
STAGE_OF = {"decision_spec.yaml": "launch", "data/derived/units.csv": "launch", "data/derived/targets.csv": "launch",
            "metadata.json": "launch", "run_state.json": "launch",
            "decision_spec.signoff.json": "sign-off", "decision_brief.signed/decision_spec.yaml": "sign-off",
            JC: "calibration", CAL: "calibration", ENGINE_REC: "calibration", BSV: "calibration", BSV_REC: "calibration",
            FIGS: "analysis", RESULTS: "analysis", REPORT: "final", MANUSCRIPT: "final", RENDER: "final", LABELS: "launch",
            "analysis/point.yaml": "analysis", UNC: "analysis", DT: "analysis", EST: "analysis", "allocation.csv": "analysis",
            "analysis/manifest.json": "analysis",
            SHEET: "sign-off", DOSSIER: "sign-off", CHOICE: "sign-off",
            LEDGER: "final", SCOPE: "final", OLD_DECLARED: "final", OLD_GATE: "final", GATES[0]: "final", GATES[1]: "final"}
HISTORY = [("SPEC", "SIGN", 30, 1.0), ("SIGN", "EVIDENCE", 60, 1.0), ("EVIDENCE", "ENGINE_CHOICE", 70, 1.4),
           ("ENGINE_CHOICE", "CONFIGURE", 80, 1.8), ("CONFIGURE", "ANALYZE", 90, 2.0), ("ANALYZE", "CRITIQUE", 240, 2.0),
           ("CRITIQUE", "GATE", 260, 3.0), ("GATE", "WRITE", 270, 3.0), ("WRITE", "PRESENTATION", 290, 3.6),
           ("PRESENTATION", "FINAL", 300, 3.8)]
REACHED = {"launch": 1, "sign-off": 4, "calibration": 5, "analysis": 6, "final": 10}   # transitions made by each stage
# What the Outcomes view (§5.7) reads beside those, and the producers' own words and numbers (X41 holds each to the
# producer's source): toolkit/analysis/value_of_information.py STATUSES, stability.py's verdicts, burden.py STATUSES and
# roles, variants.py's statuses, boundary.py's status, red_flag.py SHARE and MIN_UNITS, thresholds.yaml.
POINT, STAB, VOI, PV, BV, EMU, EMB = (f"analysis/{n}.yaml" for n in (
    "point", "stability", "value_of_information", "plan_variants", "burden_validation", "emulator", "emulator_boundary"))
BURDEN_CSV, TESTS_DIR = "data/derived/burden_references.csv", "models/decision_tests/"
KINDS = ("package_per_unit", "estimates")              # toolkit/spec/schema.py DELIVERABLE_KINDS
OUTCOME, HEADLINE, TIER = "outcome", "total_outcome_averted", "tier"
PRICE = {"A": 1.0, "B": 2.0, "C": 3.0}                 # per person; B and C are the two options of one group, C the dearer
ENVELOPE, RESERVE_SHARE, ALLOWANCE, DRAWS = 100000.0, 0.95, 0.1, 500
VOI_STATUSES = ("computed", "indicative", "too_few_draws", "no_eligible_plan")
VOI_MIN_DRAWS, VOI_INFORMATIVE_DRAWS = 30, 500
STABILITY_VERDICTS = ("ROBUST", "FRAGILE", "UNSTABLE", "NOT_RUN")
VALUE_LOSS = {"value_loss_pct_robust": 1.0, "value_loss_pct_fragile": 5.0}
BURDEN_STATUSES, BURDEN_ROLES, BURDEN_BAND = ("within", "outside", "information", "unmatched"), ("checked", "report_only", "anchor"), [0.5, 2.0]
VARIANT_STATUSES = ("evaluated", "solved", "not_solved")
TEST_THRESHOLDS = {"share": 0.10, "regret": 0.01, "dollars": 0.05}     # thresholds.yaml decision_tests
BOUNDARY_MIN, DEAREST_SHARE, DEAREST_MIN_UNITS = 0.8, 0.9, 5
EST_VERDICTS = ("yes", "no", "undetermined")           # toolkit/analysis/estimates.py: a decision rule's verdicts
# The comparison runs of the cross-run check (§5.7 run_shift): `build(..., other=<name>)` writes the same question's
# second run. `second` differs from the clean run in one spec key and what follows from it, and in nothing a
# threshold counts; the others are what the planted comparisons are made against.
OTHERS = ("twin", "second", "shifted", "shifted_same_spec", "headline", "other_question", "unverified")
STAGE_OF.update({STAB: "analysis", VOI: "analysis", PV: "analysis", BV: "analysis", EMU: "calibration", EMB: "analysis",
                 BURDEN_CSV: "launch"})
# What the Spec and evidence view (§5.3) and the Scenarios view (§5.6) read beside those, and the producers' own words
# (X53 holds each to the producer's source).
FD, BATCH, TRAJ, SETTINGS = "analysis/figure_data.yaml", "analysis/batch.yaml", "analysis/trajectories.yaml", "analysis_settings.yaml"
SIGNOFF, TIMELINE = "decision_spec.signoff.json", "figures/timeline.csv"
ARCHIVE = "evidence_planner/superseded_signoff_"        # tools/run_console/dossier.py ARCHIVE_DIR and ARCHIVE_PREFIX
WINDOW, DELIVERY, SIM_START = [2026.0, 2029.0], [2026.0, 2028.0], 2024.0
EVENT_KINDS = ("history", "decision", "routine")       # toolkit/engines/hooks.py KINDS
DYNAMICS_STATUSES = ("pass", "flag", "info")           # toolkit/analysis/figures.py trajectory_checks
MARKS = ("fetched", "matched", "unverified")           # toolkit/engines/contract.py: a parameter source's `verified`
HOOKS_CONTRACT = "figures-hooks/1"                     # toolkit/engines/hooks.py
SELECTION = "purposive: per region, the units at the lowest, median and highest comparator burden"
# The sections toolkit/spec/signoff.py sheet() writes for every spec, in its order; and those it writes only for a spec
# that has the thing (outcome adjustments, a burden reference file, candidate engines).
SHEET_ALWAYS = ("## Spec critique (decision design principles, before signing)", "## Engine defaults accepted by name",
                "## Staged inputs and columns not used", "## Derivations", "## Input geometry check",
                "## Engine parameter values set by the spec", "## Evidence dossier (inputs that drive an option's effect)")
SHEET_SOME = ("## Base outcome adjustments", "## Burden validation references", "## Candidate engines (pre-sign check)")
SHEET_SOURCES = "<!-- signoff-sheet-sources: "
DIFFERS = "Inputs whose chosen value or range differs from what execution will use"
OA = {"id": "OA1", "component": "C", "reference": "B", "value": 0.8, "source": "fixture study four, table 2"}
FOLLOW_UP, CHECKED = "follow_up_years", "checked"      # the dossier observation's optional field the producer reads
#                                                        (toolkit/spec/spec_critique.py), and the mark gap H2 asks for
EARLIER = ("2025-12-30", "2025-12-31")                 # the archived sign-offs, oldest first
STAGE_OF.update({FD: "analysis", BATCH: "analysis", TRAJ: "analysis", SETTINGS: "launch"})


def stage_of(rel):
    """The stage that writes a file. Under figures/, the tables of the calibration figures are written with the
    calibration (the Fit view reads them from there); every other figure file is written at the end of the analysis."""
    if rel.startswith("figures/"):
        return "calibration" if rel.startswith("figures/calibration_") and rel.endswith(".csv") else "analysis"
    if rel.startswith(TESTS_DIR):
        return "analysis"
    if rel.startswith(ARCHIVE):
        return "sign-off"
    return STAGE_OF[rel]


def _r(x):
    return round(float(x), 6)


def _logit(p):
    return math.log(p / (1 - p))


def _expit(x):
    return 1 / (1 + math.exp(-x))


def _at_error(value, err):
    """The model value that sits `err` from an observed value on the logit scale (kept to ten places, so a change
    recomputed from the stored rows agrees with the one recorded)."""
    return round(_expit(_logit(value) + err), 10)


def _group(u):
    return GROUPS[UNITS.index(u) // 4]


def _wobble(i):
    return ((i * 5) % 7) - 3            # -3..3, fixed per unit: the fixture has no random numbers


def _at(minute):
    return f"2026-01-01T{minute // 60:02d}:{minute % 60:02d}:00+00:00"


# ------------------------------------------------------------------------------------------- the run, as objects

def _spec(method):
    items = [{"id": f"price_{c}", "component": c, "value": v, "price_year": "2025", "price_rule": "as_published",
              "basis": "per_person", "includes": [f"supply of {c}"]} for c, v in PRICE.items()]
    # The decision is dated on the day the delivery period starts, which is the day it is signed (_at): the producer's
    # rule (toolkit/spec/spec_critique.py _decision_date) flags a delivery period that starts before either.
    spec = {"schema_version": 1, "question": "Which package should each unit get? (synthetic fixture)",
            "decision_maker": "programme lead (synthetic fixture)",
            "decision_date": "2026-01-01", "engine": "auto",
            "engine_parameters": {"p2": {"value": 3.1, "units": "ratio", "source": "the owner's range",
                                         "reason": "no measurement", "basis": "owner_judgement", "range": [1.0, 5.5],
                                         "dist": "uniform", "verified": "matched"}},
            "horizon": {"start": SIM_START, "stop": WINDOW[1]},
            "decision_units": {"csv": "data/derived/units.csv", "id": "unit_id", "label": "name", "region": GROUPING,
                               "engine_attributes": {"x1": {"column": "x1"}}},
            "options": {"populations": {"total": "pop"},
                        "components": {"A": {}, "B": {"group": TIER}, "C": {"group": TIER}}, "packages": {"generate": True},
                        "engine_map": {"A": {"A": 1}, "B": {"B": 0.5}, "C": {"B": 1}},
                        "prices": {"base_year": 2025}, "cost_items": items},
            "objective": {"outcome": OUTCOME, "sense": "avert", "horizon": list(WINDOW), "delivery": list(DELIVERY),
                          "discounting": "none"},
            "constraints": {"envelope_usd": ENVELOPE},
            "comparator": {"package": "A", "text": "every unit keeps component A alone"},
            "outcomes_reported": [OUTCOME],
            "uncertainty": {"level": 0.95, "draws": DRAWS, "seed": 1,
                            "affordability": {"max_share_over_budget": ALLOWANCE, "enforce": True}, "excluded": [
                {"parameter": HELD, "reason": f"{HELD} is a scenario value held fixed by name; DT7 runs it at both ends"}]},
            "analysis": {"evaluation": "emulator", "stability": {"holdout_group": GROUPING},
                         "plan_variants": {"variants": [{"id": "one_option", "type": "group_rule", "components": ["B", "C"],
                                                         "by": GROUPING, "text": "the cheaper option of the group in every unit"}]},
                         "decision_tests": [{"id": "DT1", "type": "parameter_values", "parameter": "p2",
                                             "values": [1.6, 4.4], "text": "Does the plan change across p2's range?"},
                                            *_spec_tests(method)]},
            "validation": {"burden": {"csv": BURDEN_CSV, "group": GROUPING, "arm": "comparator", "window": [2026.0, 2027.0]}},
            "evidence": {"dossier": DOSSIER},
            "deliverable": {"kind": "package_per_unit", "allocation_file": "allocation.csv",
                            "columns": {"unit": "unit_id", "package": "package", "value": "outcome_averted"},
                            "headline": {"name": HEADLINE, "outcome": OUTCOME}}}
    if method == "history_matching":
        spec["calibration"] = {"method": "emulator_inversion", "joint": {
            "shared": list(PARAMS), "discrepancy_sd": SD_MD, "waves": 1, "points_per_unit": 4, "shared_points": 16,
            "targets": [{"id": "unit_r2", "kind": "unit", "column": "target_r2", "window": [2021.5, 2022.0],
                         "metric": "m1", "scale": "logit", "sd": SD_OBS, "bias_sd": SD_BIAS, "bias_by": GROUPING,
                         "bias_source": "S2", "survey": "survey_a", "source": _source("unit")},
                        {"id": "grp_r1", "kind": "group", "csv": "data/derived/targets.csv",
                         "filter": {"round_id": "S1"}, "value": "value_pct", "multiplier": 0.01,
                         "ci": ["ci_low_pct", "ci_high_pct"], "ci_level": 0.95, "window": [2018.5, 2019.0],
                         "metric": "m1", "scale": "logit", "survey": "survey_a", "source": _source("group")}],
            "sensitivity": [{"id": "sd_015", "label": "sd 0.15 on the round 2 unit target",
                             "targets": {"unit_r2": {"sd": 0.15}}}],
            "unmatched": {"mode": "flag", "max_units": 2, "declared": []}}}
    else:
        spec["calibration"] = {"method": "posterior", "parameters": list(PARAMS),
                               "priors": {p: {"dist": "uniform", "range": PRIOR[p]} for p in PARAMS},
                               "sources": [{"id": s, "latent": {"quantity": q}, "noise": {"model": "binomial"}}
                                           for s, q in (("S1", "q1"), ("S2", "q2"))],
                               "targets": {"csv": "data/derived/targets.csv", "groups": GROUPING}}
    return spec


def _spec_tests(method):
    """The decision tests the spec declares for the register's rows (each names its finding, as a stored spec's do)."""
    def test(tid, kind, finding=None, **what):
        return {"id": tid, "type": kind, **what, "text": f"Does the plan change under {tid}?",
                "finding": finding or f"decision_test:{tid}"}
    return [test("DT3", "parameter_value", "R-001", parameter="p1", values=[0.42, 0.58]),
            test("DT4", "interaction_rescale", components=["B"], with_any=["A"], benchmark="B2", values=[0.5, 2.0]),
            test("DT5", "parameter_value", parameter="b_add", values=[0.0, 0.1]),
            *([test("DT6", "calibration_target", calibration_target={"units": list(UNITS[:4])})]
              if method == "history_matching" else []),
            test("DT7", "parameter_value", parameter=HELD, values=[0.0, 1.0]),
            test("DT8", "parameter_value", parameter="p2", values=[1.0, 5.5]),
            test("DT9", "unit_attribute", unit_attribute={"column": "x1", "units": "all"}),
            test("DT10", "burden_scale", burden_scale={"references": ["ref_G3"], "shares": [1.0]})]


def _source(kind, assumed=False):
    if kind == "group":
        return "survey_a round 1 group value with its 95% interval (targets.csv)"
    tail = ("sd 0.3 for the spread within the group is an ASSUMPTION (no unit-level interval in the inputs)"
            if assumed else "sd 0.3 from the round's reported unit-level interval")
    return f"survey_a round 2 unit value from the units table (target_r2); {tail}"


def _unit_error(jc, u, err, emulator=None):
    """Unit u's one target: the engine's verification sits `err` from the observed value on the target's scale, with
    the implausibility that follows; the emulator's implausibility at the best point is `emulator` (0.8 of the
    engine's when not given)."""
    t = jc["unit_targets"][u][0]
    imp, sim = _r(abs(err) / t["sd_total"]), _at_error(t["value"], err)
    jc["units"][u].update(implausibility_verified=imp, implausibility_at_best=_r(0.8 * imp if emulator is None else emulator))
    jc["verification"]["units"][u].update(implausibility=imp)
    jc["verification"]["units"][u]["targets"][0].update(simulated=sim, implausibility=imp)
    next(f for f in jc["figure_data"]["fit"] if f["target"] == t["id"])["verified"] = sim


def _group_error(jc, g, err=None, emulator=None):
    """Group g's earlier-round target: the engine's verification sits `err` from the observed value, and the
    emulator's value at the best point `emulator` from it (each left as it is when not given)."""
    tid = f"grp_r1:{g}"
    t, row = jc["targets"][tid], next(f for f in jc["figure_data"]["fit"] if f["target"] == tid)
    if err is not None:
        sim = _at_error(t["value"], err)
        jc["verification"]["targets"][tid].update(simulated=sim, implausibility=_r(abs(err) / t["sd_total"]))
        row["verified"] = sim
    if emulator is not None:
        mid = _r(_expit(_logit(t["value"]) + emulator))
        t.update(modelled_at_best=mid, implausibility_at_best=_r(abs(emulator) / t["sd_total"]))
        row.update(model_lo=_r(mid * 0.9), model_mid=mid, model_hi=_r(mid * 1.1))


def _errors(jc, g):
    """(the engine's error on each unit target of group g, its error on the group's target), on the logit scale."""
    def err(t, sim):
        return _logit(sim) - _logit(t["value"])
    units = {u: err(jc["unit_targets"][u][0], jc["verification"]["units"][u]["targets"][0]["simulated"])
             for u in UNITS if _group(u) == g}
    return units, err(jc["targets"][f"grp_r1:{g}"], jc["verification"]["targets"][f"grp_r1:{g}"]["simulated"])


def _changes(jc):
    """survey_changes.pairs, computed from the target rows as the method computes them: for each group, the mean of
    its later-round unit targets minus its earlier-round group target, observed and modelled (the engine's
    verification), on the natural and on the targets' scale; z is the difference of the two over the sd of a change."""
    sd, pairs = _r(math.sqrt(sum(v * v for v in CHANGE_PARTS.values()))), []
    for g in GROUPS:
        us = [u for u in UNITS if _group(u) == g]
        obs = [jc["unit_targets"][u][0]["value"] for u in us]
        mod = [jc["verification"]["units"][u]["targets"][0]["simulated"] for u in us]
        obs_g, mod_g = jc["targets"][f"grp_r1:{g}"]["value"], jc["verification"]["targets"][f"grp_r1:{g}"]["simulated"]
        o, m = sum(map(_logit, obs)) / len(us) - _logit(obs_g), sum(map(_logit, mod)) / len(us) - _logit(mod_g)
        o_n, m_n = sum(obs) / len(us) - obs_g, sum(mod) / len(us) - mod_g
        z = _r((m - o) / sd)
        pairs.append({"id": f"survey_a:grp_r1->unit_r2:{g}", "survey": "survey_a", "from": "grp_r1", "to": "unit_r2",
                      "group": g, "by": GROUPING, "n_terms": len(us) + 1, "rho": 0.0, "observed": _r(o), "modelled": _r(m),
                      "observed_natural": _r(o_n), "modelled_natural": _r(m_n), "observed_natural_sd": 0.03,
                      "difference": _r(m - o), "sd": sd, "sd_parts": dict(CHANGE_PARTS), "z": z,
                      "flagged": abs(z) > Z_MAX, "ratio": _r(m_n / o_n),
                      "text": f"modelled change {_r(m_n)} against {_r(o_n)} observed",
                      "basis": "engine verification at the best point",
                      "modelled_region": {"q025": _r(m - 0.1), "median": _r(m), "q975": _r(m + 0.1)}})
    jc["survey_changes"].update(pairs=pairs, flagged=[p["id"] for p in pairs if p["flagged"]])


def _move_change(m, g, z):
    """Group g's change between rounds is moved to exactly z: 0.3 of the needed difference is put on the engine's
    values for the group's unit targets (all moved by the same amount) and the rest, with the other sign, on its
    group target, so that no single target comes near the cutoff; the emulator's values move with the engine's."""
    jc = m["jc"]
    sd = math.sqrt(sum(v * v for v in CHANGE_PARTS.values()))
    units, _ = _errors(jc, g)
    mean = sum(units.values()) / len(units)
    for u, e in units.items():
        _unit_error(jc, u, e - mean + 0.3 * z * sd)
    _group_error(jc, g, err=-0.7 * z * sd, emulator=-0.63 * z * sd)
    _changes(jc)
    pair = next(p for p in jc["survey_changes"]["pairs"] if p["group"] == g)
    pair["z"] = _r(z)           # the rows give z to within their rounding; the pair records the value intended


def _region():
    return {"candidates": 2048, "accepted": 600, "share_of_prior": 0.21, "relaxed": 0,
            "ranges": copy.deepcopy(RANGE), "prior_ranges": copy.deepcopy(PRIOR),
            "contraction": {p: _r(1 - (RANGE[p][1] - RANGE[p][0]) / (PRIOR[p][1] - PRIOR[p][0])) for p in PARAMS},
            "interval95": {p: {"region": [_r(RANGE[p][0] * 1.02), _r(RANGE[p][1] * 0.98)], "prior": PRIOR[p],
                               "median": BEST[p], "point": BEST[p], "contraction95": _r(
                                   1 - (RANGE[p][1] * 0.98 - RANGE[p][0] * 1.02) / (PRIOR[p][1] - PRIOR[p][0]))}
                           for p in PARAMS}}


def _thresholds():
    return {"history_matching": {"implausibility_cutoff": CUTOFF, "candidates_log2": 11, "region_min_candidates": 64,
                                 "refine_rounds": 2, "refine_sd": 0.05, "z_grid": 41, "group_t": 21, "group_tries": 20,
                                 "waves": 1, "points_per_unit": 4, "shared_points": 16, "draws_kept": 40,
                                 "min_target_sd": 1e-06, "include_run_noise": True, "bias_draws": 64,
                                 "bias_draws_max": 1024, "bias_accept_min": 0.001, "best_point_weighting": "per_group"},
            "emulator": {"holdout_share": 0.1, "std_error_max": 3.0, "outside_share_max": 0.05, "min_holdout": 10,
                         "min_points_per_unit": 4, "ridge": 1e-06},
            "blocking": {"unmatched_units_max": 0, "clamped_units_max": 0}}


def _joint():
    """analysis/joint_calibration.yaml `data`, clean."""
    sd_u, sd_g = _r(math.sqrt(SD_OBS ** 2 + SD_BIAS ** 2 + SD_MD ** 2)), _r(math.sqrt(SD_OBS ** 2 + SD_MD ** 2))
    units, unit_targets, v_units, fit, best_z = {}, {}, {}, [], {}
    for i, u in enumerate(UNITS):
        val, err, zb = 0.10 + 0.02 * i, 0.07 * _wobble(i), 1.0 + 0.3 * i
        imp, sim, tid = _r(abs(err) / sd_u), _at_error(val, err), f"unit_r2:{u}"
        top = i == len(UNITS) - 1                      # one unit of twelve reaches the upper bound on the clean run
        units[u] = {"matched": True, "status": "matched", "best_implausibility": _r(0.1 * imp),
                    "implausibility_at_best": _r(0.8 * imp), "z_best": _r(zb),
                    "z_interval": [_r(zb - 1.2), UNIT_HI if top else _r(zb + 1.2)],
                    "region_touches_bound": ["hi"] if top else [], "direction": None, "misses": None,
                    "z_verified": _r(zb), "implausibility_verified": imp}
        unit_targets[u] = [{"id": tid, "kind": "unit", "observable": "jc0.m1", "value": _r(val), "scale": "logit",
                            "sd_obs": SD_OBS, "sd_bias": SD_BIAS, "sd_md": SD_MD, "sd_total": sd_u, "source": _source("unit"),
                            "unit": u, "bias_key": f"S2|{_group(u)}"}]
        v_units[u] = {"implausibility": imp, "ok": True, "z": _r(zb), "runs": 4, "recorded": {"jc2.m2": _r(2.0 + i)},
                      "targets": [{"target": tid, "simulated": sim, "implausibility": imp}]}
        best_z[u] = _r(zb)
        mid = _expit(_logit(val) + 0.8 * err)
        fit.append({"target": tid, "kind": "unit", "unit": u, "observable": "jc0.m1", "observed": _r(val),
                    "obs_lo": _r(_expit(_logit(val) - 1.96 * sd_u)), "obs_hi": _r(_expit(_logit(val) + 1.96 * sd_u)),
                    "model_lo": _r(mid * 0.8), "model_mid": _r(mid), "model_hi": _r(mid * 1.2), "verified": sim,
                    "status": "matched"})
    targets, v_targets = {}, {}
    for i, g in enumerate(GROUPS):
        val, em, en, tid = 0.30 + 0.06 * i, (0.05, -0.08, 0.10)[i], (0.08, -0.12, 0.16)[i], f"grp_r1:{g}"
        sim = _at_error(val, en)
        targets[tid] = {"id": tid, "kind": "group", "observable": "jc1.m1", "value": _r(val), "scale": "logit",
                        "sd_obs": SD_OBS, "sd_bias": 0.0, "sd_md": SD_MD, "sd_total": sd_g, "source": _source("group"),
                        "n_units": 4, "implausibility_at_best": _r(abs(em) / sd_g),
                        "modelled_at_best": _r(_expit(_logit(val) + em)), "non_implausible_share": 1.0}
        v_targets[tid] = {"simulated": sim, "implausibility": _r(abs(en) / sd_g), "ok": True}
        mid = targets[tid]["modelled_at_best"]
        fit.append({"target": tid, "kind": "group", "unit": None, "observable": "jc1.m1", "observed": _r(val),
                    "obs_lo": _r(_expit(_logit(val) - 1.96 * sd_g)), "obs_hi": _r(_expit(_logit(val) + 1.96 * sd_g)),
                    "model_lo": _r(mid * 0.9), "model_mid": mid, "model_hi": _r(mid * 1.1), "verified": sim,
                    "status": "matched"})
    keys = [f"S2|{g}" for g in GROUPS]
    region, th = _region(), _thresholds()
    prior = {p: {"range": PRIOR[p], "dist": "uniform", "mode": BEST[p], "source": f"fixture prior for {p}"}
             for p in PARAMS}
    wave = {"wave": 1, "unit_runs": 48, "shared_runs": 16,
            "validation": {o: {"n": 6, "status": "ok", "share_outside": 0.0, "max_abs": 1.1, "mean": 0.02, "sd": 0.6,
                               "how": "held-out runs", "covers_all": True} for o in ("jc0.m1", "jc1.m1")},
            "candidates": region["candidates"], "accepted": region["accepted"],
            "share_of_prior": region["share_of_prior"], "relaxed": 0, "unmatched": [], "conflicting": [],
            "ranges": copy.deepcopy(RANGE)}
    names = list(PARAMS) + [f"u0:{u}" for u in UNITS]
    jc = {
        "method": "history_matching", "producer": "toolkit/calibration/history_matching.py",
        "figure_data": {"names": list(PARAMS), "prior": {p: {**prior[p], "point": BEST[p]} for p in PARAMS},
                        "cutoff": CUTOFF, "fit": fit,
                        "waves": [{k: wave[k] for k in ("wave", "share_of_prior", "accepted", "candidates", "ranges")}]},
        "status": "matched", "blocked": False, "blocks": [], "cutoff": CUTOFF, "shared": prior,
        "unit_parameter": {"name": "u0", "lo": UNIT_LO, "hi": UNIT_HI, "scale": "log"},
        "region": region, "best": {"theta": dict(BEST), "z": best_z, "score": 1.9},
        "units": units, "unmatched": [], "conflicting": [], "n_units": len(UNITS), "n_matched": len(UNITS),
        "targets": targets, "unit_targets": unit_targets,
        "verification": {"units": v_units, "targets": v_targets, "bias": {k: 0.0 for k in keys}},
        "bias": {k: {"draws": 64, "proposed": 64, "sd_prior": 0.15, "mean": _r(0.02 * (i - 1)), "sd": 0.14,
                     "q025": _r(0.02 * (i - 1) - 0.27), "q975": _r(0.02 * (i - 1) + 0.27), "units": 4,
                     "offset_verified": 0.0} for i, k in enumerate(keys)},
        "bias_components": [{"blocks": [k], "units": 4, "aggregates": [f"grp_r1:{k[3:]}"], "candidates": 2048,
                             "feasible": 2048, "empty_block": 0, "no_accepted": 0, "draws_max": 64,
                             "acceptance_in_region": 1.0} for k in keys],
        "survey_changes": {"status": "computed", "z_max": Z_MAX,
                           "statistic": "z = (modelled change - observed change) / sd on the targets' scale; sd from "
                                        "sampling, offsets and run noise, not discrepancy",
                           "pairs": [], "flagged": []},
        "weighting": {"best_point": "per_group", "bias_draws": 64}, "waves": [wave], "simulated_at_boundary": {},
        "emulators": {"unit": {o: {"random_effect_var": 0.4, "gp_var": 1.2, "length_scales": [1.0, 2.0, 3.0, 4.0],
                                   "run_noise_var": 0.01, "residual_var": 0.02, "nll": -12.5}
                               for o in ("jc0.m1", "jc1.m1")}, "shared": {}},
        "thresholds": th, "prior_runs": None,
        "adapter": {"windows": {"jc0": [2021.5, 2022.0], "jc1": [2018.5, 2019.0], "jc2": [2021.0, 2022.0]},
                    "dropped_targets": [], "units": len(UNITS), "shared": list(PARAMS), "simulator_calls": 64,
                    "stored_rows_reused": 0, "stored_rows_reused_legacy": 0, "theta_held": {}},
        "primary": {"targets": {"unit_r2": {"sd": 0.3, "bias_sd": 0.15}, "grp_r1": {}}, "discrepancy_sd": SD_MD,
                    "note": "the primary setting; declared sensitivities are reported beside it, never as a block"},
        "unmatched_decision": {"proceed": True, "mode": "flag", "flagged": [], "missed": [], "declared": [],
                               "why_not": None},
        "figures": {"figures": [], "not_produced": []},
        "sensitivity": [{"id": "sd_015", "label": "sd 0.15 on the round 2 unit target",
                         "targets": {"unit_r2": {"sd": 0.15}},
                         "report": {"method": "history_matching", "status": "matched", "blocked": False, "blocks": [],
                                    "cutoff": CUTOFF, "region": {**_region(), "accepted": 410, "share_of_prior": 0.14},
                                    "n_units": len(UNITS), "n_matched": len(UNITS)}}],
        "sample": {"sample_id": hashlib.sha256(b"fixture sample").hexdigest(), "names": names,
                   "rows": [[_r(RANGE[p][0] + (RANGE[p][1] - RANGE[p][0]) * ((k * 3 + j) % 8) / 8)
                             for j, p in enumerate(PARAMS)] + [best_z[u] for u in UNITS] for k in range(8)],
                   "full_sample_id": hashlib.sha256(b"fixture full sample").hexdigest(), "weighting": "per_group"}}
    _changes(jc)
    return jc


def _companion(jc):
    """The small analysis/calibration.yaml `data` a history-matching run writes beside the joint file."""
    return {"method": "emulator_inversion",
            "units": {u: {"z": jc["best"]["z"][u], "clamped": False, "slope": 0.7} for u in UNITS},
            "diagnostics": {"units": len(UNITS), "clamped_units": 0, "grid_points": 41, "range": [UNIT_LO, UNIT_HI],
                            "target_logit_residual_max": 0.21, "slope_median": 0.7, "slope_min": 0.4},
            "identifiability": {"design": "one value per unit", "flat_slope_threshold": 0.05, "units_unidentified": [],
                                "status": "identified"},
            "joint": {"artifact": JC, "shared": list(PARAMS), "best": dict(jc["best"]["theta"]),
                      "ranges": copy.deepcopy(jc["region"]["ranges"]), "sample_id": jc["sample"]["sample_id"]}}


def _posterior():
    """analysis/calibration.yaml `data` for the posterior method, clean."""
    fit = []
    for i, u in enumerate(UNITS):
        for src, lat, base in (("S1", "q1", 0.30), ("S2", "q2", 0.10)):
            share = base + 0.02 * i
            exp = share + 0.01 * _wobble(i) / 3
            fit.append({"unit": u, "groups": [_group(u)], "source": src, "latent": lat, "observed": _r(200 * share),
                        "n": 200.0, "noise": "binomial", "expected": _r(200 * exp), "expected_share": _r(exp),
                        "observed_share": _r(share), "variance": _r(200 * exp * (1 - exp))})
    width = {p: PRIOR[p][1] - PRIOR[p][0] for p in PARAMS}
    return {"method": "posterior",
            "diagnostics": {"converged": True, "message": "CONVERGENCE: relative reduction of the objective below tolerance",
                            "n_targets": len(fit), "n_parameters": len(PARAMS), "at_bound": [],
                            "observation": "per-source (calibration.sources)", "evaluation": "run_batch",
                            "loglik_at_mode": -41.2, "start_search_refusals": 0, "fit": fit,
                            "sampler": "adaptive_metropolis", "chains": 4, "warmup": 400, "draws_per_chain": 800,
                            "rhat_max": 1.0, "ess_bulk_min": 910.0, "acceptance": 0.24, "doublings": 0},
            "identifiability": {"method": "Fisher information of the likelihood at its maximum; contraction "
                                          "1 - var_post/var_prior on each prior's own scale",
                                "classes": {p: "identified" for p in PARAMS},
                                "likelihood_classes": {p: "identified" for p in PARAMS},
                                "contraction": {"p1": 0.95, "p2": 0.9, "p3": 0.2}, "ridges": [],
                                "status": "well_identified"},
            "theta": dict(BEST), "names": list(PARAMS),
            "sample": {"sample_id": hashlib.sha256(b"fixture posterior sample").hexdigest(), "names": list(PARAMS),
                       "rows": [[_r(BEST[p] + width[p] * 0.1 * (((k * (3 + j)) % 11) - 5) / 5)
                                 for j, p in enumerate(PARAMS)] for k in range(40)],
                       "method": "adaptive_metropolis", "seed": 0},
            "priors": {p: {"dist": "uniform", "source": f"fixture prior for {p}", "range": PRIOR[p]} for p in PARAMS},
            "prior_range_tests": [], "prior_dominated": ["p3"], "units": {u: {"z": None} for u in UNITS}}


def _estimates():
    """analysis/estimates.yaml `data` for the posterior method: the predictive band of a fit over time. A stored run
    writes this file for the `estimates` deliverable; the fixture carries it so the Fit view's `series` has a case."""
    steps = list(range(1, 9))
    med = [_r(20 + 15 * math.sin(k / 2.0)) for k in steps]
    return {"level": 0.95, "label": "95% interval over calibration and observation",
            "posterior_predictive": {"steps": steps, "observed": [_r(m + _wobble(k)) for k, m in zip(steps, med)],
                                     "median": med, "lo": [_r(m - 8) for m in med], "hi": [_r(m + 8) for m in med],
                                     "coverage": 1.0, "level": 0.95, "draws": 50}}


def _test(tid, ran=True, changes=False, share=None, feasible=True):
    """One row of analysis/decision_tests.yaml as toolkit/analysis/run.py writes it from a test's record: the share of
    units that change package, the regret and the dollars moved, each in percent beside its threshold, and the
    criteria that fired (toolkit/analysis/moves.py: infeasible, share, regret, dollars). A test under which the point
    plan is not feasible changes the plan by the producer's rule."""
    share = (25.0 if changes else 0.0) if share is None else share
    regret, dollars = (2.0, 6.0) if changes else (0.0, 0.0)
    fired = [] if not ran else (["infeasible"] if not feasible else []) + [k for k, v in (
        ("share", share), ("regret", regret), ("dollars", dollars)) if v > 100 * TEST_THRESHOLDS[k]]
    moved = int(ENVELOPE * dollars / 100)

    def n(x):
        return x if ran else None
    return {"test_id": tid, "path": f"{TESTS_DIR}{tid}.yaml" if ran else None,
            "status": "run" if ran else "not_run",
            "verdict": "NOT_RUN" if not ran else "CHANGES_PLAN" if fired else "NO_CHANGE",
            "can_move_plan": True if ran else None, "threshold_pct": 100 * TEST_THRESHOLDS["share"], "share_pct": n(share),
            "regret_pct": n(regret), "regret_threshold_pct": n(100 * TEST_THRESHOLDS["regret"]),
            "dollars_moved_pct": n(dollars), "dollars_threshold_pct": n(100 * TEST_THRESHOLDS["dollars"]),
            "dollars_reallocated_pct": n(dollars / 2), "dollars_share_pct": n(dollars), "population_changed_pct": n(share),
            "regret": n(_r(6.9 * regret)), "objective_gap": n(0.0), "point_plan_feasible": n(feasible),
            "dollars_moved": n(moved), "dollars_reallocated": n(moved / 2), "population_changed": n(int(219 * share)),
            "triggered_by": n(fired), "units_changed_by_group": n([]),
            "dollars_by_component": n({"A": 0.0, "B": -float(moved), "C": float(moved)}),
            "statement": ("This test was not run: its values could not be reached." if not ran else
                          f"This test changes the plan: {share:g}% of units change package, above the 10% threshold."
                          if changes else "This test can move the plan: it was re-solved for each value."),
            "detail": {}}


VARIED = {"parameter_values": "engine parameter {parameter} set to each listed value",
          "parameter_value": "engine parameter {parameter} set to each listed value",
          "interaction_rescale": "the joint effect of the listed components rescaled to each listed ratio",
          "calibration_target": "the calibration targets of the listed units scaled",
          "calibration_offset": "the offset of the named group left free",
          "unit_attribute": "a column of the units table replaced for the listed units",
          "burden_scale": "every package's outcomes in the units of each listed external reference's group scaled"}
CRITERION = ("the package changes in more than 10% of units, or keeping the point plan loses more than 1.0% of the "
             "re-optimised objective, or more than 5% of the allocatable budget is reallocated across units and "
             "components, or the point plan is not feasible under the test")


def _test_file(row, entry):
    """models/decision_tests/<id>.yaml for a test that ran (toolkit/analysis/decision_tests.py): the question, what was
    varied and the criterion in words, and the numbers of the row as shares."""
    def share(k):
        return None if row.get(k) is None else _r(row[k] / 100)
    changes = row["verdict"] == "CHANGES_PLAN"
    return {"statement": row["statement"], "status": row["status"], "can_move_plan": row["can_move_plan"],
            "finding": entry.get("finding") or f"decision_test:{row['test_id']}", "test_id": row["test_id"],
            "type": entry.get("type"), "question": entry.get("text", ""),
            "varied": VARIED.get(entry.get("type"), "").format(parameter=entry.get("parameter")),
            "criterion": CRITERION, "decision_changes": changes, "triggered_by": row["triggered_by"],
            "point_plan_feasible": row["point_plan_feasible"], "share_units_changed": share("share_pct"),
            "units_changed_by_group": [], "regret_share": share("regret_pct"), "regret": row["regret"],
            "objective_gap": row["objective_gap"], "dollars_share": share("dollars_share_pct"),
            "dollars_moved": row["dollars_moved"], "dollars_moved_share": share("dollars_moved_pct"),
            "share_threshold": TEST_THRESHOLDS["share"], "regret_threshold": TEST_THRESHOLDS["regret"],
            "dollars_threshold": TEST_THRESHOLDS["dollars"],
            "results": [{"value": v, "units_changed": int(len(UNITS) * (row["share_pct"] or 0) / 100),
                         "point_plan_feasible": row["point_plan_feasible"], "decision_changes": changes}
                        for v in (entry.get("values") or [1.0])],
            "rejected_values": [], "produced_by": "harness", "producer": "toolkit/analysis/decision_tests.py"}


# ------------------------------------------------------------------------------------------- the Outcomes files (§5.7)

def _pop(u):
    return 1000 + 150 * UNITS.index(u)


def _averted(u, package):
    return {"A": 0.0, "A+B": 40.0, "A+C": 44.0}[package] + (3.0 * UNITS.index(u) if package != "A" else 0.0)


def _unit_cost(u, package):
    return _pop(u) * sum(PRICE[c] for c in package.split("+"))


def _summary(median, spread=0.1):
    """A draws summary as toolkit/analysis/uncertainty.py writes one."""
    return {"median": _r(median), "ci_low": _r(median * (1 - spread)), "ci_high": _r(median * (1 + spread)),
            "mean": _r(median), "level": 0.95}


def _clean_plan(dearer=3):
    """Each unit's package: the cheaper option of the group (A+B) but in the last `dearer` units (A+C)."""
    return {u: "A+C" if i >= len(UNITS) - dearer else "A+B" for i, u in enumerate(UNITS)}


def _point(plan, envelope, scale):
    """analysis/point.yaml `data` (toolkit/analysis/core.py through run.py): the plan, its mix by component and by
    package, each unit's benefit and cost, the totals and the budget reserve the spec's affordability rule asks for."""
    per = {u: {"package": p, "benefit": _r(scale * _averted(u, p)), "cost": _unit_cost(u, p),
               "averted": {OUTCOME: _r(scale * _averted(u, p))}, "z": 0.0} for u, p in plan.items()}
    total, cost = _r(sum(x["benefit"] for x in per.values())), sum(x["cost"] for x in per.values())
    names = sorted(set(plan.values()), key=lambda k: (-list(plan.values()).count(k), k))
    rule = {"max_share_over_budget": ALLOWANCE, "enforce": True}
    return {"averted": {OUTCOME: total}, "averted_discounted_objective": total,
            "comparator_total": {OUTCOME: _r(0.4 * sum(map(_pop, UNITS)))},
            "mix": {**{c: sum(c in p.split("+") for p in plan.values()) for c in PRICE}, "no_package": 0},
            "packages": {k: list(plan.values()).count(k) for k in names},
            "cost_allocated": cost, "units_on_comparator": list(plan.values()).count("A"), "budget_allocatable": envelope,
            "discount_factor": 1.0, "cost_per_outcome_averted": _r(cost / total), "plan": dict(plan), "n_units": len(plan),
            "solver": {"status": "optimal", "method": "ilp", "objective": total, "cost": cost},
            "engine_identity": _identity(), "per_unit": per,
            "reserve": {"rule": rule, "outcome": OUTCOME, "fraction": RESERVE_SHARE, "budget_allocatable": envelope,
                        "budget_planned": RESERVE_SHARE * envelope, "reserve_usd": _r((1 - RESERVE_SHARE) * envelope),
                        "draws": DRAWS, "seed": 1, "value_given_up": 0.0, "value_given_up_pct": 0.0}}


def _afford(cost, envelope, draws, share):
    return {"budget_allocatable": envelope, "draws": draws, "draws_over_budget": int(round(share * draws)),
            "share_over_budget": share, "fixed_plan_cost": _summary(cost, 0.04),
            "rule": {"max_share_over_budget": ALLOWANCE, "enforce": True}, "rule_met": share <= ALLOWANCE}


def _interval(dominated):
    return {"level": 0.95, "covers": ["engine_parameters", "calibration", "costs"], "excludes": [],
            "prior_dominated": list(dominated), "spec_fixed": [], "fixed": [HELD], "fixed_other": 0}


def _uncertainty(point, draws):
    """analysis/uncertainty.yaml `data` (toolkit/analysis/uncertainty.py): the headline with the plan held fixed, as a
    median and an interval over the draws; what was drawn and what was held at one value; the cost against the budget."""
    h = point["averted"][OUTCOME]
    return {"draws": draws, "seed": 1, "covers": _interval([])["covers"], "excludes": [],
            "label": "95% interval over engine parameters, calibration and costs",
            "fixed_plan": {OUTCOME: _summary(h)}, "reoptimised_objective": _summary(1.01 * h),
            "units_same_package_share_median": 1.0, "units_same_package_in_90pct_of_draws": len(UNITS),
            "components_in_any_draw_plan": sorted(PRICE), "interval": _interval(["p3"]), "calibration_clamped": 0,
            "headline": {"name": HEADLINE, "outcome": OUTCOME},
            "draw_plan": {"sources": [{"kind": k, "status": "drawn", "what": f"{k.replace('_', ' ')}, each draw"}
                                      for k in _interval([])["covers"]],
                          "drawn": [{"name": p, "kind": "calibrated", "value": BEST[p], "units": "share", "status": "drawn",
                                     "source_kind": "calibration", "range": RANGE[p], "dist": "sample",
                                     "source": "the calibration's accepted sample"} for p in PARAMS],
                          "fixed": [{"name": HELD, "kind": "scenario", "value": 0.5, "units": "share", "status": "fixed",
                                     "reason": f"{HELD} is held fixed by name in the signed spec", "card_range": [0.0, 1.0],
                                     "source": "fixture study one, table 5"}],
                          "prior_dominated": ["p3"]},
            "affordability": _afford(point["cost_allocated"], point["budget_allocatable"], draws, 0.01),
            "prior_dominated": ["p3"]}


def _voi(point, draws, evppi=None):
    """analysis/value_of_information.yaml `data` (toolkit/analysis/value_of_information.py): the status follows the
    number of draws (below min_draws nothing is computed; below informative_draws every value is indicative); each
    input's value beside its permutation noise floor."""
    h, evpi = point["averted"][OUTCOME], 6.0
    if draws < VOI_MIN_DRAWS:
        return {"status": "too_few_draws", "draws": draws, "min_draws": VOI_MIN_DRAWS, "evpi": None, "inputs": [],
                "informative_draws": VOI_INFORMATIVE_DRAWS}
    values = {"p1": 3.6, "p2": 1.8, "p3": 0.1, **(evppi or {})}

    def row(name, value, floor=0.5):
        return {"input": name, "kind": "input", "members": [name], "bins": 4, "status": "computed", "evppi": value,
                "pct": _r(100 * value / h), "share_of_evpi": _r(value / evpi), "df": 3, "knots": 3, "noise_floor": floor,
                "noise_mean": _r(floor / 2), "conditional_plans": 2, "policy_share_over_budget": 0.01,
                "policy_rule": "within the allowance", "above_noise_floor": value > floor, "exceeds_evpi": False,
                "supported": value > floor}
    return {"status": "computed" if draws >= VOI_INFORMATIVE_DRAWS else "indicative", "draws": draws,
            "min_draws": VOI_MIN_DRAWS, "informative_draws": VOI_INFORMATIVE_DRAWS, "level": 0.95,
            "max_share_over_budget": ALLOWANCE, "decide_now": "point", "decide_now_method": "the plan of the point analysis",
            "expected_value_decide_now": h, "expected_value_perfect_information": _r(h + evpi),
            "evpi": {"value": evpi, "pct": _r(100 * evpi / h), "se_paired": 0.5},
            "inputs": [row(k, v) for k, v in sorted(values.items(), key=lambda kv: -kv[1])],
            "interval_note": "each value is reported beside its permutation noise floor",
            "outcome": OUTCOME, "discount_factor": 1.0, "budget_allocatable": point["budget_allocatable"], "groups": {},
            "groups_not_computed": [],
            "not_drawn": [{"input": HELD, "kind": "parameter", "reason": "held fixed by name in the signed spec"}],
            "evppi_over": "the drawn inputs"}


def _stability(loss=0.4):
    """analysis/stability.yaml `data` (toolkit/analysis/stability.py): one fold per group left out. `verdict` is about
    which package each unit keeps; `value_verdict` is the worst fold's loss of value against thresholds.yaml."""
    folds = [{"units_changed": 0, "share_units_changed": 0.0, "point_plan_feasible": True, "point_plan_infeasible": [],
              "regret": 0.0, "regret_share": 0.0, "triggered_by": [], "decision_changes": False, "group": g, "n_units": 4,
              "rank_corr": 0.97, "package_concordance_pct": 100.0, "benefit_gap_pct": 0.3,
              "total_benefit_loss_pct": _r(loss * (1 - 0.25 * k)), "z_shift_mean": 0.02} for k, g in enumerate(GROUPS)]
    return {"method": f"leave-one-{GROUPING}-out", "holdout_group": GROUPING, "calibration_method": "emulator_inversion",
            "n_folds": len(folds), "folds": folds, "folds_not_run": [],
            "metrics": {"total_benefit_loss_pct_mean": _r(loss * 0.75), "total_benefit_loss_pct_worst_fold": loss,
                        "rank_correlation_mean": 0.97, "rank_correlation_worst_fold": 0.97, "folds_with_rank_corr": 3,
                        "package_concordance_pct_mean": 100.0, "package_concordance_pct_worst_fold": 100.0,
                        "package_concordance_pct_worst_fold_ge5": None, "benefit_gap_pct_mean": 0.3,
                        "benefit_gap_pct_worst_fold": 0.3, "regret_pct_worst_fold": 0.0, "dollars_moved_worst_fold": 0,
                        "population_changed_worst_fold": 0},
            "verdict": "ROBUST",
            "value_verdict": ("ROBUST" if loss <= VALUE_LOSS["value_loss_pct_robust"] else
                              "FRAGILE" if loss <= VALUE_LOSS["value_loss_pct_fragile"] else "UNSTABLE"),
            "components": {"by_component": {c: {"provided": n, "absent": len(UNITS) - n, "changed": 0}
                                            for c, n in (("A", 12), ("B", 9), ("C", 3))},
                           "stable_provision": 24, "stable_absence": 12, "changed": 0},
            "value_thresholds_pct": dict(VALUE_LOSS), "headline_metric": "total_benefit_loss_pct_worst_fold"}


def _variants(point, draws, over=0.02):
    """analysis/plan_variants.yaml `data` (toolkit/analysis/variants.py): the optimised plan and one variant under a
    rule (the cheaper option of the group in every unit), with what it gives up and how often it costs more than the
    allocatable budget over the draws."""
    plan = {u: p.replace("C", "B") for u, p in point["plan"].items()}
    h, obj = point["averted"][OUTCOME], _r(sum(_averted(u, p) for u, p in plan.items()) * point["averted"][OUTCOME]
                                           / sum(_averted(u, p) for u, p in point["plan"].items()))
    cost, changed = sum(_unit_cost(u, p) for u, p in plan.items()), sum(plan[u] != point["plan"][u] for u in plan)
    return {"headline": "unconstrained", "outcome": OUTCOME,
            "unconstrained": {"objective": h, "cost": point["cost_allocated"], "units_no_package": 0,
                              "units_on_comparator": point["units_on_comparator"], "recommendation": "the optimised plan",
                              "plan": dict(point["plan"])},
            "variants": [{"id": "one_option", "type": "group_rule", "text": "the cheaper option of the group in every unit",
                          "components": ["B", "C"], "by": GROUPING, "objective": obj, "cost": cost, "units_no_package": 0,
                          "units_on_comparator": list(plan.values()).count("A"), "recommendation": "one option per group",
                          "status": "solved", "plan": plan, "value_lost": _r(h - obj), "value_lost_pct": _r(100 * (h - obj) / h),
                          "units_changed": changed, "share_units_changed": _r(changed / len(plan)),
                          "units_changed_by_group": [],
                          "uncertainty": {"draws": draws, "interval": _interval(["p3"]), "averted": {OUTCOME: _summary(obj)},
                                          "affordability": _afford(cost, point["budget_allocatable"], draws, over)}}]}


def _burden(draws, ratios=None):
    """analysis/burden_validation.yaml `data` (toolkit/analysis/burden.py): the model's comparator level against
    external references, each as a ratio against its band. Two rows are checked and two are report-only; the spec's
    decision test DT10 names one of the report-only rows (ref_G3)."""
    ratios = {"ref_G1": 1.05, "ref_G2": 0.95, "ref_G3": 1.1, "ref_all": 0.9, **(ratios or {})}
    rows = []
    for rid, ratio in ratios.items():
        rows.append({"id": rid, "group": rid[4:], "age_band": "all", "outcome": OUTCOME, "measure": "count", "per": None,
                     "year": 2025.0, "year_gap": 1.0, "value": 1000.0, "lower": 900.0, "upper": 1100.0, "kind": "estimated",
                     "role": "report_only" if rid in ("ref_G3", "ref_all") else "checked", "interval": "published",
                     "window": [2026.0, 2027.0], "group_by": None, "source": f"fixture reference table, row {rid}",
                     "source_id": "REF-1", "derived_from": [], "tolerance": {"type": "ratio", "ratio_band": list(BURDEN_BAND)},
                     "model": {"point": _r(1000 * ratio), "median": _r(1000 * ratio), "low": _r(900 * ratio),
                               "high": _r(1100 * ratio), "interval_from": "the uncertainty draws", "note": ""},
                     "ratio": ratio, "ratio_interval": [_r(0.9 * ratio), _r(1.1 * ratio)],
                     "status": "within" if BURDEN_BAND[0] <= ratio <= BURDEN_BAND[1] else "outside"})
    count = {k: sum(r["status"] == k for r in rows) for k in BURDEN_STATUSES}
    return {"arm": "comparator", "group_column": GROUPING, "window": [2026.0, 2027.0], "csv": BURDEN_CSV, "draws": draws,
            "references": rows, "summary": {**count, "report_only": 2, "n": len(rows)},
            "statement": f"{count['within']} of {len(rows)} references are within their band."}


def _emulator():
    """analysis/emulator.yaml `data` (toolkit/analysis/run.py): the emulator's accuracy on its held-out points."""
    return {"accuracy": {"holdout_points": 40, "training_points": 160, "gps": 3, "cal_logit_rmse": 0.05,
                         "reduction_r2_median": 0.97, "reduction_r2_p10": 0.91, "reduction_mae_median": 0.01,
                         "base_log_rmse": 0.04},
            "fit_seconds": 12.5, "features": ["x1", "pop"], "training_corrections": {},
            "domain": {"inputs": list(PARAMS), "lo": [PRIOR[p][0] for p in PARAMS], "hi": [PRIOR[p][1] for p in PARAMS]}}


def _boundary(plan, runner=None, units=UNITS[6:]):
    """analysis/emulator_boundary.yaml `data` (toolkit/analysis/boundary.py): the units whose package and runner-up are
    closest, run on the engine. On the clean run the rows between them hold every component."""
    runner = runner or {"A+B": "A+C", "A+C": "A+B", "A": "A+B"}
    rows = [{"unit": u, "chosen": plan[u], "runner_up": runner[plan[u]], "gap": _r(0.02 + 0.01 * k),
             "emulator": {"ratio_chosen": _r(1.2 + 0.01 * k), "ratio_runner_up": _r(1.1 + 0.01 * k)},
             "engine": {"ratio_chosen": _r(1.21 + 0.01 * k), "ratio_runner_up": _r(1.09 + 0.01 * k)},
             "engine_tie": False, "match": True} for k, u in enumerate(units)]
    return {"status": "judged", "units_requested": len(rows), "units": len(rows), "matched": len(rows),
            "share_matched": 1.0, "threshold": BOUNDARY_MIN, "tie_rel_gap": 0.01, "engine_ties": 0, "passes": True,
            "rows": rows, "held_out_source": "engine runs at the calibrated values, three per unit",
            "engine_runs": 3 * len(rows)}


def _replan(m, plan=None, envelope=None, scale=None, draws=None):
    """Recompute every file that follows from the plan, the envelope, the size of each unit's benefit or the number of
    draws, so the files of a variant agree with each other as the clean run's do."""
    a = m["plan_args"]
    a.update({k: v for k, v in (("plan", plan), ("envelope", envelope), ("scale", scale), ("draws", draws)) if v is not None})
    m["spec"]["constraints"]["envelope_usd"], m["spec"]["uncertainty"]["draws"] = a["envelope"], a["draws"]
    m["point"] = _point(a["plan"], a["envelope"], a["scale"])
    dominated = m["unc"]["prior_dominated"] if "unc" in m else ["p3"]
    m["unc"] = _uncertainty(m["point"], a["draws"])
    m["unc"]["prior_dominated"] = dominated
    m["voi"], m["pv"] = _voi(m["point"], a["draws"]), _variants(m["point"], a["draws"])
    m["bv"], m["emb"] = _burden(a["draws"]), _boundary(a["plan"])


def _estimates_spec(spec):
    """The signed spec of an `estimates` deliverable (toolkit/spec/estimates.py): outputs and a decision rule on one
    of them; no units to allocate to, so no options, comparator, constraints or plan."""
    for k in ("options", "comparator", "objective", "constraints", "outcomes_reported", "validation", "evidence"):
        del spec[k]
    spec["analysis"] = {"evaluation": "direct"}
    spec["uncertainty"] = {"level": 0.95, "draws": DRAWS, "seed": 1}
    spec["deliverable"] = {"kind": "estimates", "windows": {"w1": [2026.0, 2027.0]},
                           "outputs": [{"name": "q1", "metric": "m1", "window": "w1", "scale": "ratio"},
                                       {"name": "q2", "metric": "m2", "window": "w1", "scale": "share"}],
                           "decision": {"kind": "threshold", "output": "q1", "threshold": 1.0, "direction": "below",
                                        "probability": 0.9}}


def _estimates_full(est):
    """What analysis/estimates.yaml `data` holds beside the predictive band for the `estimates` deliverable
    (toolkit/analysis/estimates.py): each output as a median and an interval, the decision rule's verdict, and the
    decision tests, one per parameter range, each end with the rule's verdict there."""
    def output(median, sd, scale):
        return {"mean": median, "median": median, "sd": sd, "ci_low": _r(median - 1.96 * sd),
                "ci_high": _r(median + 1.96 * sd), "n": DRAWS, "metric": "m1", "window": "w1", "scale": scale,
                "label": "95% interval over calibration and observation"}

    def test(p):
        ends = [{"value": v, "kind": "declared", "median": {"q1": 0.8}, "interval": {"q1": [0.7, 0.9]}, "loglik_drop": 0.4,
                 "rejected_by_data": False, "fit_verified": True, "verdict": "yes", "probability": 0.97} for v in PRIOR[p]]
        return {"id": f"prior_range_{p}", "parameter": p, "class": "identified", "values": list(PRIOR[p]),
                "source": "the declared prior range", "status": "run", "ends": ends, "decision_changes": False,
                "ends_rejected_by_data": []}
    est.update({"outputs": {"q1": output(0.8, 0.05, "ratio"), "q2": output(0.3, 0.02, "share")},
                "windows": {"w1": [2026.0, 2027.0]},
                "decision": {"output": "q1", "threshold": 1.0, "direction": "below", "probability_required": 0.9,
                             "probability": 0.97, "verdict": "yes",
                             "statement": "q1 is below 1.0 with probability 0.97, above the 0.9 the rule asks for."},
                "decision_tests": [test(p) for p in PARAMS[:2]],
                "parameters": {p: {"median": BEST[p], "interval": RANGE[p],
                                   "prior": {"dist": "uniform", "source": "fixture prior", "range": PRIOR[p]}} for p in PARAMS}})
    return est


def _other(m, name):
    """The second run of the same question (`build(..., other=name)`, OTHERS)."""
    more = {u: "A+C" if i >= 6 else "A+B" for i, u in enumerate(UNITS)}       # three more units on the dearer option
    if name == "twin":
        pass                                # the same spec, plan and headline: the clean pair of a comparison
    elif name == "second":
        _replan(m, envelope=110000.0)
    elif name == "shifted":
        _replan(m, plan=more, envelope=110000.0)
    elif name == "shifted_same_spec":
        _replan(m, plan=more)
    elif name == "headline":
        m["spec"]["comparator"]["text"] = "every unit keeps component A alone, at the earlier level of delivery"
        _replan(m, scale=1.6)
    elif name == "other_question":
        m["spec"]["question"] = "Which units should get component C first? (synthetic fixture)"
    elif name == "unverified":
        m["signed_after"] = lambda spec: spec["constraints"].update(envelope_usd=120000.0)


def _set_test(m, tid, **how):
    m["dt"]["tests"] = [_test(tid, **how) if t["test_id"] == tid else t for t in m["dt"]["tests"]]


# The ledger's own vocabulary (agents/findings_ledger.py): STATUSES, and OWNER_STATE for the stages used here.
OWNER_STATE = {"PLAN": "SPEC", "ANALYZE": "ANALYZE", "WRITE": "WRITE"}
SEVERITY_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
# A finding a harness check raises about the run's files, with the id and the claim toolkit/analysis/contract.py gives
# it: kind, then the key "<artifact>:<input>". On the clean run it is fixed and the input is fresh; the planted
# variant that leaves it open also makes the input stale, so the register and the Steps check agree on every fixture.
STALE_ID = "analysis_contract_stale:analysis/point.yaml:analysis/calibration.yaml"
STALE_CLAIM = "artifact analysis/point.yaml: input analysis/calibration.yaml changed (or vanished) after it was computed"


def _finding(fid, source, severity, stage, claim, status="fixed", test=None, blocking=False):
    """One row of findings.jsonl with the ledger's twenty fields and the events the ledger writes on the way to
    `status` (a critic's finding follows its critique file; a gate finding absent at a later gate is fixed)."""
    critic = source.startswith("critique")
    kind, _, key = ("critique_blocker", "", "") if critic else fid.partition(":")
    events = [{"by": source, "round": 1, "status": "open"}]
    if status == "fixed":
        events.append({"by": source, "round": 2, "status": "fixed"} if critic else
                      {"by": "gate", "note": "absent at this gate", "round": 2, "status": "fixed"})
    elif status == "declared":
        events.append({"by": "scope_declaration", "round": 1, "status": "declared"})
    elif status in ("owner_accepted", "owner_declined"):
        events += [{"by": "scope_declaration", "round": 1, "status": "declared"}, {"by": "owner", "round": 1, "status": status}]
    return {"id": fid, "kind": kind, "severity": severity, "source": source, "key": key,
            "owner_state": OWNER_STATE[stage], "owner_state_known": True, "target_stage": stage,
            "resolution_type": "decision_test" if test else "fix", "status": status, "blocking": blocking,
            "scope_declarable": True, "first_seen_round": 1, "last_seen_round": 2 if status == "fixed" else 1,
            "instances": 1, "claim": claim,
            "fix_requires": "State the limit in the report, or test it." if critic else None,
            "done_when": [{"contains": "limit", "file": "manuscript.md"}] if critic else None,
            "decision_test": ({"decision_changes": False, "file": f"models/decision_tests/{test}.yaml",
                               "finding": fid, "valid": True} if test else None),
            "events": events}


def _findings():
    return {f["id"]: f for f in (
        _finding("D-001", "critique-domain", "HIGH", "PLAN",
                 "The comparator leaves out a delivery already funded, so the gain of every package is overstated."),
        _finding("D-002", "critique-domain", "MEDIUM", "WRITE",
                 "The report gives the point value of the headline without its interval."),
        _finding("R-001", "critique-redteam", "MEDIUM", "ANALYZE",
                 "The plan may depend on p1, which the fit narrows only to the middle of its prior."),
        _finding(STALE_ID, "rigor-artifacts", "HIGH", "ANALYZE", STALE_CLAIM))}


def _declaration(n, blocker, test, why):
    return {"id": f"SD-{n:03d}", "blocker_id": blocker, "why_unresolved": why,
            **({"decision_test": f"models/decision_tests/{test}.yaml"} if test else {})}


def _choice():
    return {"at": _at(75),
            "candidates": [{"ref": ENGINE_REF, "config": {}, "name": ENGINE["name"], "version": ENGINE["version"],
                            "eligible": True, "why_not": [], "mechanisms": list(MECHANISMS),
                            "benchmarks": 4, "departures": [], "benchmark_record": "verified",
                            "benchmarks_not_in_band": [],
                            "benchmarks_reported": [{"id": "B4", "why": REPORTED_WHY, "status": "in_band", "role": "held_out"}],
                            "card_benchmark_rows_disagree_with_record": [], "components_without_benchmark": {}},
                           # agents/analysis_backend.py eligible_engines(): a candidate whose card does not fit, and
                           # one whose card cannot be read (no name, no version, no list of any kind)
                           {"ref": "toolkit.engines.other.engine:OtherEngine", "config": {}, "name": "other-engine",
                            "version": "1", "eligible": False, "why_not": ["no component B", "no outcome outcome_averted"],
                            "mechanisms": ["closed-form outcome"], "benchmarks": 0, "departures": [],
                            "benchmark_record": None, "benchmarks_not_in_band": [], "benchmarks_reported": [],
                            "card_benchmark_rows_disagree_with_record": [], "components_without_benchmark": {}},
                           {"ref": "toolkit.engines.third.engine:ThirdEngine", "eligible": False,
                            "why_not": ["card unreadable: ValueError: the engine is configured by the signed spec"]}],
            "engine": {"ref": ENGINE_REF, "config": {}},
            "why": "the one eligible engine for the spec's components and outcome",
            "engine_departures": []}


def _no_data(value, reason, rule, test=None, source="spec default accepted by name"):
    return {"model_value": value, "model_source": source, "no_data": {"reason": reason, "borrowing_rule": rule},
            "chosen": {"value": value, "rule": rule}, **({"decision_test": test} if test else {})}


def _obs(source, where, year, sites, n, value, follow_up=5.0):
    """One observation of a dossier entry: the seven fields toolkit/spec/evidence_dossier.py asks for (OBS_FIELDS), the
    optional follow-up it documents, and a mark that the source was opened and checked. No producer writes that mark
    today (gap H2 of §7); the fixture records it under `checked`, as the date the source was opened, so that the clean
    run has observations that are marked and a variant has one that is not."""
    return {"source": source, "where": where, "year": year, "sites": sites, "n": n, "sampling_frame": "households",
            "value": value, FOLLOW_UP: follow_up, CHECKED: "2025-12-15"}


def _dossier():
    """evidence_dossier.yaml (toolkit/spec/evidence_dossier.py): five entries. Two hold observations, each followed for
    five years against the three-year outcome window, well clear of it; one of them is the study that the outcome
    adjustment of the C1 variants gives as its source, word for word. Three say `no_data`, each with a decision test."""
    return {"meta": {"compiled": "2026-01-01", "status": "fixture dossier: five entries",
                     "format": "toolkit/spec/evidence_dossier.py", "local_only": True},
            "inputs": {
                "parameter:p1": {
                    "model_value": {"value": BEST["p1"], "range": PRIOR["p1"], "dist": "uniform"},
                    "model_source": "fixture prior for p1",
                    "observations": [_obs("study one", "G1", "2019", "3", "400", "0.45"),
                                     _obs("study two", "G3", "2021", "2", "250", "0.55")],
                    "comparison": "the model value sits between the two observations",
                    "chosen": {"range": PRIOR["p1"], "rule": "the prior range"}, "decision_test": "DT3"},
                "parameter:b_add": {
                    "model_value": {"value": 0.05, "range": [0.0, 0.1], "dist": "uniform"},
                    "model_source": "fixture study two, table 4",
                    "observations": [_obs(OA["source"], "all three groups", "2022", "6", "900", "0.8")],
                    "comparison": "the ratio the study reports is the one the outcome adjustment applies",
                    "chosen": {"range": [0.0, 0.1], "rule": "the engine's range"}, "decision_test": "DT5"},
                "parameter:p2": _no_data(BEST["p2"], "no measurement of p2 was found", "draw over the owner's range",
                                         "DT8", "spec engine_parameters.p2 (owner's range)"),
                f"parameter:{HELD}": _no_data(0.5, f"no source gives {HELD}", "hold at the default; run both ends as a "
                                              "scenario", "DT7"),
                "unit_attribute:x1": _no_data("the units table's column x1", "x1 is a modelled surface, not a measurement",
                                              "use the surface; test the plan against the flat alternative", "DT9")}}


def _critique(m):
    """The rows of the sheet's spec critique, (severity, principle, check, finding, what resolves it), as
    toolkit/spec/spec_critique.py findings() gives them for this spec, in its words and in the sheet's order (by
    severity). The clean spec has no HIGH row: its comparator has components, its delivery period starts on the decision
    date, a burden-scale test stands in for an anchor (MEDIUM), every no-data input is drawn, tested or excluded with
    a reason, and its cost items have no range (MEDIUM). m["critique"] holds rows a variant adds."""
    spec, rows = m["spec"], []
    if spec["deliverable"]["kind"] == "estimates":
        rows += [("INFO", 2, "headline_comparator", "An estimates deliverable has no comparator plan.", "None"),
                 ("INFO", 3, "baseline_burden", "An estimates deliverable ranks no units.", "None")]
    else:
        scale = [t["id"] for t in spec["analysis"]["decision_tests"] if t["type"] == "burden_scale"]
        held = [(x["parameter"], x["reason"]) for x in spec["uncertainty"].get("excluded", []) if "parameter" in x]
        rows += [("INFO", 2, "headline_comparator", f"The comparator is package `{spec['comparator']['package']}`.",
                  "owner judgment: Confirm it is the plan as currently funded in every unit"),
                 ("MEDIUM", 3, "baseline_burden", "The engine's own burden ranks the units (validation.burden has no anchor "
                  "row (role anchor): its references only validate); decision test(s) " + ", ".join(scale) + " scale it "
                  "to external estimates, as a test, not in the plan.",
                  "Anchor the burden (validation.burden anchor rows) or declare this check")]
        rows += [("INFO", 6, "decision_input_ranges", "Held at a point, excluded from the draws with a reason "
                  "(uncertainty.excluded): " + "; ".join(f"`parameter:{p}`: {why}" for p, why in held) + ".",
                  "owner judgment: Confirm the reason shows the input cannot move the decision")] if held else []
        rows += [("MEDIUM", 6, "decision_input_ranges", "Cost item(s) " + ", ".join(sorted(i["id"] for i in spec["options"]["cost_items"]))
                  + " of allocated components have no uncertainty range and no price test.",
                  "Add them to the costs uncertainty source with a multiplier range, or to a cost_items test")]
    rows += m["critique"]
    order = {"HIGH": 0, "MEDIUM": 1, "DECLARED": 2, "INFO": 3}
    return sorted(rows, key=lambda r: order[r[0]])


def _sheet_text(m, spec_sha, sha=None):
    """decision_spec.signoff_sheet.md as toolkit/spec/signoff.py sheet() and write_sheet() write it: every section the
    code writes for any spec (SHEET_ALWAYS), in its order and under its headings, the sections it writes for a spec that
    has the thing (SHEET_SOME), and the last line with the hashes the sheet was generated from. Bodies are shortened to
    what a view reads; no heading or marker is renamed. The clean sheet marks no row HIGH (the card's range holds the
    spec's, and the critique has no HIGH finding); with m["high"] the card's range is narrower and
    toolkit/spec/engine_parameters.py marks the row. The word also appears where it marks nothing (the critique's
    count line, a table cell). `sha`: the hash of a file of the run, for the files the sheet names."""
    card_high = 5 if m["high"] else 6
    spec, rows = m["spec"], _critique(m)
    count = {k: sum(r[0] == k for r in rows) for k in ("HIGH", "MEDIUM", "DECLARED")}
    lines = ["# Decision spec sign-off sheet", "",
             f"Signing accepts `decision_spec.yaml` (sha256 `{spec_sha[:16]}`) and the derived data recorded in "
             "`data/derivations_record.json` (sha256 `none`). It also accepts what follows.", ""]
    sections = {
        SHEET_ALWAYS[0]: ["", f"{count['HIGH']} HIGH, {count['MEDIUM']} MEDIUM, {count['DECLARED']} declared. These are the "
                          "findings the gate would raise after compute; signing with one open accepts it.", "",
                          "| Severity | Principle | Check | Finding | What resolves it |", "|---|---|---|---|---|",
                          *[f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in rows], ""],
        SHEET_ALWAYS[1]: ["None: every engine unit attribute is bound to the units table, a value or the calibration.", ""]}
    oa = (spec.get("analysis") or {}).get("outcome_adjustments")
    if oa:
        sections[SHEET_SOME[0]] = [
            "Applied in every evaluation (point, draws, sensitivity, decision tests, plan variants, the boundary check): "
            "each package holding the component and not the reference has its outcomes set to the value times those of "
            "the same package with the reference in its place; a package holding both is left as the engine gives it "
            "(toolkit/analysis/outcome_adjust.py).",
            *[f"- **{a['id']}**: {a['component']} = {a['value']} x {a['reference']}. Source: {a['source']}" for a in oa], ""]
    sections[SHEET_ALWAYS[2]] = ["None: every column of every staged table is used.", ""]
    sections[SHEET_ALWAYS[3]] = [""]
    sections[SHEET_ALWAYS[4]] = ["No record (data/input_checks.json is missing): the check has not run.", ""]
    values = [f"- **{n}** = {e.get('value')} {e.get('units')}. Basis: {e.get('basis')}. Source: {e.get('source')}. "
              f"Reason: {e.get('reason')}. Uncertainty: drawn over {e.get('range')} ({e.get('dist')})."
              for n, e in (spec.get("engine_parameters") or {}).items()]
    sections[SHEET_ALWAYS[5]] = [
        *values, f"  - `{ENGINE_REF}`: card value 3 ratio (fitted; fitted once to benchmarks B1 and B4 (fixture card)); "
                 f"card range 1 to {card_high} ratio",
        *([""] + [f"- **HIGH**: the uncertainty range of {name} (1 to 5.5 ratio) reaches outside the card range of "
                  f"`{ENGINE_REF}` (1 to 5 ratio; outside_card_range: true). Signing accepts it; its source and reason "
                  "are above." for name in m["high"]] if m["high"] else []), ""]
    if (spec.get("validation") or {}).get("burden"):
        sections[SHEET_SOME[1]] = [
            "", "Signing attests that no reference below comes from, or is derived from, a source the calibration fits "
            f"(calibration sources: none declared). Reference file `{BURDEN_CSV}` (sha256 `{sha(BURDEN_CSV)[:16]}`).", "",
            "| Reference | Kind | Source id | Derived from | Citation |", "|---|---|---|---|---|",
            *[f"| {r['id']} | {r['kind']} | {r['source_id']} | none | {r['source']} |" for r in _burden(DRAWS)["references"]], ""]
    if (spec.get("evidence") or {}).get("dossier"):
        def row(key, e):
            field = (f"no data: {e['no_data']['reason']}; borrowing rule: {e['no_data']['borrowing_rule']}" if "no_data" in e else
                     "; ".join(f"{o['value']} ({o['source']}, {o['where']}, {o['year']}, n {o['n']}, {o['sampling_frame']})"
                               for o in e["observations"]))
            chosen = e["chosen"].get("range", e["chosen"].get("value"))
            note = " (owner decision, HIGH confidence in both studies)" if key == "parameter:p1" else ""
            return (f"| {key} | component {'B' if key.endswith('b_add') else 'A'} | {chosen} (the spec's own value) | "
                    f"{e['model_value']} ({e['model_source']}) | {field} | {chosen}{note} | {e['chosen']['rule']}"
                    + (f"; {e['decision_test']}" if e.get("decision_test") else "") + " | same |")
        sections[SHEET_ALWAYS[6]] = [
            f"Signing attests `{DOSSIER}` (sha256 `{sha(DOSSIER)[:16]}`).", "", f"**{DIFFERS}** (0; advisory: the owner decides):",
            "- none", "", "| Input | Used by | Execution will use | Model value (source) | Field / trial value (source) | Chosen | "
            "Rule; test | Differs |", "|---|---|---|---|---|---|---|---|",
            *[row(k, e) for k, e in sorted(m["dossier"]["inputs"].items())], "", "Every decision input has a sourced entry.", ""]
    else:
        sections[SHEET_ALWAYS[6]] = ["None: no component in the spec's options has declared decision inputs.", ""]
    sections[SHEET_SOME[2]] = [*[f"- `{c['ref']}`: " + ("eligible" if c["eligible"] else "ineligible: " + "; ".join(c["why_not"]))
                                 for c in m["choice"]["candidates"]], ""]
    for head, body in sections.items():
        if head not in m["sheet_without"]:
            lines += [head, *body]
    sources = {"spec_sha256": spec_sha,
               "burden_references_sha256": sha(BURDEN_CSV) if (spec.get("validation") or {}).get("burden") else "none",
               "evidence_dossier_sha256": sha(DOSSIER) if (spec.get("evidence") or {}).get("dossier") else "none",
               "derivations_sha256": "none", "geometry_evidence_sha256": "none"}
    return "\n".join(lines).rstrip("\n") + f"\n\n{SHEET_SOURCES}{json.dumps(sources, sort_keys=True)} -->\n"


# ---- the schedule and the trajectory record (§5.6), the archived sign-offs (§5.3)

def _schedule(m):
    """analysis/figure_data.yaml `data.schedule` (toolkit/analysis/figure_data.py schedule()): per group, the engine's
    delivery events with the number of units that hold each, in the order the producer sorts them. Components are the
    engine's (A and B); the label tells the two options of the group apart. History and routine events are the
    comparator's and hold in every arm; decision events follow each unit's package in the plan, lie inside the
    delivery period and are the only ones the decision pays for (toolkit/engines/hooks.py). The stored rows have no
    arm and no `charged` key: the producer drops them."""
    obj, plan = m["spec"]["objective"], m["point"]["plan"]
    d0, d1 = obj.get("delivery") or obj["horizon"]
    regions = {}
    for g in GROUPS:
        packages, ev = [plan[u].split("+") for u in UNITS if _group(u) == g], {}
        for component, label, kind, t0, n in (
                ("A", "past round", "history", SIM_START + 0.5, len(packages)), ("A", "past round", "history", SIM_START + 1.5, len(packages)),
                ("A", "round", "decision", d0 + 0.25, sum("A" in x for x in packages)),
                *[("B", label, "decision", d0 + dt, sum(option in x for x in packages))
                  for dt in (0.5, 1.5) for option, label in (("B", "half round"), ("C", "full round"))],
                ("A", "routine round", "routine", d1 + 0.5, len(packages))):
            if n:
                ev[json.dumps([component, label, kind, round(t0, 4), None])] = n
        regions[g] = {"units": len(packages), "events": [dict(zip(("component", "label", "kind", "t0", "t1"), json.loads(k)), units=n)
                                                         for k, n in sorted(ev.items())]}
    out = {"status": "ok", "delivery": [float(d0), float(d1)], "window": [float(x) for x in obj["horizon"]],
           "start": float(m["spec"]["horizon"]["start"]), "regions": regions}
    if m["sched_edit"]:
        m["sched_edit"](out)
    return out


def _figure_data(m):
    """analysis/figure_data.yaml `data`: the keys the views read (the schedule; each unit's comparator level, which the
    trajectory step samples units by). The plan's frontier, costs and replay are left out."""
    level = {u: _r(0.4 * (1 + 0.05 * _wobble(i))) for i, u in enumerate(UNITS)}
    return {"primary": OUTCOME, "comparator": m["spec"]["comparator"]["package"], "region": GROUPING,
            "burden": {u: {"population": float(_pop(u)), "per_person": v, "comparator_total": _r(v * _pop(u))} for u, v in level.items()},
            "schedule": _schedule(m)}


def _timeline_csv(schedule):
    """figures/timeline.csv, the table of the harness's timeline figure (toolkit/analysis/figures.py fig_timeline)."""
    return _csv(["region", "component", "label", "kind", "t0", "t1", "units", "region_units"],
                [[g, e["component"], e["label"], e["kind"], e["t0"], e["t1"], e["units"], r["units"]]
                 for g, r in schedule["regions"].items() for e in r["events"]])


def _sample(fd):
    """toolkit/analysis/trajectories.py sample_units(): per group, three units spread over the comparator level."""
    out = {}
    for g in GROUPS:
        ids = sorted((u for u in UNITS if _group(u) == g), key=lambda u: (fd["burden"][u]["per_person"], u))
        out[g] = [ids[k] for k in sorted({int(round(k * (len(ids) - 1) / 2)) for k in range(3)})]
    return out


def _trajectories(m, fd):
    """analysis/trajectories.yaml `data` (toolkit/analysis/trajectories.py run()): the sample, and one monthly series per
    arm for the sampled units pooled (`total`) and per group, from the start of the simulated period. The plan's series
    is the comparator's until delivery starts (but for m["series_gap"], a relative difference a variant plants) and
    below it after. Per-unit series and per-seed effects are left out. `engine` is the engine's full identity, which
    the producer holds to analysis/point.yaml `engine_identity`; `analysis_engine` is the analysis header's engine."""
    spec, sample = m["spec"], _sample(fd)
    d0, d1 = fd["schedule"]["delivery"]
    start, stop = spec["horizon"]["start"], spec["horizon"]["stop"]
    t = [_r(start + k / 12) for k in range(int(round((stop - start) * 12)))]

    def panel(level, ids):
        comparator = [_r(level * (1 + 0.1 * math.sin(2 * math.pi * x))) for x in t]
        plan = [_r(c * ((1 + m["series_gap"]) if x < d0 else 0.7 if x < d1 else 0.85)) for x, c in zip(t, comparator)]
        return {"t": t, "arms": {"comparator": {"m1": comparator}, "plan": {"m1": plan}}, "target": {}, "units": list(ids)}

    def level(ids):
        return sum(fd["burden"][u]["per_person"] * fd["burden"][u]["population"] for u in ids) / sum(fd["burden"][u]["population"] for u in ids)
    every = [u for ids in sample.values() for u in ids]
    return {"status": "ok", "sample": sample, "per_region": 3, "seeds": 3, "analysis_engine": dict(ENGINE), "engine": _identity(),
            "hooks_contract": HOOKS_CONTRACT, "selection": SELECTION, "metrics": ["m1"],
            "metric_info": {"m1": {"label": "level of m1", "units": "share", "aggregate": "mean", "denominator": "people"}},
            "calibration_metric": None, "delivery": [d0, d1], "window": list(fd["schedule"]["window"]),
            "series": {"total": panel(level(every), every), "regions": {g: panel(level(ids), ids) for g, ids in sample.items()}},
            "compute": {"backend": "local", "workers": 2, "n": 3 * len(every), "n_failed": 0, "wall_s": 40.0}}


def _dynamics(tr):
    """The checks the harness pairs with the trajectory figure (toolkit/analysis/figures.py trajectory_checks()),
    recomputed here from the record by the producer's rule and written in its words: the largest relative difference
    between the arms before delivery starts (`flag` above 1e-9); and, when delivery ends before the outcome window does,
    the step in the plan's series across the end of delivery, for information. This spec names no calibration metric
    and window, so the producer writes no comparison with a target interval."""
    d0, d1 = tr["delivery"]
    panels, worst = {"total": tr["series"]["total"], **tr["series"]["regions"]}, 0.0
    for s in panels.values():
        pre = [(c, p) for x, c, p in zip(s["t"], s["arms"]["comparator"]["m1"], s["arms"]["plan"]["m1"]) if x < d0 - 1e-9]
        if pre:
            worst = max(worst, max(abs(c - p) for c, p in pre) / max(max(abs(c) for c, _ in pre), 1e-12))
    rows = [{"check": "plan_equals_comparator_before_delivery", "value": _r(worst), "status": "pass" if worst <= 1e-9 else "flag",
             "detail": "largest relative difference before delivery"}]
    if d1 < tr["window"][1] - 1e-9:
        s = tr["series"]["total"]
        i1 = next(i for i, x in enumerate(s["t"]) if x >= d1)
        plan = s["arms"]["plan"]["m1"]
        rows.append({"check": "end_of_delivery_step", "value": {"m1": _r((plan[i1] - plan[i1 - 1]) / max(abs(plan[i1 - 1]), 1e-12))},
                     "status": "info", "detail": "relative step in the plan's series across the end of delivery (month before vs after)"})
    return rows


def _earlier(spec):
    """The two specs signed before the current one, oldest first: the first differs from the second in who decides,
    the second from the current spec in the seed of the draws, so each sign-off changed one section of the spec."""
    second = copy.deepcopy(spec)
    second["uncertainty"]["seed"] = 2
    first = copy.deepcopy(second)
    first["decision_maker"] = "programme lead and deputy (synthetic fixture)"
    return [first, second]


def _archive(m, files):
    """evidence_planner/superseded_signoff_<date>/: the sign-offs a later one replaced, as a stored run keeps them (each
    folder holds the record and the spec it signed, under either of the two names a stored run uses; the later one
    also the sheet the owner read). m["archive_bare"]: one more folder that holds a sheet and no record."""
    notes = ("first sign-off (fixture)", "who decides corrected (fixture)")
    for n, (day, spec) in enumerate(zip(EARLIER, m["earlier"])):
        text = _spec_text(spec, [])
        sha, folder = hashlib.sha256(text.encode()).hexdigest(), f"{ARCHIVE}{day}/"
        files[folder + (SPEC_FILES[1] if n else f"decision_spec.signed_{sha[:8]}.yaml")] = text
        files[folder + SIGNOFF] = json.dumps({"signed_by": "owner", "signed_at": f"{day}T10:00:00+00:00", "note": notes[n],
                                              "sha256": {"decision_spec.yaml": sha}, "producer": "toolkit/spec/signoff.py",
                                              "schema_version": 1}, indent=1)
        if n:
            files[folder + SHEET] = (f"# Decision spec sign-off sheet\n\nSigning accepts `decision_spec.yaml` (sha256 `{sha[:16]}`) "
                                     "and the derived data recorded in `data/derivations_record.json` (sha256 `none`). It also "
                                     "accepts what follows.\n\n" + "\n\n".join(SHEET_ALWAYS[1:6]) + "\n")
    if m["archive_bare"]:
        files[f"{ARCHIVE}{EARLIER[1]}b/{SHEET}"] = "# Decision spec sign-off sheet\n\nA sheet that was written and never signed.\n"


# ---- the engine record and the figure set (§5.4, §5.12)

def _seeds(row, computed, low=0.96, high=1.04):
    """A benchmark row with its computed value, four seeds spread around it, and the status
    toolkit/engines/benchmark.py gives a value against its band (`in_band` follows the status)."""
    seeds = [_r(computed * (low + (high - low) * k / 3)) for k in range(4)]
    inside = row["band"][0] <= computed <= row["band"][1]
    row.update(computed=computed, se_log=0.02, per_seed=seeds, seed_min=min(seeds), seed_max=max(seeds),
               in_band=inside, status="in_band" if inside else "out_of_band")
    return row


def _bench_row(bid, metric, role, target, band, source, **more):
    """One row of the engine record's benchmark table, with the keys toolkit/engines/benchmark.py writes. The clean
    value is the band's centre on the log scale, so it is clear of both edges, and so are its seeds."""
    row = {"id": bid, "metric": metric, "role": role, "target": target, "band": list(band), "source": source,
           "band_sha256": hashlib.sha256(f"{bid} band".encode()).hexdigest()}
    _seeds(row, _r(math.sqrt(band[0] * band[1])))
    row.update(more)
    return row


def _engine_record():
    """analysis/engine.yaml `data` (toolkit/analysis/run.py): the card as the run used it, the engine's parameters and
    the values the signed spec sets, shaped as a stored run's. Four benchmarks, all in band at the centre of the band;
    B4 is a cost benchmark whose band spans more than a factor of ten, and the engine choice only reports it."""
    def par(name, value, kind, units, lo, hi, source):
        return {"name": name, "value": value, "kind": kind, "units": units, "lo": lo, "hi": hi, "source": source,
                "verified": "matched"}
    fitted_src = "fitted once to benchmarks B1 and B4 (fixture card)"
    return {
        "card": {"name": ENGINE["name"], "version": ENGINE["version"], "mechanisms": list(MECHANISMS),
                 "components": {"A": [0, 1], "B": [0, 1]}, "unit_attributes": {"x1": "level before any package, [0, 1]"},
                 "outputs": {"per_capita": ["outcome"], "windows": ["m1"], "cost_drivers": ["A_given"]},
                 "age_groups": ["all"], "valid_ranges": {"x1": [0.0, 1.0]},
                 "fitted": {"p2": {"value": 3.0, "lo": 1.0, "hi": 6.0, "box": [0.5, 8.0], "benchmarks": ["B1", "B4"]},
                            "f1": {"value": 0.4, "lo": 0.3, "hi": 0.6, "box": [0.0, 1.0], "benchmarks": ["B1", "B4"]}},
                 "benchmarks": [
                     _bench_row("B1", "component A against none, outcome ratio at 12 months", "fit", 0.6, [0.4, 0.9],
                                "fixture study one, table 2"),
                     _bench_row("B2", "component B against none, outcome ratio over 24 months", "held_out", 0.5,
                                [0.36, 0.7], "fixture study two, table 1"),
                     _bench_row("B3", "components A and B against A alone, outcome ratio at 24 months", "held_out", 0.8,
                                [0.64, 1.0], "fixture study three, table 3"),
                     _bench_row("B4", "cost per outcome averted, component A against none", "held_out", 12.0, [2.0, 72.0],
                                "fixture costing review", outcome_averted_per_capita=0.1, cost_per_capita=1.2,
                                comparator_outcome_per_capita=0.4, share_averted=0.25, price_floor=3.6,
                                target_below_price_floor=False, share_averted_needed_for_band=0.05)],
                 "notes": ["one outcome only; no change of the level within the horizon"]},
        "parameters": [par("p1", 0.5, "calibrated", "share", 0.2, 0.8, "calibrated per run to the unit targets"),
                       par("p2", 3.0, "fitted", "ratio", 1.0, 6.0, fitted_src),
                       par("p3", 30.0, "calibrated", "days", 10.0, 50.0, "calibrated per run to the unit targets"),
                       par("f1", 0.4, "fitted", "share", 0.3, 0.6, fitted_src),
                       par("b_add", 0.05, "scenario", "share", 0.0, 0.1, "fixture study two, table 4"),
                       par(HELD, 0.5, "scenario", "share", 0.0, 1.0, "fixture study one, table 5")],
        # toolkit/spec/engine_parameters.py rows(): one entry per value the signed spec sets
        "spec_values": {"p2": {"value": 3.1, "units": "ratio", "card_value": 3.0, "card_kind": "fitted",
                               "card_range": [1.0, 6.0], "card_source": fitted_src, "range": [1.0, 5.5],
                               "outside_card_range": False, "outside": None, "source": "the owner's range",
                               "reason": "no measurement", "uncertainty": "drawn over [1, 5.5] (uniform)"}}}


def _identity():
    return {"name": ENGINE["name"], "version": ENGINE["version"],
            **{k: hashlib.sha256(f"toy {k}".encode()).hexdigest() for k in ("code_sha256", "params_sha256", "config_sha256")}}


def _at_spec_values():
    """analysis/benchmarks_at_spec_values.yaml (toolkit/engines/benchmark.py, written for the harness step of
    toolkit/spec/engine_parameters.py): the benchmark a decision test of the spec reads, recomputed at the spec's
    values. It has no artifact header; its record file holds its hash and the spec's."""
    row = _bench_row("B2", "component B against none, outcome ratio over 24 months", "held_out", 0.5, [0.36, 0.7],
                     "fixture study two, table 1")
    _seeds(row, 0.51)
    by = {s: int(s == row["status"]) for s in BENCH_STATUSES}
    arm = {"arm": "a1", "setting": "s1", "ref": ENGINE_REF, "config": None, "config_name": None,
           "engine_identity": _identity(), "config_sha256": None, "superseded": {}}
    return {"producer": "toolkit/engines/benchmark.py", "engine_identity": _identity(), "benchmarks": [row],
            "initialisation": {"method": "no-package run-in before the horizon starts", "evidence": []},
            "settings_calibrated": {}, "fitted_used": {"p2": 3.0, "f1": 0.4}, "configs_validated": {},
            "configs_not_exercised": [], "seeds": [11, 12, 13, 14], "n_sims": 8,
            "summary": {"in_band": by["in_band"], "total": 1, "by_status": by, "held_out_in_band": by["in_band"],
                        "held_out_total": 1},
            "spec_parameters": {"p2": 3.1}, "arm_engines": {"B2": {"arms": {"a1": arm}}}}


def _png(name):
    """A valid PNG of one pixel whose colour follows from `name`, so each figure's image has its own hash."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    pixel = b"\x00" + hashlib.sha256(name.encode()).digest()[:3]
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixel, 9)) + chunk(b"IEND", b""))


SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>\n'


def _figure_set(m):
    """The figures of the run as (name, tier, the analysis files drawn from, title, what it shows). `standard`: a
    subset of the standard set of toolkit/analysis/figures.py under its own names and its three tiers, with one figure
    per calibration table the method writes. `estimates`: the four figures toolkit/analysis/estimates.py draws."""
    if m["fig"]["set"] == "estimates":
        return [("diagnostics_ppc", "diagnostics", [EST], None, None), ("diagnostics_rt", "diagnostics", [EST], None, None),
                ("estimates_q1", "decision", [EST], None, None), ("estimates_q2", "decision", [EST], None, None)]
    cal = JC if m["method"] == "history_matching" else CAL
    return [("data_coverage", "data_model", [], "12 units in 3 groups; every engine attribute bound", "data coverage and provenance"),
            ("parameters", "data_model", [ENGINE_REC], "6 engine parameters; 1 set by the spec", "engine parameters and their ranges"),
            *[(os.path.basename(rel)[:-4], "data_model", [cal], f"{os.path.basename(rel)[12:-4].replace('_', ' ')} of the "
               "calibration, 12 units", "calibration diagnostics") for rel in _figures(m)],
            ("timeline", "simulation", [FD], "what each package delivers, and when", "delivery schedule"),
            ("trajectories", "simulation", [TRAJ], "outcome over time under the plan and the comparator",
             "simulated outcome over time"),
            ("headline", "decision", ["analysis/point.yaml", UNC], "the plan against the comparator, with its interval",
             "headline result"),
            ("frontier", "decision", ["analysis/point.yaml"], "outcome averted against the envelope", "budget frontier"),
            ("decision_tests", "decision", [DT], "which decision tests change the plan",
             "decision tests: does the plan change?")]


def _figure_files(m, files, sha, artifact):
    """analysis/figures.yaml and, per figure, its image, vector copy, table and provenance record, as the producer of
    m["fig"]["set"] writes them (toolkit/analysis/figures.py render() and _save(); toolkit/analysis/estimates.py _fig();
    scripts/figure_validator.py for the record). Returns the manifest's entries for the images."""
    f, standard = m["fig"], m["fig"]["set"] == "standard"
    shown = [{"path": LABELS, "sha256": sha(LABELS), "kind": "presentation"}] if standard else []
    entries, missing, listed, earlier = [], [], {}, hashlib.sha256(b"the source as it was when the figure was drawn").hexdigest()
    plan = _figure_set(m)
    for name, tier, drawn_from, title, shows in plan:
        reason = (f["not_produced"].get(name) or (NO_TESTS_FILE if DT in drawn_from and m["dt"] is None else None)
                  or (NO_TRAJECTORIES if TRAJ in drawn_from and TRAJ not in files else None))
        if reason:
            missing.append({"figure": name, "reason": reason, "tier": tier})
            continue
        png, sources = f"figures/{name}.png", drawn_from + (["decision_spec.yaml", "data/derived/units.csv"] if standard else [])
        files[png], files[png[:-4] + ".svg"] = _png(name), SVG
        if standard:
            files.setdefault(png[:-4] + ".csv", _csv(["item", "value"], [[name, 1], ["units", len(UNITS)]]))
            entry = {"figure": name, "path": png, "svg": png[:-4] + ".svg", "table": png[:-4] + ".csv", "title": title,
                     "caption": f"{title[0].upper()}{title[1:]}; drawn by the harness from the files listed as its sources.",
                     "shows": shows, "sources": sources, "offset_free": True, "theme": "light", "tier": tier,
                     "group": "decision" if tier == "decision" else "diagnostics", "presentation": copy.deepcopy(shown)}
            if name == "trajectories":      # toolkit/analysis/figures.py fig_trajectories(): the checks travel with the figure
                entry["checks"] = m["dynamics"]
        else:
            entry = {"id": name, "path": png, "svg": png[:-4] + ".svg", "sources": sources, "tier": tier}
        if name in f["no_path"]:
            del entry["path"]
        entries.append(entry)
        record = {"expected_annotations": [], "expected_n_data_points": None,
                  "generator_script": "/work/toolkit/analysis/figures.py", "paths_relative_to": "run_dir", "png_path": png,
                  "presentation": copy.deepcopy(shown), "produced_by": "harness", "producer": FIG_PRODUCER["standard"],
                  "source_hashes": {x: earlier if f["stale"].get(name) == x else sha(x) for x in sources},
                  "written_at_utc": _at(233)}
        text = json.dumps(record, indent=2, sort_keys=True)
        if name not in f["no_record"]:
            files[png + ".provenance.json"] = text[:len(text) // 2] if name in f["bad_record"] else text
        if name not in f["unlisted"]:
            listed[png] = {"producer": FIG_PRODUCER[f["set"]], "role": "figure", "sha256": sha(png)}
    tiers = {}
    for name, tier, *_ in plan if standard else []:
        tiers.setdefault(tier, []).append(name)
    overviews = {t: f"figures/overview_{t}.png" for t in tiers} if entries else {}
    for rel in overviews.values():
        files[rel] = _png(rel)
    groups = {"decision": list(tiers.get("decision", [])),
              "diagnostics": [n for t in ("data_model", "simulation") for n in tiers.get(t, [])]} if standard else {}
    inputs = sorted({x for _, _, drawn_from, _, _ in plan for x in drawn_from if x in files})
    artifact(FIGS, "figures", f["producer"] or FIG_PRODUCER[f["set"]], inputs,
             {"figures": entries, "not_produced": missing, "tiers": tiers, "groups": groups, "overviews": overviews}, 234)
    return listed


def _report_files(m):
    """report.md, manuscript.md and the render record. The report names figures by path (and one by its file name
    alone); it also uses the word trajectories in a sentence, which names no figure."""
    lines = ["# Report (synthetic fixture)", "", "The plan, its tests and the trajectories behind it are set out below.", ""]
    for rel in m["fig"]["report"]:
        lines += [f"![{os.path.basename(rel)[:-4].replace('_', ' ')}]({rel})", ""]
    report = "\n".join(lines)
    script = ("# Manuscript (synthetic fixture)\n\nThe limit of this analysis is stated with each result.\n\n"
              + "".join(f"See {rel}.\n" for rel in m["fig"]["manuscript"]))
    return {REPORT: report, MANUSCRIPT: script,
            RENDER: json.dumps({"report_sha256": hashlib.sha256(report.encode()).hexdigest(),
                                "writer_span": [len(lines[0]) + 2, len(report)]}, indent=1)}


def _model(method, figures="standard", kind="package_per_unit"):
    names = (["figures/estimates_q1.png", "estimates_q2.png"], ["figures/diagnostics_ppc.png"]) if figures == "estimates" \
        else (["figures/headline.png", "figures/frontier.png", "decision_tests.png"], ["figures/data_coverage.png"])
    m = {"method": method, "spec": _spec(method),
         # the engine record (§5.4): `bound`: the analysis headers hold the engine choice's hash; `choice_after`: an
         # edit made to engine_choice.json after the analysis; `cut`: the engine record stops inside its benchmark table
         # `bsv`: the benchmarks recomputed at the spec's values, when the run has them (the clean run has none: the
         # file has no artifact header and is outside the manifest, which the Steps view raises: gap H13)
         "eng": _engine_record(), "bsv": None, "bound": True, "choice_after": None, "cut": False,
         # the figure set (§5.12): `producer` another producer name; `not_produced` {figure: reason}; `stale` {figure:
         # the source whose recorded hash is an earlier one}; `no_path` entries with no path; `no_record`, `bad_record`
         # figures whose provenance record is absent or cut off; `unlisted` images the manifest leaves out; `dropped`,
         # `redrawn` images removed or replaced after the manifest; `report`, `manuscript` what the texts name
         "fig": {"set": figures, "producer": None, "not_produced": {}, "stale": {}, "no_path": (), "no_record": (),
                 "bad_record": (), "unlisted": (), "dropped": (), "redrawn": (), "report": names[0], "manuscript": names[1]}, "unc": {"draws": 50, "seed": 1, "calibration_clamped": 0,
                                                          "prior_dominated": ["p3"]},
         # every test ran and none changes the plan or comes near a threshold (§9: a plan-changing test, a near miss and a
         # test that did not run are each something the Outcomes view raises, so each is a variant)
         "dt": {"tests": [_test("DT1")] + [_test(t["id"]) for t in _spec_tests(method)]},
         # the register's sources (§6); `comments` are comment lines of the spec, `disk` an edit made to the spec on
         # disk after the sign-off, `old` the two files an older run keeps dispositions in
         "findings": _findings(), "ledger_extra": [], "choice": _choice(), "dossier": _dossier(), "high": [],
         "scope": None,
         "signed_by": "owner", "signoff_note": "fixture sign-off", "comments": [], "disk": None, "old": None,
         # `gates`: the run keeps its gate records; `unjudged`: findings whose done-when test the last gate did not
         # judge; `stale`: analysis/point.yaml records another hash for its input than the file has;
         # `high`: the parameters the sign-off sheet marks HIGH
         "gates": True, "unjudged": (), "stale": False}
    if method == "history_matching":
        m["jc"] = _joint()
    else:
        m["cal"], m["est"] = _posterior(), _estimates()
    # the Outcomes files (§5.7): `plan_args` what the plan-dependent files are computed from (_replan); `raw` {file:
    # a change to its text as written}; `signed_after` an edit made to the signed copy of the spec after the sign-off
    m.update(kind=kind, plan_args={"plan": _clean_plan(), "envelope": ENVELOPE, "scale": 1.0, "draws": DRAWS},
             stab=_stability(), emu=_emulator(), raw={}, signed_after=None)
    # the Spec and Scenarios files (§5.3, §5.6): `critique` rows a variant adds to the sheet's spec critique;
    # `sheet_without` headings the sheet lacks; `no_sheet` the run holds no sheet; `archive_bare` one more archive
    # folder with a sheet and no record; `earlier` the specs of the archived sign-offs; `sched_edit` a change to the
    # stored schedule; `traj` whether the run holds a trajectory record, `traj_edit` a change to it, `series_gap` the
    # relative difference between the arms before delivery starts, `dyn_edit` a change to the checks as stored
    m.update(critique=[], sheet_without=(), no_sheet=False, archive_bare=False, sched_edit=None, traj=True, traj_edit=None,
             series_gap=0.0, dyn_edit=None, dynamics=[])
    _replan(m)
    if kind == "estimates":
        _estimates_spec(m["spec"])
        m["est"], m["dt"] = _estimates_full(m["est"]), None
        m["unc"] = {"draws": DRAWS, "seed": 1, "covers": ["calibration", "observation"], "excludes": [],
                    "label": "95% interval over calibration and observation",
                    "interval": {"level": 0.95, "covers": ["calibration", "observation"], "excludes": [], "prior_dominated": ["p3"]},
                    "fixed_plan": {"not_applicable": "an estimates deliverable has no plan"},
                    "outputs": copy.deepcopy(m["est"]["outputs"]), "prior_dominated": ["p3"]}
    m["earlier"] = _earlier(m["spec"])
    return m


# ------------------------------------------------------------------------------------------- the planted edits

def _flag(m):
    _move_change(m, "G2", -4.5)


def _near(m):
    _move_change(m, "G3", 2.5)


def _block(m, proceed=False):
    jc = m["jc"]
    rows = list(range(0, 36, 3))
    jc.update(status="blocked", blocked=True, blocks=[{"code": "offset_sampling_low_acceptance", "detail": {
        "rows": 40, "failed": len(rows), "failed_rows": rows, "by_component": [{"blocks": [OFFSET], "rows": rows}],
        "why": "region-sample rows whose offset and unit values never met the group targets; they were redrawn"}}])
    most = jc["thresholds"]["history_matching"]["bias_draws_max"]      # the block shows in the offset's own row too
    jc["bias"][OFFSET].update(draws=2, proposed=most, mean=-0.31, sd=0.02, q025=-0.33, q975=-0.29)
    next(c for c in jc["bias_components"] if OFFSET in c["blocks"]).update(
        no_accepted=len(rows), draws_max=most, acceptance_in_region=_r(1 - len(rows) / 40))
    jc["unmatched_decision"].update(proceed=proceed, why_not=None if proceed else
                                    "block offset_sampling_low_acceptance is not declared in the spec")


def _declare(m, ran=True):
    """A signed declaration covering offset S2|G2 that names decision test DT2, and DT2's result."""
    d = {"offset": OFFSET, "reason": "the round 2 offset for G2 is not reachable at sd 0.15; tested by DT2",
         "decision_test": "DT2"}
    m["spec"]["calibration"]["joint"]["unmatched"]["declared"].append(dict(d))
    m["spec"]["analysis"]["decision_tests"].append({"id": "DT2", "type": "calibration_offset", "offset": OFFSET,
                                                    "text": "Does the plan change when G2's offset is freed?"})
    m["jc"]["unmatched_decision"]["declared"].append(dict(d))
    m["dt"]["tests"].append(_test("DT2", ran))


def _blocked_declared(m, ran=True):
    _block(m, proceed=True)
    _declare(m, ran)


def _flag_declared(m):
    _flag(m)
    _declare(m)


def _set_range(m, p, lo, hi, best=None):
    reg = m["jc"]["region"]
    reg["ranges"][p] = [lo, hi]
    reg["contraction"][p] = _r(1 - (hi - lo) / (PRIOR[p][1] - PRIOR[p][0]))
    reg["interval95"][p]["region"] = [lo, hi]
    for w in (m["jc"]["waves"][-1], m["jc"]["figure_data"]["waves"][-1]):
        w["ranges"][p] = [lo, hi]
    if best is not None:
        m["jc"]["best"]["theta"][p] = best
        reg["interval95"][p]["point"] = best


def _tiny(m):
    reg = m["jc"]["region"]
    reg.update(accepted=40, share_of_prior=2e-05)
    for w in (m["jc"]["waves"][-1], m["jc"]["figure_data"]["waves"][-1]):
        w.update(accepted=40, share_of_prior=2e-05)
    _set_range(m, "p2", PRIOR["p2"][0], PRIOR["p2"][1], best=PRIOR["p2"][0])


def _on_bound(m):
    if m["method"] == "history_matching":
        _set_range(m, "p2", PRIOR["p2"][0], RANGE["p2"][1], best=PRIOR["p2"][0])
    else:
        cal = m["cal"]
        cal["theta"]["p2"] = PRIOR["p2"][0]
        cal["sample"]["rows"][0][1] = PRIOR["p2"][0]
        cal["diagnostics"]["at_bound"] = ["p2"]


def _bound_gap(share):
    def edit(m):
        lo, hi = PRIOR["p3"]
        _set_range(m, "p3", RANGE["p3"][0], hi - share * (hi - lo))
    return edit


def _assumed(m):
    m["jc"]["unit_targets"]["U07"][0]["source"] = _source("unit", assumed=True)


def _dominated(m):
    m["unc"]["prior_dominated"] = ["p1", "p3"]


def _emulator_gap(m):
    _group_error(m["jc"], "G3", emulator=1.9 * m["jc"]["targets"]["grp_r1:G3"]["sd_total"])


def _unit_bounds(m):
    for u in UNITS[:3]:
        m["jc"]["units"][u]["region_touches_bound"] = ["hi"]
        m["jc"]["units"][u]["z_interval"][1] = UNIT_HI


def _one_run(m):
    for v in m["jc"]["verification"]["units"].values():
        v["runs"] = 1


def _not_converged(m):
    m["cal"]["diagnostics"].update(converged=False, message="ABNORMAL: iteration limit reached", rhat_max=1.41)


def _unit_score(m, u, engine, emulator):
    """Unit u's one target: the engine's verification moves to implausibility `engine` (model above the observed
    value, simulated value to match) and the emulator's implausibility at the best point to `emulator`."""
    _unit_error(m["jc"], u, engine * m["jc"]["unit_targets"][u][0]["sd_total"], emulator)
    _changes(m["jc"])


def _near_cutoff(m):
    _unit_score(m, "U03", 2.6, 2.3)


def _unit_gap(m):
    _unit_score(m, "U03", 1.9, 0.3)


def _one_side(m):
    if m["method"] == "history_matching":
        jc = m["jc"]
        _, before = _errors(jc, "G2")
        for u in UNITS[4:8]:
            _unit_score(m, u, 1.6, 1.4)
        units, _ = _errors(jc, "G2")       # the group's earlier-round target moves as far, so the change is as it was
        up = before + sum(units.values()) / len(units)
        _group_error(jc, "G2", err=up, emulator=0.9 * up)
        _changes(jc)
        return
    for f in m["cal"]["diagnostics"]["fit"]:
        if f["groups"] == ["G2"]:
            exp = f["observed"] + 1.5 * math.sqrt(f["variance"])
            f.update(expected=_r(exp), expected_share=_r(exp / f["n"]), variance=_r(exp * (1 - exp / f["n"])))


def _second_measure(m):
    m["jc"]["region"]["interval95"]["p3"]["contraction95"] = 0.8


def _diagnostic(key, value):
    def edit(m):
        m["cal"]["diagnostics"][key] = value
    return edit


def _no_series(m):
    del m["est"]["posterior_predictive"]


# ---- the science register's edits (§5.8, §6)

def _restate(m, fid, **how):
    f = m["findings"][fid]
    m["findings"][fid] = _finding(fid, f["source"], f["severity"], f["target_stage"], f["claim"], **how)


def _left_open(fid, blocking=False):
    return lambda m: _restate(m, fid, status="open", blocking=blocking)


def _stale_open(m):
    _restate(m, STALE_ID, status="open", blocking=True)
    m["stale"] = True          # what the finding is about is true of the run's files


def _gate(m, rnd):
    """A gate decision as scripts/validate_critique_yaml.py writes it for round `rnd`; done_when_status holds, per
    critic's finding, what scripts/done_when.py run() records: {passes, detail, mechanical, frozen_round, result,
    open, severity}. In round 1 no test passes; in the last round the test of each finding the ledger holds as
    fixed passes, unless the variant leaves it not judged."""
    last, status = rnd == len(GATES), {}
    for f in m["findings"].values():
        if not f["source"].startswith("critique"):
            continue
        passes = (None if f["id"] in m["unjudged"] else True) if last and f["status"] == "fixed" else False
        status[f["id"]] = {
            "passes": passes, "mechanical": passes is not None, "frozen_round": 1,
            "detail": ("pass manuscript.md exists; pass manuscript.md: found 'limit'" if passes else
                       "manuscript.md: 'limit' not found" if passes is False else "needs a judgement that was not recorded"),
            "result": "passed" if passes else "failed" if passes is False else "not_judged",
            "open": f["status"] == "open" or not last, "severity": f["severity"]}
    held = [{"id": f["id"], "reviewer": f["source"], "severity": f["severity"]} for f in m["findings"].values()
            if f["severity"] == "HIGH" and f["source"].startswith("critique") and (f["status"] == "open" or not last)]
    results = [e["result"] for e in status.values()]
    return json.dumps({
        "unresolved_high": held, "accept_blockers": [], "structural_mismatch": False, "structural_reviewers": [],
        "spec_violations": [], "registry_violations": [], "rigor_violations": [], "blocker_attempts": {},
        "escalation_required": False, "rounds_remaining": len(GATES) - rnd, "action": "PATCH" if held else "ACCEPT",
        "rule_matched": 2 if held else 1, "rationale": f"{len(held)} unresolved HIGH blocker(s)." if held else "No blocker is open.",
        "declared": [], "declared_prev": [], "done_when_status": status,
        "check_summary": {k: results.count(k) for k in ("passed", "failed", "not_judged")}, "post_write": False}, indent=1)


def _scope_declare(m, blocker, test, why):
    """scope_declaration.yaml gains a declaration (the file is written only when something is declared)."""
    m["scope"] = m["scope"] or {"declarations": []}
    m["scope"]["declarations"].append(_declaration(len(m["scope"]["declarations"]) + 1, blocker, test, why))


def _depart(m, *subjects, status="signed"):
    """A signed engine departure per subject (a key of DEPARTURES): the entry in the signed spec, and its copy in
    engine_choice.json as toolkit/engines/benchmark_coverage.py gate() records it: the status of the signature, then
    for a benchmark its own status and role in the engine record and the components it covers (`component=value`),
    for a component `no benchmark`, role `none` and the component."""
    for s in subjects:
        d = DEPARTURES[s]
        m["spec"].setdefault("engine_departures", []).append(dict(d))
        if "benchmark" in d:
            row = _bench(m, s)
            failing = {"benchmark_status": row["status"], "role": row["role"], "components": ["B=1"]}
        else:
            failing = {"benchmark_status": "no benchmark", "role": "none", "components": [s]}
        for deps in (m["choice"]["engine_departures"], _chosen(m)["departures"]):
            deps.append({**d, "status": status, **failing})


def _declared(m):
    """One of each declaration, each naming a decision test that ran with no change: a signed departure for benchmark
    B2, which is out of band, and one for component B; for history matching a declared calibration miss; finding
    R-001 held as declared by the ledger and declared in scope_declaration.yaml."""
    _b2_out(m)
    _depart(m, "B")
    if m["method"] == "history_matching":
        m["spec"]["calibration"]["joint"]["unmatched"]["declared"].append(dict(CAL_DECLARED))
        m["jc"]["unmatched_decision"]["declared"].append(dict(CAL_DECLARED))
    _restate(m, "R-001", status="declared", test="DT3")
    _scope_declare(m, "R-001", "DT3", SCOPE_WHY)


def _on_declared(*edits):
    """An edit made on top of the run with one of each declaration."""
    def edit(m):
        _declared(m)
        for e in edits:
            e(m)
    return edit


def _c12_declared(m):
    _restate(m, "D-001", status="declared", test="DT1", blocking=True)
    _scope_declare(m, "D-001", "DT1", "The funded delivery cannot be costed from the inputs of this run; DT1 runs the "
                                      "plan across the range that it moves.")


def _each_departure(m):
    return [d for deps in (m["choice"]["engine_departures"], m["choice"]["candidates"][0]["departures"]) for d in deps]


def _departure_no_test(m):
    for d in _each_departure(m) + m["spec"]["engine_departures"]:     # signed that way: the choice copies the spec's entry
        if d.get("component") == "B":
            del d["decision_test"]


def _disk_only(m):
    m["disk"] = lambda spec: spec["uncertainty"]["excluded"].append(
        {"parameter": "q7", "reason": "added to the file after the sign-off; no signature covers it"})


def _merge_test(m):
    m["dossier"]["inputs"]["unit_attribute:x2"] = _no_data(
        "the units table's column x2", "x2 has no measurement", "use the default; DT3 covers it with p1", "DT3")


def _merge_input(m):
    m["spec"]["uncertainty"]["excluded"].append({"parameter": "q4", "reason": "q4 is held fixed by name"})
    m["dossier"]["inputs"]["unit_attribute:q4"] = _no_data(
        "the units table's column q4", "q4 has no measurement", "hold at the default; DT1 runs the plan across it", "DT1")


def _lead(m):
    _declared(m)
    m.update(signed_by="lead", signoff_note="lead mode: the harness accepts the drafted spec")
    for d in _each_departure(m):
        d["status"] = "unsigned"       # toolkit/engines/benchmark_coverage.py signature(): a lead's signature


def _older(m):
    for fid in ("D-002", "R-001"):
        _restate(m, fid, status="open")
    m["scope"] = None
    m["old"] = {"declared": ["R-001"], "tested": ["D-002"]}


PROSE = ["# DEPARTURE (owner decision, fixture): the comparator is the funded plan, not the status quo; DT1 covers it.",
         "# LIMITATION (owner-visible): the best point of p2 sits near the floor of its range.",
         "# The values below are drawn from two studies. ASSUMPTION (owner-visible): the two share one sampling frame.",
         "# The tests of the declared choice are listed under analysis; see the DEPARTURE block above."]


def _prose(m):
    m["comments"] = list(PROSE)


def _bad_line(m):
    whole = json.dumps(m["findings"]["D-002"], sort_keys=True)
    m["ledger_extra"].append(whole[:len(whole) // 2])       # a row cut off in the middle, as a write that stopped


def _odd_status(m):
    m["findings"]["D-002"]["status"] = "resolved"            # not one of the ledger's STATUSES


def _ghost(m):
    _scope_declare(m, "X-404", "DT1", "Declared by an id that no finding has.")


def _no_tests_file(m):
    m["dt"] = None


# ---- the Model view's edits (§5.4)

def _bench(m, bid):
    return next(b for b in m["eng"]["card"]["benchmarks"] if b["id"] == bid)


def _chosen(m):
    return m["choice"]["candidates"][0]


def _moved(bid, computed, **more):
    """Benchmark `bid` of the engine record gets another computed value (seeds and status to match); where the engine
    choice only reports the benchmark, its row there copies the status, as the producer does."""
    def edit(m):
        row = _seeds(_bench(m, bid), computed)
        row.update(more)
        if more.get("status"):
            row["in_band"] = more["status"] == "in_band"
        for r in _chosen(m)["benchmarks_reported"]:
            if r["id"] == bid:
                r["status"] = row["status"]
    return edit


def _b2_out(m, ran=True):
    """Benchmark B2 out of band, with the signed departure that names it and decision test DT4."""
    _moved("B2", 0.78)(m)
    _depart(m, "B2")
    if not ran:
        _set_test(m, "DT4", ran=False)


def _b3_out_disk(m):
    _moved("B3", 1.12)(m)
    m["disk"] = lambda spec: spec.setdefault("engine_departures", []).append(
        {"engine": ENGINE["name"], "benchmark": "B3", "decision_test": "DT1",
         "reason": "added to the file after the sign-off; no signature covers it"})


def _seed_cross(m):
    seeds = [0.5, 0.58, 0.66, 0.93]
    _bench(m, "B1").update(per_seed=seeds, seed_min=min(seeds), seed_max=max(seeds))


def _no_benchmarks(m):
    card = m["eng"]["card"]
    card["benchmarks"] = []
    for f in card["fitted"].values():
        f["benchmarks"] = []
    _chosen(m).update(benchmarks=0, benchmarks_reported=[])


def _list_disagrees(m):
    _chosen(m)["benchmarks_not_in_band"].append({"id": "B1", "role": "fit", "status": "out_of_band"})


def _unbound(m, named=False):
    m["bound"] = False
    if named:
        m["spec"]["engine"] = {"ref": ENGINE_REF, "config": {}}


def _choice_edited(m):
    m["choice_after"] = lambda doc: doc.update(at=_at(240))


def _outside_card(m):
    m["eng"]["spec_values"]["p2"].update(card_range=[1.0, 5.0], outside_card_range=True, outside="range")
    next(p for p in m["eng"]["parameters"] if p["name"] == "p2")["hi"] = 5.0
    m["eng"]["card"]["fitted"]["p2"]["hi"] = 5.0
    m["spec"]["engine_parameters"]["p2"]["outside_card_range"] = True
    m["high"] = ["p2"]


def _flag_contradicts(m):
    m["eng"]["spec_values"]["p2"].update(range=[1.0, 7.0], uncertainty="drawn over [1, 7] (uniform)")


def _unsourced(m):
    by = {p["name"]: p for p in m["eng"]["parameters"]}
    by["b_add"]["verified"] = "unverified"
    by[HELD].update(lo=None, hi=None)


def _fitted_on_bound(m):
    m["eng"]["card"]["fitted"]["f1"]["value"] = 0.6
    next(p for p in m["eng"]["parameters"] if p["name"] == "f1")["value"] = 0.6


def _b1(**change):
    return lambda m: _bench(m, "B1").update(change)


def _share(tid, share):
    def edit(m):
        t = next(t for t in m["dt"]["tests"] if t["test_id"] == tid)
        t.update(share_pct=share, statement=f"This test can move the plan: {share:g}% of units change package, under "
                                            f"the {t['threshold_pct']:g}% threshold.")
    return edit


# ---- the Figures view's edits (§5.12)

FIG_ROLE = {"standard": {"decision": "frontier", "other": "parameters", "record": "calibration_fit",
                         "stale": ("headline", "analysis/point.yaml"), "plain": "data_coverage"},
            "estimates": {"decision": "estimates_q1", "other": "diagnostics_ppc", "record": "diagnostics_rt",
                          "stale": ("estimates_q1", EST), "plain": "diagnostics_ppc"}}


def _role(m, role):
    return FIG_ROLE[m["fig"]["set"]][role]


def _fig_stale(m):
    name, source = _role(m, "stale")
    m["fig"]["stale"][name] = source


def _fig_absent(m, named=False):
    m["fig"]["not_produced"]["trajectories"] = NO_TRAJECTORIES
    if named:
        m["fig"]["report"].append("figures/trajectories.png")


def _fig_unused(m):
    m["fig"]["report"].remove(f"figures/{_role(m, 'decision')}.png")


def _fig_set(key, role):
    return lambda m: m["fig"].update({key: (_role(m, role),)})


def _fig_producer(m):
    m["fig"]["producer"] = "toolkit/analysis/other.py"


def _fig_files(role, *ends):
    """The files of one figure, by its role in the figure set of the build (FIG_ROLE)."""
    def files(method, figures):
        name = FIG_ROLE[figures][role]
        return [f"figures/{name if isinstance(name, str) else name[0]}{end}" for end in ends]
    return files


HM, BOTH, POST = ("history_matching",), METHODS, ("posterior",)
SIGNED = SPEC_FILES + [JC, DT]
DECLARED = SPEC_FILES + [CHOICE, ENGINE_REC, LEDGER, SCOPE, GATES[1]]     # what the `declared` variant itself changes
DEPARTED = SPEC_FILES + [CHOICE]                                           # what a signed departure itself changes
UNIT_FILES = [JC, "figures/calibration_fit.csv", "figures/calibration_residuals.csv", "figures/calibration_unit_fit.csv"]
# `across` lists the checks of OTHER views the same edit must move, by their full ids ({"register.undisposed": "look"}); an
# Outcomes case holds every view's checks to it, so an edit that moves a check it does not name is caught. `against`
# (Outcomes, run_shift): the edit is to the comparison, not to the run: the clean run is read against the run that
# `build(..., other=<against>)` writes. `kinds`: the deliverable kinds an edit is defined for (default package_per_unit).
# Defect name -> view (the view whose check moves; none: the Fit view), case (scorecard, or None), check (the one
# check of that view that moves), status (what it moves to), also (any other check of that view the same edit must
# move, by rule), unpinned (checks of that view whose status the edit leaves open), figure_sets (the figure sets a
# Figures edit is defined for),
# edit (exactly what is changed, in one sentence), files (the files the edit itself touches, per method; files that
# record a hash of those follow, and so does the last gate record when a critic's finding changes its status), methods, and the function that applies it. `status: ok` marks a variant kept to
# show a rule's edge: nothing may be raised on it. `also` lists any other check the same edit must move, by rule.
DEFECTS = {
    "C2_change_flagged": {
        "case": "C2", "check": "changes", "status": "problem", "methods": HM, "apply": _flag, "files": UNIT_FILES,
        "edit": "The engine's values for group G2 are moved (its four later-round unit targets down, its earlier-round "
                "group target up, each still well inside the cutoff) so that the group's change between rounds, "
                "recomputed from those rows in survey_changes.pairs, has z -4.5 against the limit z_max 3.0, with "
                "flagged true and its id listed in survey_changes.flagged; nothing declares it."},
    "C2_change_flagged_declared": {
        "case": "C2", "check": "changes", "status": "look", "methods": HM, "apply": _flag_declared,
        "files": SIGNED + UNIT_FILES[1:],
        "edit": "The same flagged G2 pair (z -4.5, from the same moved target rows), covered by a declaration for offset S2|G2 in the signed spec's "
                "calibration.joint.unmatched.declared and in unmatched_decision.declared that names decision test "
                "DT2, which ran with verdict NO_CHANGE."},
    "C2_change_near_miss": {
        "case": "C2", "check": "changes", "status": "look", "methods": HM, "apply": _near, "files": UNIT_FILES,
        "edit": "The engine's values for group G3 are moved (unit targets up, group target down) so that its change in "
                "survey_changes.pairs, recomputed from those rows, has z 2.5, inside the limit z_max 3.0 but within 25% "
                "of it, with flagged left false."},
    "C13_blocked_undeclared": {
        "case": "C13", "check": "status", "status": "problem", "methods": HM, "apply": _block, "files": [JC],
        "edit": "status becomes blocked with one block, offset_sampling_low_acceptance on offset S2|G2, that no "
                "declaration covers; that offset's row in bias has 2 of 1,024 draws accepted and its row in "
                "bias_components counts the failed rows; unmatched_decision.proceed is false."},
    "C13_blocked_declared": {
        "case": "C13", "check": "status", "status": "look", "methods": HM, "apply": _blocked_declared, "files": SIGNED,
        "edit": "The same block on offset S2|G2 with unmatched_decision.proceed true, covered by a declaration in "
                "the signed spec and in unmatched_decision.declared that names decision test DT2, which ran with "
                "verdict NO_CHANGE."},
    "C13_blocked_test_not_run": {
        "across": {'outcomes.tests_that_move': 'look'},
        "case": "C13", "check": "status", "status": "problem", "methods": HM, "files": SIGNED,
        "apply": lambda m: _blocked_declared(m, ran=False),
        "edit": "The same block and declaration naming DT2, but analysis/decision_tests.yaml records DT2 with "
                "status not_run and verdict NOT_RUN."},
    "C13_tiny_region": {
        "case": "C13", "check": "region", "status": "look", "methods": HM, "apply": _tiny,
        "files": [JC, "figures/calibration_region.csv", "figures/calibration_waves.csv"],
        "edit": "region.accepted drops from 600 to 40 of 2,048 candidates (below region_min_candidates 64; "
                "share_of_prior 0.00002), p2's accepted range equals its prior range (contraction 0) and "
                "best.theta.p2 sits on the prior's lower bound."},
    "C13_best_on_bound": {
        "case": "C13", "check": "region", "status": "look", "methods": BOTH, "apply": _on_bound,
        "files": {"history_matching": [JC, "figures/calibration_region.csv"], "posterior": [CAL]},
        "edit": "p2's best value is set to its prior's lower bound 1.0 (history matching: region.ranges.p2[0] and "
                "best.theta.p2; posterior: theta.p2, one sample row and diagnostics.at_bound)."},
    "bound_within_tolerance": {
        "case": "C13", "check": "region", "status": "look", "methods": HM, "apply": _bound_gap(5e-07),
        "files": [JC, "figures/calibration_region.csv"],
        "edit": "p3's accepted upper end is moved to 0.5e-6 of the prior range's width below the prior's upper "
                "bound, which counts as reached."},
    "bound_outside_tolerance": {
        "case": "C13", "check": "region", "status": "ok", "methods": HM, "apply": _bound_gap(2e-06),
        "files": [JC, "figures/calibration_region.csv"],
        "edit": "p3's accepted upper end is moved to 2e-6 of the prior range's width below the prior's upper "
                "bound, which does not count as reached."},
    "C3_assumed_sd": {
        "case": "C3", "check": "target_error", "status": "look", "methods": HM, "apply": _assumed, "files": [JC],
        "edit": "The source text of unit target unit_r2:U07 labels its sd 0.3 an ASSUMPTION."},
    "C14_prior_dominated": {
        "across": {'outcomes.voi_support': 'look'},
        "case": "C14", "check": "constrained", "status": "look", "methods": BOTH, "apply": _dominated, "files": [UNC],
        "edit": "analysis/uncertainty.yaml data.prior_dominated gains p1, whose range contracted by more than half."},
    "emulator_vs_engine": {
        "case": None, "check": "emulator_vs_engine", "status": "look", "methods": HM, "apply": _emulator_gap,
        "files": [JC, "figures/calibration_unit_fit.csv"],
        "edit": "The emulator's value at the best point for group target grp_r1:G3 moves to implausibility 1.9 "
                "(modelled_at_best to match) against the engine's 0.27, a difference above 1 with both under the "
                "cutoff 3.0; the engine's values, and so the change between rounds, are as on the clean run."},
    "unit_bounds": {
        "case": None, "check": "unit_bounds", "status": "look", "methods": HM, "apply": _unit_bounds,
        "files": [JC, "figures/calibration_bound_share.csv"],
        "edit": "Units U01, U02 and U03 get region_touches_bound ['hi'] (interval end 6.0), so 4 of 12 unit "
                "intervals reach the unit parameter's bound instead of 1."},
    "replicates": {
        "case": None, "check": "replicates", "status": "look", "methods": HM, "apply": _one_run, "files": [JC],
        "edit": "verification.units[u].runs is 1 for every unit instead of 4."},
    "C13_not_converged": {
        "case": "C13", "check": "status", "status": "problem", "methods": POST, "apply": _not_converged,
        "files": [CAL], "also": {"convergence": "problem"},
        "edit": "diagnostics.converged is false (message ABNORMAL: iteration limit reached; rhat_max 1.41)."},
    # Added with the checks the spec gained after the first acceptance pass (section 3a).
    "near_cutoff": {
        "case": None, "check": "near_cutoff", "status": "look", "methods": HM, "apply": _near_cutoff, "files": UNIT_FILES,
        "edit": "The engine's verification of unit target unit_r2:U03 moves to implausibility 2.6 (emulator 2.3), "
                "under the cutoff 3.0 and within 25% of it, with the unit still matched."},
    "group_bias": {
        "case": None, "check": "group_bias", "status": "look", "methods": BOTH, "apply": _one_side,
        "files": {"history_matching": UNIT_FILES, "posterior": [CAL, "figures/calibration_fit.csv"]},
        "edit": "Every unit target of group G2 gets a model value above its observed value (history matching: engine "
                "implausibility 1.6, emulator 1.4, and the group's earlier-round target moved up as far, so its change "
                "between rounds is as on the clean run; posterior: expected 1.5 sd above observed), so the group's "
                "mean signed score is beyond 1 while no single target nears a cutoff."},
    "emulator_vs_engine_unit": {
        "case": None, "check": "emulator_vs_engine", "status": "look", "methods": HM, "apply": _unit_gap,
        "files": UNIT_FILES,
        "edit": "The engine's verification of unit target unit_r2:U03 moves to implausibility 1.9 against the "
                "emulator's 0.3, a difference above 1 on a unit target, with every group target unchanged."},
    "C14_second_measure": {
        "case": "C14", "check": "constrained", "status": "look", "methods": HM, "apply": _second_measure, "files": [JC],
        "edit": "region.interval95.p3.contraction95 becomes 0.8 while region.contraction.p3 stays 0.2, for a "
                "parameter that analysis/uncertainty.yaml lists as prior-dominated."},
    "convergence_rhat": {
        "case": None, "check": "convergence", "status": "look", "methods": POST, "files": [CAL],
        "apply": _diagnostic("rhat_max", 1.03),
        "edit": "diagnostics.rhat_max becomes 1.03 (above 1.01) with diagnostics.converged still true."},
    "convergence_ess": {
        "case": None, "check": "convergence", "status": "look", "methods": POST, "files": [CAL],
        "apply": _diagnostic("ess_bulk_min", 250.0),
        "edit": "diagnostics.ess_bulk_min becomes 250 (below 400) with diagnostics.converged still true."},
    "series_not_recorded": {
        "case": None, "check": "status", "status": "ok", "methods": POST, "apply": _no_series, "files": [EST],
        "edit": "analysis/estimates.yaml loses data.posterior_predictive, so the method records no fit over time."},
    # The science register (§5.8, §6): `view` names the view whose check moves (the entries above are the Fit view's).
    # `follows: False` marks an edit made after the run finished, which no recorded hash follows.
    "C12_open_high": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": _left_open("D-001", blocking=True), "files": [LEDGER, GATES[1]], "also": {"decides_unknown": "look"},
        "edit": "In findings.jsonl the critic's finding D-001 (HIGH) is left with status open and blocking true, its "
                "last event open, with no decision test and no declaration."},
    "C12_declared_tested": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _c12_declared, "files": [LEDGER, SCOPE, GATES[1]],
        "edit": "The same finding D-001 (HIGH, blocking) has status declared with decision test DT1, which ran with "
                "verdict NO_CHANGE, and scope_declaration.yaml declares it by its id with that test."},
    "test_unlinked": {
        "across": {'outcomes.tests_that_move': 'look'},
        "view": "register", "case": None, "check": "tests_unlinked", "status": "look", "methods": BOTH,
        "apply": lambda m: _set_test(m, "DT1", changes=True), "files": [DT], "also": {"undisposed": "look"},
        "edit": "analysis/decision_tests.yaml records DT1, a test no finding, declaration or dossier entry names, with "
                "verdict CHANGES_PLAN (25% of units against the 10% threshold)."},
    # §9: the clean run holds nothing declared, so every declared case is a variant; this one holds one of each
    "declared": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _declared, "files": DECLARED,
        "edit": "The run holds one of each declaration, each naming a decision test that ran with verdict NO_CHANGE: "
                "benchmark B2 is out of band (0.78 against 0.36 to 0.7) under a signed engine departure naming DT4, "
                "component B has a signed departure naming DT5, the signed spec declares the calibration miss "
                "grp_r1:G1 with DT6 (history matching), and the ledger holds finding R-001 as declared with DT3, "
                "which scope_declaration.yaml declares by its id."},
    "declared_test_not_run": {
        "across": {'outcomes.tests_that_move': 'look'},
        "view": "register", "case": None, "check": "declared_untested", "status": "problem", "methods": BOTH,
        "apply": _on_declared(lambda m: _set_test(m, "DT4", ran=False)), "files": DECLARED + [DT],
        "edit": "On the run of variant declared, analysis/decision_tests.yaml records DT4, the test the signed engine "
                "departure for benchmark B2 names, with status not_run and verdict NOT_RUN."},
    "declared_no_test": {
        "view": "register", "case": None, "check": "declared_untested", "status": "look", "methods": BOTH,
        "apply": _on_declared(_departure_no_test), "files": DECLARED, "also": {"decides_unknown": "look"},
        "edit": "On the run of variant declared, the signed spec's engine departure for component B, and its copy in "
                "engine_choice.json, have no decision_test key."},
    "open_no_test": {
        "view": "register", "case": None, "check": "decides_unknown", "status": "look", "methods": BOTH,
        "apply": _left_open("D-002"), "files": [LEDGER, GATES[1]], "also": {"undisposed": "look"},
        "edit": "In findings.jsonl the critic's finding D-002 (MEDIUM, not blocking) is left with status open and no "
                "decision test."},
    "process_open": {
        "view": "register", "case": None, "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _stale_open, "files": [LEDGER, "analysis/point.yaml"],
        "edit": "In findings.jsonl the finding a harness check raised about a run file (analysis_contract_stale for "
                "analysis/point.yaml and its input analysis/calibration.yaml, source rigor-artifacts, HIGH) is left "
                "with status open and blocking true, and analysis/point.yaml records another hash for that input "
                "than the file has, so the Steps check on recorded inputs fires on the same run."},
    "implemented_unverified": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "look", "methods": BOTH,
        "apply": lambda m: m.update(unjudged=("D-002",)), "files": [GATES[1]],
        "edit": "gate_r2.json, the last gate record, gives the done-when test of finding D-002 (MEDIUM), which the "
                "ledger holds as fixed, as not judged (passes null) instead of passed."},
    "sheet_high": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": lambda m: m.update(high=["p2"]), "files": [SHEET],
        "edit": "decision_spec.signoff_sheet.md gives a card range that the spec's range of p2 reaches outside and "
                "marks that line HIGH, and the sign-off record holds no hash for the sheet (toolkit/spec/signoff.py "
                "sign() records none), so nothing binds the line to the signature."},
    "implemented_no_gate": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": lambda m: m.update(gates=False), "files": list(GATES),
        "edit": "The run has no gate record (gate_r1.json and gate_r2.json are not in the run folder), while the "
                "ledger holds three findings as fixed, one of them HIGH."},
    "declared_only_on_disk": {
        "view": "register", "case": None, "check": "declared_untested", "status": "ok", "methods": BOTH,
        "apply": _disk_only, "files": ["decision_spec.yaml"], "follows": False,
        "edit": "After the sign-off, decision_spec.yaml on disk gains an uncertainty.excluded entry for parameter q7 "
                "that the signed copy, and the hash in the sign-off record, do not hold."},
    "merge_shared_test": {
        "view": "register", "case": None, "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _on_declared(_merge_test), "files": DECLARED + [DOSSIER],
        "edit": "On the run of variant declared, evidence_dossier.yaml gains a no-data entry unit_attribute:x2 whose "
                "decision_test is DT3, the test the declared finding R-001 already names."},
    "merge_excluded_nodata": {
        "view": "register", "case": None, "check": "declared_untested", "status": "ok", "methods": BOTH,
        "apply": _merge_input, "files": SPEC_FILES + [DOSSIER],
        "edit": "The signed spec's uncertainty.excluded gains parameter q4 and evidence_dossier.yaml gains a no-data "
                "entry unit_attribute:q4 whose decision_test is DT1, which ran with verdict NO_CHANGE."},
    "signed_by_lead": {
        "view": "register", "case": None, "check": "undisposed", "status": "look", "methods": BOTH,
        "apply": _lead, "files": DECLARED + ["decision_spec.signoff.json"],
        "edit": "The run of variant declared, signed by lead instead of the owner: decision_spec.signoff.json records "
                "signed_by lead, and engine_choice.json, as the producer writes it under a lead's signature, marks "
                "both engine departures unsigned."},
    "older_layout": {
        "view": "register", "case": None, "check": "undisposed", "status": "ok", "methods": BOTH,
        "apply": _older, "files": [LEDGER, OLD_DECLARED, OLD_GATE, GATES[1]],
        "also": {"declared_untested": "look", "decides_unknown": "look"},
        "edit": "The run has an older layout: findings.jsonl holds D-002 and R-001 as open with no decision test, "
                "there is no scope_declaration.yaml, declared_findings.yaml lists R-001 as declared and "
                "final_gate.json lists D-002 under resolved_by_test."},
    "prose_only": {
        "view": "register", "case": None, "check": "prose_only", "status": "look", "methods": BOTH,
        "apply": _prose, "files": SPEC_FILES,
        "edit": "Both copies of the spec gain four comment lines: one starts a DEPARTURE, one a LIMITATION, one "
                "starts an ASSUMPTION in the middle of the line, and one only mentions a departure."},
    # §9, the third fixture: inputs that contradict each other or miss a piece; no check may answer ok or look
    "findings_bad_line": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": _bad_line, "files": [LEDGER],
        "edit": "findings.jsonl gains a last line that is a row cut off in the middle, which is not valid JSON."},
    "finding_unknown_status": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "not_available", "methods": BOTH,
        "apply": _odd_status, "files": [LEDGER, GATES[1]],
        "edit": "In findings.jsonl finding D-002 has the status resolved, a word the ledger does not write."},
    "scope_names_no_finding": {
        "view": "register", "case": "C12", "check": "undisposed", "status": "problem", "methods": BOTH,
        "apply": _ghost, "files": [SCOPE],
        "edit": "scope_declaration.yaml gains a declaration whose blocker_id X-404 is the id of no finding in "
                "findings.jsonl, with decision test DT1."},
    "decision_tests_missing": {
        "across": {'outcomes.tests_that_move': 'not_available', 'outcomes.infeasible_tests': 'not_available', 'outcomes.fixed_inputs': 'not_available'},
        "view": "register", "case": None, "check": "declared_untested", "status": "not_available", "methods": BOTH,
        "apply": _no_tests_file, "files": [DT] + [f"figures/decision_tests{e}" for e in (".png", ".svg", ".csv")],
        "also": {"tests_unlinked": "not_available", "decides_unknown": "not_available"},
        "edit": "analysis/decision_tests.yaml is not in the run folder, and the manifest does not list it."},
    # The Model view (§5.4). The clean run's engine record has four benchmarks in band at the centre of their bands;
    # B4 is reported by the engine choice and does not gate; nothing is declared, so each departure is a variant.
    "C4_out_of_band": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "problem", "methods": BOTH,
        "apply": _moved("B3", 1.12), "files": [ENGINE_REC], "also": {"choice_text": "problem"},
        "edit": "In analysis/engine.yaml benchmark B3 (held out) has computed 1.12 against its band 0.64 to 1.0, with "
                "status out_of_band and in_band false, while no departure names it and engine_choice.json lists it "
                "neither as reported only nor among its benchmarks not in band."},
    "C4_out_of_band_departure": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _b2_out, "files": DEPARTED + [ENGINE_REC], "also": {"departures": "look"},
        "edit": "In analysis/engine.yaml benchmark B2 (held out) has computed 0.78 against its band 0.36 to 0.7, with "
                "status out_of_band; the signed spec gains a departure for B2 naming decision test DT4, which ran "
                "with verdict NO_CHANGE, and engine_choice.json records it as signed with the benchmark's status."},
    "C4_out_of_band_departure_not_run": {
        "across": {'outcomes.tests_that_move': 'look'},
        "view": "model", "case": "C4", "check": "benchmarks", "status": "problem", "methods": BOTH,
        "apply": lambda m: _b2_out(m, ran=False), "files": DEPARTED + [ENGINE_REC, DT], "also": {"departures": "problem"},
        "edit": "Benchmark B2 is out of band as in C4_out_of_band_departure, and analysis/decision_tests.yaml records "
                "DT4, the test its signed departure names, with status not_run and verdict NOT_RUN."},
    "C4_out_of_band_departure_unsigned": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "problem", "methods": BOTH,
        "apply": _b3_out_disk, "files": [ENGINE_REC, "decision_spec.yaml"], "also": {"choice_text": "problem"},
        "edit": "Benchmark B3 is out of band as in C4_out_of_band, and after the sign-off decision_spec.yaml on disk "
                "gains an engine departure for B3 naming DT1 that the signed copy does not hold."},
    "C4_not_gating_out_of_band": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _moved("B4", 1.5), "files": [ENGINE_REC, CHOICE],
        "edit": "Benchmark B4, which engine_choice.json lists under benchmarks_reported (it does not gate), has "
                "computed 1.5 against its band 2.0 to 72.0, with status out_of_band in the engine record and in "
                "the reported row."},
    "C4_near_edge": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _moved("B1", 0.84), "files": [ENGINE_REC],
        "edit": "Benchmark B1 has computed 0.84 in its band 0.4 to 0.9: in band, within a quarter of the band's log "
                "width of the upper edge, with every seed still inside the band."},
    "C4_unreachable": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _moved("B4", 95.0, status="band_mis_based", mis_based_reason=MIS_BASED[1], reach_in_fitted_range=[80.0, 110.0]),
        "files": [ENGINE_REC, CHOICE],
        "edit": "Benchmark B4, reported only, has computed 95.0 above its band 2.0 to 72.0 with status band_mis_based "
                "and mis_based_reason unreachable_in_fitted_range (reach 80.0 to 110.0), in the engine record and in "
                "the reported row."},
    "no_benchmarks": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "look", "methods": BOTH,
        "apply": _no_benchmarks, "files": [ENGINE_REC, CHOICE],
        "edit": "The engine record's data.card.benchmarks is an empty list (no fitted parameter names a benchmark), "
                "and engine_choice.json counts 0 benchmarks for the engine and reports none."},
    "choice_list_disagrees": {
        "view": "model", "case": None, "check": "choice_text", "status": "problem", "methods": BOTH,
        "apply": _list_disagrees, "files": [CHOICE],
        "edit": "In engine_choice.json the chosen engine's benchmarks_not_in_band lists B1 as out_of_band, while the "
                "engine record has B1 in band."},
    "choice_unbound_named": {
        "view": "model", "case": None, "check": "choice_bound", "status": "ok", "methods": BOTH,
        "apply": lambda m: _unbound(m, named=True), "files": SPEC_FILES,
        "edit": "The signed spec names the engine by its reference instead of leaving it to the engine choice, and "
                "engine_choice_sha256 is null in every analysis header and in the manifest, as the harness writes "
                "it for a named engine."},
    "choice_unbound_open": {
        "view": "model", "case": None, "check": "choice_bound", "status": "look", "methods": BOTH,
        "apply": _unbound, "files": lambda method, figures: artifacts(method),
        "edit": "engine_choice_sha256 is null in every analysis header and in the manifest, while the signed spec "
                "says engine: auto and so leaves the engine to the engine choice."},
    "choice_hash_mismatch": {
        "view": "model", "case": None, "check": "choice_bound", "status": "problem", "methods": BOTH,
        "apply": _choice_edited, "files": [CHOICE], "follows": False,
        "edit": "engine_choice.json is rewritten after the analysis (another time in its `at`), so the hash the "
                "analysis headers hold for it is no longer the file's."},
    "spec_value_outside_card": {
        "view": "model", "case": None, "check": "spec_values", "status": "look", "methods": BOTH,
        "apply": _outside_card, "files": SPEC_FILES + [ENGINE_REC, SHEET],
        "edit": "The card's range for p2 is 1.0 to 5.0, so the spec's range 1.0 to 5.5 reaches outside it: the "
                "engine record's spec_values.p2 has outside_card_range true and outside range, the signed spec "
                "sets outside_card_range true for p2, and the sign-off sheet marks the line HIGH."},
    "spec_value_flag_contradicts": {
        "view": "model", "case": None, "check": "spec_values", "status": "problem", "methods": BOTH,
        "apply": _flag_contradicts, "files": [ENGINE_REC],
        "edit": "The engine record's spec_values.p2 has range 1.0 to 7.0 against card_range 1.0 to 6.0 while its "
                "outside_card_range stays false and its outside stays null."},
    "seed_range_crosses_edge": {
        "view": "model", "case": None, "check": "seed_range", "status": "look", "methods": BOTH,
        "apply": _seed_cross, "files": [ENGINE_REC],
        "edit": "Benchmark B1 keeps computed 0.6 at the centre of its band 0.4 to 0.9 and status in_band, with seeds "
                "from 0.5 to 0.93, so its seed range crosses the upper edge."},
    "parameters_unverified": {
        "view": "model", "case": None, "check": "parameters_sourced", "status": "look", "methods": BOTH,
        "apply": _unsourced, "files": [ENGINE_REC],
        "edit": "In the engine record's parameters, b_add is marked unverified and c1 has no range (lo and hi null)."},
    "fitted_on_bound": {
        "view": "model", "case": None, "check": "fitted_at_bound", "status": "look", "methods": BOTH,
        "apply": _fitted_on_bound, "files": [ENGINE_REC],
        "edit": "The fitted parameter f1 has the value 0.6, the upper end of its fitted range 0.3 to 0.6, in "
                "data.card.fitted and in data.parameters."},
    "departure_component": {
        "view": "model", "case": None, "check": "departures", "status": "look", "methods": BOTH,
        "apply": lambda m: _depart(m, "B"), "files": DEPARTED,
        "edit": "The signed spec gains an engine departure for component B, which no benchmark covers, naming "
                "decision test DT5, which ran with verdict NO_CHANGE and no unit changing package; engine_choice.json "
                "records it as signed with benchmark_status no benchmark."},
    "departure_near_miss": {
        "across": {'outcomes.tests_that_move': 'look'},
        "view": "model", "case": None, "check": "departures", "status": "look", "methods": BOTH,
        "apply": lambda m: (_b2_out(m), _share("DT4", 9.0)(m)), "files": DEPARTED + [ENGINE_REC, DT],
        "also": {"benchmarks": "look"},
        "edit": "Benchmark B2 is out of band under its signed departure as in C4_out_of_band_departure, and "
                "analysis/decision_tests.yaml records DT4, the test the departure names, with 9% of units changing "
                "package against its 10% threshold: verdict NO_CHANGE, within 25% of the threshold."},
    "spec_benchmarks_recomputed": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "ok", "methods": BOTH,
        "apply": lambda m: m.update(bsv=_at_spec_values()), "files": [BSV, BSV_REC, DT],
        "edit": "The run also holds analysis/benchmarks_at_spec_values.yaml and its record file: benchmark B2, which "
                "decision test DT4 reads, recomputed at the spec's values (0.51, in band), listed as an input of the "
                "decision tests and, having no artifact header, left outside the manifest."},
    # §9, the third fixture for the Model view
    "departure_test_not_run": {
        "across": {'outcomes.tests_that_move': 'look'},
        "view": "model", "case": None, "check": "departures", "status": "problem", "methods": BOTH,
        "apply": lambda m: (_depart(m, "B"), _set_test(m, "DT5", ran=False)), "files": DEPARTED + [DT],
        "edit": "The signed spec holds the departure for component B as in departure_component, and "
                "analysis/decision_tests.yaml records DT5, the test it names, with status not_run and verdict "
                "NOT_RUN, while every benchmark is in band."},
    "benchmark_unknown_status": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "not_available", "methods": BOTH,
        "apply": _b1(status="passed"), "files": [ENGINE_REC], "also": {"choice_text": "not_available"},
        "edit": "Benchmark B1 has the status passed, a word toolkit/engines/benchmark.py does not write."},
    "benchmark_no_band": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "not_available", "methods": BOTH,
        "apply": _b1(band=None, target=None), "files": [ENGINE_REC],
        "edit": "Benchmark B1 has band null and target null while its status stays in_band, so its place in the "
                "band cannot be computed."},
    "benchmark_status_contradicts": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "problem", "methods": BOTH,
        "apply": _b1(computed=0.95), "files": [ENGINE_REC],
        "edit": "Benchmark B1 has computed 0.95, above its band 0.4 to 0.9, while its status stays in_band and its "
                "in_band stays true."},
    "engine_record_unparsable": {
        "view": "model", "case": "C4", "check": "benchmarks", "status": "not_available", "methods": BOTH,
        "apply": lambda m: m.update(cut=True), "files": [ENGINE_REC], "unpinned": ("choice_bound",),
        "also": {k: "not_available" for k in ("choice_text", "spec_values", "seed_range", "parameters_sourced", "fitted_at_bound")},
        "edit": "analysis/engine.yaml stops inside its benchmark table, in the middle of a list that is never "
                "closed, so it is not valid YAML."},
    # The Figures view (§5.12). `figure_sets` names the producers of analysis/figures.yaml an edit is defined for.
    "figure_stale_source": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "problem", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_stale, "files": _fig_files("stale", ".png.provenance.json"),
        "edit": "The provenance record of one decision figure (headline; estimates_q1 in the estimates set) holds "
                "another hash for one of its sources than the file now has."},
    "figure_spec_redrafted": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "ok", "methods": BOTH,
        "apply": _disk_only, "files": ["decision_spec.yaml"], "follows": False,
        "edit": "After the sign-off, decision_spec.yaml on disk gains an entry the signed copy does not hold, so "
                "the hash every provenance record holds for the spec is the signed copy's and not the file's."},
    "figure_not_produced": {
        "view": "figures", "case": None, "check": "figure_missing", "status": "look", "methods": BOTH,
        "apply": _fig_absent,
        "files": [FIGS] + [f"figures/trajectories{e}" for e in (".png", ".svg", ".csv", ".png.provenance.json")],
        "edit": "analysis/figures.yaml lists trajectories under not_produced with the producer's reason (it needs "
                "trajectory runs), and the figure's image, table and provenance record are not in the run folder."},
    "figure_not_produced_named": {
        "view": "figures", "case": None, "check": "figure_missing", "status": "problem", "methods": BOTH,
        "apply": lambda m: _fig_absent(m, named=True),
        "files": [FIGS, REPORT] + [f"figures/trajectories{e}" for e in (".png", ".svg", ".csv", ".png.provenance.json")],
        "edit": "trajectories is not produced as in figure_not_produced, and report.md names it by its path "
                "figures/trajectories.png."},
    "figure_unused": {
        "view": "figures", "case": None, "check": "figure_unused", "status": "look", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_unused, "files": [REPORT],
        "edit": "report.md no longer names one produced decision figure by its path (frontier; estimates_q1 in the "
                "estimates set), and no other text names it."},
    "figure_image_missing": {
        "view": "figures", "case": None, "check": "figure_missing", "status": "problem", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("dropped", "other"), "files": _fig_files("other", ".png"),
        "follows": False, "also": {"figure_in_manifest": "problem"},
        "edit": "The image of one figure the index lists as produced (parameters; diagnostics_ppc in the estimates "
                "set) is not in the run folder, while the index and the manifest still list it."},
    "figure_outside_manifest": {
        "view": "figures", "case": None, "check": "figure_in_manifest", "status": "problem", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("unlisted", "other"), "files": ["analysis/manifest.json"],
        "edit": "analysis/manifest.json does not list the image of one produced figure (parameters; diagnostics_ppc "
                "in the estimates set) among its deliverables."},
    "figure_manifest_hash": {
        "view": "figures", "case": None, "check": "figure_in_manifest", "status": "problem", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("redrawn", "other"), "files": _fig_files("other", ".png"),
        "follows": False,
        "edit": "The image of one produced figure (parameters; diagnostics_ppc in the estimates set) is replaced "
                "after the manifest was written, so the manifest's hash for it is not the file's."},
    # §9, the third fixture for the Figures view
    "figure_record_missing": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "not_available", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("no_record", "record"),
        "files": _fig_files("record", ".png.provenance.json"),
        "edit": "One produced figure (calibration_fit; diagnostics_rt in the estimates set) has no provenance "
                "record in the run folder."},
    "figure_record_malformed": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "not_available", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("bad_record", "record"),
        "files": _fig_files("record", ".png.provenance.json"),
        "edit": "The provenance record of one produced figure (calibration_fit; diagnostics_rt in the estimates "
                "set) is cut off in the middle and is not valid JSON."},
    "figures_unknown_producer": {
        "view": "figures", "case": None, "check": "figure_fresh", "status": "not_available", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_producer, "files": [FIGS],
        "also": {k: "not_available" for k in ("figure_missing", "figure_unused", "figure_in_manifest")},
        "edit": "analysis/figures.yaml names toolkit/analysis/other.py as its producer, which is neither of the "
                "two producers of that file."},
    "figure_no_path": {
        "view": "figures", "case": None, "check": "figure_missing", "status": "not_available", "methods": BOTH,
        "figure_sets": FIGURE_SETS, "apply": _fig_set("no_path", "plain"), "files": [FIGS],
        "also": {"figure_fresh": "not_available", "figure_in_manifest": "not_available"},
        "edit": "In analysis/figures.yaml the entry of one produced figure outside the decision tier (data_coverage; "
                "diagnostics_ppc in the estimates set) has no path key."},
}

# ---- the Outcomes view's edits (§5.7)

def _headline_draws(median, low, high):
    """The draws' summary of the headline moved against the point value (shares of the point value)."""
    def edit(m):
        h = m["point"]["averted"][OUTCOME]
        m["unc"]["fixed_plan"][OUTCOME].update(median=_r(median * h), ci_low=_r(low * h), ci_high=_r(high * h))
    return edit


def _evppi(**values):
    return lambda m: m.update(voi=_voi(m["point"], m["plan_args"]["draws"], values))


def _ratio(rid, ratio, test_ran=True):
    def edit(m):
        m["bv"] = _burden(m["plan_args"]["draws"], {rid: ratio})
        if not test_ran:
            _set_test(m, "DT10", ran=False)
    return edit


def _unknown_package(m):
    point = m["point"]
    point["plan"]["U01"] = point["per_unit"]["U01"]["package"] = "A+D"
    point["packages"] = {k: list(point["plan"].values()).count(k) for k in sorted(set(point["plan"].values()))}


def _variant_no_cost(m):
    v = m["pv"]["variants"][0]
    v.update(status="not_solved", cost=None, objective=None, value_lost=None, value_lost_pct=None, plan=None)
    v["uncertainty"] = None


def _pair(tid, **words):
    def edit(m):
        next(t for t in m["dt"]["tests"] if t["test_id"] == tid).update(words)
    return edit


def _stops_short(key):
    """A file that stops inside the list under `key`, which is never closed."""
    return lambda text: text[:text.index(f"  {key}:")] + f"  {key}: [{{test_id: DT1,\n"


def _reversed_interval(m):
    f = m["unc"]["fixed_plan"][OUTCOME]
    f["ci_low"], f["ci_high"] = f["ci_high"], f["ci_low"]


def _est_test(**how):
    def edit(m):
        t = m["est"]["decision_tests"][0]
        if "verdict" in how:
            t["ends"][1].update(verdict=how["verdict"], probability=0.4)
            t["decision_changes"] = True
        if "status" in how:
            t["status"] = how["status"]
    return edit


PLAN_FILES = [POINT, "allocation.csv", RESULTS]
EST_KIND = ("estimates",)


def _out(check, status, apply, files, edit, case=None, **more):
    return {"view": "outcomes", "case": case, "check": check, "status": status, "methods": BOTH, "apply": apply,
            "files": files, "edit": edit, **more}


def _nothing(m):
    return None


OUTCOME_CHECKS = ("dearest_wins", "share_of_burden", "point_vs_draws", "tests_that_move", "burden_refs", "voi_support",
                  "boundary_coverage", "run_shift", "stability_agreement", "infeasible_tests", "variant_budget", "fixed_inputs")
DEFECTS.update({
    "C8_dearest_wins": _out(
        "dearest_wins", "look", lambda m: _replan(m, plan=_clean_plan(dearer=11)), PLAN_FILES, case="C8",
        edit="The plan gives the dearer option of the group (A+C) to eleven of the twelve units, 92% against the "
             "producer's 90%, and every file computed from the plan follows."),
    "C8_dearest_few_units": _out(
        "dearest_wins", "ok", lambda m: _replan(m, plan={u: "A+C" if i >= 8 else "A" for i, u in enumerate(UNITS)}),
        PLAN_FILES, case="C8",
        edit="The plan leaves eight units on the comparator package and gives the dearer option to the other four, "
             "all of the units that hold the group but fewer than the producer's minimum of five."),
    "C7_share_of_burden": _out(
        "share_of_burden", "look", lambda m: _replan(m, scale=7.0), PLAN_FILES, case="C7",
        edit="Every unit's benefit is seven times larger, so the headline is 55% of the comparator total, and the "
             "draws and every file computed from the point follow."),
    "point_outside_interval": _out(
        "point_vs_draws", "problem", _headline_draws(0.8, 0.72, 0.88), [UNC],
        edit="In analysis/uncertainty.yaml the headline's median over the draws is 80% of the point value and its "
             "interval ends at 88% of it, so the point value lies above the interval."),
    "point_far_from_median": _out(
        "point_vs_draws", "look", _headline_draws(0.93, 0.84, 1.02), [UNC],
        edit="In analysis/uncertainty.yaml the headline's median over the draws is 93% of the point value, which is "
             "inside the interval and 7.5% of the median above it."),
    "interval_reversed": _out(
        "point_vs_draws", "problem", _reversed_interval, [UNC],
        edit="In analysis/uncertainty.yaml the two ends of the headline's interval are exchanged, so its lower end is "
             "above its upper end."),
    "test_changes_linked": _out(
        "tests_that_move", "look", lambda m: _set_test(m, "DT9", changes=True), [DT],
        edit="DT9, the test the dossier's entry for unit attribute x1 names, has the verdict CHANGES_PLAN with 25% "
             "of units against the 10% threshold."),
    "test_changes_unlinked": _out(
        "tests_that_move", "look", lambda m: _set_test(m, "DT1", changes=True), [DT],
        across={"register.tests_unlinked": "look", "register.undisposed": "look"},
        edit="DT1, a test no finding, declaration or dossier entry names, has the verdict CHANGES_PLAN with 25% of "
             "units against the 10% threshold."),
    "test_near_miss": _out(
        "tests_that_move", "look", lambda m: _set_test(m, "DT1", share=8.0), [DT],
        edit="DT1 keeps the verdict NO_CHANGE with 8% of units changing package, within a quarter of its 10% threshold."),
    "test_not_run": _out(
        "tests_that_move", "ok", lambda m: m["dt"]["tests"].append(_test(f"scenario_excluded_{HELD}", ran=False)), [DT],
        edit="analysis/decision_tests.yaml holds one more test, the automatic one for the scenario value the spec "
             "holds fixed by name, with status not_run and verdict NOT_RUN: the producer's own not-run scenario for an "
             "input the signed spec excludes, which is listed and does not raise the check (§5.7, settled)."),
    "test_pair_contradicts": _out(
        "tests_that_move", "problem", _pair("DT1", verdict="NOT_RUN"), [DT], across={"register.tests_unlinked": "problem"},
        edit="DT1 has status run beside the verdict NOT_RUN, a pair the analysis does not write."),
    "tests_unparsable": _out(
        "tests_that_move", "not_available", lambda m: m["raw"].update({DT: _stops_short("tests")}), [DT],
        also={"infeasible_tests": "not_available", "fixed_inputs": "not_available"},
        across={f"register.{k}": "not_available" for k in ("tests_unlinked", "declared_untested", "decides_unknown")},
        edit="analysis/decision_tests.yaml stops inside its list of tests, which is never closed."),
    "burden_report_only_tested": _out(
        "burden_refs", "look", _ratio("ref_G3", 2.6), [BV],
        edit="Reference ref_G3, which is report-only and named by decision test DT10 that ran, has a ratio of 2.6 "
             "against its band of 0.5 to 2 and the status outside."),
    "burden_report_only_untested": _out(
        "burden_refs", "problem", _ratio("ref_all", 2.6), [BV],
        edit="Reference ref_all, which is report-only and named by no decision test, has a ratio of 2.6 against "
             "its band of 0.5 to 2 and the status outside."),
    "burden_checked_outside": _out(
        "burden_refs", "problem", _ratio("ref_G1", 2.6), [BV],
        edit="Reference ref_G1, a checked row, has a ratio of 2.6 against its band of 0.5 to 2 and the status outside."),
    "burden_test_not_run": _out(
        "burden_refs", "problem", _ratio("ref_G3", 2.6, test_ran=False), [BV, DT], also={"tests_that_move": "look"},
        edit="Reference ref_G3 is outside its band as in burden_report_only_tested, and DT10, the test that names "
             "it, has status not_run."),
    "voi_indicative": _out(
        "voi_support", "look", lambda m: _replan(m, draws=200), SPEC_FILES, case="C14",
        edit="The signed spec asks for 200 draws, below the 500 the producer calls informative, so the value of "
             "information has the status indicative and every file follows the spec."),
    "voi_too_few_draws": _out(
        "voi_support", "not_available", lambda m: _replan(m, draws=20), SPEC_FILES, case="C14",
        edit="The signed spec asks for 20 draws, below the producer's minimum of 30, so the value of information "
             "has the status too_few_draws with no value computed."),
    "voi_none_above_floor": _out(
        "voi_support", "look", _evppi(p1=0.3, p2=0.2), [VOI], case="C14",
        edit="In analysis/value_of_information.yaml every input's value is below its noise floor of 0.5, so none is "
             "supported."),
    "C14_dominated_carries_value": _out(
        "voi_support", "look", _evppi(p3=2.4), [VOI], case="C14",
        edit="Input p3, which the uncertainty file lists as prior-dominated, has a value of 2.4 against its noise "
             "floor of 0.5: above the floor and 40% of the total."),
    "boundary_uncovered": _out(
        "boundary_coverage", "look", lambda m: m.update(emb=_boundary(m["point"]["plan"], {"A+B": "A"}, UNITS[3:9])), [EMB],
        edit="The emulator boundary check holds six units whose package is A+B with the runner-up A, so component C "
             "is in no row."),
    "stability_disagree": _out(
        "stability_agreement", "look", lambda m: m.update(stab=_stability(loss=2.0)), [STAB],
        edit="In analysis/stability.yaml the worst fold loses 2% of the value, so value_verdict is FRAGILE beside the "
             "plan's verdict ROBUST."),
    "test_infeasible": _out(
        "infeasible_tests", "look", lambda m: _set_test(m, "DT1", feasible=False), [DT], also={"tests_that_move": "look"},
        across={"register.tests_unlinked": "look", "register.undisposed": "look"},
        edit="Under DT1 the point plan is not feasible (point_plan_feasible false, triggered_by infeasible), which "
             "by the producer's rule gives the verdict CHANGES_PLAN."),
    "variant_over_budget": _out(
        "variant_budget", "look", lambda m: m.update(pv=_variants(m["point"], m["plan_args"]["draws"], over=0.3)), [PV],
        edit="The plan variant costs more than the allocatable budget in 30% of draws, against the 10% the signed "
             "spec allows, with rule_met false."),
    "variant_no_cost": _out(
        "variant_budget", "not_available", _variant_no_cost, [PV],
        edit="The plan variant has the status not_solved, with no cost, no plan and no draws."),
    "fixed_input_changes_plan": _out(
        "fixed_inputs", "look", lambda m: _set_test(m, "DT7", changes=True), [DT], also={"tests_that_move": "look"},
        edit=f"DT7, which varies {HELD}, an input the uncertainty file lists as held at one value, has the verdict "
             "CHANGES_PLAN."),
    "plan_unknown_package": _out(
        "dearest_wins", "not_available", _unknown_package, PLAN_FILES,
        edit="The plan gives unit U01 the package A+D, and the signed spec defines no component D."),
    "unknown_deliverable_kind": _out(
        "dearest_wins", "not_available", lambda m: m["spec"]["deliverable"].update(kind="ranking"), SPEC_FILES,
        also={k: "not_available" for k in OUTCOME_CHECKS[1:] if k != "run_shift"},
        edit="The signed spec's deliverable.kind is ranking, which is neither of the two kinds the analysis writes."),
    "C6_shift_spec_differs": _out(
        "run_shift", "look", _nothing, [], case="C6", against="shifted",
        edit="Nothing in this run: it is compared with the run `shifted`, in which three more units of twelve get the "
             "dearer option and the signed spec's constraints.envelope_usd differs."),
    "C6_shift_same_spec": _out(
        "run_shift", "problem", _nothing, [], case="C6", against="shifted_same_spec",
        edit="Nothing in this run: it is compared with the run `shifted_same_spec`, in which three more units of "
             "twelve get the dearer option and the signed spec is the same in every key."),
    "C6_headline_shift": _out(
        "run_shift", "look", _nothing, [], case="C6", against="headline",
        edit="Nothing in this run: it is compared with the run `headline`, whose headline is 1.6 times this one's "
             "and whose signed spec differs in comparator.text."),
    "C6_second_run": _out(
        "run_shift", "look", _nothing, [], case="C6", against="second",
        edit="Nothing in this run: it is compared with the run `second`, which has the same plan and headline and "
             "a signed spec that differs in constraints.envelope_usd alone (a key changed with no shift is still a change)."),
    "C6_twin_run": _out(
        "run_shift", "ok", _nothing, [], case="C6", against="twin",
        edit="Nothing in this run: it is compared with the run `twin`, which has the same signed spec, plan and headline."),
    "C6_other_question": _out(
        "run_shift", "not_available", _nothing, [], case="C6", against="other_question",
        edit="Nothing in this run: it is compared with the run `other_question`, whose signed spec asks another question."),
    "C6_unverified": _out(
        "run_shift", "not_available", _nothing, [], case="C6", against="unverified",
        edit="Nothing in this run: it is compared with the run `unverified`, whose signed copy of the spec was "
             "edited after the sign-off and no longer has the recorded hash."),
    "estimates_test_changes": _out(
        "tests_that_move", "look", _est_test(verdict="no"), [EST], kinds=EST_KIND, methods=POST, figure_sets=FIGURE_SETS[1:],
        edit="In analysis/estimates.yaml the first decision test has decision_changes true: at one end of the "
             "parameter's range the rule's verdict is no where the estimate's is yes."),
    "estimates_test_unknown_status": _out(
        "tests_that_move", "not_available", _est_test(status="done"), [EST], kinds=EST_KIND, methods=POST, figure_sets=FIGURE_SETS[1:],
        edit="In analysis/estimates.yaml the first decision test has the status done, a word the producer does not write."),
})


# ---- the Spec and evidence view (§5.3) and the Scenarios view (§5.6)
SPEC_CHECKS = ("effect_window", "values_sourced", "dossier_coverage", "signoff_note", "presign_sections", "presign_high",
               "decision_date")
SCENARIOS_CHECKS = ("trajectories_present", "dynamics", "schedule_window")
TRAJ_FIGURE = ["figures/trajectories" + end for end in (".png", ".svg", ".csv", ".png.provenance.json")]
SECOND = f"{ARCHIVE}{EARLIER[1]}/"


def _cited(m):
    """The dossier observation whose source is, word for word, the source of the outcome adjustment a variant adds."""
    return next(o for e in m["dossier"]["inputs"].values() for o in e.get("observations", []) if o["source"] == OA["source"])


def _follow_up(years=None, where=None):
    def edit(m):
        o = _cited(m)
        if years is None:
            del o[FOLLOW_UP]
        else:       # the producer's own finding for it (toolkit/spec/spec_critique.py _follow_up), on the sheet
            o[FOLLOW_UP] = years
            m["critique"].append(("MEDIUM", 3, "follow_up_window", "1 evidence observation(s) followed the effect for less "
                                  f"than the objective window of 3 years: `parameter:b_add` ({o['source']}: {years:g} years). "
                                  "The engine carries the effect past what they observed.",
                                  "owner judgment: Confirm how the effect is assumed to hold (or wane) beyond the follow-up, "
                                  "and that a decision test varies it"))
        if where:
            o["where"] = where
    return edit


def _adjust(then=None):
    """The signed spec holds one outcome adjustment (OA), whose source is, word for word, the source of one dossier
    observation; `then` is a further edit. The clean spec holds none (§5.3: an outcome adjustment is a `look` for the
    reader, since no field records how long its evidence was followed), so every case with one is a variant."""
    def edit(m):
        m["spec"]["analysis"]["outcome_adjustments"] = [dict(OA)]
        if then:
            then(m)
    return edit


def _unmark(first_only):
    def edit(m):
        marked = [o for e in m["dossier"]["inputs"].values() for o in e.get("observations", [])]
        for o in marked[:1] if first_only else marked:
            del o[CHECKED]
    return edit


def _date(value, finding):
    def edit(m):       # the producer's own HIGH finding for the date (spec_critique.py _decision_date), on the sheet
        m["spec"]["decision_date"] = value
        m["critique"].append(("HIGH", 2, "decision_date", finding, "Start the delivery window at the decision date; delivered "
                              "actions are fixed inputs" if "before the decision date" in finding else "Give the date the decision is made"))
    return edit


def _many_sections(m):
    m["signoff_note"] = ""
    second = m["earlier"][1]
    second["constraints"]["envelope_usd"] = 0.9 * ENVELOPE
    second["comparator"]["text"] = "every unit keeps component A alone, as first drafted"
    second["objective"].pop("delivery")
    second["outcomes_reported"] = [OUTCOME, "cost"]
    second["decision_date"] = "2025-12-31"


def _event(group, **event):
    def edit(schedule):
        schedule["regions"][group]["events"].append({"component": "B", "label": "full round", "kind": "decision", "t1": None,
                                                     "units": 1, **event})
    return lambda m: m.update(sched_edit=edit)


def _sv(check, status, apply, files, edit, view="spec", case=None, **more):
    return {"view": view, "case": case, "check": check, "status": status, "methods": BOTH, "apply": apply, "files": files,
            "edit": edit, **more}


DEFECTS.update({
    "C1_outcome_adjustment": _sv(
        "effect_window", "look", _adjust(), SPEC_FILES, case="C1", also={"values_sourced": "look"},
        edit="The signed spec holds one outcome adjustment (OA1: component C at 0.8 times reference B) whose source is, word "
             "for word, the source of a dossier observation that records follow_up_years 5 against the three-year outcome "
             "window; the adjustment itself has no field for a follow-up and no key that cites the entry."),
    "C1_follow_up_short": _sv(
        "effect_window", "look", _adjust(_follow_up(1.0)), SPEC_FILES + [DOSSIER], case="C1", also={"values_sourced": "look"},
        edit="The signed spec holds the outcome adjustment OA1, and in evidence_dossier.yaml the observation with the same "
             "source text records follow_up_years 1 against the three-year outcome window, with the producer's MEDIUM row "
             "for it in the sheet's spec critique."),
    "C1_follow_up_prose_only": _sv(
        "effect_window", "look", _adjust(_follow_up(where="all three groups; followed for five years")), SPEC_FILES + [DOSSIER],
        case="C1", also={"values_sourced": "look"},
        edit="The signed spec holds the outcome adjustment OA1, and in evidence_dossier.yaml the observation with the same "
             "source text has no follow_up_years field while its `where` text says it was followed for five years."),
    "C1_source_not_in_dossier": _sv(
        "effect_window", "look", _adjust(lambda m: m["spec"]["analysis"]["outcome_adjustments"][0].update(source="fixture study five, table 1")),
        SPEC_FILES, case="C1", also={"values_sourced": "look"},
        edit="The signed spec holds the outcome adjustment OA1 with a source text that no observation of the dossier has."),
    "value_unverified": _sv(
        "values_sourced", "look", lambda m: m["spec"]["engine_parameters"]["p2"].update(verified="unverified"), SPEC_FILES,
        edit="The signed spec marks the source of the value it sets for p2 as unverified."),
    "value_no_mark": _sv(
        "values_sourced", "look", lambda m: m["spec"]["engine_parameters"]["p2"].pop("verified"), SPEC_FILES,
        edit="The signed spec's entry for the value it sets for p2 carries no mark for its source, while analysis/engine.yaml "
             "marks the engine card's own source for p2 as matched."),
    "value_unknown_mark": _sv(
        "values_sourced", "not_available", lambda m: m["spec"]["engine_parameters"]["p2"].update(verified="seen"), SPEC_FILES,
        edit="The signed spec marks the source of the value it sets for p2 with the word seen, which is none of the three "
             "words a source's mark takes."),
    "C15_no_data_no_test": _sv(
        "dossier_coverage", "look", lambda m: m["dossier"]["inputs"][f"parameter:{HELD}"].pop("decision_test"), [DOSSIER], case="C15",
        across={"register.declared_untested": "look", "register.decides_unknown": "look"},
        edit=f"In evidence_dossier.yaml the entry for {HELD}, which says no data, names no decision test."),
    "C15_observation_unchecked": _sv(
        "dossier_coverage", "look", _unmark(True), [DOSSIER], case="C15",
        edit="In evidence_dossier.yaml one of the three observations has no `checked` field, and the other two keep theirs."),
    "C15_no_checked_marks": _sv(
        "dossier_coverage", "look", _unmark(False), [DOSSIER], case="C15",
        edit="In evidence_dossier.yaml no observation has a `checked` field, as in every stored run (gap H2)."),
    "dossier_unparsable": _sv(
        "dossier_coverage", "not_available", lambda m: m["raw"].update({DOSSIER: lambda text: text[:text.index("    observations:")] + "    observations: [{source: study one,\n"}),
        [DOSSIER], case="C15",
        across={"register.declared_untested": "look", "register.decides_unknown": "look", "register.undisposed": "not_available"},
        edit="evidence_dossier.yaml stops inside its first list of observations, which is never closed."),
    "signoff_note_empty": _sv(
        "signoff_note", "look", lambda m: m.update(signoff_note=""), [SIGNOFF],
        edit="decision_spec.signoff.json has an empty note; the signature changed one section of the spec (uncertainty)."),
    "signoff_note_empty_many_sections": _sv(
        "signoff_note", "look", _many_sections, [SIGNOFF, SECOND + SPEC_FILES[1], SECOND + SIGNOFF, SECOND + SHEET],
        edit="decision_spec.signoff.json has an empty note, and the spec signed before it differs from the current one in "
             "six sections (comparator, constraints, decision_date, objective, outcomes_reported, uncertainty)."),
    "archive_no_record": _sv(
        "signoff_note", "not_available", lambda m: m.update(archive_bare=True), [f"{ARCHIVE}{EARLIER[1]}b/{SHEET}"],
        edit="One more folder of archived sign-offs holds a sheet and no sign-off record."),
    "sheet_section_missing": _sv(
        "presign_sections", "look", lambda m: m.update(sheet_without=(SHEET_ALWAYS[0],)), [SHEET],
        edit="decision_spec.signoff_sheet.md has no spec critique section, as a sheet written before that section existed."),
    "sheet_missing": _sv(
        "presign_sections", "not_available", lambda m: m.update(no_sheet=True), [SHEET], also={"presign_high": "not_available"},
        edit="The run holds no decision_spec.signoff_sheet.md."),
    "presign_high_bullet": _sv(
        "presign_high", "look", lambda m: m.update(high=["p2"]), [SHEET], across={"register.undisposed": "problem"},
        edit="decision_spec.signoff_sheet.md marks one line HIGH (the range of p2 against the card's), the edit of sheet_high."),
    "decision_date_after_delivery_start": _sv(
        "decision_date", "look", _date("2026-09-01", "The delivery window starts in 2026, before the decision date 2026-09-01: "
                                       "actions delivered before the decision would be optimised."),
        SPEC_FILES, also={"presign_high": "look"},
        edit="The signed spec dates the decision 2026-09-01, after its delivery period starts (2026.0), and the sheet's spec "
             "critique holds the producer's HIGH row for it."),
    "decision_date_unreadable": _sv(
        "decision_date", "look", _date("soon", "decision_date 'soon' is not a year or an ISO date."), SPEC_FILES,
        also={"presign_high": "look"},
        edit="The signed spec's decision_date is the word soon, and the sheet's spec critique holds the producer's HIGH row for it."),
    "spec_unverified": _sv(
        "effect_window", "not_available", lambda m: m.update(signed_after=lambda spec: spec["constraints"].update(envelope_usd=1.2 * ENVELOPE)),
        [SPEC_FILES[1]], follows=False, also={k: "not_available" for k in SPEC_CHECKS[1:]},
        across={"model.departures": "not_available", "figures.figure_fresh": "not_available", "register.undisposed": "problem",
                "register.prose_only": "not_available",
                **{f"outcomes.{k}": "not_available" for k in ("dearest_wins", "tests_that_move", "boundary_coverage",
                                                              "infeasible_tests", "variant_budget", "fixed_inputs")}},
        edit="The signed copy of the spec was edited after the sign-off and no longer has the hash the sign-off record holds."),
    # the Scenarios view
    "C5_no_trajectories": _sv(
        "trajectories_present", "problem", lambda m: m.update(traj=False), [TRAJ, *TRAJ_FIGURE], view="scenarios", case="C5",
        also={"dynamics": "not_available"}, across={"figures.figure_missing": "look"},
        edit="The run has its batch and point files and no analysis/trajectories.yaml, and analysis/figures.yaml lists the "
             "trajectories figure as not produced with the producer's reason."),
    "trajectories_other_engine": _sv(
        "trajectories_present", "problem", lambda m: m.update(traj_edit=lambda tr: tr["engine"].update(code_sha256=hashlib.sha256(b"other code").hexdigest())),
        [TRAJ], view="scenarios", case="C5", also={"dynamics": "not_available"},
        edit="analysis/trajectories.yaml records an engine whose code hash is not the one analysis/point.yaml records as "
             "the analysis's engine identity."),
    "dynamics_flag": _sv(
        "dynamics", "problem", lambda m: m.update(series_gap=0.02), [TRAJ], view="scenarios", case="C5",
        edit="In analysis/trajectories.yaml the plan's series is 2% above the comparator's before delivery starts, and the "
             "check plan_equals_comparator_before_delivery in analysis/figures.yaml has that value and the status flag."),
    "dynamics_unknown_status": _sv(
        "dynamics", "not_available", lambda m: m.update(dyn_edit=lambda rows: rows[0].update(status="warn")), [FIGS], view="scenarios",
        edit="In analysis/figures.yaml the first check of the trajectories figure has the status warn, a word the producer does not write."),
    "schedule_decision_outside": _sv(
        "schedule_window", "look", _event("G1", t0=DELIVERY[1] + 0.25), [FD, TIMELINE], view="scenarios",
        edit="The stored schedule of group G1 holds a decision event at 2028.25, after the delivery period ends (2028.0) and "
             "inside the outcome window."),
    "schedule_event_after_window": _sv(
        "schedule_window", "look", _event("G2", component="A", label="routine round", kind="routine", t0=WINDOW[1] + 0.5, units=4),
        [FD, TIMELINE], view="scenarios",
        edit="The stored schedule of group G2 holds a routine event at 2029.5, after the outcome window ends (2029.0)."),
    "schedule_unknown_kind": _sv(
        "schedule_window", "not_available", _event("G3", kind="campaign", t0=DELIVERY[0] + 0.75), [FD, TIMELINE], view="scenarios",
        edit="The stored schedule of group G3 holds an event of the kind campaign, which is none of the three kinds the hook writes."),
    "no_schedule_kind": _sv(
        "schedule_window", "not_available", _nothing, [], view="scenarios", kinds=EST_KIND, methods=POST, figure_sets=FIGURE_SETS[1:],
        also={"trajectories_present": "not_available", "dynamics": "not_available"},
        edit="Nothing: the run of an estimates deliverable has no schedule, no batch and no trajectory record."),
})
# What the edits of the views built earlier move in these two views.
for _n in ("sheet_high", "spec_value_outside_card"):      # each marks one line of the sheet HIGH
    DEFECTS[_n].setdefault("across", {})["spec.presign_high"] = "look"
for _n in ("figure_not_produced", "figure_not_produced_named", "figures_unknown_producer"):
    DEFECTS[_n].setdefault("across", {})["scenarios.dynamics"] = "not_available"

REGISTER_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "register")
SPEC_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "spec")
SCENARIOS_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "scenarios")
MODEL_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "model")
FIGURES_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "figures")
OUTCOMES_DEFECTS = tuple(n for n, s in DEFECTS.items() if s.get("view") == "outcomes")


def artifacts(method):
    """The analysis files that carry an artifact header, for a method (standard figure set)."""
    return ([JC, CAL] if method == "history_matching" else [CAL, EST]) + [ENGINE_REC, POINT, UNC, DT, FIGS,
                                                                           EMU, EMB, VOI, BV, STAB, PV, BATCH, FD]


def figure_sets(defect):
    """The figure sets an edit is defined for: those it names, else the standard set."""
    return DEFECTS[defect].get("figure_sets", FIGURE_SETS[:1])


def touched(defect, method, figures="standard"):
    """The files a defect's edit itself changes for a method and a figure set."""
    f = DEFECTS[defect]["files"]
    return list(f(method, figures) if callable(f) else f[method] if isinstance(f, dict) else f)


# ------------------------------------------------------------------------------------------- writing the run

def _yaml(doc):
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000)


def _csv(header, rows):
    return "\n".join(",".join("" if v is None else str(v) for v in r) for r in [header, *rows]) + "\n"


def _head_commit():
    try:
        r = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and len(r.stdout.strip()) == 40:
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "0" * 40          # no git: Run.commit_known cannot ask either, and the Steps check stays ok


def _spec_text(spec, comments):
    """The spec as YAML; comment lines go in front of its `calibration` key, where a stored spec keeps most of them."""
    text = _yaml(spec)
    return text.replace("\ncalibration:", "\n" + "\n".join(comments) + "\ncalibration:", 1) if comments else text


def _register_files(m):
    """findings.jsonl as the ledger saves it (open rows first, then by severity and id; keys sorted), the scope
    declaration, and the two files an older run keeps dispositions in."""
    rows = sorted(m["findings"].values(), key=lambda f: (f["status"] != "open", SEVERITY_ORDER[f["severity"]], f["id"]))
    out = {LEDGER: "".join(json.dumps(f, sort_keys=True) + "\n" for f in rows) + "".join(x + "\n" for x in m["ledger_extra"])}
    if m["scope"] is not None:
        out[SCOPE] = _yaml(m["scope"])
    if m["gates"]:
        out.update({rel: _gate(m, n) for n, rel in enumerate(GATES, 1)})
    if m["old"]:
        named = [m["findings"][i] for i in m["old"]["declared"]]
        why = (f"{len(named)} blocker(s) unresolved, rounds exhausted. Must write scope_declaration.yaml acknowledging "
               "each blocker by id.")
        out[OLD_DECLARED] = _yaml({"rule": 3, "rationale": why, "declared": [
            {k: f[k] for k in ("id", "severity", "source", "claim")} for f in named]})
        out[OLD_GATE] = json.dumps({
            "unresolved_high": [], "accept_blockers": [], "structural_mismatch": False, "structural_reviewers": [],
            "spec_violations": [], "registry_violations": [], "rigor_violations": [], "blocker_attempts": {},
            "escalation_required": True, "rounds_remaining": 0, "action": "DECLARE_SCOPE", "rule_matched": 3,
            "rationale": why, "declared": [], "declared_prev": [],
            "done_when_status": {i: {"passes": True, "detail": "pass manuscript.md exists; pass manuscript.md: found 'limit'",
                                     "mechanical": True, "frozen_round": 1} for i in m["old"]["tested"]},
            "resolved_by_test": list(m["old"]["tested"]), "post_write": True}, indent=1)
    return out


def _figures(m):
    if m["method"] == "posterior":
        f = m["cal"]["diagnostics"]["fit"]
        return {"figures/calibration_fit.csv": _csv(list(f[0]), [list(r.values()) for r in f])}
    jc = m["jc"]
    fit, reg = jc["figure_data"]["fit"], jc["region"]
    unit_rows = [r for r in fit if r["kind"] == "unit"]
    return {
        "figures/calibration_fit.csv": _csv(
            ["unit", "region", "target", "modelled", "residual_logit"],
            [[r["unit"], _group(r["unit"]), r["observed"], r["verified"],
              _r(_logit(r["verified"]) - _logit(r["observed"]))] for r in unit_rows]),
        "figures/calibration_residuals.csv": _csv(
            ["target", "observed", "modelled", "residual", "status"],
            [[r["target"], r["observed"], r["verified"], _r(r["observed"] - r["verified"]), r["status"]]
             for r in unit_rows]),
        "figures/calibration_unit_fit.csv": _csv(list(fit[0]), [list(r.values()) for r in fit]),
        "figures/calibration_bound_share.csv": _csv(
            ["group", "units", "best_at_top", "interval_reaches_top"],
            [[g, 4, 0.0, sum("hi" in jc["units"][u]["region_touches_bound"] for u in UNITS if _group(u) == g) / 4]
             for g in GROUPS]),
        "figures/calibration_region.csv": _csv(
            ["panel", "region_min", "region_max", "prior_lo", "prior_hi", "abs_spearman"],
            [[p, *reg["ranges"][p], *reg["prior_ranges"][p], None] for p in PARAMS]),
        "figures/calibration_waves.csv": _csv(
            ["wave", "share_of_prior", "accepted", "candidates"],
            [[w["wave"], w["share_of_prior"], w["accepted"], w["candidates"]] for w in jc["waves"]])}


def _render(m, stage):
    """Every file of the run as {path: text}, for a run that has reached `stage`."""
    method, files = m["method"], {}

    def sha(rel):
        return hashlib.sha256(_raw(files[rel])).hexdigest()

    def artifact(rel, kind, producer, inputs, data, minute, stale=False):
        earlier = hashlib.sha256(b"the input as it was when this file was computed").hexdigest()
        files[rel] = _yaml({"schema": f"analysis.{kind}/1", "produced_by": "harness", "producer": producer,
                            "spec_sha256": sha("decision_spec.yaml"), "spec_signed": True, "engine": ENGINE,
                            "engine_choice_sha256": sha(CHOICE) if m["bound"] else None,
                            "inputs": {**{i: earlier if stale else sha(i) for i in inputs}, **HARNESS_INPUT},
                            "written_at": _at(minute), "data": data})

    # as signed; an edit made to the file on disk after the sign-off (m["disk"]) replaces it once every hash is taken
    files["decision_spec.yaml"] = _spec_text(m["spec"], m["comments"])
    cols = ["unit_id", GROUPING, "name", "pop", "target_r2", "x1"]
    files["data/derived/units.csv"] = _csv(cols, [[u, _group(u), f"unit {u}", 1000 + 150 * i, _r(0.10 + 0.02 * i),
                                                   _r(0.5 + 0.03 * _wobble(i))] for i, u in enumerate(UNITS)])
    if method == "history_matching":
        files["data/derived/targets.csv"] = _csv(
            [GROUPING, "level", "round_id", "value_pct", "ci_low_pct", "ci_high_pct"],
            [[g, "group", "S1", 30 + 6 * i, 22 + 6 * i, 39 + 6 * i] for i, g in enumerate(GROUPS)])
    else:
        files["data/derived/targets.csv"] = _csv(
            ["unit", GROUPING, "source", "latent", "observed", "n"],
            [[f["unit"], f["groups"][0], f["source"], f["latent"], int(f["observed"]), int(f["n"])]
             for f in m["cal"]["diagnostics"]["fit"]])
    files["decision_brief.signed/decision_spec.yaml"] = files["decision_spec.yaml"]
    files[SETTINGS] = _yaml({"uncertainty": {"draws": DRAWS}, "analysis": {"workers": 2}})
    files[DOSSIER] = _yaml(m["dossier"])
    if DOSSIER in m["raw"]:
        files[DOSSIER] = m["raw"][DOSSIER](files[DOSSIER])
    plans = m["kind"] == "package_per_unit"
    if plans:
        files[BURDEN_CSV] = _csv(["id", "group", "outcome", "year", "value", "lower", "upper", "kind", "source_id", "role"],
                                 [[r["id"], r["group"], r["outcome"], r["year"], r["value"], r["lower"], r["upper"], r["kind"],
                                   r["source_id"], r["role"]] for r in _burden(DRAWS)["references"]])
    if not m["no_sheet"]:       # written from the spec, the dossier and the reference file, whose hashes its last line holds
        files[SHEET] = _sheet_text(m, sha("decision_spec.yaml"), sha)
    _archive(m, files)
    # The record holds what toolkit/spec/signoff.py sign() lists under sha256 (`{SPEC: sha, **extra}`, extra being
    # the derivation record, a reference file and the evidence files): here the spec and the dossier. The sheet's hash
    # is not there: sign() compares it with the one the owner was shown (expect_sheet_sha256) and records none.
    files["decision_spec.signoff.json"] = json.dumps(
        {"signed_by": m["signed_by"], "signed_at": _at(50), "note": m["signoff_note"],
         "sha256": {"decision_spec.yaml": sha("decision_spec.yaml"), DOSSIER: sha(DOSSIER)},
         "producer": "toolkit/spec/signoff.py", "schema_version": 1}, indent=1)
    files[CHOICE] = json.dumps(m["choice"], indent=1)
    files[LABELS] = _yaml({"labels": {"units": {u: f"unit {u}" for u in UNITS[:2]}}, "theme": "light"})

    def cut(rel):       # a change to a file's text as written (a file that stops short), made before anything hashes it
        if rel in m["raw"] and rel in files:
            files[rel] = m["raw"][rel](files[rel])
    files.update(_register_files(m))
    data_in = ["decision_spec.yaml", "data/derived/units.csv", "data/derived/targets.csv"]
    artifact(ENGINE_REC, "engine", "toolkit/analysis/run.py", ["decision_spec.yaml"], m["eng"], 95)
    if m["cut"]:       # the file stops inside the benchmark table, in a list that is never closed
        files[ENGINE_REC] = files[ENGINE_REC][:files[ENGINE_REC].index("    benchmarks:\n")] + "    benchmarks: [{id: B1,\n"
    if m["bsv"]:
        files[BSV] = _yaml(m["bsv"])
        files[BSV_REC] = json.dumps({
            "benchmarks": m["bsv"]["arm_engines"], "config_sha256": "no-config", "engine_identity": _identity(), "file": BSV,
            "pack_sha256": hashlib.sha256(b"toy pack").hexdigest(),
            "producer": "toolkit/spec/engine_parameters.py:write_spec_benchmarks", "sha256": sha(BSV),
            "spec_parameters": m["bsv"]["spec_parameters"], "spec_sha256": sha("decision_spec.yaml"), "written_at": _at(85)},
            indent=1, sort_keys=True)
    if plans:      # the emulator is fitted before the calibration (run.py); its batch files are not in the fixture
        artifact(EMU, "emulator", "toolkit/analysis/run.py", data_in[:2], m["emu"], 98)
    if method == "history_matching":
        artifact(JC, "joint_calibration", "toolkit/analysis/joint_calibration.py", data_in, m["jc"], 100)
        artifact(CAL, "calibration", "toolkit/analysis/calibration.py", ["data/derived/units.csv", JC],
                 _companion(m["jc"]), 110)
    else:
        artifact(CAL, "calibration", "toolkit/analysis/calibration.py", data_in[1:], m["cal"], 110)
    files.update(_figures(m))
    unc = copy.deepcopy(m["unc"])
    for part in ("interval", "draw_plan"):          # the lists of prior-dominated inputs are one list, written three times
        if part in unc:
            unc[part]["prior_dominated"] = list(unc["prior_dominated"])
    if plans:      # in the order toolkit/analysis/run.py writes them
        point = m["point"]
        artifact(POINT, "point", "toolkit/analysis/core.py", [CAL], point, 150, stale=m["stale"])
        artifact(EMB, "emulator_boundary", "toolkit/analysis/boundary.py", [POINT], m["emb"], 160)
        artifact(UNC, "uncertainty", "toolkit/analysis/uncertainty.py", [POINT], unc, 200)
        cut(UNC)
        artifact(VOI, "value_of_information", "toolkit/analysis/value_of_information.py", [UNC], m["voi"], 205)
        artifact(BV, "burden_validation", "toolkit/analysis/burden.py", [POINT, UNC, BURDEN_CSV], m["bv"], 210)
        artifact(STAB, "stability", "toolkit/analysis/stability.py", [POINT, CAL], m["stab"], 215)
        artifact(PV, "plan_variants", "toolkit/analysis/variants.py", [POINT], m["pv"], 220)
        if m["dt"] is not None:
            artifact(DT, "decision_tests", "toolkit/analysis/decision_tests.py", [POINT, UNC] + [BSV] * bool(m["bsv"]), m["dt"], 230)
            cut(DT)
            entries = {t["id"]: t for t in m["spec"]["analysis"]["decision_tests"]}
            for row in m["dt"]["tests"]:
                if row["path"]:
                    files[row["path"]] = _yaml(_test_file(row, entries.get(row["test_id"], {})))
        files["allocation.csv"] = _csv(["unit_id", "package", "outcome_averted"],
                                       [[u, x["package"], x["benefit"]] for u, x in point["per_unit"].items()])
        # the outcome batch's own record (toolkit/analysis/batch.py through run.py); its rows are not in the fixture
        artifact(BATCH, "batch", "toolkit/analysis/batch.py", data_in[1:2], {
            "evaluation": "emulator", "points": 64, "jobs": 192, "packages": ["A", "A+B", "A+C"],
            "fitted_names": ["p2", "f1"], "prior_names": [],
            "summary": {"n": 192, "n_failed": 0, "reused": 0, "computed": 192, "path": "analysis/batch/results.jsonl",
                        "rejected_rows": 0}}, 97)
        # what the figures are drawn from: the schedule, written with the analysis; the trajectory record, written by
        # the trajectory step after it, with its own header (no spec hash, and the manifest does not list it)
        fd = _figure_data(m)
        artifact(FD, "figure_data", "toolkit/analysis/figure_data.py", [POINT, UNC], fd, 231)
        if m["fig"]["set"] == "standard" and "timeline" not in m["fig"]["not_produced"]:
            files[TIMELINE] = _timeline_csv(fd["schedule"])
        if m["traj"]:
            tr = _trajectories(m, fd)
            m["dynamics"] = _dynamics(tr)
            if m["traj_edit"]:
                m["traj_edit"](tr)
            if m["dyn_edit"]:
                m["dyn_edit"](m["dynamics"])
            files[TRAJ] = _yaml({"schema": "analysis.trajectories/1", "produced_by": "harness",
                                 "producer": "toolkit/analysis/trajectories.py",
                                 "inputs": {i: sha(i) for i in ("decision_spec.yaml", POINT, CAL, FD, "data/derived/units.csv",
                                                                SETTINGS, CHOICE)},
                                 "written_at": _at(233), "data": tr})
        files[RESULTS] = ("# Results (synthetic fixture)\n\n- units: 12\n- packages: "
                          + ", ".join(f"{k} in {n}" for k, n in point["packages"].items())
                          + f"\n- {HEADLINE}: {point['averted'][OUTCOME]:g}\n")
    else:
        artifact(UNC, "uncertainty", "toolkit/analysis/uncertainty.py", [CAL], unc, 200)
        files[RESULTS] = "# Results (synthetic fixture)\n\n- outputs: q1, q2\n"
    if "est" in m:
        artifact(EST, "estimates", "toolkit/analysis/estimates.py", [CAL], m["est"], 232)
        cut(EST)
    images = _figure_files(m, files, sha, artifact)
    arts = [r for r in files if r.startswith("analysis/") and r.endswith(".yaml") and r not in (BSV, TRAJ)]
    files["analysis/manifest.json"] = json.dumps({
        "artifacts": {r: {"kind": r[9:-5], "producer": f"toolkit/analysis/{r[9:-5]}.py", "sha256": sha(r)} for r in arts},
        "complete": True,
        "deliverables": {**({"allocation.csv": {"producer": "toolkit/analysis/run.py", "role": "allocation",
                                                "sha256": sha("allocation.csv")}} if plans else {}),
                         **{r: {"producer": "toolkit/analysis/decision_tests.py", "role": "decision_test", "sha256": sha(r)}
                            for r in sorted(files) if r.startswith(TESTS_DIR)},
                         **images,
                         RESULTS: {"producer": "toolkit/analysis/claims.py", "role": "results", "sha256": sha(RESULTS)}},
        "engine": ENGINE, "engine_choice_sha256": sha(CHOICE) if m["bound"] else None,
        "expected": {"decision_tests": {t["test_id"]: t["path"] for t in (m["dt"] or {"tests": []})["tests"]},
                     "kinds": [r[9:-5] for r in arts]},
        "finished_at": _at(235), "produced_by": "harness", "reason": "", "settings_sha256": None,
        "spec_sha256": sha("decision_spec.yaml"), "spec_signed": True, "status": "complete", "wall_s": 8100.0}, indent=1)

    final, commit = stage == "final", _head_commit()
    hist = HISTORY[:REACHED[stage]]
    files["metadata.json"] = json.dumps({
        "question": m["spec"]["question"], "started": _at(0), "pipeline": "v2", "max_rounds": 2, "signoff": "owner",
        "commit": commit, "launcher": "fixture",
        "invocations": [{"pipeline": "v2", "commit": commit, "at": _at(0), "launcher": "fixture"}],
        "engine": "toolkit.engines.toy.engine:ToyEngine",
        **({"status": "completed", "rounds": 1, "model_spend_usd": hist[-1][3], "finished": _at(305)} if final else {})},
        indent=1)
    files["run_state.json"] = json.dumps({
        "question": m["spec"]["question"], "state": hist[-1][1], "round": 1, "max_rounds": 2, "signoff": "owner",
        "engine": ENGINE["name"], "budget_usd": 30.0, "spent_usd": hist[-1][3],
        "status": "completed" if final else None, "paused": "awaiting_signoff" if stage == "launch" else None,
        "journal": {"SPEC#0:try1": {"task": "draft_spec", "status": "completed", "started": _at(1), "ok": True,
                                    "allowance_usd": 3.0, "usd": 1.0, "completed": _at(30),
                                    "output": {"ok": True, "notes": "drafted the decision spec"}}},
        "history": [{"at": _at(t), "from": a, "to": b, "why": "ok", "round": 1, "spent_usd": usd}
                    for a, b, t, usd in hist]}, indent=1)
    files.update(_report_files(m))
    # what happens after the run wrote its hashes: an image removed or replaced, the engine choice rewritten, the spec
    # on disk edited
    for name in m["fig"]["dropped"]:
        del files[f"figures/{name}.png"]
    for name in m["fig"]["redrawn"]:
        files[f"figures/{name}.png"] = _png(f"{name}, drawn again")
    if m["choice_after"]:
        doc = copy.deepcopy(m["choice"])
        m["choice_after"](doc)
        files[CHOICE] = json.dumps(doc, indent=1)
    if m["disk"]:
        disk = copy.deepcopy(m["spec"])
        m["disk"](disk)
        files["decision_spec.yaml"] = _spec_text(disk, m["comments"])
    if m["signed_after"]:
        signed = copy.deepcopy(m["spec"])
        m["signed_after"](signed)
        files[SPEC_FILES[1]] = _spec_text(signed, m["comments"])
    keep = STAGES[:STAGES.index(stage) + 1]
    return {r: t for r, t in files.items() if stage_of(r) in keep}


def _raw(content):
    return content if isinstance(content, bytes) else content.encode("utf-8")


def build(dir, upto=None, defect=None, method="history_matching", figures="standard", kind="package_per_unit", other=None):
    """Write one fixture run into `dir` (created if missing). Returns {path: sha256} of the files written. Each file is
    written with lib.safe_io.write_nofollow below `dir`: a link at any name under it is refused, never followed.
    `figures` names the producer whose shape analysis/figures.yaml has (FIGURE_SETS): `standard` is
    toolkit/analysis/figures.py; `estimates` is toolkit/analysis/estimates.py, for the posterior method only (it
    draws from analysis/estimates.yaml)."""
    if method not in METHODS:
        raise ValueError(f"unknown method {method!r}; one of {', '.join(METHODS)}")
    if figures not in FIGURE_SETS:
        raise ValueError(f"unknown figure set {figures!r}; one of {', '.join(FIGURE_SETS)}")
    if figures == "estimates" and method != "posterior":
        raise ValueError("the estimates figure set is drawn from analysis/estimates.yaml, which only the posterior method writes")
    stage = "final" if upto is None else upto
    if stage not in STAGES:
        raise ValueError(f"unknown stage {upto!r}; one of {', '.join(STAGES)}")
    if kind not in KINDS:
        raise ValueError(f"unknown deliverable kind {kind!r}; one of {', '.join(KINDS)}")
    if kind == "estimates" and (method != "posterior" or figures != "estimates"):
        raise ValueError("the estimates deliverable is built with the posterior method and the estimates figure set")
    if other is not None and (other not in OTHERS or kind != "package_per_unit"):
        raise ValueError(f"unknown comparison run {other!r}; one of {', '.join(OTHERS)}, for the package_per_unit kind")
    m = _model(method, figures, kind)
    if other is not None:
        _other(m, other)
    if defect is not None:
        if defect not in DEFECTS:
            raise ValueError(f"unknown defect {defect!r}; see --list")
        if kind not in DEFECTS[defect].get("kinds", KINDS[:1]):
            raise ValueError(f"defect {defect} is not defined for the {kind} deliverable")
        if method not in DEFECTS[defect]["methods"]:
            raise ValueError(f"defect {defect} is not defined for method {method}")
        if figures != "standard" and figures not in figure_sets(defect):
            raise ValueError(f"defect {defect} is not defined for the {figures} figure set")
        DEFECTS[defect]["apply"](m)
    out = {}
    os.makedirs(dir, exist_ok=True)
    for rel, content in _render(m, stage).items():
        write_nofollow(dir, rel, _raw(content))
        out[rel] = hashlib.sha256(_raw(content)).hexdigest()
    return out


# ------------------------------------------------------------------------------------------- self-test

def domain_words():
    with open(os.path.join(HERE, "domain_words.txt"), encoding="utf-8") as f:
        return [w.strip().lower() for w in f if w.strip() and not w.startswith("#")]


def _headers(dir, shas):
    """Each analysis file's header, parsed from the file on disk: {path: (spec_sha256, {input: sha})}."""
    out = {}
    for rel in shas:
        if rel.startswith("analysis/") and rel.endswith(".yaml") and rel != BSV:     # BSV has no artifact header
            with open(os.path.join(dir, rel), encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            out[rel] = (doc.get("spec_sha256"), doc["inputs"], doc)
    return out


def _follows(clean_dir, clean, first):
    """`first` plus every file that records a hash of a changed file, found from the clean run's own headers."""
    changed, heads = set(first), _headers(clean_dir, clean)
    signed = SPEC_FILES[1] in changed       # the spec as signed changed: every file that records its hash follows
    records = {}                            # each figure's provenance record: the files whose hashes it holds
    for rel in clean:
        if rel.endswith(".provenance.json"):
            with open(os.path.join(clean_dir, rel), encoding="utf-8") as f:
                doc = json.load(f)
            records[rel] = set(doc["source_hashes"]) | {e["path"] for e in doc["presentation"]}
    while True:
        more = {r for r, (_, inputs, _) in heads.items()
                if signed or CHOICE in changed or any(i in changed for i in inputs if i != "decision_spec.yaml")}
        more |= {r for r, sources in records.items() if signed or (sources - {"decision_spec.yaml"}) & changed}
        if (signed or BSV in changed) and BSV_REC in clean:
            more.add(BSV_REC)
        if REPORT in changed:
            more.add(RENDER)
        if CHOICE in changed or any(r.endswith(".png") and "overview_" not in r for r in changed):
            more.add("analysis/manifest.json")
        if signed:
            more |= {"decision_spec.signoff.json", SHEET}
        if DOSSIER in changed | more:                    # the sign-off record holds its hash
            more.add("decision_spec.signoff.json")
        if {DOSSIER, BURDEN_CSV} & (changed | more) and SHEET in clean:      # so does the sheet's last line
            more.add(SHEET)
        if any((r.startswith("analysis/") and r not in (BSV, BSV_REC)) or r in ("allocation.csv", RESULTS) for r in changed | more):
            more.add("analysis/manifest.json")
        if more <= changed:
            return changed
        changed |= more


def _underived(dir):
    """What cannot be recomputed in a built history-matching run's change pairs from its own target rows (nothing
    when every pair follows): the change is the mean of the group's later-round unit rows minus its earlier-round
    group row, observed and modelled, and z is their difference on the targets' scale over the pair's sd."""
    with open(os.path.join(dir, JC), encoding="utf-8") as f:
        jc = yaml.safe_load(f)["data"]
    with open(os.path.join(dir, "data/derived/units.csv"), encoding="utf-8") as f:
        head, *rows = [line.strip().split(",") for line in f]
    group_of = {r[0]: r[head.index(GROUPING)] for r in rows}
    out, sc = [], jc["survey_changes"]
    for p in sc["pairs"]:
        us = [u for u in jc["unit_targets"] if group_of[u] == p["group"]]
        obs = [jc["unit_targets"][u][0]["value"] for u in us]
        mod = [jc["verification"]["units"][u]["targets"][0]["simulated"] for u in us]
        tid = f"{p['from']}:{p['group']}"
        obs_g, mod_g = jc["targets"][tid]["value"], jc["verification"]["targets"][tid]["simulated"]
        want = {"observed_natural": sum(obs) / len(obs) - obs_g, "modelled_natural": sum(mod) / len(mod) - mod_g,
                "observed": sum(map(_logit, obs)) / len(obs) - _logit(obs_g),
                "modelled": sum(map(_logit, mod)) / len(mod) - _logit(mod_g)}
        out += [f"{p['group']} {k}: {p[k]} in the pair, {_r(w)} from the target rows" for k, w in want.items()
                if abs(p[k] - w) > 1e-5]
        if abs(p["sd"] - math.sqrt(sum(v * v for v in p["sd_parts"].values()))) > 1e-5:
            out.append(f"{p['group']} sd {p['sd']} is not the root sum of squares of its parts")
        if abs(p["z"] - (p["modelled"] - p["observed"]) / p["sd"]) > 1e-4 or p["flagged"] != (abs(p["z"]) > sc["z_max"]):
            out.append(f"{p['group']} z {p['z']} (flagged {p['flagged']}) does not follow from its change and sd")
    if sorted(sc["flagged"]) != sorted(p["id"] for p in sc["pairs"] if p["flagged"]):
        out.append("survey_changes.flagged does not list the flagged pairs")
    return out


def _ledger_words():
    """(the fields of a ledger row, its status words), read from the producer's source without importing it."""
    import ast
    with open(os.path.join(REPO, "agents", "findings_ledger.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fields = next([s.target.id for s in n.body if isinstance(s, ast.AnnAssign)] for n in tree.body
                  if isinstance(n, ast.ClassDef) and n.name == "Finding")
    words = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                 and any(getattr(t, "id", None) == "STATUSES" for t in n.targets))
    return fields, words


def _register_cases(tmp, ok):
    """The register's files (§6): shaped as the producers write them, and agreeing with each other on the clean run."""
    fields, words = _ledger_words()
    for method in METHODS:
        d = os.path.join(tmp, "clean", method)

        def text(rel, d=d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                return f.read()
        rows = [json.loads(line) for line in text(LEDGER).splitlines()]
        ok(f"X15 {method}: each findings.jsonl row has exactly the ledger's fields and one of its status words",
           len(rows) == 4 and all(sorted(r) == sorted(fields) and r["status"] in words for r in rows)
           and all(e["status"] in words for r in rows for e in r["events"]), str([sorted(set(r) ^ set(fields)) for r in rows]))
        critics = [r for r in rows if r["source"].startswith("critique")]
        ok(f"X16 {method}: no finding is open or declared; critics' findings and one raised by a harness check are both present",
           all(r["status"] == "fixed" for r in rows) and len(critics) == 3
           and [r["id"] for r in rows if r not in critics] == [STALE_ID]
           and {r["severity"] for r in critics} == {"HIGH", "MEDIUM"})

        def built(name, rel, d=d, method=method):
            with open(os.path.join(tmp, "defect", method, name, rel), encoding="utf-8") as f:
                return f.read()

        def named_tests(read, there):
            """(the tests file's rows by id, the test each declaration, no-data entry and finding names) of a run."""
            tests = {t["test_id"]: t for t in yaml.safe_load(read(DT))["data"]["tests"]}
            scope = yaml.safe_load(read(SCOPE)) if there(SCOPE) else {"declarations": []}
            spec, found = yaml.safe_load(read(SPEC_FILES[1])), [json.loads(line) for line in read(LEDGER).splitlines()]
            named = ([os.path.basename(e["decision_test"])[:-5] for e in scope["declarations"]]
                     + [e["decision_test"] for e in json.loads(read(CHOICE))["engine_departures"]]
                     + [e["decision_test"] for e in yaml.safe_load(read(DOSSIER))["inputs"].values() if "no_data" in e]
                     + [e["decision_test"] for e in (spec["calibration"].get("joint") or {}).get("unmatched", {}).get("declared", [])]
                     + [os.path.basename(r["decision_test"]["file"])[:-5] for r in found if r["decision_test"]])
            return tests, named
        tests, named = named_tests(text, lambda rel: os.path.exists(os.path.join(d, rel)))
        ran = {k for k, t in tests.items() if t["status"] == "run"}
        choice, spec = json.loads(text(CHOICE)), yaml.safe_load(text(SPEC_FILES[1]))
        dossier = yaml.safe_load(text(DOSSIER))["inputs"]
        verdicts = sorted({t["verdict"] for t in tests.values()})
        moved = {v: sorted(t["test_id"] for t in yaml.safe_load(built(v, DT))["data"]["tests"] if t["verdict"] != "NO_CHANGE")
                 for v in ("test_changes_linked", "test_changes_unlinked", "test_not_run")}
        ok(f"X17 {method}: on the clean run every decision test ran with no change (§9) and every test a dossier entry "
           f"names ran; the variants hold a plan-changing test a row names, one no row names, and a test that did not run",
           verdicts == ["NO_CHANGE"] and set(tests) == ran and set(named) <= ran and len(set(named)) == len(named) == 3
           and "DT9" in named and "DT1" not in named
           and moved == {"test_changes_linked": ["DT9"], "test_changes_unlinked": ["DT1"], "test_not_run": [f"scenario_excluded_{HELD}"]},
           str((verdicts, sorted(set(named) - ran), named, moved)))
        eng = yaml.safe_load(text(ENGINE_REC))["data"]["card"]["benchmarks"]
        chosen = choice["candidates"][0]
        status = {b["id"]: b["status"] for b in eng}
        ok(f"X18 {method}: the clean run holds nothing declared (no departure, no declared miss, no scope declaration, "
           f"no finding held as declared), and the engine choice and the engine record agree on every benchmark (§9)",
           not os.path.exists(os.path.join(d, SCOPE)) and "engine_departures" not in spec and choice["engine_departures"] == []
           and chosen["departures"] == [] and all(r["status"] not in ("declared", "owner_accepted") and not r["decision_test"] for r in rows)
           and (method != "history_matching" or (spec["calibration"]["joint"]["unmatched"]["declared"] == []
                                                 and yaml.safe_load(text(JC))["data"]["unmatched_decision"]["declared"] == []))
           and chosen["benchmarks"] == len(eng) and all(r["status"] == status[r["id"]] for r in chosen["benchmarks_reported"])
           and [r["id"] for r in chosen["benchmarks_not_in_band"]] == [i for i, st in status.items() if st != "in_band"] == [])
        dtests, dnamed = named_tests(lambda rel: built("declared", rel), lambda rel: os.path.exists(os.path.join(tmp, "defect", method, "declared", rel)))
        drows = [json.loads(line) for line in built("declared", LEDGER).splitlines()]
        dscope, dchoice = yaml.safe_load(built("declared", SCOPE)), json.loads(built("declared", CHOICE))
        dspec = yaml.safe_load(built("declared", SPEC_FILES[1]))
        dstatus = {b["id"]: b["status"] for b in yaml.safe_load(built("declared", ENGINE_REC))["data"]["card"]["benchmarks"]}
        ok(f"X18b {method}, declared: every test a declaration names ran with no change; the scope declaration names a "
           f"finding the ledger holds as declared with the same test; each engine departure is signed with the keys the "
           f"engine choice writes, is the signed spec's entry, and gives the benchmark the engine record's own status"
           f"{'; the signed spec declares a calibration miss' if method == 'history_matching' else ''}",
           all(dtests[t]["status"] == "run" and dtests[t]["verdict"] == "NO_CHANGE" for t in set(dnamed) - {"DT9"})
           and len(set(dnamed)) == len(dnamed) - 1 and {"DT3", "DT4", "DT5"} <= set(dnamed)
           and all(e["blocker_id"] in {r["id"] for r in drows} for e in dscope["declarations"])
           and [r["id"] for r in drows if r["status"] == "declared"] == ["R-001"]
           and all(r["status"] != "declared" or r["decision_test"]["file"] in [e["decision_test"] for e in dscope["declarations"]] for r in drows)
           and [sorted(e) for e in dchoice["engine_departures"]] == [
               sorted(["engine", k, "reason", "decision_test", "status", "benchmark_status", "role", "components"])
               for k in ("benchmark", "component")] and all(e["status"] == "signed" for e in dchoice["engine_departures"])
           and [{k: e[k] for k in s} for e, s in zip(dchoice["engine_departures"], dspec["engine_departures"])] == dspec["engine_departures"]
           and dchoice["candidates"][0]["departures"] == dchoice["engine_departures"]
           and [(e["benchmark_status"], e["role"]) for e in dchoice["engine_departures"]] == [(dstatus["B2"], "held_out"), ("no benchmark", "none")]
           and dstatus["B2"] == "out_of_band" and dchoice["candidates"][0]["benchmarks_not_in_band"] == []
           and (method != "history_matching" or dspec["calibration"]["joint"]["unmatched"]["declared"] == [CAL_DECLARED]
                == yaml.safe_load(built("declared", JC))["data"]["unmatched_decision"]["declared"]))
        sheet = text(SHEET).splitlines()
        ok(f"X19 {method}: the clean sign-off sheet marks no row HIGH, names the signed spec's hash, and holds the word "
           f"HIGH where it marks nothing",
           sum(line.startswith("- **HIGH**:") for line in sheet) == 0 and sum("HIGH" in line for line in sheet) == 2
           and not any(line.startswith("| HIGH |") for line in sheet)
           and hashlib.sha256(text(SPEC_FILES[1]).encode()).hexdigest()[:16] in sheet[2])
        no_data = [k for k, e in dossier.items() if "no_data" in e]
        ok(f"X20 {method}: the dossier holds no-data entries and one with observations; the signed spec excludes an "
           f"input by name; no comment line",
           len(no_data) == 3 and sum("observations" in e for e in dossier.values()) == 2
           and spec["uncertainty"]["excluded"][0]["parameter"] == HELD and f"parameter:{HELD}" in no_data
           and not any(line.lstrip().startswith("#") for line in text(SPEC_FILES[1]).splitlines()))

        high = built("sheet_high", SHEET).splitlines()
        ok(f"X19b {method}, sheet_high: the sheet marks one row HIGH, and the sign-off record is the clean run's: it "
           f"holds a hash for the spec and the dossier and none for the sheet, as toolkit/spec/signoff.py sign() writes it",
           sum(line.startswith("- **HIGH**:") for line in high) == 1 and sum("HIGH" in line for line in high) == 3
           and built("sheet_high", "decision_spec.signoff.json") == text("decision_spec.signoff.json")
           and sorted(json.loads(text("decision_spec.signoff.json"))["sha256"]) == sorted(["decision_spec.yaml", DOSSIER]))
        signed, disk = built("declared_only_on_disk", SPEC_FILES[1]), built("declared_only_on_disk", SPEC_FILES[0])
        ok(f"X21 {method}: declared_only_on_disk changes the spec on disk alone; the signed copy and the sign-off "
           f"record's hash are the clean run's",
           "q7" in disk and "q7" not in signed and signed == text(SPEC_FILES[1])
           and built("declared_only_on_disk", "decision_spec.signoff.json") == text("decision_spec.signoff.json"))
        prose = [line for line in built("prose_only", SPEC_FILES[1]).splitlines() if line.startswith("#")]
        ok(f"X22 {method}: prose_only puts the same four comment lines in both copies of the spec, which still parse",
           prose == PROSE and built("prose_only", SPEC_FILES[0]) == built("prose_only", SPEC_FILES[1])
           and yaml.safe_load(built("prose_only", SPEC_FILES[1])) == spec)
        last = built("findings_bad_line", LEDGER).splitlines()[-1]
        try:
            json.loads(last)
            parses = True
        except ValueError:
            parses = False
        odd = [json.loads(x)["status"] for x in built("finding_unknown_status", LEDGER).splitlines()]
        ok(f"X23 {method}: the bad ledger line is not JSON and the four rows before it are; the odd status is not a "
           f"word the ledger writes",
           not parses and len(built("findings_bad_line", LEDGER).splitlines()) == 5
           and [w for w in odd if w not in words] == ["resolved"])
        old = yaml.safe_load(built("older_layout", OLD_DECLARED))
        gate = json.loads(built("older_layout", OLD_GATE))
        ok(f"X24 {method}: the older layout has no scope declaration, and its two files name findings the ledger holds as open",
           not os.path.exists(os.path.join(tmp, "defect", method, "older_layout", SCOPE))
           and [e["id"] for e in old["declared"]] == ["R-001"] and gate["resolved_by_test"] == ["D-002"]
           and {json.loads(x)["id"]: json.loads(x)["status"] for x in built("older_layout", LEDGER).splitlines()}
           == {"D-001": "fixed", "D-002": "open", "R-001": "open", STALE_ID: "fixed"})
    ok("X25 twenty-one edits name the register as their view, each for both methods; the Fit view's edits name none",
       len(REGISTER_DEFECTS) == 21 and all(DEFECTS[n]["methods"] == METHODS for n in REGISTER_DEFECTS)
       and all(s.get("view") in (None, "register", "model", "figures", "outcomes", "spec", "scenarios") for s in DEFECTS.values()))


def _producer_words():
    """What the producers' code holds, read from their source without importing it: the benchmark status words
    (toolkit/engines/benchmark.py STATUSES), the tiers of the standard figure set, its figure names and the producer
    name (toolkit/analysis/figures.py TIERS, DECISION_SETS, PRODUCER), and whether each sentence the fixture copies is
    still in the file that writes it."""
    import ast

    def source(*parts):
        with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
            return f.read()

    def constant(text, name):
        return next(ast.literal_eval(n.value) for n in ast.parse(text).body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == name for t in n.targets))
    bench, figs = source("toolkit", "engines", "benchmark.py"), source("toolkit", "analysis", "figures.py")
    cover, est = source("toolkit", "engines", "benchmark_coverage.py"), source("toolkit", "analysis", "estimates.py")
    tiers, sets = constant(figs, "TIERS"), constant(figs, "DECISION_SETS")
    quoted = (all(f'"{w}"' in bench for w in MIS_BASED) and REPORTED_WHY.split(", ")[0] in cover
              and NO_TRAJECTORIES in figs and NO_TESTS_FILE.replace("decision_tests", "{kind}") in figs
              and 'if name.startswith("estimates_") else "diagnostics"' in est)
    return {"statuses": constant(bench, "STATUSES"), "tiers": {**tiers, "decision": sets["package_per_unit"]},
            "producer": constant(figs, "PRODUCER"), "quoted": quoted}


def clean_of(tmp, method):
    """The names in the clean run's analysis folder."""
    return {f"analysis/{n}" for n in os.listdir(os.path.join(tmp, "clean", method, "analysis"))}


def _model_cases(tmp, ok):
    """The Model view's files (§5.4): shaped as the producers write them, and clear of every rule on the clean run."""
    words = _producer_words()
    ok("X26 the status words, the figure tiers and names, the producer name and the copied sentences are the producers' own",
       tuple(words["statuses"]) == BENCH_STATUSES and words["producer"] == FIG_PRODUCER["standard"] and words["quoted"])
    for method in METHODS:
        d = os.path.join(tmp, "clean", method)

        def doc(rel, d=d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                return json.load(f) if rel.endswith(".json") else yaml.safe_load(f)
        eng, choice, spec = doc(ENGINE_REC), doc(CHOICE), doc(SPEC_FILES[1])
        rows, chosen = eng["data"]["card"]["benchmarks"], doc(CHOICE)["candidates"][0]
        clear = [0.25 * math.log(b["band"][1] / b["band"][0]) < math.log(v / b["band"][0]) < 0.75 * math.log(b["band"][1] / b["band"][0])
                 for b in rows for v in (b["computed"], b["seed_min"], b["seed_max"])]
        ok(f"X27 {method}: four benchmarks, each in band with its value and its seeds clear of both edges on the log "
           f"scale; both roles; one band spans more than a factor of ten",
           len(rows) == 4 and all(b["status"] == "in_band" and b["in_band"] is True for b in rows) and all(clear)
           and {b["role"] for b in rows} == {"fit", "held_out"} and sum(b["band"][1] / b["band"][0] > 10 for b in rows) == 1)
        heads = _headers(d, {r: 1 for r in artifacts(method)})
        with open(os.path.join(d, CHOICE), "rb") as f:
            now = hashlib.sha256(f.read()).hexdigest()
        ok(f"X28 {method}: the spec leaves the engine to the engine choice, and every analysis header and the manifest "
           f"hold the hash of engine_choice.json",
           spec["engine"] == "auto" and all(h[2]["engine_choice_sha256"] == now for h in heads.values())
           and doc("analysis/manifest.json")["engine_choice_sha256"] == now and len(heads) == len(artifacts(method)))
        ok(f"X29 {method}: the engine choice has three candidates (one eligible, one whose card does not fit, one with "
           f"no readable card); the chosen one lists no benchmark as not in band and one as reported, with the record's status",
           [c["eligible"] for c in choice["candidates"]] == [True, False, False] and choice["engine"]["ref"] == chosen["ref"]
           and "name" not in choice["candidates"][2] and chosen["benchmarks_not_in_band"] == [] and chosen["benchmarks"] == 4
           and [(r["id"], r["status"], r["why"]) for r in chosen["benchmarks_reported"]] == [("B4", "in_band", REPORTED_WHY)])
        sv, pars = eng["data"]["spec_values"], eng["data"]["parameters"]
        ok(f"X30 {method}: one spec-set value, inside the card's range and flagged so, also in the signed spec; every "
           f"parameter has a range and is not marked unverified; no fitted parameter sits on its fitted range's end",
           list(sv) == list(spec["engine_parameters"]) == ["p2"] and sv["p2"]["outside_card_range"] is False
           and sv["p2"]["outside"] is None and sv["p2"]["card_range"][0] <= sv["p2"]["range"][0] and sv["p2"]["range"][1] <= sv["p2"]["card_range"][1]
           and sv["p2"]["value"] == spec["engine_parameters"]["p2"]["value"]
           and all(p["lo"] is not None and p["hi"] is not None and p["verified"] == "matched" for p in pars)
           and all(f["lo"] < f["value"] < f["hi"] for f in eng["data"]["card"]["fitted"].values())
           and sorted(p["name"] for p in pars if p["kind"] == "fitted") == sorted(eng["data"]["card"]["fitted"]))
        def built(name, rel, method=method):
            with open(os.path.join(tmp, "defect", method, name, rel), encoding="utf-8") as f:
                return f.read()
        held = {}
        for v in ("C4_out_of_band_departure", "departure_component"):
            signed, record = yaml.safe_load(built(v, SPEC_FILES[1]))["engine_departures"], json.loads(built(v, CHOICE))
            tests = {t["test_id"]: t for t in yaml.safe_load(built(v, DT))["data"]["tests"]}
            row = {b["id"]: b for b in yaml.safe_load(built(v, ENGINE_REC))["data"]["card"]["benchmarks"]}
            held[v] = (len(signed) == 1 and record["engine_departures"] == record["candidates"][0]["departures"]
                       and [{k: e[k] for k in signed[0]} for e in record["engine_departures"]] == signed
                       and record["engine_departures"][0]["status"] == "signed"
                       and record["engine_departures"][0]["benchmark_status"] == (row[signed[0]["benchmark"]]["status"] if "benchmark" in signed[0] else "no benchmark")
                       and all(tests[e["decision_test"]]["status"] == "run" and tests[e["decision_test"]]["verdict"] == "NO_CHANGE"
                               and tests[e["decision_test"]]["share_pct"] < 0.75 * tests[e["decision_test"]]["threshold_pct"] for e in signed))
        ok(f"X31 {method}: a variant's signed departure is in the signed spec and, with the benchmark's own status, in "
           f"the engine choice; its test ran, changed nothing and sits clear of its threshold", all(held.values()), str(held))
        v = "spec_benchmarks_recomputed"
        again, record, tests_file = yaml.safe_load(built(v, BSV)), json.loads(built(v, BSV_REC)), yaml.safe_load(built(v, DT))
        ok(f"X32 {method}: the clean run has no benchmarks recomputed at the spec's values; the variant's file has no "
           f"artifact header, its record holds the file's hash and the signed spec's, the decision tests list it as an "
           f"input and the manifest does not list it",
           BSV not in clean_of(tmp, method) and "spec_sha256" not in again and again["producer"] == "toolkit/engines/benchmark.py"
           and [b["id"] for b in again["benchmarks"]] == ["B2"] == list(record["benchmarks"])
           and record["sha256"] == hashlib.sha256(built(v, BSV).encode()).hexdigest() and record["file"] == BSV
           and record["spec_sha256"] == tests_file["spec_sha256"] and BSV in tests_file["inputs"]
           and BSV not in json.loads(built(v, "analysis/manifest.json"))["artifacts"])
        try:
            yaml.safe_load(built("engine_record_unparsable", ENGINE_REC))
            parses = True
        except yaml.YAMLError:
            parses = False
        out = [b for b in yaml.safe_load(built("C4_unreachable", ENGINE_REC))["data"]["card"]["benchmarks"] if b["status"] != "in_band"]
        ok(f"X33 {method}: the cut engine record is not YAML; the unreachable band is written in the producer's words",
           not parses and [(b["id"], b["status"], b["mis_based_reason"], b["in_band"]) for b in out] == [("B4", BENCH_STATUSES[3], MIS_BASED[1], False)])
        signed, disk = built("C4_out_of_band_departure_unsigned", SPEC_FILES[1]), built("C4_out_of_band_departure_unsigned", SPEC_FILES[0])
        ok(f"X34 {method}: the unsigned departure is in the spec on disk alone",
           "benchmark: B3" in disk and "benchmark: B3" not in signed and signed == yaml.safe_dump(spec, sort_keys=False, default_flow_style=False, allow_unicode=True, width=1000))
    ok("X35 the Model view's edits: every check has one that fires and one whose inputs contradict each other or miss a piece",
       {DEFECTS[n]["check"] for n in MODEL_DEFECTS} == {"benchmarks", "departures", "choice_text", "spec_values", "choice_bound",
                                                        "seed_range", "parameters_sourced", "fitted_at_bound"}
       and all(DEFECTS[n]["methods"] == METHODS for n in MODEL_DEFECTS) and len(MODEL_DEFECTS) == 25, str(len(MODEL_DEFECTS)))


def _figures_cases(tmp, ok):
    """The Figures view's files (§5.12): shaped as the two producers write them, fresh and used on the clean run."""
    words = _producer_words()
    for label, method, figures in (("history_matching", "history_matching", "standard"), ("posterior", "posterior", "standard"),
                                   ("estimates", "posterior", "estimates")):
        d = os.path.join(tmp, "clean", label)

        def raw(rel, d=d):
            with open(os.path.join(d, rel), "rb") as f:
                return f.read()

        def doc(rel):
            return json.loads(raw(rel)) if rel.endswith(".json") else yaml.safe_load(raw(rel))
        index, man = doc(FIGS), doc("analysis/manifest.json")
        data, standard = index["data"], figures == "standard"
        entries = data["figures"]
        want = (["figure", "path", "svg", "table", "title", "caption", "shows", "sources", "offset_free", "theme", "tier",
                 "group", "presentation"] if standard else ["id", "path", "svg", "sources", "tier"])
        ok(f"X36 {label}: analysis/figures.yaml names its producer and holds every figure as produced, with the "
           f"producer's own keys; nothing is listed as not produced",
           index["producer"] == FIG_PRODUCER[figures] and index["schema"] == "analysis.figures/1" and data["not_produced"] == []
           and all(list(e) == want + ["checks"] * (e.get("figure") == "trajectories") for e in entries)
           and list(data) == ["figures", "not_produced", "tiers", "groups", "overviews"])
        names = [e["figure" if standard else "id"] for e in entries]
        if standard:
            ok(f"X37 {label}: three tiers under the producer's names, every figure name one of its tier in the producer's "
               f"code, and an overview image per tier",
               list(data["tiers"]) == ["data_model", "simulation", "decision"] and sorted(sum(data["tiers"].values(), [])) == sorted(names)
               and all(set(v) <= set(words["tiers"][k]) and v for k, v in data["tiers"].items())
               and all(e["tier"] == next(k for k, v in data["tiers"].items() if e["figure"] in v) for e in entries)
               and all(raw(rel)[:8] == b"\x89PNG\r\n\x1a\n" for rel in data["overviews"].values()) and len(data["overviews"]) == 3)
        else:
            ok(f"X37 {label}: the estimates producer records no tiers, groups or overviews; each entry's tier follows its name",
               data["tiers"] == data["groups"] == data["overviews"] == {} and
               all(e["tier"] == ("decision" if e["id"].startswith("estimates_") else "diagnostics") for e in entries) and len(entries) == 4)
        fresh, listed, images = [], [], set()
        for e in entries:
            rec, png = doc(e["path"] + ".provenance.json"), raw(e["path"])
            images.add(hashlib.sha256(png).hexdigest())
            fresh.append(png[:8] == b"\x89PNG\r\n\x1a\n" and sorted(rec["source_hashes"]) == sorted(e["sources"]) and rec["png_path"] == e["path"]
                         and all(hashlib.sha256(raw(x)).hexdigest() == h for x, h in rec["source_hashes"].items())
                         and all(hashlib.sha256(raw(x["path"])).hexdigest() == x["sha256"] for x in rec["presentation"])
                         and rec["presentation"] == e.get("presentation", []) and (not standard or raw(e["table"])))
            listed.append(man["deliverables"].get(e["path"]) == {"producer": FIG_PRODUCER[figures], "role": "figure",
                                                                 "sha256": hashlib.sha256(png).hexdigest()})
        ok(f"X38 {label}: each figure has a valid image of its own, its table and a provenance record whose source and "
           f"presentation hashes are the files'; the manifest lists each image with its hash",
           all(fresh) and all(listed) and len(images) == len(entries) and len(entries) == (4 if not standard else 13 if method == "history_matching" else 8))
        texts = raw(REPORT).decode() + raw(MANUSCRIPT).decode() + raw(RESULTS).decode()
        decision = [e["path"] for e in entries if e["tier"] == "decision"]
        ok(f"X39 {label}: every decision figure is named by its path or its file name in a report text; the render "
           f"record holds the report's hash; the word trajectories is in the report and names no file",
           len(decision) >= 2 and all(os.path.basename(x) in texts for x in decision) and any(x not in texts for x in decision)
           and doc(RENDER)["report_sha256"] == hashlib.sha256(raw(REPORT)).hexdigest() and "trajectories" in texts
           and "trajectories.png" not in texts)
    ok("X40 the Figures view's edits: every check has one that fires and one whose inputs contradict each other or miss a piece",
       {DEFECTS[n]["check"] for n in FIGURES_DEFECTS} == {"figure_fresh", "figure_missing", "figure_unused", "figure_in_manifest"}
       and len(FIGURES_DEFECTS) == 12 and sum("estimates" in figure_sets(n) for n in FIGURES_DEFECTS) == 9, str(len(FIGURES_DEFECTS)))


def _outcome_words():
    """What the producers of the Outcomes files hold, read from their source without importing it."""
    import ast

    def source(*parts):
        with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
            return f.read()

    def constant(text, name):
        return next(ast.literal_eval(n.value) for n in ast.parse(text).body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == name for t in n.targets))
    th = yaml.safe_load(source("toolkit", "analysis", "thresholds.yaml"))
    flag, stab, run = source("toolkit", "analysis", "red_flag.py"), source("toolkit", "analysis", "stability.py"), source("toolkit", "analysis", "run.py")
    return {"voi": constant(source("toolkit", "analysis", "value_of_information.py"), "STATUSES"),
            "burden": constant(source("toolkit", "analysis", "burden.py"), "STATUSES"),
            "roles": constant(source("toolkit", "analysis", "burden.py"), "OPTIONAL")["role"],
            "kinds": constant(source("toolkit", "spec", "schema.py"), "DELIVERABLE_KINDS"),
            "criteria": constant(source("toolkit", "analysis", "moves.py"), "CRITERIA"),
            "dearest": (constant(flag, "SHARE"), constant(flag, "MIN_UNITS")), "th": th,
            "quoted": all(f'"{w}"' in stab for w in STABILITY_VERDICTS) and all(f'"{w}"' in source("toolkit", "analysis", "variants.py") for w in VARIANT_STATUSES)
            and '"judged" if n else "not_judged"' in source("toolkit", "analysis", "boundary.py")
            and '"NOT_RUN" if x["status"] == "not_run" else "CHANGES_PLAN" if x["decision_changes"] else "NO_CHANGE"' in run
            and all(f'"{w}"' in source("toolkit", "analysis", "estimates.py") for w in EST_VERDICTS)
            and CRITERION.split(", or")[0].replace("10%", "{thr:.0%}") in source("toolkit", "analysis", "decision_tests.py"),
            "order": [k for k in ("emulator", "calibration", "point", "emulator_boundary", "uncertainty", "value_of_information",
                                  "burden_validation", "stability", "plan_variants", "decision_tests")
                      if f'S.write("{k}"' in run],
            "written": sorted(("emulator", "calibration", "point", "emulator_boundary", "uncertainty", "value_of_information",
                               "burden_validation", "stability", "plan_variants", "decision_tests"),
                              key=lambda k: run.index(f'S.write("{k}"'))}


def _outcomes_cases(tmp, ok):
    """The Outcomes view's files (§5.7): shaped as the producers write them, agreeing with each other on the clean
    run and clear of every rule and of its near-miss band."""
    w = _outcome_words()
    ok("X41 the status words, verdicts, kinds, criteria and thresholds of the Outcomes files are the producers' own",
       tuple(w["voi"]) == VOI_STATUSES and tuple(w["burden"]) == BURDEN_STATUSES and tuple(w["roles"]) == BURDEN_ROLES
       and tuple(w["kinds"]) == KINDS and list(w["criteria"]) == list(TEST_THRESHOLDS) and w["dearest"] == (DEAREST_SHARE, DEAREST_MIN_UNITS)
       and w["th"]["stability"] == VALUE_LOSS and w["th"]["burden"]["default_ratio_band"] == BURDEN_BAND
       and (w["th"]["value_of_information"]["min_draws"], w["th"]["value_of_information"]["informative_draws"]) == (VOI_MIN_DRAWS, VOI_INFORMATIVE_DRAWS)
       and w["th"]["emulator"]["boundary_rank_agreement_min"] == BOUNDARY_MIN
       and [w["th"]["decision_tests"][k] for k in ("share_threshold", "regret_share_threshold", "dollars_share_threshold")] == list(TEST_THRESHOLDS.values())
       and w["quoted"] and w["order"] == w["written"], str({k: v for k, v in w.items() if k != "th"}))
    for method in METHODS:
        d = os.path.join(tmp, "clean", method)

        def doc(rel, d=d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                return json.load(f) if rel.endswith(".json") else yaml.safe_load(f)

        def data(rel, d=d):
            return doc(rel, d)["data"]
        spec, point, unc, tests = doc(SPEC_FILES[1]), data(POINT), data(UNC), data(DT)["tests"]
        with open(os.path.join(d, "allocation.csv"), encoding="utf-8") as f:
            alloc = [line.strip().split(",") for line in f][1:]
        heads = _headers(d, {r: 1 for r in artifacts(method)})
        at = [heads[r][2]["written_at"] for r in (EMU, CAL, POINT, EMB, UNC, VOI, BV, STAB, PV, DT)]
        ok(f"X42 {method}: every file the Outcomes view reads is there, each with the producer's name in its header and "
           f"written in the order toolkit/analysis/run.py writes them",
           all(heads[r][2]["producer"] == f"toolkit/analysis/{n}.py" for r, n in (
               (POINT, "core"), (UNC, "uncertainty"), (DT, "decision_tests"), (STAB, "stability"), (VOI, "value_of_information"),
               (PV, "variants"), (BV, "burden"), (EMU, "run"), (EMB, "boundary"))) and at == sorted(at)
           and all(os.path.exists(os.path.join(d, t["path"])) for t in tests))
        items = {i["component"]: i for i in spec["options"]["cost_items"]}
        groups = {}
        for c, v in spec["options"]["components"].items():
            groups.setdefault(v.get("group"), []).append(c)
        options = groups[TIER]
        dearest = max(options, key=lambda c: items[c]["value"])
        holds = [p for p in point["plan"].values() if set(p.split("+")) & set(options)]
        share = sum(dearest in p.split("+") for p in holds) / len(holds)
        cost = sum(sum(items[c]["value"] for c in p.split("+")) * (1000 + 150 * i) for i, p in enumerate(point["plan"].values()))
        ok(f"X43 {method}: a plan over the twelve units with two packages that differ in the option of one group; the "
           f"cheaper option in most units; the plan, its mix, its cost from the signed spec's cost items, the "
           f"allocation file and the per-unit rows are one plan",
           len(point["plan"]) == 12 and sorted(point["packages"]) == ["A+B", "A+C"] and sorted(options) == ["B", "C"] and dearest == "C"
           and share < 0.75 * DEAREST_SHARE and len(holds) >= DEAREST_MIN_UNITS
           and all(i["basis"] == "per_person" for i in items.values()) and cost == point["cost_allocated"] == point["solver"]["cost"]
           and [(r[0], r[1]) for r in alloc] == list(point["plan"].items()) == [(u, x["package"]) for u, x in point["per_unit"].items()]
           and abs(sum(float(r[2]) for r in alloc) - point["averted"][OUTCOME]) < 1e-6
           and point["mix"] == {"A": 12, "B": 9, "C": 3, "no_package": 0} and point["packages"] == {"A+B": 9, "A+C": 3}
           and point["budget_allocatable"] == spec["constraints"]["envelope_usd"] and cost <= point["reserve"]["budget_planned"]
           and abs(point["reserve"]["reserve_usd"] + point["reserve"]["budget_planned"] - point["budget_allocatable"]) < 1e-6
           and abs(point["cost_per_outcome_averted"] - cost / point["averted"][OUTCOME]) < 1e-5, str((share, cost)))
        f = unc["fixed_plan"][OUTCOME]
        h = point["averted"][OUTCOME]
        ok(f"X44 {method}: the headline's point value equals its median and lies inside the interval; it is under half "
           f"of the comparator and clear of that threshold; the headline is the signed spec's",
           f["median"] == h and f["ci_low"] < h < f["ci_high"] and h / point["comparator_total"][OUTCOME] < 0.75 * 0.5
           and unc["headline"] == spec["deliverable"]["headline"] == {"name": HEADLINE, "outcome": OUTCOME}
           and unc["draws"] == spec["uncertainty"]["draws"] and unc["interval"]["prior_dominated"] == unc["prior_dominated"]
           == unc["draw_plan"]["prior_dominated"])
        near = [t["test_id"] for t in tests for k, lim in (("share_pct", "threshold_pct"), ("regret_pct", "regret_threshold_pct"),
                                                             ("dollars_share_pct", "dollars_threshold_pct")) if t[k] >= 0.75 * t[lim]]
        named = {t["id"]: t for t in spec["analysis"]["decision_tests"]}
        files = {t["test_id"]: doc(t["path"]) for t in tests}
        ok(f"X45 {method}: every decision test ran with no change, feasible and clear of its three thresholds; each "
           f"has its result file with the spec entry's question and finding; one test varies the input held fixed",
           all(t["status"] == "run" and t["verdict"] == "NO_CHANGE" and t["point_plan_feasible"] is True and t["triggered_by"] == [] for t in tests)
           and not near and set(named) == set(files)
           and all(x["question"] == named[k]["text"] and x["finding"] == named[k].get("finding", f"decision_test:{k}") and x["decision_changes"] is False
                   and x["share_threshold"] == TEST_THRESHOLDS["share"] for k, x in files.items())
           and [x["name"] for x in unc["draw_plan"]["fixed"]] == [HELD] == unc["interval"]["fixed"] and named["DT7"]["parameter"] == HELD, str(near))
        stab, voi, pv, bv, emb = data(STAB), data(VOI), data(PV), data(BV), data(EMB)
        above = [r["input"] for r in voi["inputs"] if r["above_noise_floor"]]
        ok(f"X46 {method}: the two stability verdicts agree; value of information is computed on enough draws, with inputs "
           f"above their noise floor, none of them prior-dominated",
           stab["verdict"] == stab["value_verdict"] == "ROBUST" and stab["metrics"]["total_benefit_loss_pct_worst_fold"] < 0.75 * VALUE_LOSS["value_loss_pct_robust"]
           and stab["value_thresholds_pct"] == VALUE_LOSS and stab["holdout_group"] == GROUPING
           and voi["status"] == "computed" and voi["draws"] == unc["draws"] >= voi["informative_draws"] and above == ["p1", "p2"]
           and not set(above) & set(unc["prior_dominated"]) and all(r["supported"] == r["above_noise_floor"] == (r["evppi"] > r["noise_floor"]) for r in voi["inputs"])
           and voi["expected_value_decide_now"] == h)
        v = pv["variants"][0]
        rows = {r["id"]: r for r in bv["references"]}
        covered = {c for r in emb["rows"] for k in ("chosen", "runner_up") for c in r[k].split("+")}
        ok(f"X47 {method}: the plan variant is within budget in all but 2% of draws against the spec's 10%; every burden "
           f"reference is inside its band and clear of its ends; the boundary check holds every component and agrees "
           f"with the plan",
           v["status"] == "solved" and v["cost"] <= pv["unconstrained"]["cost"] <= point["budget_allocatable"] and v["value_lost"] >= 0
           and v["uncertainty"]["affordability"]["share_over_budget"] < 0.75 * spec["uncertainty"]["affordability"]["max_share_over_budget"]
           and v["uncertainty"]["affordability"]["rule_met"] is True and pv["unconstrained"]["plan"] == point["plan"]
           and [x["id"] for x in spec["analysis"]["plan_variants"]["variants"]] == [v["id"]]
           and all(r["status"] == "within" and 1.25 * r["tolerance"]["ratio_band"][0] < r["ratio"] < 0.75 * r["tolerance"]["ratio_band"][1] for r in rows.values())
           and bv["summary"] == {"within": 4, "outside": 0, "information": 0, "unmatched": 0, "report_only": 2, "n": 4}
           and [k for k, r in rows.items() if r["role"] == "report_only"] == ["ref_G3", "ref_all"]
           and named["DT10"]["burden_scale"]["references"] == ["ref_G3"]
           and covered == set(spec["options"]["components"]) and emb["passes"] is True and emb["status"] == "judged"
           and all(r["chosen"] == point["plan"][r["unit"]] for r in emb["rows"]), str(sorted(covered)))

        second = os.path.join(tmp, "other", method, "second")
        got = {name: build(os.path.join(tmp, "other", method, name), method=method, other=name) for name in OTHERS}
        s2, p2 = doc(SPEC_FILES[1], second), data(POINT, second)
        differ = sorted(f"{a}.{b}" for a in spec for b in (spec[a] if isinstance(spec[a], dict) else [""])
                        if (spec[a][b] if b else spec[a]) != (s2[a][b] if b else s2[a]))
        ok(f"X48 {method}: the second run asks the same question with a signed spec that differs in one key; its plan and "
           f"headline are the clean run's, and its own files agree with its spec",
           differ == ["constraints.envelope_usd"] and s2["question"] == spec["question"] and p2["plan"] == point["plan"]
           and p2["averted"] == point["averted"] and p2["budget_allocatable"] == s2["constraints"]["envelope_usd"]
           and json.loads(open(os.path.join(second, "decision_spec.signoff.json")).read())["sha256"]["decision_spec.yaml"] == got["second"][SPEC_FILES[1]])

        def other(name, rel):
            return doc(rel, os.path.join(tmp, "other", method, name))
        moved = {name: sum(a != b for a, b in zip(other(name, POINT)["data"]["plan"].values(), point["plan"].values())) for name in OTHERS}
        ok(f"X49 {method}: the comparison runs: a package's units shift by a quarter of the units with and without a spec "
           f"key that differs; a headline 1.6 times larger; another question; a signed copy that no longer has the recorded hash",
           moved == {"twin": 0, "second": 0, "shifted": 3, "shifted_same_spec": 3, "headline": 0, "other_question": 0, "unverified": 0}
           and other("shifted", SPEC_FILES[1]) == s2 and other("shifted_same_spec", SPEC_FILES[1]) == spec
           and abs(other("headline", POINT)["data"]["averted"][OUTCOME] / h - 1.6) < 1e-6
           and other("headline", SPEC_FILES[1])["comparator"]["text"] != spec["comparator"]["text"]
           and other("other_question", SPEC_FILES[1])["question"] != spec["question"]
           and got["unverified"][SPEC_FILES[1]] != got["unverified"]["decision_spec.yaml"]
           == other("unverified", "decision_spec.signoff.json")["sha256"]["decision_spec.yaml"], str(moved))

        def built(name, rel, method=method):
            with open(os.path.join(tmp, "defect", method, name, rel), encoding="utf-8") as f:
                return f.read()
        broken = 0
        for name, rel in (("tests_unparsable", DT),):
            try:
                yaml.safe_load(built(name, rel))
            except yaml.YAMLError:
                broken += 1
        fr = yaml.safe_load(built("interval_reversed", UNC))["data"]["fixed_plan"][OUTCOME]
        few = yaml.safe_load(built("C8_dearest_few_units", POINT))["data"]["plan"]
        ok(f"X50 {method}: the third fixtures: a tests file that is not YAML, an interval whose ends are exchanged, a plan "
           f"with a package the spec does not define, a variant with no cost, too few draws with nothing computed, a "
           f"kind the analysis does not write; and the dearest option in fewer units than the producer's minimum",
           broken == 1 and fr["ci_low"] > fr["ci_high"]
           and "A+D" in yaml.safe_load(built("plan_unknown_package", POINT))["data"]["plan"].values() and "A+D,".join(["U01,", ""]) in built("plan_unknown_package", "allocation.csv")
           and yaml.safe_load(built("variant_no_cost", PV))["data"]["variants"][0]["cost"] is None
           and yaml.safe_load(built("voi_too_few_draws", VOI))["data"] == {"status": "too_few_draws", "draws": 20, "min_draws": VOI_MIN_DRAWS, "evpi": None,
                                                                         "inputs": [], "informative_draws": VOI_INFORMATIVE_DRAWS}
           and yaml.safe_load(built("unknown_deliverable_kind", SPEC_FILES[1]))["deliverable"]["kind"] not in KINDS
           and sum("C" in x for x in few.values()) == 4 < DEAREST_MIN_UNITS and sum(x != "A" for x in few.values()) == 4)

    d = os.path.join(tmp, "clean", "estimates_kind")
    clean = build(d, method="posterior", figures="estimates", kind="estimates")
    with open(os.path.join(d, EST), encoding="utf-8") as f:
        est = yaml.safe_load(f)["data"]
    with open(os.path.join(d, SPEC_FILES[1]), encoding="utf-8") as f:
        spec = yaml.safe_load(f)
    ok("X51 the estimates kind: no plan, tests file or allocation; analysis/estimates.yaml holds the outputs, the decision "
       "rule's verdict in the producer's words and the decision tests, each run with no change of the verdict",
       spec["deliverable"]["kind"] == "estimates" and not {POINT, DT, "allocation.csv", STAB, VOI, PV, BV, EMU, EMB} & set(clean)
       and "options" not in spec and est["decision"]["verdict"] in EST_VERDICTS and est["decision"]["output"] == spec["deliverable"]["decision"]["output"]
       and all(t["status"] == "run" and t["decision_changes"] is False and all(e["verdict"] == est["decision"]["verdict"] for e in t["ends"])
               for t in est["decision_tests"]) and len(est["decision_tests"]) == 2 and sorted(est["outputs"]) == ["q1", "q2"]
       and clean == build(os.path.join(tmp, "again", "estimates_kind"), method="posterior", figures="estimates", kind="estimates"))
    for name in DEFECTS:
        if "estimates" not in DEFECTS[name].get("kinds", KINDS[:1]):
            continue
        got = build(os.path.join(tmp, "defect", "estimates_kind", name), defect=name, method="posterior", figures="estimates", kind="estimates")
        diff = {r for r in set(got) | set(clean) if got.get(r) != clean.get(r)}
        expect = _follows(d, clean, touched(name, "posterior", "estimates"))
        ok(f"X8 estimates kind: {name} changes the files its edit names, the files that hash them, and no others",
           diff == expect, f"unexpected {sorted(diff - expect)}, missing {sorted(expect - diff)}")
    checks = {DEFECTS[n]["check"] for n in OUTCOMES_DEFECTS}
    third = {DEFECTS[n]["check"] for n in OUTCOMES_DEFECTS if DEFECTS[n]["status"] in ("problem", "not_available")}
    ok("X52 the Outcomes view's edits: every check has one that fires; the comparison edits name a comparison run and "
       "change no file; an edit for the estimates kind names it",
       checks == set(OUTCOME_CHECKS) and {"tests_that_move", "point_vs_draws", "burden_refs", "voi_support", "variant_budget", "run_shift", "dearest_wins"} <= third
       and all((DEFECTS[n].get("against") in OTHERS) == (DEFECTS[n]["check"] == "run_shift") and (DEFECTS[n]["files"] == []) == ("against" in DEFECTS[n])
               for n in OUTCOMES_DEFECTS) and len(OUTCOMES_DEFECTS) == 37, str((sorted(set(OUTCOME_CHECKS) - checks), len(OUTCOMES_DEFECTS))))


def _spec_scenarios_words():
    """Whether each word and sentence the Spec and Scenarios fixtures copy is still in the producer that writes it,
    read from the source without importing it."""
    import ast

    def source(*parts):
        with open(os.path.join(REPO, *parts), encoding="utf-8") as f:
            return f.read()

    def constant(text, name):
        return next(ast.literal_eval(n.value) for n in ast.parse(text).body if isinstance(n, ast.Assign)
                    and any(getattr(t, "id", None) == name for t in n.targets))
    sign, crit, geo = source("toolkit", "spec", "signoff.py"), source("toolkit", "spec", "spec_critique.py"), source("toolkit", "spec", "geometry.py")
    par, dos, bur = source("toolkit", "spec", "engine_parameters.py"), source("toolkit", "spec", "evidence_dossier.py"), source("toolkit", "analysis", "burden.py")
    hooks, traj, figs = source("toolkit", "engines", "hooks.py"), source("toolkit", "analysis", "trajectories.py"), source("toolkit", "analysis", "figures.py")
    always = [constant(crit, "SECTION"), *[h for h in SHEET_ALWAYS[1:4] if f'"{h}"' in sign], *[SHEET_ALWAYS[4]] * (f'out = ["{SHEET_ALWAYS[4]}"]' in geo),
              *[SHEET_ALWAYS[5]] * (f'out = ["{SHEET_ALWAYS[5]}"]' in par), constant(dos, "SECTION")]
    rows = _critique(_model("history_matching")) + [("", 0, "", "decision_date {!r} is not a year or an ISO date.", ""),
                                                    ("", 0, "", "followed the effect for less than", "")]
    quoted = [x for x in ("the findings the gate would raise after compute; signing with one open accepts it.",
                          "| Severity | Principle | Check | Finding | What resolves it |", "Anchor the burden (validation.burden anchor rows) or declare this check",
                          "of allocated components", "Confirm it is the plan as currently funded in every unit",
                          "actions delivered before the decision would be optimised.", "is not a year or an ISO date.",
                          "followed the effect for less than") if x not in crit]
    return {"always": tuple(always), "some": all(f'"{h}"' in sign + bur for h in SHEET_SOME), "kinds": constant(hooks, "KINDS"),
            "contract": constant(hooks, "HOOKS_CONTRACT"), "footer": constant(sign, "SHEET_SOURCES"), "differs": constant(dos, "DIFFERS_HEADING"),
            "oa": constant(source("toolkit", "analysis", "outcome_adjust.py"), "REQUIRED"), "quoted": quoted, "n_rows": len(rows),
            "more": SELECTION in traj and '"schema": "analysis.trajectories/1"' in traj and '"pass" if worst <= 1e-9 else "flag"' in figs
            and '"status": "info"' in figs and 'o.get("follow_up_years")' in crit and "fetched | matched | unverified" in source("toolkit", "engines", "contract.py")
            and 'ARCHIVE_DIR, ARCHIVE_PREFIX = "evidence_planner", "superseded_signoff_"' in source("tools", "run_console", "dossier.py")
            and '"verified")' in par and "No record ({ARTIFACT} is missing): the check has not run." in geo and 'ARTIFACT = "data/input_checks.json"' in geo}


def _spec_scenarios_cases(tmp, ok):
    """The files of the Spec and evidence view (§5.3) and the Scenarios view (§5.6): shaped as the producers write them,
    agreeing with each other on the clean run and clear of every rule of the two views."""
    import datetime
    w = _spec_scenarios_words()
    ok("X53 the sheet's headings and sentences, the event kinds, the trajectory record's words and the archive's names are the producers' own",
       w["always"] == SHEET_ALWAYS and w["some"] and tuple(w["kinds"]) == EVENT_KINDS and w["contract"] == HOOKS_CONTRACT
       and w["footer"] == SHEET_SOURCES and w["differs"] == DIFFERS and set(w["oa"]) == set(OA) and not w["quoted"] and w["more"], str(w))
    for method in METHODS:
        d = os.path.join(tmp, "clean", method)

        def text(rel, d=d):
            with open(os.path.join(d, rel), encoding="utf-8") as f:
                return f.read()

        def doc(rel, d=d):
            return json.loads(text(rel, d)) if rel.endswith(".json") else yaml.safe_load(text(rel, d))

        def sha(rel, d=d):
            return hashlib.sha256(text(rel, d).encode()).hexdigest()
        spec, record, dossier = doc(SPEC_FILES[1]), doc(SIGNOFF), doc(DOSSIER)["inputs"]
        obj = spec["objective"]
        day, signed = datetime.date.fromisoformat(spec["decision_date"]), datetime.date.fromisoformat(record["signed_at"][:10])
        frac = lambda x: x.year + (x - datetime.date(x.year, 1, 1)).days / (365 + 1 / 4)      # noqa: E731  (spec_critique.py _year_frac)
        ok(f"X54 {method}: the signed spec names who decides and dates the decision so that the producer's rule raises "
           f"nothing: the delivery period, inside the outcome window, starts neither before the decision date nor before "
           f"the day of the signature, and the date is not past on that day",
           spec["decision_maker"] and obj["delivery"][0] >= frac(day) and obj["delivery"][0] >= frac(signed) and (signed - day).days <= 90
           and obj["horizon"][0] <= obj["delivery"][0] < obj["delivery"][1] <= obj["horizon"][1] and spec["evidence"] == {"dossier": DOSSIER})
        obs = [o for e in dossier.values() for o in e.get("observations", [])]
        cited = [o for o in obs if o["source"] == OA["source"]]
        span = obj["horizon"][1] - obj["horizon"][0]
        ok(f"X55 {method}: no outcome adjustment (§5.3: one is for the reader to judge, so each is a variant), and one "
           f"dossier observation with the source a variant's adjustment gives, word for word; every observation records a "
           f"follow-up clear of the outcome window and a checked mark; every no-data entry names a decision test; the one "
           f"spec-set value has a source the spec itself marks matched",
           "outcome_adjustments" not in spec["analysis"] and len(cited) == 1 and len(obs) == 3
           and all(o[FOLLOW_UP] >= span / 0.75 and o[CHECKED] for o in obs)
           and all(e.get("decision_test") for e in dossier.values() if "no_data" in e)
           and all("no_data" in e or e.get("observations") for e in dossier.values())
           and [(e["verified"], bool(e["source"])) for e in spec["engine_parameters"].values()] == [("matched", True)]
           and spec["engine_parameters"]["p2"]["verified"] in MARKS)
        sheet = text(SHEET).splitlines()
        heads = [x for x in sheet if x.startswith("## ")]
        foot = json.loads(sheet[-1][len(SHEET_SOURCES):-4])
        severities = [x.split("|")[1].strip() for x in sheet if x.startswith("| ") and x.split("|")[1].strip() in ("HIGH", "MEDIUM", "DECLARED", "INFO")]
        ok(f"X56 {method}: the sheet holds every section the sign-off code writes for any spec, in its order, and the two "
           f"it writes for this spec (none for outcome adjustments, which it has not); its critique has no HIGH row and its count line says so; its last line holds the "
           f"hashes of the spec, the dossier and the reference file; the signature has a note",
           [h for h in heads if h in SHEET_ALWAYS] == list(SHEET_ALWAYS) and set(heads) == set(SHEET_ALWAYS + SHEET_SOME[1:])
           and severities == ["MEDIUM", "MEDIUM", "INFO", "INFO"] and "0 HIGH, 2 MEDIUM, 0 declared." in text(SHEET)
           and foot == {"spec_sha256": sha(SPEC_FILES[1]), "burden_references_sha256": sha(BURDEN_CSV), "evidence_dossier_sha256": sha(DOSSIER),
                        "derivations_sha256": "none", "geometry_evidence_sha256": "none"}
           and record["note"].strip() and record["signed_by"] == "owner", str((heads, severities)))
        chain = []
        for day_ in EARLIER:
            folder = os.path.join(d, ARCHIVE + day_)
            rec = doc(f"{ARCHIVE}{day_}/{SIGNOFF}")
            copies = [os.path.join(at, n) for at, _, names in os.walk(folder) for n in names if n.startswith("decision_spec") and n.endswith(".yaml")]
            with open(copies[0], "rb") as f:
                raw = f.read()
            chain.append((rec, hashlib.sha256(raw).hexdigest(), yaml.safe_load(raw), len(copies)))
        specs = [c[2] for c in chain] + [spec]
        changed = [sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k)) for a, b in zip(specs, specs[1:])]
        ok(f"X57 {method}: two archived sign-offs, each a record with a note and the one spec copy whose hash it holds, "
           f"older than the current one; each sign-off changed one section of the spec; no archive folder lacks its record",
           all(n == 1 and rec["sha256"]["decision_spec.yaml"] == h and rec["note"] for rec, h, _, n in chain)
           and [c[0]["signed_at"] for c in chain] == sorted(c[0]["signed_at"] for c in chain) and chain[-1][0]["signed_at"] < record["signed_at"]
           and changed == [["decision_maker"], ["uncertainty"]]
           and sorted(os.listdir(os.path.join(d, "evidence_planner"))) == ["superseded_signoff_" + x for x in EARLIER], str(changed))
        fd, point, tr = doc(FD)["data"], doc(POINT), doc(TRAJ)
        sch, plan = fd["schedule"], point["data"]["plan"]
        events = [(g, e) for g, r in sch["regions"].items() for e in r["events"]]
        (d0, d1), w1 = sch["delivery"], sch["window"][1]
        per_group = {g: {lab: sum(c in plan[u].split("+") for u in UNITS if _group(u) == g) for c, lab in (("B", "half round"), ("C", "full round"))}
                     for g in GROUPS}
        with open(os.path.join(d, TIMELINE), encoding="utf-8") as f:
            table = [line.strip().split(",") for line in f][1:]
        ok(f"X58 {method}: the stored schedule has the producer's keys and no arm; every kind is one the hook writes; each "
           f"decision event lies inside the delivery period and holds the units whose package has its option; no event "
           f"starts at or after the end of the outcome window; history lies before delivery and routine outside it; the "
           f"timeline's table has one row per event; the batch and point files exist",
           sch["status"] == "ok" and [sch["delivery"], sch["window"]] == [obj["delivery"], obj["horizon"]] and sch["start"] == spec["horizon"]["start"]
           and all(list(e) == ["component", "label", "kind", "t0", "t1", "units"] and e["kind"] in EVENT_KINDS and e["t0"] < w1 for _, e in events)
           and {e["kind"] for _, e in events} == set(EVENT_KINDS)
           and all(d0 <= e["t0"] < d1 for _, e in events if e["kind"] == "decision")
           and all(e["t0"] < d0 for _, e in events if e["kind"] == "history") and all(not d0 <= e["t0"] < d1 for _, e in events if e["kind"] == "routine")
           and all(e["units"] == per_group[g][e["label"]] for g, e in events if e["component"] == "B")
           and len(table) == len(events) and [r[3] for r in table] == [e["kind"] for _, e in events]
           and os.path.exists(os.path.join(d, BATCH)) and doc(BATCH)["producer"] == "toolkit/analysis/batch.py"
           and doc(FD)["producer"] == "toolkit/analysis/figure_data.py", str(len(events)))
        data, entry = tr["data"], next(e for e in doc(FIGS)["data"]["figures"] if e.get("figure") == "trajectories")
        total = data["series"]["total"]
        ok(f"X59 {method}: the trajectory record has its producer's header (no spec hash) and words; its inputs have the "
           f"hashes of the files; its engine is the analysis's identity in the point file and its analysis engine the "
           f"header's; nine sampled units, three per group; the arms are equal before delivery and the plan lower after",
           list(tr) == ["schema", "produced_by", "producer", "inputs", "written_at", "data"] and tr["producer"] == "toolkit/analysis/trajectories.py"
           and all(sha(i) == h for i, h in tr["inputs"].items()) and len(tr["inputs"]) == 7
           and data["status"] == "ok" and data["engine"] == point["data"]["engine_identity"] and data["analysis_engine"] == point["engine"]
           and [len(v) for v in data["sample"].values()] == [3, 3, 3] and data["seeds"] == 3 and list(data["series"]["regions"]) == list(GROUPS)
           and data["delivery"] == sch["delivery"] and data["window"] == sch["window"] and total["t"][0] == sch["start"]
           and all((c == p) == (x < d0) for x, c, p in zip(total["t"], total["arms"]["comparator"]["m1"], total["arms"]["plan"]["m1"])))
        ok(f"X60 {method}: the dynamics checks travel with the trajectories figure in analysis/figures.yaml, in the "
           f"producer's words; they are what the producer's rule gives for the record; none is flagged",
           entry["checks"] == _dynamics(data) and [(c["check"], c["status"]) for c in entry["checks"]]
           == [("plan_equals_comparator_before_delivery", "pass"), ("end_of_delivery_step", "info")]
           and all(c["status"] in DYNAMICS_STATUSES for c in entry["checks"]) and entry["checks"][0]["value"] == 0.0
           and TRAJ in entry["sources"] and TRAJ in doc(FIGS)["inputs"], str(entry["checks"]))

        def built(name, rel, method=method):
            with open(os.path.join(tmp, "defect", method, name, rel), encoding="utf-8") as f:
                return f.read()

        def there(name, rel, method=method):
            return os.path.exists(os.path.join(tmp, "defect", method, name, rel))
        try:
            yaml.safe_load(built("dossier_unparsable", DOSSIER))
            parses = True
        except yaml.YAMLError:
            parses = False
        short = yaml.safe_load(built("C1_follow_up_short", DOSSIER))["inputs"]["parameter:b_add"]["observations"][0]
        prose = yaml.safe_load(built("C1_follow_up_prose_only", DOSSIER))["inputs"]["parameter:b_add"]["observations"][0]
        marks = [[CHECKED in o for e in yaml.safe_load(built(v, DOSSIER))["inputs"].values() for o in e.get("observations", [])]
                 for v in ("C15_observation_unchecked", "C15_no_checked_marks")]
        late = yaml.safe_load(built("decision_date_after_delivery_start", SPEC_FILES[1]))
        many = [yaml.safe_load(built("signoff_note_empty_many_sections", rel)) for rel in (SECOND + SPEC_FILES[1], SPEC_FILES[1])]
        adjusted = {v: yaml.safe_load(built(v, SPEC_FILES[1]))["analysis"].get("outcome_adjustments")
                    for v in ("C1_outcome_adjustment", "C1_follow_up_short", "C1_follow_up_prose_only", "C1_source_not_in_dossier")}
        plain_oa = yaml.safe_load(built("C1_outcome_adjustment", DOSSIER))["inputs"]["parameter:b_add"]["observations"][0]
        ok(f"X61 {method}: the Spec edits: an outcome adjustment with the producer's keys and its section on the sheet, whose "
           f"source one dossier observation has word for word (in three of the four); a follow-up shorter than the window "
           f"with the producer's finding on the sheet; "
           f"one in prose alone; a spec-set value with no mark; one or every observation unmarked; a dossier that is not YAML; a signed copy that no "
           f"longer has the recorded hash; a decision date after the start of delivery with the producer's HIGH row; an "
           f"empty note on a signature that changed six sections; a sheet with no critique section; no sheet; an "
           f"archive folder with a sheet alone",
           all(len(a) == 1 and set(a[0]) == set(OA) for a in adjusted.values())
           and [a[0]["source"] == OA["source"] for a in adjusted.values()] == [True, True, True, False]
           and all(SHEET_SOME[0] in built(v, SHEET) for v in adjusted) and plain_oa[FOLLOW_UP] >= span / 0.75
           and "verified" not in yaml.safe_load(built("value_no_mark", SPEC_FILES[1]))["engine_parameters"]["p2"]
           and short[FOLLOW_UP] < span and "| MEDIUM | 3 | follow_up_window |" in built("C1_follow_up_short", SHEET)
           and FOLLOW_UP not in prose and "five years" in prose["where"] and marks == [[False, True, True], [False] * 3] and not parses
           and hashlib.sha256(built("spec_unverified", SPEC_FILES[1]).encode()).hexdigest() != json.loads(built("spec_unverified", SIGNOFF))["sha256"]["decision_spec.yaml"]
           and built("spec_unverified", SIGNOFF) == text(SIGNOFF)
           and frac(datetime.date.fromisoformat(late["decision_date"])) > late["objective"]["delivery"][0]
           and "| HIGH | 2 | decision_date |" in built("decision_date_after_delivery_start", SHEET) and "1 HIGH, 2 MEDIUM" in built("decision_date_after_delivery_start", SHEET)
           and json.loads(built("signoff_note_empty_many_sections", SIGNOFF))["note"] == ""
           and len([k for k in set(many[0]) | set(many[1]) if many[0].get(k) != many[1].get(k)]) == 6
           and SHEET_ALWAYS[0] not in built("sheet_section_missing", SHEET) and all(h in built("sheet_section_missing", SHEET) for h in SHEET_ALWAYS[1:])
           and not there("sheet_missing", SHEET)
           and os.listdir(os.path.join(tmp, "defect", method, "archive_no_record", ARCHIVE + EARLIER[1] + "b")) == [SHEET]
           and yaml.safe_load(built("value_unknown_mark", SPEC_FILES[1]))["engine_parameters"]["p2"]["verified"] not in MARKS)

        def schedule(name):
            s_ = yaml.safe_load(built(name, FD))["data"]["schedule"]
            return [e for r in s_["regions"].values() for e in r["events"]]
        flagged = next(e for e in yaml.safe_load(built("dynamics_flag", FIGS))["data"]["figures"] if e.get("figure") == "trajectories")["checks"]
        none = yaml.safe_load(built("C5_no_trajectories", FIGS))["data"]
        other = yaml.safe_load(built("trajectories_other_engine", TRAJ))["data"]["engine"]
        ok(f"X62 {method}: the Scenarios edits: no trajectory record beside the batch and point files, with the figure "
           f"listed as not produced in the producer's words; a record for another engine code; a flagged check that the "
           f"record's own series give; a status word the producer does not write; a decision event after delivery; an "
           f"event after the window; a kind the hook does not write; and a run stopped at the calibration has neither a "
           f"batch nor a point file",
           not there("C5_no_trajectories", TRAJ) and there("C5_no_trajectories", BATCH) and there("C5_no_trajectories", POINT)
           and {"figure": "trajectories", "reason": NO_TRAJECTORIES, "tier": "simulation"} in none["not_produced"]
           and other["code_sha256"] != point["data"]["engine_identity"]["code_sha256"] and other["name"] == ENGINE["name"]
           and flagged[0]["status"] == "flag" and flagged[0]["value"] > 1e-9
           and flagged == _dynamics(yaml.safe_load(built("dynamics_flag", TRAJ))["data"])
           and next(e for e in yaml.safe_load(built("dynamics_unknown_status", FIGS))["data"]["figures"] if e.get("figure") == "trajectories")["checks"][0]["status"] not in DYNAMICS_STATUSES
           and [e["t0"] for e in schedule("schedule_decision_outside") if e["kind"] == "decision" and not d0 <= e["t0"] < d1] == [d1 + 0.25]
           and [e["t0"] for e in schedule("schedule_event_after_window") if e["t0"] >= w1] == [w1 + 0.5]
           and [e["kind"] for e in schedule("schedule_unknown_kind") if e["kind"] not in EVENT_KINDS] == ["campaign"]
           and not {BATCH, POINT, TRAJ, FD} & set(os.listdir(os.path.join(tmp, "upto", method, "calibration", "analysis")) and
                                                 {f"analysis/{n}" for n in os.listdir(os.path.join(tmp, "upto", method, "calibration", "analysis"))}))
    kind = os.path.join(tmp, "clean", "estimates_kind")
    with open(os.path.join(kind, SHEET), encoding="utf-8") as f:
        sheet = f.read()
    ok("X63 the estimates kind has no schedule, batch or trajectory record and its spec no outcome adjustment; its sheet "
       "holds every section written for any spec and no HIGH row",
       not {FD, BATCH, TRAJ} & {f"analysis/{n}" for n in os.listdir(os.path.join(kind, "analysis"))}
       and all(h in sheet for h in SHEET_ALWAYS) and "0 HIGH, 0 MEDIUM, 0 declared." in sheet and "| HIGH |" not in sheet)
    third = {v: {c for n in names for c, st in {DEFECTS[n]["check"]: DEFECTS[n]["status"], **DEFECTS[n].get("also", {})}.items()
                 if st in ("problem", "not_available")} for v, names in (("spec", SPEC_DEFECTS), ("scenarios", SCENARIOS_DEFECTS))}
    fires = {v: {DEFECTS[n]["check"] for n in names if DEFECTS[n]["status"] in ("look", "problem")} for v, names in (("spec", SPEC_DEFECTS), ("scenarios", SCENARIOS_DEFECTS))}
    ok("X64 the edits of the Spec view (20) and of the Scenarios view (8): every check has one that fires and one whose "
       "inputs contradict each other or miss a piece, on which it is neither ok nor look",
       fires["spec"] == set(SPEC_CHECKS) == third["spec"] and fires["scenarios"] == set(SCENARIOS_CHECKS) == third["scenarios"]
       and len(SPEC_DEFECTS) == 20 and len(SCENARIOS_DEFECTS) == 8, str((fires, third, len(SPEC_DEFECTS), len(SCENARIOS_DEFECTS))))


def _self_test():
    fails = []

    def ok(name, cond, detail=""):
        print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f": {detail}" if not cond and detail else ""))
        if not cond:
            fails.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        clean = {}
        for method in METHODS:
            d = os.path.join(tmp, "clean", method)
            clean[method] = build(d, method=method)
            cal = JC if method == "history_matching" else CAL
            with open(os.path.join(d, cal), encoding="utf-8") as f:
                doc = yaml.safe_load(f)
            want = {"run_state.json", "metadata.json", "decision_spec.yaml", "decision_spec.signoff.json",
                    "decision_brief.signed/decision_spec.yaml", "data/derived/units.csv", "analysis/manifest.json",
                    UNC, DT, cal, "figures/calibration_fit.csv"}
            ok(f"X1 {method}: the clean run builds with every file the Steps and Fit views read",
               want <= set(clean[method]) and doc["data"]["method"] == method, str(sorted(want - set(clean[method]))))
            again = build(os.path.join(tmp, "again", method), method=method)
            ok(f"X2 {method}: two builds of the clean run are identical", again == clean[method])

            heads = _headers(d, clean[method])
            spec_sha = clean[method]["decision_spec.yaml"]
            stale = [(r, i) for r, (_, inputs, _) in heads.items() for i, s in inputs.items()
                     if not i.startswith("..") and clean[method].get(i) != s]
            ok(f"X3 {method}: every input hash an analysis header records matches the file written",
               not stale and all(len(inputs) > 1 for _, inputs, _ in heads.values()), str(stale))
            with open(os.path.join(d, "analysis/manifest.json")) as f:
                man = json.load(f)
            with open(os.path.join(d, "decision_spec.signoff.json")) as f:
                so = json.load(f)
            listed = {**man["artifacts"], **man["deliverables"]}
            ok(f"X4 {method}: headers, sign-off and manifest all record the spec and files on disk; the trajectory record "
               f"alone has no spec hash in its header and is outside the manifest, as its producer writes it",
               all(s == spec_sha for r, (s, _, _) in heads.items() if r != TRAJ) and heads[TRAJ][0] is None
               and so["sha256"] == {"decision_spec.yaml": spec_sha, DOSSIER: clean[method][DOSSIER]}
               and clean[method]["decision_brief.signed/decision_spec.yaml"] == spec_sha
               and set(man["artifacts"]) == set(heads) - {TRAJ} and all(clean[method][r] == v["sha256"] for r, v in listed.items()))
            with open(os.path.join(d, "run_state.json")) as f:
                state = json.load(f)
            moves = [(h["from"], h["to"]) for h in state["history"]]
            ok(f"X5 {method}: the run finished, with no repeated transition and no failed task",
               state["state"] == "FINAL" and state["status"] == "completed" and len(set(moves)) == len(moves)
               and all(j["ok"] and j["usd"] < 0.9 * j["allowance_usd"] for j in state["journal"].values()))
            with open(os.path.join(d, "data/derived/units.csv")) as f:
                rows = [line.strip().split(",") for line in f]
            ok(f"X6 {method}: twelve units in three groups under the grouping column",
               rows[0][1] == GROUPING and len(rows) == 13 and sorted({r[1] for r in rows[1:]}) == list(GROUPS))

            for stage in STAGES:
                got = build(os.path.join(tmp, "upto", method, stage), upto=stage, method=method)
                later = {r for r in clean[method] if STAGES.index(stage_of(r)) > STAGES.index(stage)}
                ok(f"X7 {method}: upto {stage} leaves out the {len(later)} file(s) later stages write",
                   set(got) == set(clean[method]) - later, str(sorted(set(got) ^ (set(clean[method]) - later))))

            for name, spec in DEFECTS.items():
                if method not in spec["methods"] or "package_per_unit" not in spec.get("kinds", KINDS[:1]):
                    continue
                got = build(os.path.join(tmp, "defect", method, name), defect=name, method=method)
                diff = {r for r in set(got) | set(clean[method]) if got.get(r) != clean[method].get(r)}
                expect = (_follows(d, clean[method], touched(name, method)) if spec.get("follows", True)
                          else set(touched(name, method)))
                # a test's own result file follows its row in the tests file, or its entry in the signed spec
                # and the timeline figure's table follows the stored schedule, which no file hashes
                own = {r for r in diff if r.startswith(TESTS_DIR)}
                drawn = {TIMELINE} & diff if FD in expect else set()
                expect, diff = expect - {TIMELINE}, diff - drawn
                ok(f"X8 {method}: {name} changes the files its edit names, the files that hash them, and no others",
                   diff - own == expect and (not own or DT in expect or SPEC_FILES[1] in expect),
                   f"unexpected {sorted(diff - own - expect)}, missing {sorted(expect - diff)}")

        jc_doc = _headers(os.path.join(tmp, "clean", "history_matching"), {JC: 1})[JC][2]["data"]
        zs = [abs(p["z"]) for p in jc_doc["survey_changes"]["pairs"]]
        assert len(zs) == len(GROUPS)
        gaps = [abs(jc_doc["verification"]["targets"][t]["implausibility"] - v["implausibility_at_best"])
                for t, v in jc_doc["targets"].items()]
        at_bound = sum(bool(u["region_touches_bound"]) for u in jc_doc["units"].values())
        ok("X9 the clean history-matching run sits clear of every threshold and its near-miss band",
           max(zs) < 0.75 * Z_MAX and max(gaps) < 0.75 and at_bound / len(UNITS) < 0.75 * 0.25
           and jc_doc["status"] == "matched" and not jc_doc["blocks"]
           and all(v["runs"] > 1 for v in jc_doc["verification"]["units"].values())
           and all(PRIOR[p][0] < lo and hi < PRIOR[p][1] for p, (lo, hi) in jc_doc["region"]["ranges"].items()))
        ok("X10 every defect entry names its case, check, status, methods and one-sentence edit",
           all(s["check"] and s["status"] in ("ok", "look", "problem", "not_available") and s["methods"] and s["edit"].endswith(".")
               and s["edit"].count(". ") == 0 and "case" in s for s in DEFECTS.values()),
           str([n for n, s in DEFECTS.items() if s["edit"].count(". ")]))
        for name in [None] + [n for n, spec in DEFECTS.items() if "history_matching" in spec["methods"]]:
            d = os.path.join(tmp, "defect", "history_matching", name) if name else os.path.join(tmp, "clean", "history_matching")
            ok(f"X13 {name or 'the clean run'}: each change between rounds can be recomputed from the run's own target rows",
               not _underived(d), str(_underived(d)[:3]))
        for name in ("C13_blocked_undeclared", "C13_blocked_declared", "C13_blocked_test_not_run"):
            jc = _headers(os.path.join(tmp, "defect", "history_matching", name), {JC: 1})[JC][2]["data"]
            share = {k: b["draws"] / b["proposed"] for k, b in jc["bias"].items()}
            comp = next(c for c in jc["bias_components"] if OFFSET in c["blocks"])
            ok(f"X14 {name}: the block on offset {OFFSET} shows in that offset's own row (few draws accepted), not only in the block list",
               share[OFFSET] < 0.01 and all(v == 1.0 for k, v in share.items() if k != OFFSET)
               and comp["no_accepted"] == jc["blocks"][0]["detail"]["failed"] and comp["acceptance_in_region"] < 1.0,
               str((share, comp)))
        _register_cases(tmp, ok)
        _model_cases(tmp, ok)
        est = build(os.path.join(tmp, "clean", "estimates"), method="posterior", figures="estimates")
        for name in FIGURES_DEFECTS:
            if "estimates" not in figure_sets(name):
                continue
            got = build(os.path.join(tmp, "defect", "estimates", name), defect=name, method="posterior", figures="estimates")
            diff = {r for r in set(got) | set(est) if got.get(r) != est.get(r)}
            first = touched(name, "posterior", "estimates")
            expect = _follows(os.path.join(tmp, "clean", "estimates"), est, first) if DEFECTS[name].get("follows", True) else set(first)
            ok(f"X8 estimates figure set: {name} changes the files its edit names, the files that hash them, and no others",
               diff == expect, f"unexpected {sorted(diff - expect)}, missing {sorted(expect - diff)}")
        _figures_cases(tmp, ok)
        _outcomes_cases(tmp, ok)
        _spec_scenarios_cases(tmp, ok)
        for bad in ({"method": "unknown"}, {"defect": "unknown"}, {"upto": "unknown"},
                    {"defect": "C2_change_flagged", "method": "posterior"}, {"figures": "unknown"}, {"figures": "estimates"},
                    {"figures": "estimates", "method": "posterior", "defect": "figure_not_produced"},
                    {"kind": "unknown"}, {"kind": "estimates"}, {"other": "unknown"}, {"defect": "estimates_test_changes"},
                    {"kind": "estimates", "method": "posterior", "figures": "estimates", "defect": "C8_dearest_wins"},
                    {"kind": "estimates", "method": "posterior", "figures": "estimates", "other": "second"}):
            try:
                build(os.path.join(tmp, "bad"), **bad)
                refused = False
            except ValueError:
                refused = True
            ok(f"X11 build refuses {bad}", refused)

        words, hits = domain_words(), []
        for root, _, names in os.walk(tmp):
            for n in names:
                with open(os.path.join(root, n), "rb") as f:        # an image is searched as the bytes it is
                    text = (os.path.relpath(os.path.join(root, n), tmp) + "\n" + f.read().decode("latin-1")).lower()
                hits += [(n, w) for w in words if w in text]
        ok(f"X12 no built fixture file holds a domain word ({len(words)} words)", len(words) > 50 and not hits,
           str(sorted(set(hits))[:8]))
    print("OK: all self-test cases passed." if not fails else f"FAILED: {fails}")
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", nargs="?")
    ap.add_argument("--method", default="history_matching", choices=METHODS)
    ap.add_argument("--figures", default="standard", choices=FIGURE_SETS)
    ap.add_argument("--kind", default="package_per_unit", choices=KINDS)
    ap.add_argument("--other", choices=OTHERS, help="write the same question's comparison run of that name")
    ap.add_argument("--defect", choices=sorted(DEFECTS))
    ap.add_argument("--upto", choices=STAGES)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.list:
        for name, s in DEFECTS.items():
            print(f"{name}\t{s['case'] or '-'}\t{s['check']} -> {s['status']}\t{', '.join(s['methods'])}\t{s['edit']}")
        return 0
    if not a.dir:
        ap.error("a directory to write the run into is needed")
    print(f"{len(build(a.dir, upto=a.upto, defect=a.defect, method=a.method, figures=a.figures, kind=a.kind, other=a.other))} files written to {a.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
