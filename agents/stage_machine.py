#!/usr/bin/env python3
"""The command line of the v2 stage machine. The code is the package agents/stage_machine/ (split by
scripts/refactor/split_stage_machine.py); this file is never imported, since the package shadows it.

    python3 agents/stage_machine.py status <run_dir>
    python3 agents/stage_machine.py sign <run_dir> --by owner [--note "..."]      # the owner signs the spec
    python3 agents/stage_machine.py --self-test
"""
# split_stage_machine: the code lives in the package agents/stage_machine/
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from stage_machine import main  # noqa: E402  (agents/stage_machine/, the package)


if __name__ == "__main__":
    sys.exit(main())
