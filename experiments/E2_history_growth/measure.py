"""Measure conversation-history growth in E1 replay cases (stage agent transcripts).

    python3 experiments/E2_history_growth/measure.py runs/_replay/E1

For each case: per-API-turn context (deduplicated by message id), growth from
first to last turn, and which tool results contribute most re-read volume.
Re-read volume of a tool result = its size (bytes/4 ≈ tokens) × number of later
API turns in the same session (every later turn re-reads it from cache).
"""
import collections, glob, json, os, re, sys

CAT = [("bash:python-script", re.compile(r"\bpython3?\s+\S+\.py")),
       ("bash:python-inline", re.compile(r"\bpython3?\s+(-c|-\s*<<|<<)")),
       ("bash:cat/head/tail/sed", re.compile(r"^\s*(cd [^;&]+[;&]+\s*)?(cat|head|tail|sed|awk)\b")),
       ("bash:grep/find/ls", re.compile(r"^\s*(cd [^;&]+[;&]+\s*)?(grep|find|ls|wc)\b")),
       ("bash:wait/poll", re.compile(r"\bsleep\b|grep -q[^|]*&& break")),
       ("bash:pip", re.compile(r"\bpip\b"))]


def categorize(name, inp):
    if name == "Bash":
        cmd = (inp or {}).get("command", "")
        for k, rx in CAT:
            if rx.search(cmd):
                return k
        return "bash:other"
    return {"Read": "read", "Write": "write", "Edit": "edit", "Skill": "skill",
            "Glob": "glob", "Grep": "grep"}.get(name, (name or "?").lower())


def analyze(path):
    turns, by_id, uses, results = [], set(), {}, []
    for line in open(path, errors="replace"):
        try: d = json.loads(line)
        except ValueError: continue
        m = d.get("message") or {}
        if d.get("type") == "assistant" and m.get("usage"):
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    uses[b["id"]] = (b.get("name"), b.get("input"))
            mid = m.get("id")
            if mid in by_id: continue
            by_id.add(mid); u = m["usage"]
            turns.append((u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0))
        if d.get("type") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    if isinstance(c, list):
                        n = sum(len(x.get("text", "")) if isinstance(x, dict) and x.get("type") == "text" else (1500 * 4 if isinstance(x, dict) and x.get("type") == "image" else 0) for x in c)
                    else:
                        n = len(c or "")
                    results.append((b.get("tool_use_id"), n, len(turns)))
    T = len(turns)
    agg = collections.Counter(); cnt = collections.Counter(); size = collections.Counter(); big = []
    for tid, n, at in results:
        name, inp = uses.get(tid, (None, None)); k = categorize(name, inp)
        tok = n / 4; reread = tok * max(T - at, 0)
        agg[k] += reread; cnt[k] += 1; size[k] += tok
        big.append((reread, tok, k, (json.dumps(inp)[:90] if inp else "")))
    return {"turns": T, "start": turns[0] if turns else 0, "end": turns[-1] if turns else 0,
            "sum_ctx": sum(turns), "tool_tokens": sum(size.values()), "reread": agg, "count": cnt,
            "size": size, "big": sorted(big, reverse=True)[:5]}


def stage_transcripts(batch):
    for meta in glob.glob(os.path.join(batch, "*", "*", "claude_session", "*", "subagents", "*.meta.json")):
        try: t = json.load(open(meta)).get("agentType")
        except ValueError: continue
        if t == "modeler":
            yield meta.split("/claude_session/")[0], meta[:-len(".meta.json")] + ".jsonl"


def main(batch):
    tot = collections.Counter(); tot_cnt = collections.Counter(); tot_sum = 0; tot_tool = 0
    print(f"{'case':38} {'turns':>5} {'start_K':>7} {'end_K':>6} {'growth_K':>8} {'tool_out_K':>10} {'ctx_sum_M':>9}")
    for case, tr in sorted(stage_transcripts(batch)):
        r = analyze(tr)
        name = "/".join(case.split("/")[-2:])
        print(f"{name:38} {r['turns']:5d} {r['start']/1e3:7.0f} {r['end']/1e3:6.0f} {(r['end']-r['start'])/1e3:8.0f} {r['tool_tokens']/1e3:10.0f} {r['sum_ctx']/1e6:9.1f}")
        tot.update(r["reread"]); tot_cnt.update(r["count"]); tot_sum += r["sum_ctx"]; tot_tool += r["tool_tokens"]
        for rr, tok, k, inp in r["big"][:2]:
            print(f"     top: {k:22} {tok/1e3:6.1f}K tok × later turns = {rr/1e6:5.2f}M re-read  {inp}")
    s = sum(tot.values()) or 1
    print(f"\nTool-result re-read volume by category (all modeler sessions); share of all context tokens = {100*s/tot_sum:.0f}%")
    for k, v in tot.most_common():
        print(f"  {k:24} calls={tot_cnt[k]:4d}  re-read={v/1e6:6.1f}M  ({100*v/s:4.1f}% of tool re-reads)")


if __name__ == "__main__":
    main(sys.argv[1])
