#!/usr/bin/env python3
"""The read-only diagnostics server for Claude Desktop: MCP over stdio (docs/design/diagnostic-data.md §8).

    python3 tools/diagnostics/mcp_server.py                 # Claude Desktop starts it (tools/diagnostics/README.md)
    python3 tools/diagnostics/mcp_server.py --self-test

JSON-RPC 2.0, one message per line on stdin and stdout (the MCP stdio transport), standard library only. It makes no
network call and has no tool that changes a run, a launch, a cluster, a PR or a budget. Its writes, both outside the
repository and through lib.safe_io: each tool call is logged to <home>/usage.jsonl, and `note_feedback` appends to
<home>/feedback.jsonl. A failed log write never fails a call. Every result is the contract's envelope
{schema: "diag.<tool>/1", as_of, sources, data, notes, derived}; credential-like strings are redacted from it.

Roots (environment): MA_DIAG_RUNS (default <repo>/runs), MA_DIAG_MIRROR (~/modeling-agentic-keep/mirror, follow.py's
copies of cloud runs), MA_DIAG_HOME (~/modeling-agentic-keep/diagnostics: the logs).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for _p in (HERE, os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import diag_runs as DR  # noqa: E402

VERSION = 1
SERVER = {"name": "modeling-agentic-diagnostics", "version": "1"}
PROTOCOL = "2025-06-18"
KEEP = os.path.expanduser("~/modeling-agentic-keep")
_SECRET = re.compile(r"(sk-ant-[A-Za-z0-9_-]{8,}|sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
                     r"|AccountKey=[^;\s\"]+|SharedAccessSignature=[^\s\"]+|[?&]sig=[A-Za-z0-9%/+=]{16,}"
                     r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
                     r"|AKIA[0-9A-Z]{16})")


def roots() -> dict:
    return {"runs": os.environ.get("MA_DIAG_RUNS") or os.path.join(REPO, "runs"),
            "mirror": os.environ.get("MA_DIAG_MIRROR") or os.path.join(KEEP, "mirror")}


def home() -> str:
    return os.path.expanduser(os.environ.get("MA_DIAG_HOME") or os.path.join(KEEP, "diagnostics"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def redact(x):
    if isinstance(x, str):
        return _SECRET.sub("[redacted]", x)
    if isinstance(x, list):
        return [redact(v) for v in x]
    if isinstance(x, dict):
        return {k: redact(v) for k, v in x.items()}
    return x


def envelope(tool: str, data, sources=(), notes=(), derived=None) -> dict:
    if data is None:
        data = {"available": False, "why": "; ".join(str(n) for n in notes if n) or "no data"}
    return redact({"schema": f"diag.{tool}/{VERSION}", "as_of": _now(), "sources": list(sources), "data": data,
                   "notes": list(notes), "derived": derived or {}})


def _append(name: str, rec: dict) -> bool:
    """One line in <home>/<name>, through lib.safe_io (no link followed); False on any failure, never raised."""
    try:
        from lib.safe_io import write_nofollow
        h = home()
        os.makedirs(h, exist_ok=True)
        line = json.dumps(redact(rec), default=str)[:4000] + "\n"
        write_nofollow(h, name, line.encode(), append=True)
        return True
    except Exception:  # noqa: BLE001 -- the log never fails a call
        return False


# ------------------------------------------------------------------ tools
def _run(args):
    found = DR.discover(roots())
    rid = str(args.get("run") or "")
    if rid not in found:
        raise ValueError(f"unknown run {rid!r}: list_runs names the runs under the roots")
    return found, found[rid]


def t_list_runs(args):
    rows = []
    for rid, run in sorted(DR.discover(roots()).items()):
        st, why = run.json("run_state.json")
        st = st or {}
        rows.append({"run": rid, "question": st.get("question"), "state": st.get("state"), "round": st.get("round"),
                     "status": st.get("status"), "paused": st.get("paused"), "spent_usd": st.get("spent_usd"),
                     "last_written": run.written().get("utc"), **({"unreadable": why} if why else {})})
    return {"runs": rows}, [{"path": p} for p in roots().values()], [], {}


def t_run_status(args):
    _, run = _run(args)
    return DR.run_status(run)


def t_run_events(args):
    _, run = _run(args)
    since = args.get("since_seq")
    ev, derived, total = DR.events(run, args.get("kind"), int(since) if since is not None else None,
                                   int(args.get("limit") or 200))
    dropped = sum(int(e.get("dropped_before") or 0) for e in ev)
    return ({"events": ev, "total": total, "next_seq": max([e.get("seq") or 0 for e in ev] or [0]),
             "dropped_before": dropped}, DR._src(run, ["events.jsonl", "run_state.json"]),
            [] if derived == "record" else ["no events.jsonl (a run from before it existed): rebuilt from the "
                                            "history and the journal"], {"events": derived})


def t_run_findings(args):
    _, run = _run(args)
    return DR.run_findings(run, args.get("status"))


def t_run_spend(args):
    _, run = _run(args)
    return DR.run_spend(run)


def t_run_compare(args):
    found, run = _run(args)
    return DR.run_compare(run, list(found.values()))


def t_run_artifact(args):
    _, run = _run(args)
    return DR.run_artifact(run, args.get("path"))


def t_owner_inbox(args):
    return DR.owner_inbox(DR.discover(roots()))


def t_note_feedback(args):
    useful = args.get("useful")
    if useful not in ("yes", "no", "partly"):
        raise ValueError("useful must be yes, no or partly")
    rec = {"v": VERSION, "at": _now(), "question": str(args.get("question") or "")[:1000], "useful": useful,
           "missing": str(args.get("missing") or "")[:2000]}
    wrote = _append("feedback.jsonl", rec)
    return {"recorded": wrote}, [], [] if wrote else ["the feedback could not be written (see the diagnostics folder)"], {}


_RUN = {"run": {"type": "string", "description": "a run id from list_runs, e.g. runs/<folder>"}}
TOOLS = {
    "list_runs": (t_list_runs, "Every run under the roots (local runs and mirrored cloud runs): state, round, status, "
                               "pause, spend and when a file was last written.", {}, []),
    "run_status": (t_run_status, "What a run is doing now and since when: state, round, running task and its elapsed "
                                 "time, heartbeat age, ETA, spend against its cap, open findings, the owner question "
                                 "of a pause. Says when the data is stale.", _RUN, ["run"]),
    "run_events": (t_run_events, "What happened when: the run's events (stage, task, round, gate, finding, owner "
                                 "question and answer, heartbeat, status), filtered by kind and sequence.",
                   {**_RUN, "kind": {"type": "string"}, "since_seq": {"type": "integer"},
                    "limit": {"type": "integer", "description": "default 200"}}, ["run"]),
    "run_findings": (t_run_findings, "The run's findings with the rounds each stayed open and the fix tasks planned "
                                     "for it (with their spend, inferred from the journal).",
                     {**_RUN, "status": {"type": "string"}}, ["run"]),
    "run_spend": (t_run_spend, "Model spend and time by stage and by task, and the cloud compute the run recorded.",
                  _RUN, ["run"]),
    "run_compare": (t_run_compare, "This run's time and spend by stage against the finished runs of the same question.",
                    _RUN, ["run"]),
    "run_artifact": (t_run_artifact, "One allow-listed harness record of a run (state, events, findings, gates, fix "
                                     "plans, owner requests, spec, sign-off sheet, budget and compute ledgers, step "
                                     "logs (tail), report, provenance, analysis YAML).",
                     {**_RUN, "path": {"type": "string", "description": "relative to the run folder"}}, ["run", "path"]),
    "owner_inbox": (t_owner_inbox, "Everything waiting on the owner across runs, with since when and the command "
                                   "that answers it.", {}, []),
    "note_feedback": (t_note_feedback, "Record whether the diagnostics answered a question, and what was missing "
                                       "(appended to the local feedback log; the only tool that writes).",
                      {"question": {"type": "string"}, "useful": {"type": "string", "enum": ["yes", "no", "partly"]},
                       "missing": {"type": "string"}}, ["question", "useful"]),
}


def call(name: str, args: dict) -> tuple[dict, bool]:
    """(envelope, is_error) for one tool call, logged to usage.jsonl."""
    t0, err, out = time.monotonic(), None, None
    try:
        if name not in TOOLS:
            raise ValueError(f"unknown tool {name!r}")
        res = TOOLS[name][0](args if isinstance(args, dict) else {})
        out = envelope(name, *res)
    except Exception as e:  # noqa: BLE001 -- a tool error is a result, never a crash of the server
        err = f"{type(e).__name__}: {e}"[:500]
        out = envelope(name, None, notes=[err])
    data = out.get("data") or {}
    n = next((len(v) for v in data.values() if isinstance(v, list)), 1 if data.get("available", True) else 0) \
        if isinstance(data, dict) else 0
    _append("usage.jsonl", {"v": VERSION, "at": _now(), "tool": name,
                            "args": {k: (str(v)[:200]) for k, v in (args or {}).items()} if isinstance(args, dict) else {},
                            "records": n, "error": err, "ms": round((time.monotonic() - t0) * 1000)})
    return out, err is not None


def handle(msg: dict):
    """The response to one JSON-RPC message, or None for a notification."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": SERVER}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [{"name": n, "description": d, "annotations": {"readOnlyHint": n != "note_feedback"},
                             "inputSchema": {"type": "object", "properties": props, "required": req}}
                            for n, (_, d, props, req) in TOOLS.items()]}
    elif method == "tools/call":
        out, is_err = call(str(params.get("name")), params.get("arguments") or {})
        result = {"content": [{"type": "text", "text": json.dumps(out, default=str, indent=1)}], "isError": is_err}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(inp=sys.stdin, out=sys.stdout) -> None:
    for line in inp:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(msg) if isinstance(msg, dict) else {"jsonrpc": "2.0", "id": None,
                                                              "error": {"code": -32600, "message": "invalid request"}}
        if resp is not None:
            out.write(json.dumps(resp, default=str) + "\n")
            out.flush()


