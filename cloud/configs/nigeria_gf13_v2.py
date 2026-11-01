"""E7 Nigeria question on architecture v2 (main.py --v2), scoped to the 13 Global Fund-supported states
(owner decision 2026-11-01). NOT launched: the owner sets the budget and launches.

Inputs never enter the public repo (DHS terms). Before a launch:
  1. fresh clone of origin/v2 at TREE (no other changes);
  2. python cloud/nigeria_gf13_inputs.py fetch <dir> --account modelingagentic     (sha256-checked)
  3. python cloud/nigeria_gf13_inputs.py stage-tree <dir> TREE                      (local-only commit, push disabled)
  4. python cloud/nigeria_gf13_inputs.py question <dir> > QUESTION                  (original question + scope + paths)
The launcher ships `git archive HEAD` of TREE, so the inputs travel in the code tarball; `required_files`
refuses a launch whose tree lacks any of them, and the harness copies the files the question names into
the run's data/inputs/ with their sha256."""
import json
import os

from run_cluster import RunConfig

TREE = os.path.expanduser("~/modeling-agentic-v2-nigeria")
QUESTION = os.path.expanduser("~/modeling-agentic-keep/nigeria_gf13_v2/question.txt")
_MAN = os.path.join(TREE, "data", "nigeria_gf13", "manifest.json")
INPUTS = tuple(f"data/nigeria_gf13/{n}" for n in sorted(json.load(open(_MAN))["files"])) if os.path.exists(_MAN) \
    else ()                                          # the manifest below fails the launch until stage-tree has run

CONFIG = RunConfig(
    name="v2-nigeria-gf13-gcc",
    question_file=QUESTION,
    tree=TREE,
    require_main=False,                              # v2 branch plus the local inputs commit
    labels=("N1",),
    storage_prefix="runs/v2_nigeria_gf13",
    out_dir=os.path.expanduser("~/modeling-agentic-keep/nigeria_gf13_v2/run"),
    flags=("--v2", "--max-budget-usd", "100", "--signoff", "owner", "--signoff-hours", "24"),
    max_rounds=4,
    compute_start=1, compute_max=8,
    compute_env={"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": "8",
                 "MA_COMPUTE_MAX_CORE_HOURS": "200", "MA_COMPUTE_CALL_CORE_HOURS": "100"},
    required_files=RunConfig.__dataclass_fields__["required_files"].default + ("main.py", "agents/stage_machine.py")
    + INPUTS + ("data/nigeria_gf13/manifest.json",),
)
