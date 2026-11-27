"""Run budget: the harness-owned spend ledger and session cost read-back.

Phase 48 γ moved this code out of main.py unchanged; main.py imports every
name back, so `main.<name>` (the self-tests) still
works. Tested by `python3 main.py --self-test`.
"""

import contextlib
import fcntl
import json
import math
import os
import sys

from agents import enforcement

REPO_ROOT = enforcement.REPO_ROOT


BUDGET_LEDGER = "budget_ledger.json"


def load_budget_ledger(run_path: str) -> dict:
    """The harness-owned spend record (agents may not write it). Not metadata.json:
    the planner may edit that file (Codex review of PR #15)."""
    try:
        with open(os.path.join(run_path, BUDGET_LEDGER)) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("sessions"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"sessions": []}


RECONCILE_PROMPT = ("The previous session was interrupted by a crash. Reply with the single word OK "
                    "and do nothing else; the harness is reading this conversation's cost.")


def charge_session(ledger: dict, session: int, result, allowance: float | None,
                   session_id: str | None = None, elapsed_s: float | None = None,
                   source: str = "result") -> dict:
    """Record one session's spend.
    - A cost report (the session's own, or after a crash the cumulative cost
      read back by resuming the conversation): charged exactly.
    - Otherwise (cost unreadable): the whole remaining allowance, so the cap
      can never be exceeded (Codex reviews, PR #15 and #23)."""
    cost = getattr(result, "total_cost_usd", None) if result is not None else None
    if isinstance(cost, (int, float)):
        # The SDK reports CUMULATIVE cost for a resumed conversation (same
        # session_id; verified 2026-10-23): charge only the increase.
        sid = getattr(result, "session_id", None)
        # Everything already charged to this conversation, including a crashed
        # session's read-back: each report adds only its increase.
        prev = sum(e.get("total_cost_usd") or 0 for e in ledger.get("sessions", [])
                   if sid and e.get("session_id") == sid)
        entry = {"session": session, "session_id": sid, "reported_usd": round(cost, 4),
                 "total_cost_usd": round(max(cost - prev, 0), 4), "source": source}
    else:
        entry = {"session": session, "total_cost_usd": round(allowance or 0, 4),
                 "source": "unreconciled: charged the full remaining allowance"}
    ledger.setdefault("sessions", []).append(entry)
    return ledger


def write_budget_ledger(run_path: str, ledger: dict, cap: float | None) -> None:
    ledger = {**ledger, "cap_usd": cap,
              "spent_usd": round(sum(e.get("total_cost_usd") or 0 for e in ledger["sessions"]), 4)}
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from lib.safe_io import write_nofollow       # no link followed, atomic (follow-up to review round 5)
    write_nofollow(run_path, BUDGET_LEDGER, json.dumps(ledger, indent=2).encode(), atomic=True)


def writeup_reserve(cap: float | None) -> float:
    """Phase 29: dollars held back from the modeling sessions so the final
    write-up can always run (8% of the cap, at least $10, at most half)."""
    if cap is None:
        return 0.0
    return round(min(max(0.08 * cap, 10.0), 0.5 * cap), 2)


def remaining_budget(cap: float | None, records: list[dict]) -> float | None:
    """Dollars left under a run-wide cap, summed over every session so far
    (including earlier invocations of a resumed run). None = no cap."""
    if cap is None:
        return None
    spent = sum(r.get("total_cost_usd") or 0 for r in records
                if isinstance(r.get("total_cost_usd"), (int, float))
                and not isinstance(r.get("total_cost_usd"), bool)
                and math.isfinite(float(r.get("total_cost_usd"))))
    return round(cap - spent, 4)


def _validate_usd(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: finite non-negative number required, got {value!r}")
    f = float(value)
    if not math.isfinite(f) or f < 0.0:
        raise ValueError(f"{label}: finite non-negative number required, got {value!r}")
    return round(f, 4)


@contextlib.contextmanager
def _budget_ledger_lock(run_path: str):
    scripts = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    from lib.safe_io import open_append_nofollow
    with open_append_nofollow(run_path, ".budget_ledger.lock") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def reserve_compute(
    run_path: str,
    amount_usd: float,
    *,
    cap_usd: float | None,
    stage: str = "ENGINE_ADMIT",
    label: str = "admission_compute",
) -> tuple[bool, str | None]:
    """Reserve `amount_usd` against the run's remaining balance BEFORE dispatching cloud compute
    (docs/design/agent-built-engines.md §6.2, BE33). Serialized with flock on `.budget_ledger.lock`.
    Returns `(True, reservation_id)` when persisted, or `(False, reason)` without mutating the ledger."""
    import uuid
    amt = _validate_usd(amount_usd, "reserve_compute amount_usd")
    eff_cap = _validate_usd(cap_usd, "reserve_compute cap_usd") if cap_usd is not None else None
    with _budget_ledger_lock(run_path):
        ledger = load_budget_ledger(run_path)
        rem = remaining_budget(eff_cap, ledger["sessions"])
        if rem is not None and rem + 1e-9 < amt:
            return False, f"insufficient budget for {stage} {label}: need ${amt:.2f}, remaining ${rem:.2f}"
        res_id = f"res-{stage.lower()}-{uuid.uuid4().hex[:10]}"
        ledger["sessions"].append({
            "session": len(ledger["sessions"]) + 1,
            "session_id": res_id,
            "stage": str(stage),
            "label": str(label),
            "status": "reserved",
            "reserved_usd": amt,
            "total_cost_usd": amt,
            "source": f"{stage} compute reservation",
        })
        write_budget_ledger(run_path, ledger, eff_cap)
        return True, res_id


def reconcile_compute(
    run_path: str,
    reservation_id: str,
    actual_usd: float,
    *,
    cap_usd: float | None = None,
) -> dict:
    """Reconcile a prior `reserve_compute` entry to its measured spend `actual_usd` (BE33)."""
    act = _validate_usd(actual_usd, "reconcile_compute actual_usd")
    cap_in = _validate_usd(cap_usd, "reconcile_compute cap_usd") if cap_usd is not None else None
    with _budget_ledger_lock(run_path):
        ledger = load_budget_ledger(run_path)
        eff_cap = cap_in if cap_in is not None else ledger.get("cap_usd")
        matched = None
        for entry in ledger["sessions"]:
            if entry.get("session_id") == reservation_id:
                entry["total_cost_usd"] = act
                entry["actual_usd"] = act
                entry["status"] = "reconciled"
                matched = entry
                break
        if matched is None:
            raise KeyError(f"reconcile_compute: unknown reservation_id {reservation_id!r}")
        write_budget_ledger(run_path, ledger, eff_cap)
        return matched
