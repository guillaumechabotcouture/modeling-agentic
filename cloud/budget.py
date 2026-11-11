"""Launch budget (stub). The real module, the owner-signed budget and its spend estimator, is PR #192
(f6-budget-core); this file stands in until it merges, and is replaced by it whole.

    python cloud/budget.py --self-test

`cloud/launch_watch.py` calls `launch_budget_ok(scope, cap_usd, name=...)` before every launch and reads spend
through `job_meter(scope)`. Both raise NotImplementedError here, which the watch treats as a refusal (fail
closed): until #192 lands, no launch passes the watch.
"""
from __future__ import annotations

import sys


def launch_budget_ok(scope, cap_usd, name, reserve=True) -> tuple[bool, str]:
    raise NotImplementedError("cloud/budget.py is a stub (PR #192 not merged): no signed budget to check against")


def job_meter(scope):
    raise NotImplementedError("cloud/budget.py is a stub (PR #192 not merged): no spend estimator")


def _self_test() -> int:
    bad = []
    for fn, a in ((launch_budget_ok, ("f6", 10.0, "x")), (job_meter, ("f6",))):
        try:
            fn(*a)
            bad.append(fn.__name__)
        except NotImplementedError:
            pass
    print("\n".join(f"FAIL B0 {b} must refuse" for b in bad) or "PASS B0 the stub refuses every launch and meter")
    print("OK: all self-test cases passed." if not bad else "FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_self_test() if sys.argv[1:2] == ["--self-test"] else 2)
