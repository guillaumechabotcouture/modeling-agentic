"""Self-tests of agents/stage_machine.py (common), run by `python3 agents/stage_machine.py --self-test`.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
The module under test is imported as _stage_machine_under_test.
"""
from __future__ import annotations

import os
import _stage_machine_under_test as _SM
from _stage_machine_under_test import SIGNOFF_SHEET


# ------------------------------------------------------------------ self-test
def _t_owner_disposes(run_dir: str) -> None:
    """Self-tests: the owner disposes of each open review condition (deferred to the owner) and the sheet is
    regenerated, as an owner does before signing (W0-G, owner decision 2026-11-21)."""
    import yaml
    from toolkit.spec import review_conditions as RC
    rows = RC.parse(RC.read_bytes(run_dir))
    for r in rows:
        if not r.get("disposition"):
            r.update(disposition="deferred", owner="owner", due="2026-12-31")
    if rows:
        from lib.safe_io import write_nofollow
        write_nofollow(run_dir, RC.FILE, yaml.safe_dump({"conditions": rows}, sort_keys=False).encode(), atomic=True)
        if os.path.exists(os.path.join(run_dir, SIGNOFF_SHEET)):
            from toolkit.spec import signoff as SO
            SO.write_sheet(run_dir)


_TOY_BATCH_FN = '''import os
import time


def f(p):
    with open(os.environ["MA_TOY_CALLS"], "a") as fh:
        fh.write(f"{p['i']}\\n")
    time.sleep(float(os.environ.get("MA_TOY_SLEEP_S") or 0))
    if p["i"] >= int(os.environ.get("MA_TOY_SLOW_FROM") or 99):
        parent = os.getppid()
        for _ in range(600):              # until the step is killed (this process is then orphaned)
            time.sleep(0.1)
            if os.getppid() != parent:
                break
    return {"v": p["i"]}
'''
_TOY_BATCH_DRIVER = '''import json
import os
import sys
sys.path.insert(0, {scripts!r})
import compute

if os.environ.get("MA_TOY_INFRA_FROM"):
    # T61: rows from this input on come back as compute.py reports a chunk lost with its worker.
    _real_map = compute.map_fn

    def _lossy(run_dir, fn_spec, inputs, **kw):
        rows, s = _real_map(run_dir, fn_spec, inputs, **kw)
        k0 = int(os.environ["MA_TOY_INFRA_FROM"])
        return [r if inputs[r["i"]]["i"] < k0 else {{"i": r["i"], "ok": False, "kind": "infra",
                                                    "error": "chunk failed: its worker left the cluster"}}
                for r in rows], s
    compute.map_fn = _lossy


def main(rd):
    os.makedirs(os.path.join(rd, "analysis", "batch"), exist_ok=True)
    rows, _ = compute.run_map(rd, os.path.join(rd, "models", "toy_fn.py") + "::f", [{{"i": i}} for i in range(6)],
                              os.path.join(rd, "analysis", "batch", "rows.jsonl"), backend="local", max_workers=1,
                              purpose="toy batch")
    json.dump([bool(r.get("ok")) for r in rows], open(os.path.join(rd, "analysis", "toy_rows.json"), "w"))
    return 0 if all(r.get("ok") for r in rows) else 1


if __name__ == "__main__":         # the local pool's spawned processes import this file again
    sys.exit(main(sys.argv[1]))
'''


_TOY_REFUSAL = """import json, hashlib, os, sys
rd = sys.argv[1]
os.makedirs(os.path.join(rd, "analysis"), exist_ok=True)
blocked = ("schema: analysis.blocked/1\\ndata:\\n  reason: 'calibration_ridge: the likelihood has 1 flat direction(s); 3 of "
           "4 parameter(s) sit at a bound: z:u0, z:u1, z:u2'\\n  where: calibration\\n  exception: CalibrationRefused\\n"
           "  code: calibration_ridge\\n  at_bound: [z:u0, z:u1, z:u2]\\n")
p = os.path.join(rd, "analysis", "blocked.yaml")
open(p, "w").write(blocked)
sha = hashlib.sha256(open(p, "rb").read()).hexdigest()
json.dump({"status": "blocked", "artifacts": {"analysis/blocked.yaml": {"kind": "blocked", "sha256": sha}}},
          open(os.path.join(rd, "analysis", "manifest.json"), "w"))
sys.stderr.write("toolkit.analysis.calibration.CalibrationRefused: calibration_ridge: the likelihood has 1 flat "
                 "direction(s)\\n")
sys.exit(1)
"""


class _ModuleGlobals(dict):
    """globals() of the stage machine module, for the moved self-tests: reads its names and writes them by
    assignment to it, so a patch reaches every module that holds the name (split_stage_machine.py)."""

    def __init__(self, module):
        super().__init__()
        self._m = module

    def __getitem__(self, k):
        try:
            return getattr(self._m, k)
        except AttributeError:
            raise KeyError(k) from None

    def __setitem__(self, k, v):
        setattr(self._m, k, v)

    def __contains__(self, k):
        return hasattr(self._m, k)

    def get(self, k, default=None):
        return getattr(self._m, k, default)


SM_GLOBALS = _ModuleGlobals(_SM)