def _self_test() -> int:
    import io
    import tempfile
    fails = []

    def ok(c, label):
        print(("  ok   " if c else "  FAIL ") + label)
        if not c:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        os.environ["MA_DIAG_RUNS"] = os.path.join(td, "runs")
        os.environ["MA_DIAG_MIRROR"] = os.path.join(td, "mirror")
        os.environ["MA_DIAG_HOME"] = os.path.join(td, "home")
        DR.make_fixture(os.path.join(td, "runs"), "a_run")
        DR.make_fixture(os.path.join(td, "mirror", "launch1"), "label1", paused="awaiting_scope_signoff", events=False)
        msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_runs", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                 "params": {"name": "run_status", "arguments": {"run": "runs/a_run"}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                 "params": {"name": "run_artifact", "arguments": {"run": "runs/a_run", "path": "../../etc/hosts"}}},
                {"jsonrpc": "2.0", "id": 6, "method": "nope"}]
        out = io.StringIO()
        serve(io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\nnot json\n"), out)
        rs = [json.loads(x) for x in out.getvalue().splitlines()]
        by = {r.get("id"): r for r in rs}
        ok(by[1]["result"]["protocolVersion"] == "2024-11-05" and "tools" in by[1]["result"]["capabilities"]
           and 2 in by and len([r for r in rs if r.get("id") is None]) == 1,
           "MS1 initialize echoes the client's protocol; a notification gets no reply; a bad line a parse error")
        names = [t["name"] for t in by[2]["result"]["tools"]]
        ok(set(names) == set(TOOLS) and all(t["inputSchema"]["type"] == "object" for t in by[2]["result"]["tools"])
           and not any(n.startswith(("apply_", "prepare_", "sign", "resume")) for n in names),
           f"MS2 the tool list is the read tools and note_feedback only ({names})")
        lr = json.loads(by[3]["result"]["content"][0]["text"])
        ok(lr["schema"] == "diag.list_runs/1" and [r["run"] for r in lr["data"]["runs"]] == ["mirror/launch1/label1",
                                                                                             "runs/a_run"],
           f"MS3 list_runs covers local and mirrored runs ({[r['run'] for r in lr['data']['runs']]})")
        stt = json.loads(by[4]["result"]["content"][0]["text"])
        ok(stt["schema"] == "diag.run_status/1" and stt["data"]["state"] == "CRITIQUE" and stt["sources"]
           and by[4]["result"]["isError"] is False, "MS4 run_status returns the versioned envelope with its sources")
        bad = json.loads(by[5]["result"]["content"][0]["text"])
        ok(by[5]["result"]["isError"] is True and bad["data"]["available"] is False,
           "MS5 a refused path is an error result, never a crash")
        ok(by[6]["error"]["code"] == -32601, "MS6 an unknown method is a JSON-RPC error")
        usage = [json.loads(x) for x in open(os.path.join(td, "home", "usage.jsonl"))]
        ok([u["tool"] for u in usage] == ["list_runs", "run_status", "run_artifact"] and usage[2]["error"]
           and usage[0]["records"] == 2, f"MS7 every call is logged with its record count and error ({usage[:1]})")
        fb = call("note_feedback", {"question": "status?", "useful": "partly", "missing": "eta"})
        lines = open(os.path.join(td, "home", "feedback.jsonl")).read().splitlines()
        ok(not fb[1] and json.loads(lines[-1])["missing"] == "eta", "MS8 note_feedback appends to the feedback log")
        ok(call("note_feedback", {"question": "q", "useful": "maybe"})[1], "MS8b a bad feedback value is refused")
        os.environ["MA_DIAG_HOME"] = os.path.join(td, "home", "usage.jsonl", "x")     # a file where a folder must be
        r9, e9 = call("list_runs", {})
        ok(not e9 and r9["data"]["runs"], "MS9 a log that cannot be written never fails the call")
        os.environ["MA_DIAG_HOME"] = os.path.join(td, "home")
        ok(redact({"a": "key ghp_" + "x" * 30 + " and AccountKey=abc123;"}) == {"a": "key [redacted] and [redacted];"},
           "MS10 credential-like strings are redacted")
        inbox = call("owner_inbox", {})[0]["data"]["items"]
        ok([i["ref"] for i in inbox] == ["mirror/launch1/label1"], "MS11 the owner inbox covers mirrored cloud runs")
        ok(call("run_status", {"run": "runs/../runs/a_run"})[1], "MS12 a run id must be one list_runs named")
    for k in ("MA_DIAG_RUNS", "MA_DIAG_MIRROR", "MA_DIAG_HOME"):
        os.environ.pop(k, None)
    if fails:
        print(f"FAIL: {len(fails)} self-test case(s) failed")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    serve()
