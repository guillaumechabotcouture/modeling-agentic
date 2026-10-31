"""Shared settings for the eval-suite configs (Phase 50 α): one config per question,
`cloud/configs/eval_<question>.py`. Small, cheap runs: Opus agents, a US$35 cap, at
most 3 rounds, lead sign-off, a small compute pool.

Environment:
    EVAL_TAG   the arm being measured (default "main"), e.g. "s3" for a stage-machine
               branch; it names the cluster, the storage prefix
               (runs/evals/<tag>/<question>) and the collect dir
               ~/modeling-agentic-keep/evals/<tag>/<question>/<label>/runs/<run>/
    EVAL_TREE  a committed checkout to ship instead of ~/modeling-agentic-launch
               (a branch is allowed then; without it the tree must be origin/main)
    EVAL_REPS  replicates per question (default 1; labels R1..Rn on one cluster)

The question text is read from evals/<q>/question.txt in this checkout (never shipped);
the shipped tree must hold the question's inputs (data/evals/<q>/), or launch refuses.
`python3 scripts/eval_suite.py run` prints the launch commands for the whole suite.
"""
import glob
import os

from run_cluster import RunConfig

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BUDGET_USD = 35
MAX_ROUNDS = 3


def eval_config(question: str) -> RunConfig:
    tag = os.environ.get("EVAL_TAG", "main")
    reps = int(os.environ.get("EVAL_REPS", "1"))
    tree = os.environ.get("EVAL_TREE", "")
    inputs = sorted(os.path.relpath(p, REPO) for p in glob.glob(os.path.join(REPO, "data", "evals", question, "*")))
    if not inputs:
        raise ValueError(f"no inputs in data/evals/{question}/: run scripts/eval_suite.py generate")
    return RunConfig(
        name=f"ev-{question.split('_')[0]}-{tag}-gcc",
        question_file=os.path.join(REPO, "evals", question, "question.txt"),
        tree=os.path.expanduser(tree or "~/modeling-agentic-launch"),
        require_main=not tree,
        labels=tuple(f"R{i + 1}" for i in range(reps)),
        storage_prefix=f"runs/evals/{tag}/{question}",
        out_dir=os.path.expanduser(f"~/modeling-agentic-keep/evals/{tag}/{question}"),
        flags=("--max-budget-usd", str(BUDGET_USD), "--signoff", "lead"),
        max_rounds=MAX_ROUNDS,
        compute_start=1, compute_max=4,
        compute_env={"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": "4",
                     "MA_COMPUTE_MAX_CORE_HOURS": "40", "MA_COMPUTE_CALL_CORE_HOURS": "16"},
        required_files=RunConfig.__dataclass_fields__["required_files"].default + tuple(inputs),
    )
