"""E7 attempt 8: the attempt-7 question and settings on the post-Milestone-A harness
(harness-computed trial-effect, figure-rebuild and headline-binding checks; the
cluster stops itself once the run is stored)."""
import os

from run_cluster import RunConfig

CONFIG = RunConfig(
    name="e7r8-quality-gcc",
    question_file=os.path.expanduser("~/modeling-agentic-keep/e7_question.txt"),
    storage_prefix="runs/e7r8",
    out_dir=os.path.expanduser("~/modeling-agentic-keep/e7r8_cloud"),
    flags=("--max-budget-usd", "600", "--signoff", "owner", "--signoff-hours", "24"),
    compute_start=1, compute_max=24,
    compute_env={"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": "24",
                 "MA_COMPUTE_MAX_CORE_HOURS": "1500", "MA_COMPUTE_CALL_CORE_HOURS": "400"},
)
