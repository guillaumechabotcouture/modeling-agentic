#!/usr/bin/env python3
"""A conversation's cost from its Claude CLI transcripts (Phase 34 δ).

After a crash the harness reads the cost back by resuming the conversation
(main.reconcile_conversation_cost). When that read-back fails, the session
used to be charged the whole remaining allowance, which ends the run (E7
attempt 5; the 2026-10-27 pipeline smoke: a 6-second crash charged $4.50 of
a $9 cap). The transcripts record every model call's token usage, so the
cost can be estimated instead: on E7 attempt 6 this gives $361.52 against
the $360.62 the CLI reported (+0.25%).

    python3 scripts/lib/transcript_cost.py <session_id> [--root ~/.claude/projects]
    python3 scripts/lib/transcript_cost.py --self-test

Prices are list prices per million tokens (input, output, cache read,
5-minute cache write, 1-hour cache write); update them when prices change.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
import tempfile

PRICES = {
    "opus": (4.0, 20.0, 0.20, 5.0, 8.0),       # Claude Opus 5.5
    "sonnet": (2.0, 10.0, 0.20, 2.5, 4.0),     # Claude Sonnet 5.5
}
HAIKU_BASE = ((0.10, 0.50), (0.50, 2.50))     # Claude Haiku 5.5: prompt <= 100K tokens, above
_HAIKU_LONG = 100_000


def _price(model: str | None, prompt_tokens: int) -> tuple[float, ...]:
    m = str(model or "").lower()
    if "haiku" in m:
        i, o = HAIKU_BASE[prompt_tokens > _HAIKU_LONG]
        return (i, o, 0.1 * i, 1.25 * i, 2 * i)
    # Unknown models are priced as Opus, the most expensive, so the estimate errs high.
    return PRICES["sonnet" if "sonnet" in m else "opus"]


def file_cost(path: str) -> tuple[float, int]:
    """(dollars, model calls) for one transcript file. A message can be
    logged more than once (streamed parts share its id): count it once."""
    calls: dict = {}
    try:
        f = open(path, errors="replace")
    except OSError:
        return 0.0, 0
    with f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            msg = r.get("message") if isinstance(r, dict) else None
            if r.get("type") != "assistant" or not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                continue
            calls[msg.get("id") or r.get("uuid") or len(calls)] = (msg.get("model"), msg["usage"])
    total = 0.0
    for model, u in calls.values():
        cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
        w5 = cc.get("ephemeral_5m_input_tokens")
        w5 = max(int(u.get("cache_creation_input_tokens") or 0) - w1h, 0) if w5 is None else int(w5)
        inp, out = int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
        rd = int(u.get("cache_read_input_tokens") or 0)
        p = _price(model, inp + rd + w5 + w1h)
        total += (inp * p[0] + out * p[1] + rd * p[2] + w5 * p[3] + w1h * p[4]) / 1e6
    return total, len(calls)


def conversation_cost(session_id: str, root: str | None = None) -> float | None:
    """Cumulative cost of a conversation: the lead transcript plus every
    subagent transcript. None when no lead transcript is found."""
    if not session_id:
        return None
    root = root or os.path.expanduser("~/.claude/projects")
    leads = glob.glob(os.path.join(root, "*", f"{session_id}.jsonl"))
    if not leads:
        return None
    subs = glob.glob(os.path.join(root, "*", session_id, "subagents", "*.jsonl"))
    return sum(file_cost(p)[0] for p in leads + subs)


def _self_test() -> int:
    fails = []

    def ok(c, m):
        if not c:
            fails.append(m)

    def turn(mid, model, inp=0, out=0, rd=0, w5=0, w1h=0):
        return json.dumps({"type": "assistant", "message": {"id": mid, "model": model, "usage": {
            "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": rd,
            "cache_creation_input_tokens": w5 + w1h,
            "cache_creation": {"ephemeral_5m_input_tokens": w5, "ephemeral_1h_input_tokens": w1h}}}}) + "\n"

    with tempfile.TemporaryDirectory() as root:
        proj = os.path.join(root, "-tmp-ab-R1")
        os.makedirs(os.path.join(proj, "s1", "subagents"))
        with open(os.path.join(proj, "s1.jsonl"), "w") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            f.write(turn("m1", "claude-opus-5-5", inp=1_000_000))          # $4
            f.write(turn("m1", "claude-opus-5-5", inp=1_000_000))          # same message logged twice
            f.write(turn("m2", "claude-opus-5-5", out=100_000, w1h=500_000))  # $2 + $4
            f.write("not json\n")
        with open(os.path.join(proj, "s1", "subagents", "agent-a.jsonl"), "w") as f:
            f.write(turn("m3", "claude-sonnet-5-5", rd=1_000_000, w5=400_000))  # $0.20 + $1.00
            f.write(turn("m4", "claude-haiku-5-5", inp=50_000, out=10_000))     # short prompt: $0.005 + $0.005
            f.write(turn("m5", "claude-haiku-5-5", inp=200_000))                # long prompt: $0.10
        c = conversation_cost("s1", root)
        ok(c is not None and abs(c - 11.31) < 1e-9, f"TC1 lead + subagents, deduplicated, per-model prices: {c}")
        ok(conversation_cost("nope", root) is None and conversation_cost("", root) is None,
           "TC2 no transcript -> None (the caller falls back)")
        ok(abs(file_cost(os.path.join(proj, "s1.jsonl"))[0] - 10.0) < 1e-9
           and file_cost(os.path.join(proj, "s1.jsonl"))[1] == 2, "TC3 one file: 2 calls, $10")
        ok(_price("some-new-model", 10)[0] == PRICES["opus"][0], "TC4 an unknown model is priced as Opus")
    if fails:
        print("SELF-TEST FAILURES:")
        for f in fails:
            print("  -", f)
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_id", nargs="?")
    ap.add_argument("--root")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if not a.session_id:
        ap.error("session_id required")
    c = conversation_cost(a.session_id, a.root)
    print("no transcript found" if c is None else f"${c:.4f}")
    return 0 if c is not None else 1


if __name__ == "__main__":
    sys.exit(main())
