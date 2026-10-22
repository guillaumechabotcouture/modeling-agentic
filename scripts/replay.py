#!/usr/bin/env python3
"""Stage replay: run ONE pipeline stage against frozen fixtures (architecture v2, step 1).

    python3 scripts/replay.py run --fixtures <root> --variants a.yaml b.yaml \
        [--only F1 F2] [--seeds 1] [--concurrency 2] [--out <batch_dir>] [--stage MODEL]
    python3 scripts/replay.py run --backend coiled --cluster-name NAME --workers N \
        --fixtures <root> --variants a.yaml b.yaml [--max-total-usd 300] [--dry-agent]
    python3 scripts/replay.py collect --batch <batch_dir> [--watch]   # pull cloud results
    python3 scripts/replay.py status --batch <batch_dir>
    python3 scripts/replay.py shutdown --cluster-name NAME
    python3 scripts/replay.py case --fixture F --variant v.yaml --seed 0 --out <batch_dir>  # one case (used on VMs)
    python3 scripts/replay.py --self-test        # stubbed agent, no API calls, Coiled mocked

A variant YAML:
    id: baseline-prompt
    model: claude-opus-5-5          # optional: override the stage agent's model
    prompt_file: prompts/x.md       # optional: replaces the agent prompt ({run_dir} substituted)
    skills: default | [a, b, ...]   # optional: override preloaded skills
    max_turns: 150                  # optional
    budget_usd: 25                  # hard stop (CLI --max-budget-usd)
    wall_timeout_s: 3600
    tools: [Read, Write, Bash, Skill]   # optional: override the stage agent's tools
    env: {KEY: value}               # optional: extra env vars for the agent session
    tool_output_cap: {bash_chars: 6000, read_lines: 300}   # optional (E2): cap tool output added to context
    compaction: {window_tokens: 150000}                    # optional (E2): CLI auto-compact window
    fresh_context_every: 20         # optional (E2): segments of <=N turns, fresh session + handoff
    effort: medium                  # optional (E2): stage agent reasoning effort (low|medium|high|max)

Each case = (fixture, variant, seed) runs in its own workspace under
<repo>/runs/_replay/<batch>/<variant>/<fixture>-s<seed>/ with the fixture's
inputs (text copied, large data hard-linked read-only). A thin Sonnet lead
spawns the real stage agent from agents/__init__.py, so its skills, tools,
hooks and enforcement are exactly the pipeline's. Outputs: case.json
(telemetry summary + deterministic scores), turns.csv.

Stages: MODEL is implemented. Add a stage by registering it in STAGES with the
agent name and scorer.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime

import yaml
import hashlib
import io
import tarfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

import stage_scorers  # noqa: E402
from telemetry import RATIOS  # noqa: E402

LINK_MIN_BYTES = 1 * 2**20
STAGES = {"MODEL": {"agent": "modeler", "score": stage_scorers.score_model},
          # E5: one critic per variant (`agent:` in the variant YAML) — a pipeline critic
          # agent, `deterministic` (validators only, no LLM) or `crossmodel` (Codex).
          "CRITIQUE": {"agent": None, "score": stage_scorers.score_critique}}
NON_LLM_CRITICS = ("deterministic", "crossmodel")


def stage_agent_name(stage: str, variant: dict) -> str:
    name = variant.get("agent") or STAGES[stage]["agent"]
    if not name:
        raise ValueError(f"stage {stage} needs `agent:` in the variant YAML")
    return name

LEAD_PROMPT = ("You are a replay harness. Spawn the `{agent}` subagent exactly once with the task "
               "below, verbatim, and wait for it to finish. Do not do any work yourself, do not "
               "summarize, and do not spawn anything else. Then end your turn.\n\n<task>\n{task}\n</task>")


# --------------------------------------------------------------------------- workspace

def make_workspace(fixture_dir: str, ws: str) -> str:
    """Copy fixture inputs into ws/run; hard-link large files read-only."""
    src = os.path.join(fixture_dir, "inputs")
    run = os.path.join(ws, "run")
    if os.path.exists(ws):
        shutil.rmtree(ws)
    for root, _, files in os.walk(src):
        for fn in files:
            s = os.path.join(root, fn)
            d = os.path.join(run, os.path.relpath(s, src))
            os.makedirs(os.path.dirname(d), exist_ok=True)
            if os.path.getsize(s) >= LINK_MIN_BYTES:
                os.chmod(s, 0o444)                      # fixture data must not be modified in place
                try:
                    os.link(s, d)
                    continue
                except OSError:
                    pass
            shutil.copy2(s, d)
            os.chmod(d, 0o644)
    return run


def load_variant(path: str) -> dict:
    v = yaml.safe_load(open(path)) or {}
    v.setdefault("id", os.path.splitext(os.path.basename(path))[0])
    v.setdefault("budget_usd", 25.0)
    v.setdefault("wall_timeout_s", 3600)
    v.setdefault("path", os.path.abspath(path))       # for payload accounting in preflight
    if v.get("prompt_file") and not os.path.isabs(v["prompt_file"]):
        v["prompt_file"] = os.path.join(os.path.dirname(os.path.abspath(path)), v["prompt_file"])
    return v


def stage_agent_def(stage: str, question: str, run_dir: str, variant: dict):
    from agents import build_agents
    defs = build_agents(question=question, run_dir=run_dir)
    d = defs[stage_agent_name(stage, variant)]
    changes = {}
    if variant.get("model"):
        changes["model"] = variant["model"]
    if variant.get("prompt_file"):
        changes["prompt"] = open(variant["prompt_file"]).read().replace("{run_dir}", run_dir)
    if isinstance(variant.get("skills"), list):
        changes["skills"] = variant["skills"]
    if variant.get("max_turns"):
        changes["maxTurns"] = int(variant["max_turns"])
    if isinstance(variant.get("tools"), list):
        changes["tools"] = list(variant["tools"])
    if variant.get("effort"):                       # E2: retained thinking is a large share of history
        changes["effort"] = variant["effort"]
    return dataclasses.replace(d, **changes) if changes else d


# --------------------------------------------------------------------------- agent runners

async def sdk_runner(stage, fixture_dir, run_dir, variant, telemetry_rows) -> dict:
    """Real run through the Claude Agent SDK.

    Optional history-growth levers (E2; default off, see agents/output_caps.py):
      tool_output_cap: {bash_chars: N, read_lines: M}
      compaction: {window_tokens: N}
      fresh_context_every: N   run the stage as segments of at most N turns, each a
                               fresh session continued via a deterministic handoff
    """
    question = json.load(open(os.path.join(run_dir, "metadata.json"))).get("question", "")
    task = open(os.path.join(fixture_dir, "spawn_prompt.md")).read().replace("{run_dir}", run_dir)
    if stage == "CRITIQUE" and stage_agent_name(stage, variant) in NON_LLM_CRITICS:
        fn = deterministic_critic if stage_agent_name(stage, variant) == "deterministic" else crossmodel_critic
        return await fn(run_dir, variant)
    adef = stage_agent_def(stage, question, run_dir, variant)
    seg_n = int(variant.get("fresh_context_every") or 0)
    if not seg_n:
        return await _sdk_session(stage, run_dir, variant, adef, task, float(variant["budget_usd"]),
                                  telemetry_rows)
    from agents import output_caps
    total_turns = int(adef.maxTurns or variant.get("max_turns") or 60)
    budget_left = float(variant["budget_usd"])
    t_start = time.time()
    task0 = task + output_caps.WORKLOG_INSTRUCTION.replace("{run_dir}", run_dir)
    used, seg, segments, last = 0, 1, [], {}
    while used < total_turns and budget_left > 0.05:
        n = min(seg_n, total_turns - used)
        if seg == 1:
            prompt = task0
        else:
            wl = os.path.join(run_dir, output_caps.WORKLOG_REL)
            prompt = output_caps.build_handoff(
                task0, open(wl, errors="replace").read() if os.path.exists(wl) else "",
                output_caps.written_files(run_dir, t_start), seg, used, total_turns)
        before = len(telemetry_rows)
        last = await _sdk_session(stage, run_dir, variant, dataclasses.replace(adef, maxTurns=n),
                                  prompt, budget_left, telemetry_rows)
        seg_turns = sum(1 for r in telemetry_rows[before:] if r["agent"] != "lead")
        cost = last.get("cost_usd") or 0.0
        budget_left -= cost
        segments.append({"segment": seg, "turns": seg_turns, "cost_usd": cost, "subtype": last.get("subtype")})
        used += seg_turns
        if seg_turns < n or last.get("is_error") or seg_turns == 0:
            break                       # the agent finished (or failed) before its turn cap
        seg += 1
    total_cost = sum(x["cost_usd"] for x in segments)
    return {**last, "cost_usd": total_cost, "num_turns": used, "segments": segments}


async def _sdk_session(stage, run_dir, variant, adef, task, budget_usd, telemetry_rows) -> dict:
    from claude_agent_sdk import (query, ClaudeAgentOptions, AssistantMessage, ResultMessage,
                                  SystemMessage)
    from agents import create_hooks, enforcement, output_caps
    agent = stage_agent_name(stage, variant)
    trace = open(os.path.join(os.path.dirname(run_dir), "trace.jsonl"), "a")
    hooks = create_hooks(run_dir, trace, datetime.now(), registered_agents={agent},
                         max_turns={agent: adef.maxTurns})
    if variant.get("tool_output_cap"):
        hooks = output_caps.wrap_hooks(hooks, variant["tool_output_cap"], run_dir)
    # The session tool set caps every agent's tools, so the lead must carry the
    # union of its own and the stage agent's (as main.py's LEAD_TOOLS does);
    # its prompt still restricts it to one spawn. add_dirs lets the CLI's file
    # tools reach a workspace outside the repo.
    session_tools = sorted({"Agent", "Skill"} | set(adef.tools or []))
    opts = ClaudeAgentOptions(
        system_prompt="You are a stage-replay harness. Follow instructions literally.",
        model=enforcement.MODEL_FAST, agents={agent: adef}, tools=session_tools,
        allowed_tools=session_tools, add_dirs=[os.path.dirname(run_dir)],
        permission_mode="bypassPermissions", setting_sources=["project"], hooks=hooks, cwd=REPO,
        env={**enforcement.filtered_env(dict(os.environ), False),
             **output_caps.compaction_env(variant.get("compaction")),
             **{str(k): str(v) for k, v in (variant.get("env") or {}).items()}},
        max_budget_usd=float(budget_usd), max_buffer_size=enforcement.MAX_BUFFER_SIZE)
    seen, result, session_id = set(), None, None
    try:
        async for m in query(prompt=LEAD_PROMPT.format(agent=agent, task=task), options=opts):
            if isinstance(m, SystemMessage) and m.subtype == "init":
                session_id = (m.data or {}).get("session_id")
            elif isinstance(m, AssistantMessage) and m.usage:
                mid = m.message_id or id(m)
                if mid in seen:
                    continue
                seen.add(mid)
                u = m.usage
                telemetry_rows.append({
                    "agent": agent if m.parent_tool_use_id else "lead", "model": m.model,
                    "ts": time.time(), "input": u.get("input_tokens") or 0,
                    "cache_read": u.get("cache_read_input_tokens") or 0,
                    "cache_write": u.get("cache_creation_input_tokens") or 0,
                    "output": u.get("output_tokens") or 0,
                    "tool": next((b.name for b in m.content if getattr(b, "name", None)), "")})
            elif isinstance(m, ResultMessage):
                result = m
    finally:
        trace.close()
    return {"session_id": session_id,
            "subtype": getattr(result, "subtype", None), "is_error": getattr(result, "is_error", None),
            "cost_usd": getattr(result, "total_cost_usd", None),
            "model_usage": getattr(result, "model_usage", None),
            "num_turns": getattr(result, "num_turns", None)}


_STUB_REVIEWERS = ("critique-methods", "critique-domain", "critique-presentation", "critique-redteam")


async def deterministic_critic(run_dir: str, variant: dict) -> dict:
    """E5 arm: the mechanical gate alone (no LLM). Writes empty PASS stubs for
    the four critics so the validator runs, collects every finding it raises
    (unresolved HIGHs, accept blockers, rigor violations of any severity),
    removes the stubs, and records the findings as critique_deterministic.yaml."""
    import subprocess
    stubs = []
    for r in _STUB_REVIEWERS:
        p = os.path.join(run_dir, f"critique_{r.split('-', 1)[1]}.yaml")
        yaml.safe_dump({"reviewer": r, "round": 1, "verdict": "PASS",
                        "structural_mismatch": {"detected": False}, "blockers": [], "carried_forward": []},
                       open(p, "w"), sort_keys=False)
        stubs.append(p)
    t0 = time.time()
    cmd = [sys.executable, os.path.join(HERE, "validate_critique_yaml.py"), run_dir, "--max-rounds", "8",
           "--current-round", "1", "--spec-compliance", "--parameter-registry", "--rigor-artifacts",
           "--json", "--no-history"]
    proc = await asyncio.to_thread(subprocess.run, cmd, cwd=REPO, capture_output=True, text=True,
                                   timeout=float(variant.get("wall_timeout_s", 900)))
    for p in stubs:
        os.remove(p)
    out = proc.stdout.strip()
    try:
        dec = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        dec = {"error": (proc.stderr or out)[-500:]}
    findings = [("HIGH", b) for b in dec.get("unresolved_high") or []] + \
               [("HIGH", b) for b in dec.get("accept_blockers") or []] + \
               [(str(v.get("severity", "MEDIUM")).upper(), v) for v in dec.get("rigor_violations") or []]
    seen, blockers = set(), []
    for sev, f in findings:
        key = (f.get("kind"), f.get("claim"))
        if key in seen:
            continue
        seen.add(key)
        blockers.append({"id": f"DET-{len(blockers) + 1:03d}", "severity": sev if sev in ("HIGH", "MEDIUM", "LOW") else "MEDIUM",
                         "kind": f.get("kind"), "claim": str(f.get("claim") or f.get("kind") or ""),
                         "evidence": json.dumps({k: v for k, v in f.items() if k not in ("claim",)}, default=str)[:2000]})
    yaml.safe_dump({"reviewer": "deterministic", "round": 1, "action": dec.get("action"),
                    "validator_exit": proc.returncode, "blockers": blockers,
                    **({"error": dec["error"]} if "error" in dec else {})},
                   open(os.path.join(run_dir, "critique_deterministic.yaml"), "w"), sort_keys=False)
    return {"session_id": None, "subtype": "deterministic", "is_error": "error" in dec, "cost_usd": 0.0,
            "num_turns": 0, "duration_s": round(time.time() - t0, 1)}


_CROSSMODEL_OUTPUTS = ("critique_crossmodel.yaml", "critique_crossmodel.yaml.tmp", "critique_crossmodel.md")


def _tree_hashes(root: str) -> dict:
    """sha256 per file under root (the critic's own outputs excluded)."""
    import hashlib
    out = {}
    for dp, _, files in os.walk(root):
        for fn in files:
            if fn in _CROSSMODEL_OUTPUTS:
                continue
            p = os.path.join(dp, fn)
            try:
                with open(p, "rb") as f:
                    out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                pass
    return out


def _tree_diff(a: dict, b: dict) -> list:
    return sorted({k for k in a.keys() | b.keys() if a.get(k) != b.get(k)})


async def crossmodel_critic(run_dir: str, variant: dict) -> dict:
    """E5 arm: scripts/cross_model_critic.py (Codex, gpt-6.1-sol) as a subprocess.
    Needs a logged-in `codex` on PATH (local machine, or a VM with the Codex
    setup); otherwise the critic writes verdict UNAVAILABLE, which scores as 0
    detections. OpenAI cost is not visible here (cost_usd = None)."""
    import subprocess
    t0 = time.time()
    before = _tree_hashes(run_dir)
    cmd = [sys.executable, os.path.join(HERE, "cross_model_critic.py"), run_dir, "--round", "1",
           "--timeout-s", str(int(variant.get("wall_timeout_s", 1800)))]
    if variant.get("model"):
        cmd += ["--model", variant["model"]]
    proc = await asyncio.to_thread(subprocess.run, cmd, cwd=REPO, capture_output=True, text=True,
                                   timeout=float(variant.get("wall_timeout_s", 1800)) + 120)
    changed = _tree_diff(before, _tree_hashes(run_dir))
    return {"session_id": None, "subtype": "crossmodel",
            "is_error": proc.returncode != 0 or bool(changed), "cost_usd": None,
            "num_turns": 0, "duration_s": round(time.time() - t0, 1), "stderr_tail": proc.stderr[-400:],
            "integrity_violation": changed[:20],
            "sandbox": os.environ.get("CROSS_MODEL_SANDBOX", "read-only")}


async def dry_runner(stage, fixture_dir, run_dir, variant, telemetry_rows) -> dict:
    """--dry-agent: no LLM call; scorers run on the fixture as-is. Used to test
    the plumbing (workspace, scoring, packaging, cloud ship/collect) for free."""
    await asyncio.sleep(0)
    return {"session_id": None, "subtype": "dry", "is_error": False, "cost_usd": 0.0, "num_turns": 0}


def _subagent_rows_from_transcripts(session_id, rows):
    """Fallback if the stream carried no subagent usage: parse CLI transcripts."""
    if not session_id or any(r["agent"] != "lead" for r in rows):
        return rows
    import re
    import telemetry
    key = re.sub(r"[^A-Za-z0-9]", "-", REPO)
    sess = os.path.expanduser(f"~/.claude/projects/{key}/{session_id}")
    if not os.path.isdir(sess):
        return rows
    extra = telemetry.collect(sess, None)
    for r in extra:
        rows.append({"agent": r["agent"], "model": r["model"], "ts": 0, "input": r["input"],
                     "cache_read": r["cache_read"], "cache_write": r["cache_write"],
                     "output": r["output"], "tool": r["tool"]})
    return rows


# --------------------------------------------------------------------------- one case

async def run_case(stage, fixture_dir, variant, batch_dir, seed, runner=sdk_runner) -> dict:
    stage = variant.get("stage", stage)          # E5 critic variants carry `stage: CRITIQUE`
    fid = os.path.basename(os.path.normpath(fixture_dir))
    ws = os.path.join(batch_dir, variant["id"], f"{fid}-s{seed}")
    run_dir = make_workspace(fixture_dir, ws)
    rows: list[dict] = []
    t0 = time.time()
    status = "ok"
    try:
        res = await asyncio.wait_for(runner(stage, fixture_dir, run_dir, variant, rows),
                                     timeout=float(variant["wall_timeout_s"]))
    except asyncio.TimeoutError:
        res, status = {}, "wall_timeout"
    except Exception as e:  # noqa: BLE001 - recorded per case
        res, status = {"error": f"{type(e).__name__}: {e}"[:500]}, "error"
    wall = time.time() - t0
    rows = _subagent_rows_from_transcripts(res.get("session_id"), rows)
    weighted = sum(sum(r[k] * RATIOS[k] for k in RATIOS) for r in rows)
    cost = res.get("cost_usd")
    for r in rows:
        r["cost_usd"] = (sum(r[k] * RATIOS[k] for k in RATIOS) / weighted * cost) if cost and weighted else None
    with open(os.path.join(ws, "turns.csv"), "w", newline="") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
    if res.get("subtype") == "error_max_budget_usd" or "budget" in str(res.get("subtype", "")):
        status = "budget_stop"
    stage_rows = [r for r in rows if r["agent"] != "lead"]
    scores = (stage_scorers.score_model(run_dir) if stage == "MODEL" else
              stage_scorers.score_critique(run_dir, stage_agent_name(stage, variant)) if stage == "CRITIQUE"
              else {})
    case = {
        "fixture": fid, "variant": variant["id"], "seed": seed, "stage": stage, "status": status,
        "result": {k: v for k, v in res.items() if k != "model_usage"},
        "telemetry": {
            "wall_s": round(wall, 1), "cost_usd": cost,
            "turns": len(stage_rows), "lead_turns": len(rows) - len(stage_rows),
            "input_tokens": sum(r["input"] + r["cache_read"] + r["cache_write"] for r in stage_rows),
            "output_tokens": sum(r["output"] for r in stage_rows),
            "start_context": (stage_rows[0]["input"] + stage_rows[0]["cache_read"] + stage_rows[0]["cache_write"])
                             if stage_rows else None,
        },
        "scores": scores, "flat": stage_scorers.flatten(scores) if scores else {},
        "workspace": ws,
    }
    json.dump(case, open(os.path.join(ws, "case.json"), "w"), indent=1, default=str)
    return case


# --------------------------------------------------------------------------- backends

class LocalBackend:
    def __init__(self, concurrency: int = 2):
        self.sem = asyncio.Semaphore(concurrency)

    async def submit(self, coro_factory):
        async with self.sem:
            return await coro_factory()


# The Coiled backend is fire-and-forget (see scripts/replay_coiled.py): `run
# --backend coiled` launches and returns; `collect` installs results into the
# same batch layout as LocalBackend, so replay_compare.py is backend-agnostic.


async def run_batch(stage, fixture_dirs, variants, batch_dir, seeds=1, backend=None, runner=sdk_runner):
    backend = backend or LocalBackend()
    jobs = [backend.submit(lambda f=f, v=v, s=s: run_case(stage, f, v, batch_dir, s, runner))
            for f in fixture_dirs for v in variants for s in range(seeds)]
    cases = await asyncio.gather(*jobs)
    import replay_coiled
    replay_coiled.write_batch_json(batch_dir, cases)
    return cases


# --------------------------------------------------------------------------- preflight

def _dir_bytes(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _code_bytes(repo: str) -> int:
    import subprocess
    try:
        files = subprocess.check_output(["git", "-C", repo, "ls-files", "-co", "--exclude-standard"],
                                        text=True).split("\n")
    except (OSError, subprocess.CalledProcessError):
        return 0
    return sum(os.path.getsize(os.path.join(repo, f)) for f in files
               if f and os.path.isfile(os.path.join(repo, f)))


def preflight(planned: list, variants: list, backend: str, workers: int | None, repo: str = REPO,
              max_fixture_mb: float = 50, max_upload_gb: float = 1.0, upload_mbps: float = 10.0,
              allow_large: str | None = None, out: str | None = None) -> dict:
    """Size/time/cost review before a launch. Refuses oversized payloads unless
    `allow_large` carries a reason. Prompted by a launch that shipped ~2.7 GB of
    fixtures (incl. a 472 MB raster the stage never read) with no review."""
    fx_bytes = {}
    for c in planned:
        fx_bytes.setdefault(c["fixture_id"], _dir_bytes(c["fixture_dir"]))
    var_bytes = 0
    for v in variants:
        if v.get("path") and os.path.exists(v["path"]):
            var_bytes += os.path.getsize(v["path"])
        if v.get("prompt_file") and os.path.exists(v["prompt_file"]):
            var_bytes += os.path.getsize(v["prompt_file"])
    code = _code_bytes(repo) if backend == "coiled" else 0
    n_workers = min(workers or len(planned), len(planned)) if planned else 0
    per_case = {c["key"]: fx_bytes[c["fixture_id"]] + (code + var_bytes if backend == "coiled" else 0)
                for c in planned}
    upload = (sum(fx_bytes[c["fixture_id"]] for c in planned) + code * n_workers + var_bytes * len(planned)
              if backend == "coiled" else 0)
    caps = {v["id"]: {k: v.get(k) for k in ("max_turns", "budget_usd", "wall_timeout_s", "model")}
            for v in variants}
    violations = [f"fixture {f} is {b / 2**20:.1f} MB > --max-fixture-mb {max_fixture_mb}"
                  for f, b in sorted(fx_bytes.items()) if b > max_fixture_mb * 2**20]
    if upload > max_upload_gb * 2**30:
        violations.append(f"total upload {upload / 2**30:.2f} GB > --max-upload-gb {max_upload_gb}")
    rec = {"backend": backend, "cases": len(planned), "workers": n_workers,
           "fixture_mb": {f: round(b / 2**20, 2) for f, b in sorted(fx_bytes.items())},
           "code_mb": round(code / 2**20, 2), "variants_kb": round(var_bytes / 1024, 1),
           "per_case_mb": {k: round(b / 2**20, 2) for k, b in per_case.items()},
           "upload_mb": round(upload / 2**20, 1),
           "upload_eta_min": round(upload / (upload_mbps * 2**20) / 60, 1) if upload else 0.0,
           "upload_mbps_assumed": upload_mbps, "caps": caps,
           "total_budget_usd": round(sum(c["budget_usd"] for c in planned), 2),
           "limits": {"max_fixture_mb": max_fixture_mb, "max_upload_gb": max_upload_gb},
           "violations": violations, "override_reason": allow_large if violations else None,
           "refused": bool(violations) and not allow_large,
           "at": datetime.now().isoformat(timespec="seconds")}
    lines = [f"PREFLIGHT  backend={backend}  cases={len(planned)}  workers={n_workers}"]
    for f, mb in rec["fixture_mb"].items():
        lines.append(f"  fixture {f:18} {mb:9.2f} MB")
    lines.append(f"  code {rec['code_mb']} MB per worker, variants {rec['variants_kb']} KB")
    lines.append(f"  upload total {rec['upload_mb']} MB, ~{rec['upload_eta_min']} min at {upload_mbps} MB/s")
    for vid, cp in caps.items():
        lines.append(f"  variant {vid:18} turns<={cp['max_turns']} budget<=${cp['budget_usd']} "
                     f"wall<={cp['wall_timeout_s']}s model={cp['model']}")
    lines.append(f"  total spend cap ${rec['total_budget_usd']}")
    for v in violations:
        lines.append(f"  VIOLATION: {v}")
    if violations:
        lines.append(f"  OVERRIDE: {allow_large}" if allow_large else
                     "  REFUSED: pass --allow-large \"<reason>\" to launch anyway")
    print("\n".join(lines))
    if out:
        os.makedirs(out, exist_ok=True)
        json.dump(rec, open(os.path.join(out, "preflight.json"), "w"), indent=1)
    if rec["refused"]:
        raise SystemExit("preflight refused the launch: " + "; ".join(violations))
    return rec


# --------------------------------------------------------------------------- self-test

def _self_test() -> int:
    failures = []
    def ok(c, m):
        if not c: failures.append(m)

    async def stub_runner(stage, fixture_dir, run_dir, variant, rows):
        # Simulates the stage agent: writes two model artifacts, emits usage.
        os.makedirs(os.path.join(run_dir, "models"), exist_ok=True)
        open(os.path.join(run_dir, "models", "outcome_fn.py"), "w").write("def outcome_fn(p):\n    return {}\n")
        if variant["id"] == "better":
            yaml.safe_dump({"models": []}, open(os.path.join(run_dir, "models", "model_comparison.yaml"), "w"))
        for i in range(3):
            rows.append({"agent": "modeler", "model": "claude-opus-5-5", "ts": i, "input": 5,
                         "cache_read": 1000 * (i + 1), "cache_write": 100, "output": 50, "tool": "Bash"})
        rows.append({"agent": "lead", "model": "claude-sonnet-5-5", "ts": 9, "input": 5,
                     "cache_read": 10, "cache_write": 10, "output": 5, "tool": "Agent"})
        await asyncio.sleep(0)
        return {"session_id": None, "subtype": "success", "is_error": False, "cost_usd": 1.0}

    with tempfile.TemporaryDirectory() as td:
        fx = os.path.join(td, "fx", "F1", "inputs", "data")
        os.makedirs(fx)
        open(os.path.join(fx, "big.bin"), "wb").write(b"\0" * (LINK_MIN_BYTES + 10))
        open(os.path.join(td, "fx", "F1", "inputs", "plan.md"), "w").write("plan")
        json.dump({"question": "Allocate a budget across districts"},
                  open(os.path.join(td, "fx", "F1", "inputs", "metadata.json"), "w"))
        open(os.path.join(td, "fx", "F1", "spawn_prompt.md"), "w").write("Run directory: {run_dir}")
        vdir = os.path.join(td, "v"); os.makedirs(vdir)
        for vid in ("base", "better"):
            yaml.safe_dump({"id": vid, "budget_usd": 1}, open(os.path.join(vdir, f"{vid}.yaml"), "w"))
        variants = [load_variant(os.path.join(vdir, f"{v}.yaml")) for v in ("base", "better")]
        batch = os.path.join(td, "batch")
        cases = asyncio.run(run_batch("MODEL", [os.path.join(td, "fx", "F1")], variants, batch,
                                      seeds=2, backend=LocalBackend(2), runner=stub_runner))
        ok(len(cases) == 4, f"R1 4 cases, got {len(cases)}")
        c = cases[0]
        ok(c["telemetry"]["turns"] == 3 and c["telemetry"]["lead_turns"] == 1, f"R2 stage vs lead turns: {c['telemetry']}")
        ok(c["telemetry"]["start_context"] == 1105, f"R3 start context: {c['telemetry']['start_context']}")
        ok(c["scores"]["artifacts"]["present"].get("outcome_fn") is True, "R4 scorer saw stub artifact")
        ws_big = os.path.join(c["workspace"], "run", "data", "big.bin")
        ok(os.path.exists(ws_big) and not os.access(ws_big, os.W_OK), "R5 large data linked read-only")
        rates = {x["variant"]: x["flat"]["artifact_rate"] for x in cases}
        ok(rates["better"] > rates["base"], f"R6 variants differ: {rates}")
        ok(os.path.exists(os.path.join(batch, "batch.json")), "R7 batch.json written")
        bj = json.load(open(os.path.join(batch, "batch.json")))
        ok(isinstance(bj, dict) and len(bj["cases"]) == 4, "R7b batch.json carries cases (and preflight)")
        # P1-P4: launch preflight
        import replay_coiled as _rc
        plan = _rc.plan_cases([os.path.join(td, "fx", "F1")], variants, 1)
        import contextlib
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            pf = preflight(plan, variants, "local", None, max_fixture_mb=50, out=os.path.join(td, "pf_ok"))
        ok(not pf["violations"] and "PREFLIGHT" in buf.getvalue() and pf["total_budget_usd"] == 2.0,
           f"P1 clean preflight: {pf['violations']}")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                preflight(plan, variants, "coiled", 2, max_fixture_mb=0.5, out=os.path.join(td, "pf_bad"))
            ok(False, "P2 oversized fixture not refused")
        except SystemExit as e:
            ok("max-fixture-mb" in str(e), f"P2 refusal message: {e}")
        ok(json.load(open(os.path.join(td, "pf_bad", "preflight.json")))["refused"] is True, "P3 refusal recorded")
        with contextlib.redirect_stdout(io.StringIO()):
            pf2 = preflight(plan, variants, "coiled", 2, max_fixture_mb=0.5, allow_large="needs the raster",
                            out=os.path.join(td, "pf_over"))
        ok(pf2["override_reason"] == "needs the raster" and not pf2["refused"] and pf2["upload_mb"] > 0
           and pf2["code_mb"] > 0, "P4 override recorded with reason; coiled upload counted")
        bdir = os.path.join(td, "pf_over")
        _rc.write_batch_json(bdir, [])
        ok(json.load(open(os.path.join(bdir, "batch.json")))["preflight"]["override_reason"] == "needs the raster",
           "P5 batch.json carries the preflight record")

        async def boom(*a):
            raise RuntimeError("agent crashed")
        bad = asyncio.run(run_case("MODEL", os.path.join(td, "fx", "F1"), variants[0], batch, 9, runner=boom))
        ok(bad["status"] == "error" and "crashed" in bad["result"]["error"], "R8 crash recorded per case")
        # V1: a variant's `tools` lands on the stage AgentDefinition itself (and so
        # on both backends, which share run_case); control keeps the defaults.
        from agents import build_agents
        default_tools = list(build_agents(question="q", run_dir="runs/x")["modeler"].tools or [])
        e1 = os.path.join(REPO, "experiments", "E1_context_diet")
        vpath, cpath = os.path.join(e1, "variant.yaml"), os.path.join(e1, "control.yaml")
        if not (os.path.exists(vpath) and os.path.exists(cpath)):
            vpath, cpath = os.path.join(vdir, "e1v.yaml"), os.path.join(vdir, "e1c.yaml")
            yaml.safe_dump({"id": "e1v", "skills": [], "tools": ["Bash", "Write", "Edit", "Read", "Glob", "Grep", "Skill"]},
                           open(vpath, "w"))
            yaml.safe_dump({"id": "e1c"}, open(cpath, "w"))
        dv = stage_agent_def("MODEL", "q", "runs/x", load_variant(vpath))
        dc = stage_agent_def("MODEL", "q", "runs/x", load_variant(cpath))
        ok("Skill" in (dv.tools or []), f"V1 variant tools lack Skill: {dv.tools}")
        ok(list(dc.tools or []) == default_tools, f"V1 control tools changed: {dc.tools} vs {default_tools}")
        ok("Skill" in ({"Agent", "Skill"} | set(dv.tools or [])), "V1 session tools include Skill")

        # C1-C6: Coiled backend logic, exercised locally with Coiled/Dask mocked out.
        import replay_coiled as rcoil
        cases_plan = rcoil.plan_cases([os.path.join(td, "fx", "F1")], variants, 2)
        ok(len(cases_plan) == 4 and len({c["key"] for c in cases_plan}) == 4, "C1 plan_cases unique keys")
        try:
            rcoil.check_total_budget(cases_plan, 3.0); ok(False, "C2 budget cap not enforced")
        except SystemExit:
            pass
        ok(rcoil.check_total_budget(cases_plan, 10.0) == 4.0, "C2 budget total")
        asg = rcoil.assign_workers(cases_plan, ["w2", "w1"])
        ok(sorted(set(asg.values())) == ["w1", "w2"] and list(asg.values()).count("w1") == 2, f"C3 round-robin: {asg}")
        pf = os.path.join(vdir, "p.md"); open(pf, "w").write("PROMPT {run_dir}")
        vb = rcoil.variants_bundle([dict(variants[0], prompt_file=pf)])
        names = tarfile.open(fileobj=io.BytesIO(vb), mode="r:gz").getnames()
        ok(sorted(names) == ["base.prompt.md", "base.yaml"], f"C4 variant bundle: {names}")
        ok(repr(rcoil.Redacted("sk-secret")) == "<redacted>" and "sk-secret" not in str(rcoil.Redacted("sk-secret")),
           "C5 Redacted never prints the secret")
        # C6: the full remote case on this machine (dry agent), then install into a batch.
        code, info = rcoil.code_tarball(REPO)
        spec = {k: cases_plan[0][k] for k in ("key", "fixture_id", "variant_id", "seed")}
        rbase = os.path.join(td, "remote")
        meta = rcoil.remote_case(spec, code, rcoil.fixture_tarball(os.path.join(td, "fx", "F1")),
                                 rcoil.variants_bundle(variants), rcoil.Redacted(None), True, base=rbase)
        meta2 = rcoil.remote_case(spec, code, rcoil.fixture_tarball(os.path.join(td, "fx", "F1")),
                                  rcoil.variants_bundle(variants), rcoil.Redacted(None), True, base=rbase)
        ok(meta2.get("status") == "ok", f"C7 re-running a case on the same worker is idempotent: {meta2.get('error')}")
        full = rcoil.remote_meta(spec["key"], base=rbase)
        ok(full["state"] == "done" and full["status"] == "ok" and full["size"] > 0,
           f"C6 remote dry case ok: {meta.get('status')} {meta.get('error')}")
        ok(full.get("skipped_inputs", 0) >= 1, "C6 unchanged fixture inputs not shipped back")
        tar_local = os.path.join(td, "dl.tar.gz")
        with open(tar_local, "wb") as f:
            off = 0
            while off < full["size"]:
                b = rcoil.remote_chunk(spec["key"], off, 1000, base=rbase); f.write(b); off += len(b)
        ok(hashlib.sha256(open(tar_local, "rb").read()).hexdigest() == full["sha256"], "C6 chunked fetch verifies")
        cb = os.path.join(td, "cloudbatch")
        installed = rcoil.install_result(cb, {**spec, "worker": "w1"}, full, tar_local)
        ok(installed["workspace"].startswith(cb) and installed["cloud"]["worker"] == "w1"
           and os.path.exists(os.path.join(installed["workspace"], "case.json")), "C6 installed in local layout")
        ok(installed["result"]["subtype"] == "dry" and installed["telemetry"]["cost_usd"] == 0.0, "C6 dry agent")
    # X1-X3 (E5 crossmodel on Coiled): codex setup mocked, key never persisted,
    # and only crossmodel cases are planned to receive the OpenAI key.
    import replay_coiled as rcx
    px = rcx.plan_cases(["/f/F1"], [{"id": "methods", "budget_usd": 1}, {"id": "crossmodel", "agent": "crossmodel",
                                                                          "budget_usd": 0}], 1)
    ok([c["needs_codex"] for c in px] == [False, True], f"X1 only crossmodel needs codex: {px}")
    with tempfile.TemporaryDirectory() as tdx:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tz:
            for name in ("bin/codex", "bin/codex-code-mode-host", "codex-path/rg", "codex-resources/bwrap",
                         "codex-resources/voice/bin/codex-voice-host"):
                data = b"#!/bin/sh\necho codex\n"; ti = tarfile.TarInfo(name); ti.size = len(data)
                tz.addfile(ti, io.BytesIO(data))
        calls = []
        class _R:
            def __init__(self, rc=0, out=""): self.returncode, self.stdout, self.stderr = rc, out, ""
        def fake_run(cmd, **kw):
            calls.append((cmd[1:], kw.get("input"))); return _R(0, "Successfully logged in" if "login" in cmd else "codex-cli 0.163.0")
        envx = {"PATH": "/usr/bin"}
        info = rcx.setup_codex(envx, rcx.Redacted("sk-test-SECRET"), home=tdx, download=lambda u: buf.getvalue(), run=fake_run)
        ok(info["installed"] and info["login_rc"] == 0
           and envx["PATH"].startswith(os.path.join(tdx, ".local", "codex", "bin"))
           and info["helpers"] == ["bin/codex-code-mode-host", "codex-path/rg", "codex-resources/bwrap"]
           and not os.path.exists(os.path.join(tdx, ".local", "codex", "codex-resources", "voice")),
           f"X2 codex package + helpers installed (voice skipped), logged in, on PATH: {info}")
        ok(any(c[0][:2] == ["login", "--with-api-key"] and c[1] == "sk-test-SECRET" for c in calls)
           and "sk-test-SECRET" not in json.dumps(info) and 'gpt-6.1-sol' in open(os.path.join(tdx, ".codex", "config.toml")).read(),
           "X2 key only on stdin, never in the record; config written")
        bad = rcx.setup_codex({"PATH": ""}, rcx.Redacted("k"), home=tdx, download=lambda u: (_ for _ in ()).throw(OSError("offline")))
        ok(bad["installed"] is False and "offline" in bad.get("error", ""), "X3 failed setup recorded, never raised")

    with tempfile.TemporaryDirectory() as tdy:
        open(os.path.join(tdy, "a.csv"), "w").write("x")
        h0 = _tree_hashes(tdy)
        open(os.path.join(tdy, "critique_crossmodel.yaml"), "w").write("ok")
        ok(_tree_diff(h0, _tree_hashes(tdy)) == [], "X4 critic outputs excluded from integrity check")
        open(os.path.join(tdy, "a.csv"), "w").write("tampered")
        ok(_tree_diff(h0, _tree_hashes(tdy)) == ["a.csv"], "X4 modified input detected")
        leak = os.path.join(tdy, "critique_crossmodel.md"); open(leak, "w").write("key sk-proj-ABCDEFGHIJKLMNOP end")
        hits = rcx.redact_secrets([leak, os.path.join(tdy, "a.csv")], ["sk-proj-ABCDEFGHIJKLMNOP"])
        ok(hits == ["critique_crossmodel.md"] and "sk-proj" not in open(leak).read(), f"X5 secret redacted: {hits}")

    # S4 (E2): effort override reaches the stage AgentDefinition; absent = unchanged.
    d_eff = stage_agent_def("MODEL", "q", "runs/x", {"id": "e", "effort": "medium"})
    d_def = stage_agent_def("MODEL", "q", "runs/x", {"id": "d"})
    ok(d_eff.effort == "medium" and d_def.effort == stage_agent_def("MODEL", "q", "runs/x", {}).effort,
       f"S4 effort override: {d_eff.effort} / {d_def.effort}")

    # S1-S3 (E2): fresh_context_every segments the stage with a deterministic
    # handoff; stops early when the agent finishes; default path = 1 session.
    import agents.output_caps as _oc
    calls = []
    async def fake_session(stage, run_dir, variant, adef, task, budget, rows):
        calls.append({"turns": adef.maxTurns, "task": task, "budget": budget})
        k = len(calls)
        if k == 1:
            os.makedirs(os.path.join(run_dir, "models"), exist_ok=True)
            open(os.path.join(run_dir, _oc.WORKLOG_REL), "w").write("built model_core.py; next: calibrate")
            open(os.path.join(run_dir, "models", "model_core.py"), "w").write("x = 1\n")
        n_t = adef.maxTurns if k < 3 else 3            # third segment finishes early
        for _ in range(n_t):
            rows.append({"agent": "modeler", "model": "m", "ts": 0, "input": 1, "cache_read": 0,
                         "cache_write": 0, "output": 1, "tool": "Bash"})
        return {"session_id": f"s{k}", "subtype": "success", "is_error": False, "cost_usd": 1.0, "num_turns": n_t}
    _orig_session, _orig_def = globals()["_sdk_session"], globals()["stage_agent_def"]
    globals()["_sdk_session"] = fake_session
    globals()["stage_agent_def"] = lambda st, q, rd, v: dataclasses.replace(
        __import__("claude_agent_sdk").AgentDefinition(description="d", prompt="p"), maxTurns=50)
    try:
        with tempfile.TemporaryDirectory() as td:
            fx = os.path.join(td, "FX"); run = os.path.join(td, "ws", "run")
            os.makedirs(fx); os.makedirs(run)
            open(os.path.join(fx, "spawn_prompt.md"), "w").write("Do the MODEL stage in {run_dir}.")
            json.dump({"question": "q"}, open(os.path.join(run, "metadata.json"), "w"))
            rows = []
            res = asyncio.run(sdk_runner("MODEL", fx, run, {"id": "v", "budget_usd": 10, "fresh_context_every": 20}, rows))
            ok([c["turns"] for c in calls] == [20, 20, 10], f"S1 segment turn caps: {[c['turns'] for c in calls]}")
            ok(res["num_turns"] == 43 and res["cost_usd"] == 3.0 and len(res["segments"]) == 3,
               f"S1 totals: {res.get('num_turns')} {res.get('cost_usd')}")
            ok("Working log (required)" in calls[0]["task"] and "CONTINUATION, segment 2" in calls[1]["task"]
               and "built model_core.py" in calls[1]["task"] and "models/model_core.py" in calls[1]["task"],
               "S2 handoff carries worklog + written files")
            ok(calls[1]["budget"] == 9.0 and calls[2]["budget"] == 8.0, "S2 budget decremented per segment")
            calls.clear()
            res = asyncio.run(sdk_runner("MODEL", fx, run, {"id": "v", "budget_usd": 10}, []))
            ok(len(calls) == 1 and calls[0]["task"] == "Do the MODEL stage in " + run + "." and "segments" not in res,
               "S3 default path is one unmodified session")
    finally:
        globals()["_sdk_session"], globals()["stage_agent_def"] = _orig_session, _orig_def

    # K1-K4 (E5): CRITIQUE stage — agent from the variant, deterministic arm,
    # stub LLM critic scored, stage taken from the variant YAML.
    with tempfile.TemporaryDirectory() as td:
        fx = os.path.join(td, "CF__p1")
        os.makedirs(os.path.join(fx, "inputs"))
        json.dump({"question": "Allocate a vaccination budget across districts"},
                  open(os.path.join(fx, "inputs", "metadata.json"), "w"))
        open(os.path.join(fx, "inputs", "results.md"), "w").write("# Results\n")
        open(os.path.join(fx, "spawn_prompt.md"), "w").write("This is critique round 1. Run directory: {run_dir}")
        ok(stage_agent_name("CRITIQUE", {"agent": "critique-methods"}) == "critique-methods", "K1 agent from variant")
        try:
            stage_agent_name("CRITIQUE", {})
            ok(False, "K1 missing agent must raise")
        except ValueError:
            pass
        det = {"id": "det", "stage": "CRITIQUE", "agent": "deterministic", "budget_usd": 0, "wall_timeout_s": 300}
        c = asyncio.run(run_case("MODEL", fx, det, os.path.join(td, "b"), 0))
        run = os.path.join(c["workspace"], "run")
        doc = yaml.safe_load(open(os.path.join(run, "critique_deterministic.yaml")))
        ok(c["stage"] == "CRITIQUE" and c["status"] == "ok" and isinstance(doc.get("blockers"), list),
           f"K2 deterministic arm ran (stage from variant): {c['status']} {doc.keys() if doc else None}")
        ok(not any(os.path.exists(os.path.join(run, f"critique_{r.split('-', 1)[1]}.yaml")) for r in _STUB_REVIEWERS),
           "K3 deterministic arm removes its PASS stubs")

        async def stub_critic(stage, fixture_dir, run_dir, variant, rows):
            yaml.safe_dump({"reviewer": "critique-methods", "round": 1, "verdict": "REVISE",
                            "structural_mismatch": {"detected": False}, "carried_forward": [],
                            "blockers": [{"id": "M-001", "severity": "HIGH", "category": "METHODS",
                                          "target_stage": "MODEL", "first_seen_round": 1,
                                          "claim": "uncertainty_report.yaml interval is zero-width",
                                          "evidence": "ci_low == ci_high", "fix_requires": "rerun UQ",
                                          "resolved": False}]},
                           open(os.path.join(run_dir, "critique_methods.yaml"), "w"), sort_keys=False)
            return {"session_id": None, "subtype": "success", "is_error": False, "cost_usd": 0.5}
        cm = {"id": "m", "stage": "CRITIQUE", "agent": "critique-methods", "budget_usd": 1, "wall_timeout_s": 60}
        c2 = asyncio.run(run_case("MODEL", fx, cm, os.path.join(td, "b"), 0, runner=stub_critic))
        ok(c2["flat"].get("n_blockers") == 1 and c2["flat"].get("n_high") == 1 and c2["flat"].get("schema_ok") == 1.0,
           f"K4 critic output scored: {c2['flat']}")

    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - "); return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["run", "case", "collect", "status", "shutdown"])
    ap.add_argument("--backend", choices=["local", "coiled"], default="local")
    ap.add_argument("--cluster-name")
    ap.add_argument("--workers", type=int)
    ap.add_argument("--max-total-usd", type=float)
    ap.add_argument("--max-fixture-mb", type=float, default=50)
    ap.add_argument("--max-upload-gb", type=float, default=1.0)
    ap.add_argument("--upload-mbps", type=float, default=10.0)
    ap.add_argument("--allow-large", metavar="REASON")
    ap.add_argument("--dry-agent", action="store_true")
    ap.add_argument("--batch")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--fixture")
    ap.add_argument("--variant")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fixtures")
    ap.add_argument("--variants", nargs="+")
    ap.add_argument("--only", nargs="*")
    ap.add_argument("--seeds", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--stage", default="MODEL", choices=sorted(STAGES))
    ap.add_argument("--out")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    runner = dry_runner if a.dry_agent else sdk_runner
    if a.cmd == "case":                     # one case; what the Coiled VM executes
        case = asyncio.run(run_case(a.stage, a.fixture, load_variant(a.variant), a.out, a.seed, runner))
        print(json.dumps({k: case[k] for k in ("fixture", "variant", "seed", "status")}))
        return 0
    if a.cmd in ("collect", "status"):
        import replay_coiled
        (replay_coiled.collect(a.batch, a.cluster_name, watch=a.watch) if a.cmd == "collect"
         else replay_coiled.status(a.batch, a.cluster_name))
        return 0
    if a.cmd == "shutdown":
        import replay_coiled
        replay_coiled.shutdown(a.cluster_name)
        return 0
    fixtures = sorted(os.path.join(a.fixtures, d) for d in os.listdir(a.fixtures)
                      if os.path.exists(os.path.join(a.fixtures, d, "fixture.yaml")))
    if a.only:
        fixtures = [f for f in fixtures if os.path.basename(f) in a.only]
    variants = [load_variant(p) for p in a.variants]
    batch = a.out or os.path.join(REPO, "runs", "_replay", datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(batch, exist_ok=True)
    import replay_coiled
    planned = replay_coiled.plan_cases(fixtures, variants, a.seeds)
    total = replay_coiled.check_total_budget(planned, a.max_total_usd)
    print(f"{len(planned)} cases, max spend ${total:.2f}")
    preflight(planned, variants, a.backend, a.workers, max_fixture_mb=a.max_fixture_mb,
              max_upload_gb=a.max_upload_gb, upload_mbps=a.upload_mbps,
              allow_large=a.allow_large, out=batch)
    if a.backend == "coiled":
        if not a.cluster_name:
            raise SystemExit("--cluster-name is required for --backend coiled")
        key = None if a.dry_agent else os.environ.get("ANTHROPIC_API_KEY")
        if not a.dry_agent and not key:
            raise SystemExit("ANTHROPIC_API_KEY must be set in the launching shell for --backend coiled")
        okey = None
        if any(c.get("needs_codex") for c in planned) and not a.dry_agent:
            okey = os.environ.get("OPENAI_API_KEY")
            if not okey:
                raise SystemExit("OPENAI_API_KEY must be set in the launching shell for crossmodel arms")
        replay_coiled.launch(planned, variants, REPO, batch, a.cluster_name, a.workers, key, a.dry_agent,
                             openai_key=okey)
        print(f"launched; collect with: replay.py collect --batch {batch} --watch")
        return 0
    cases = asyncio.run(run_batch(a.stage, fixtures, variants, batch, a.seeds, LocalBackend(a.concurrency),
                                  runner=runner))
    for c in cases:
        t = c["telemetry"]
        print(f"{c['variant']:20} {c['fixture']:18} s{c['seed']} {c['status']:12} "
              f"cost=${t['cost_usd']} turns={t['turns']} wall={t['wall_s']}s flat={c['flat']}")
    print(f"batch -> {batch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
