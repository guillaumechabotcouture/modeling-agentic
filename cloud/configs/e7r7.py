"""E7 attempt 7 (2026-10-26), as launched: one agent VM, an autoscaling pool of 8-core compute VMs."""
import os

from run_cluster import RunConfig

CONFIG = RunConfig(
    name="e7r7-quality-gcc",
    question_file=os.path.expanduser("~/modeling-agentic-keep/e7_question.txt"),
    storage_prefix="runs/e7r7",
    out_dir=os.path.expanduser("~/modeling-agentic-keep/e7r7_cloud"),
    flags=("--max-budget-usd", "600", "--signoff", "owner", "--signoff-hours", "24"),
    compute_start=1, compute_max=24,
    compute_env={"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": "24",
                 "MA_COMPUTE_MAX_CORE_HOURS": "1500", "MA_COMPUTE_CALL_CORE_HOURS": "400"},
)
