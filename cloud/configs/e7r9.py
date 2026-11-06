"""E7 attempt 9: attempt 8's question and settings, unchanged, on the post-Phase-45
harness, so the comparison with attempts 7 and 8 measures the harness changes only
(compute charged for work done, VM reuse and quota overflow, toolkit and pipeline
prompts, mechanism manifest and data-use rules, calibration spec, brief schema 2,
model card, grouped uncertainty findings, verdict binding). Pipeline v1 (the lead session), stated
explicitly: it is the pipeline attempts 7 and 8 ran, and main.py's default is v2 since 2026-11-05."""
import os

from run_cluster import RunConfig

CONFIG = RunConfig(
    name="e7r9-quality-gcc",
    question_file=os.path.expanduser("~/modeling-agentic-keep/e7_question.txt"),
    pipeline="v1",
    storage_prefix="runs/e7r9",
    out_dir=os.path.expanduser("~/modeling-agentic-keep/e7r9_cloud"),
    flags=("--max-budget-usd", "600", "--signoff", "owner", "--signoff-hours", "24"),
    compute_start=1, compute_max=24,
    compute_env={"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": "24",
                 "MA_COMPUTE_MAX_CORE_HOURS": "1500", "MA_COMPUTE_CALL_CORE_HOURS": "400"},
)
