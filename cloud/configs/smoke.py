"""Whole-path smoke: the attempt-7 question on Haiku agents, 2 rounds, a small pool, lead sign-off.
SMOKE_TREE picks the tree to ship (a branch is allowed; it must be committed)."""
import os

from run_cluster import RunConfig

CONFIG = RunConfig(
    name="run-smoke-gcc",
    question_file=os.path.expanduser("~/modeling-agentic-keep/e7r7_smoke/question.txt"),
    tree=os.path.expanduser(os.environ.get("SMOKE_TREE", "~/modeling-agentic-launch")),
    require_main=False,
    labels=("S1",),
    storage_prefix="_checks/run-smoke",
    flags=("--max-budget-usd", "9", "--signoff", "lead"),
    max_rounds=2,
    compute_start=1, compute_max=4,
    compute_env={"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": "4",
                 "MA_COMPUTE_MAX_CORE_HOURS": "20", "MA_COMPUTE_CALL_CORE_HOURS": "10",
                 "MA_SMOKE_MODEL": "claude-haiku-5-5", "MA_SMOKE_MAX_TURNS": "14"},
)
