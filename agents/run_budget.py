"""Run budget: the harness-owned spend ledger and session cost read-back.

Phase 48 γ moved this code out of main.py unchanged; main.py imports every
name back, so `main.<name>` (the self-tests, scripts/sdk_scenarios.py) still
works. Tested by `python3 main.py --self-test`.
"""

import asyncio
import json
import os
import sys

from claude_agent_sdk import (
    query,
    ResultMessage,
)
from agents import enforcement

REPO_ROOT = enforcement.REPO_ROOT


BUDGET_LEDGER = "budget_ledger.json"


def load_budget_ledger(run_path: str) -> dict:
    """The harness-owned spend record (agents may not write it; the
    integrity record watches it). Not metadata.json: the planner may edit
    that file (Codex review of PR #15)."""
    try:
        with open(os.path.join(run_path, BUDGET_LEDGER)) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("sessions"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"sessions": []}


# Phase 28: a session that dies before starting its conversation (no session
# id: no API call yet) is charged an upper bound from its duration, ~2x the
# highest hourly spend observed (E7 runs averaged ~$25-30/h).
MAX_USD_PER_HOUR = 60.0
RECONCILE_PROMPT = ("The previous session was interrupted by a crash. Reply with the single word OK "
                    "and do nothing else; the harness is reading this conversation's cost.")


async def reconcile_conversation_cost(options, session_id: str, timeout_s: float = 300, query_fn=None):
    """After a crash, the conversation's true cumulative cost: resume it for
    one tool-less turn; the SDK reports cumulative cost for a resumed
    conversation (verified 2026-10-23, crash smokes 2026-10-25). Returns the
    ResultMessage, or None if it cannot be read (API down, timeout)."""
    import dataclasses
    probe = dataclasses.replace(options, resume=session_id, max_turns=1, tools=[], allowed_tools=[],
                                agents={}, hooks=None, max_budget_usd=None)
    qf = query_fn or query

    async def go():
        async for m in qf(prompt=RECONCILE_PROMPT, options=probe):
            if isinstance(m, ResultMessage):
                # The read-back turn itself costs something: an error result or a
                # zero cost means the conversation was not actually resumed.
                ok_ = (not m.is_error and isinstance(m.total_cost_usd, (int, float))
                       and m.total_cost_usd > 0 and (m.session_id in (None, session_id)))
                return m if ok_ else None
        return None
    try:
        return await asyncio.wait_for(go(), timeout_s)
    except Exception:  # noqa: BLE001 — unknown cost: the caller charges the allowance
        return None


TRANSCRIPT_MARGIN = 1.05   # Phase 34 δ: E7 attempt 6, transcript estimate +0.25% vs reported


async def crashed_session_cost(options, session_id: str, attempts: int = 3, delay_s: float = 20.0,
                               query_fn=None, transcript_root: str | None = None):
    """Phase 34 δ: a crashed session's cumulative cost. Read it back by
    resuming the conversation (retried: the 2026-10-27 smoke's single try
    returned nothing, a second scenario's worked), else estimate it from the
    CLI transcripts (+5%). Returns (result-like or None, source)."""
    from types import SimpleNamespace
    for i in range(attempts):
        r = await reconcile_conversation_cost(options, session_id, query_fn=query_fn)
        if r is not None:
            return r, "reconciled: cumulative cost read back after a crash"
        if i + 1 < attempts:
            await asyncio.sleep(delay_s)
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts", "lib"))
    import transcript_cost
    est = transcript_cost.conversation_cost(session_id, transcript_root)
    if est is None:
        return None, "result"
    return (SimpleNamespace(total_cost_usd=round(est * TRANSCRIPT_MARGIN, 4), session_id=session_id),
            f"estimated from the CLI transcripts (+{TRANSCRIPT_MARGIN - 1:.0%}); read-back failed")


def charge_session(ledger: dict, session: int, result, allowance: float | None,
                   session_id: str | None = None, elapsed_s: float | None = None,
                   source: str = "result") -> dict:
    """Record one session's spend.
    - A cost report (the session's own, or after a crash the cumulative cost
      read back by resuming the conversation): charged exactly.
    - Crashed before its conversation started (no session id, so no API
      call): an upper bound from its duration.
    - Otherwise (cost unreadable): the whole remaining allowance, so the cap
      can never be exceeded (Codex reviews, PR #15 and #23).
    E7 attempt 5: a ~$62 crashed session was charged the whole $450 and the
    run stopped; reading the true cost back lets the run continue."""
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
    elif session_id is None and isinstance(elapsed_s, (int, float)):
        bound = max(elapsed_s, 0) / 3600 * MAX_USD_PER_HOUR
        charged = bound if allowance is None else min(bound, allowance)
        entry = {"session": session, "session_id": session_id, "total_cost_usd": round(charged, 4),
                 "source": f"upper bound: {elapsed_s:.0f}s at ${MAX_USD_PER_HOUR:.0f}/h "
                           "(crashed before its conversation started)"}
    else:
        entry = {"session": session, "total_cost_usd": round(allowance or 0, 4),
                 "source": "unreconciled: charged the full remaining allowance"}
    ledger.setdefault("sessions", []).append(entry)
    return ledger


def conversation_cost(records: list[dict]) -> float | None:
    """True spend from per-session results: the reported cost is cumulative
    within a conversation (session_id), so take each conversation's maximum
    and sum across conversations. Summing sessions over-counted resumed runs."""
    by_sid: dict = {}
    for i, r in enumerate(records):
        c = r.get("total_cost_usd")
        if isinstance(c, (int, float)):
            k = r.get("session_id") or f"_nosid{i}"
            by_sid[k] = max(by_sid.get(k, 0), c)
    return round(sum(by_sid.values()), 4) if by_sid else None


def write_budget_ledger(run_path: str, ledger: dict, cap: float | None, integrity=None) -> None:
    ledger = {**ledger, "cap_usd": cap,
              "spent_usd": round(sum(e.get("total_cost_usd") or 0 for e in ledger["sessions"]), 4)}
    with open(os.path.join(run_path, BUDGET_LEDGER), "w") as f:
        json.dump(ledger, f, indent=2)
    if integrity is not None:
        integrity.harness_wrote(BUDGET_LEDGER)


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
                if isinstance(r.get("total_cost_usd"), (int, float)))
    return round(cap - spent, 4)
