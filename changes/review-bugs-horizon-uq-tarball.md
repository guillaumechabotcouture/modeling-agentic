---
phase: "v2 debt"
commit: "external review fixes"
---

## Changelog

**Three bugs from an external review, fixed test-first, plus a fail-closed check on unreadable critic files.** `toolkit/engines/horizon.py` `delivery_problems` accepted the string `"15"` as the period [1, 5] because a string iterates as characters. It now refuses anything that is not a list or tuple of two real numbers (H5). `scripts/uq_ci_quality.py` skipped the inverted-CI check (`ci_low > ci_high`) when there was a single draw; that check now runs before the single-draw skip (I2). `cloud/vm_job.py`'s self-test tarred the whole repo root, `runs/` and `.venv` included, into memory. It now uses a small temp tree, and a `--dir` archive leaves out `.git`, `.venv` and `__pycache__` at any depth, plus a top-level `runs/` (VP10). `agents/stage_machine.py`: the gate already failed closed on an unreadable reviewer YAML (no action, a schema error naming the file; OF1). `open_findings` used to skip such a file or raise on a non-mapping. It now reports it as an open HIGH `critique_unreadable:<reviewer>` (OF2).
