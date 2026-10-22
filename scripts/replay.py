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
STAGES = {"MODEL": {"agent": "modeler", "score": stage_scorers.score_model}}

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
    if v.get("prompt_file") and not os.path.isabs(v["prompt_file"]):
        v["prompt_file"] = os.path.join(os.path.dirname(os.path.abspath(path)), v["prompt_file"])
    return v


def stage_agent_def(stage: str, question: str, run_dir: str, variant: dict):
    from agents import build_agents
    defs = build_agents(question=question, run_dir=run_dir)
    d = defs[STAGES[stage]["agent"]]
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
    return dataclasses.replace(d, **changes) if changes else d


# --------------------------------------------------------------------------- agent runners

async def sdk_runner(stage, fixture_dir, run_dir, variant, telemetry_rows) -> dict:
    """Real run through the Claude Agent SDK."""
    from claude_agent_sdk import (query, ClaudeAgentOptions, AssistantMessage, ResultMessage,
                                  SystemMessage)
    from agents import create_hooks, enforcement
    question = json.load(open(os.path.join(run_dir, "metadata.json"))).get("question", "")
    task = open(os.path.join(fixture_dir, "spawn_prompt.md")).read().replace("{run_dir}", run_dir)
    agent = STAGES[stage]["agent"]
    adef = stage_agent_def(stage, question, run_dir, variant)
    trace = open(os.path.join(os.path.dirname(run_dir), "trace.jsonl"), "a")
    hooks = create_hooks(run_dir, trace, datetime.now(), registered_agents={agent},
                         max_turns={agent: adef.maxTurns})
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
             **{str(k): str(v) for k, v in (variant.get("env") or {}).items()}},
        max_budget_usd=float(variant["budget_usd"]), max_buffer_size=enforcement.MAX_BUFFER_SIZE)
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
    scores = stage_scorers.score_model(run_dir) if stage == "MODEL" else {}
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
    json.dump(cases, open(os.path.join(batch_dir, "batch.json"), "w"), indent=1, default=str)
    return cases


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
    if a.backend == "coiled":
        if not a.cluster_name:
            raise SystemExit("--cluster-name is required for --backend coiled")
        key = None if a.dry_agent else os.environ.get("ANTHROPIC_API_KEY")
        if not a.dry_agent and not key:
            raise SystemExit("ANTHROPIC_API_KEY must be set in the launching shell for --backend coiled")
        replay_coiled.launch(planned, variants, REPO, batch, a.cluster_name, a.workers, key, a.dry_agent)
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
