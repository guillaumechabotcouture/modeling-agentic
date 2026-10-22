#!/usr/bin/env python3
"""Curate stage-replay fixtures from finished or partial runs (architecture v2, step 1).

A MODEL fixture is the frozen upstream state the modeler starts from: every
file that existed in the run directory when the first modeler was spawned
(file mtime <= the first `subagent_start` of type modeler in trace.jsonl),
minus harness bookkeeping. Core upstream documents that were revised after
that point (e.g. plan.md re-written by a later planner spawn) are included at
their latest version and flagged, because the earlier version no longer
exists on disk.

    python3 scripts/fixtures.py build --out <fixtures_root> SOURCE [SOURCE ...]
        SOURCE = <label>=<run_dir>[@utc]   (@utc: trace timestamps are UTC,
                                            e.g. runs harvested from cloud VMs)
    python3 scripts/fixtures.py list --out <fixtures_root>
    python3 scripts/fixtures.py --self-test

Each fixture: <root>/<id>/inputs/ (the frozen run dir), fixture.yaml
(id, source, question, disease, allocation, strata, file hashes, flags), and
spawn_prompt.md (the original first modeler task if a transcript is found,
else a synthesized one) with the run directory replaced by {run_dir}.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone

import yaml

# Harness bookkeeping or downstream-of-MODEL outputs: never part of a fixture.
EXCLUDE_NAMES = {
    "trace.jsonl", "harness_manifest.json", "pipeline_state.yaml", "progress.md",
    "final_gate.json", "final_gate_stderr.txt", "validator_history.jsonl",
    "report.md", "report.unrendered.md", "report.snapshot.md", "results.md",
}
EXCLUDE_PREFIXES = ("critique_", "model_comparison", "identifiability.yaml",
                    "uncertainty_report", "coherence_audit", "writer_qa", "benchmark_match",
                    "effort_floors_report", "decision_rule", "scope_declaration",
                    "archive_", "__pycache__")
# Upstream documents the modeler is told to read (agents/modeler.py §1-7,
# lead STAGE 3-4). Kept even if revised after the modeler started (flagged).
CORE_UPSTREAM = ["plan.md", "hypotheses.md", "success_criteria.yaml", "threads.yaml",
                 "citations.md", "data_quality.md", "data_provenance.md", "pre_mortem.yaml",
                 "models/identifiability_a_priori.yaml"]
# models/ files (benchmark_targets, sanity_schema, ...) count as upstream only
# if they predate the first modeler spawn: later versions contain modeler
# edits and would hand the replayed modeler pre-made outputs.
CORE_OPTIONAL: list[str] = []
MAX_FILE = 600 * 2**20


def _sha(p, n=2**20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(n), b""):
            h.update(b)
    return h.hexdigest()


def modeler_start(run_dir: str, utc: bool) -> float | None:
    """Epoch seconds of the first modeler spawn, from trace.jsonl."""
    tp = os.path.join(run_dir, "trace.jsonl")
    if not os.path.exists(tp):
        return None
    for line in open(tp, errors="replace"):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("type") == "subagent_start" and d.get("agent_type") == "modeler" and d.get("ts"):
            t = datetime.fromisoformat(d["ts"])
            if t.tzinfo is None and utc:
                t = t.replace(tzinfo=timezone.utc)
            return t.timestamp()      # naive -> local time of this machine
    return None


def _excluded(rel: str) -> bool:
    base = os.path.basename(rel)
    if base in EXCLUDE_NAMES or rel.split(os.sep)[0] in EXCLUDE_NAMES:
        return True
    if any(base.startswith(p) or rel.startswith(p) for p in EXCLUDE_PREFIXES):
        return True
    return False


def _modeler_owned(rel: str) -> bool:
    """Without a modeler start time we cannot tell planner-written models/
    files (benchmark_targets, a-priori identifiability) from modeler output,
    so models/ is dropped except the a-priori artifact."""
    return rel.startswith("models" + os.sep) and rel != os.path.join("models", "identifiability_a_priori.yaml")


def _question(run_dir: str) -> str:
    try:
        return json.load(open(os.path.join(run_dir, "metadata.json"))).get("question", "")
    except (OSError, ValueError):
        return ""


def _spawn_prompt(transcripts_root: str | None, run_dir: str) -> tuple[str | None, str]:
    """First modeler task text from CLI subagent transcripts, if available."""
    if not transcripts_root or not os.path.isdir(transcripts_root):
        return None, "none"
    best = None
    for meta in glob.glob(os.path.join(transcripts_root, "**", "subagents", "*.meta.json"), recursive=True):
        try:
            m = json.load(open(meta))
        except (OSError, ValueError):
            continue
        if (m.get("agentType") or m.get("subagent_type")) != "modeler":
            continue
        tp = meta[:-len(".meta.json")] + ".jsonl"
        first = None
        for line in open(tp, errors="replace"):
            d = json.loads(line)
            if d.get("type") == "user":
                c = (d.get("message") or {}).get("content")
                first = c if isinstance(c, str) else " ".join(
                    b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text")
                ts = d.get("timestamp", "")
                break
        if first and (best is None or ts < best[0]):
            best = (ts, first)
    if not best:
        return None, "none"
    return best[1], "transcript"


def _templatize(text: str, run_dir: str) -> str:
    name = os.path.basename(os.path.normpath(run_dir))
    return re.sub(r"[\w./~-]*" + re.escape(name), "{run_dir}", text)


def _synth_prompt(question: str) -> str:
    return ("Run directory: {run_dir}\n"
            f"Research question: {question}\n\n"
            "This is round 1 (no critique feedback yet). Build the models per plan.md and "
            "your system prompt contract, and produce the required MODEL-stage artifacts.")


def build_one(label: str, run_dir: str, out_root: str, utc: bool,
              transcripts_root: str | None = None) -> dict:
    run_dir = os.path.normpath(run_dir)
    t0 = modeler_start(run_dir, utc)
    fid = label
    dest = os.path.join(out_root, fid)
    inputs = os.path.join(dest, "inputs")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(inputs)
    files, flags, skipped_large = {}, [], []
    for root, dirs, fnames in os.walk(run_dir):
        dirs[:] = [d for d in dirs if not _excluded(os.path.relpath(os.path.join(root, d), run_dir))]
        for fn in fnames:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, run_dir)
            if _excluded(rel) or rel == "metadata.json":
                continue
            st = os.stat(src)
            core = rel in CORE_UPSTREAM or rel in CORE_OPTIONAL
            if t0 is None and _modeler_owned(rel):
                continue
            if t0 is not None and st.st_mtime > t0 + 1 and not core:
                continue                                   # written after the modeler started
            if st.st_size > MAX_FILE:
                skipped_large.append(rel); continue
            if core and t0 is not None and st.st_mtime > t0 + 1:
                flags.append(f"revised_after_model_start:{rel}")
            dst = os.path.join(inputs, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            files[rel] = {"sha256": _sha(dst), "bytes": st.st_size}
    missing = [c for c in CORE_UPSTREAM if c not in files]
    q = _question(run_dir)
    json.dump({"question": q, "fixture": fid}, open(os.path.join(inputs, "metadata.json"), "w"), indent=1)
    prompt, prompt_src = _spawn_prompt(transcripts_root, run_dir)
    prompt = _templatize(prompt, run_dir) if prompt else _synth_prompt(q)
    open(os.path.join(dest, "spawn_prompt.md"), "w").write(prompt)
    ql = q.lower()
    disease = next((d for d in ("malaria", "measles", "tuberculosis", "hiv", "polio", "cholera") if d in ql), "other")
    meta = {
        "id": fid, "stage": "MODEL", "source_run": run_dir, "question": q, "disease": disease,
        "allocation": bool(re.search(r"allocat|budget", ql)),
        "strata": [disease, label.rstrip("0123456789").lower() or "run"],
        "model_start_epoch": t0, "model_start_found": t0 is not None,
        "n_files": len(files), "bytes": sum(f["bytes"] for f in files.values()),
        "missing_core_upstream": missing, "flags": flags, "skipped_large": skipped_large,
        "spawn_prompt_source": prompt_src, "files": files,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    yaml.safe_dump(meta, open(os.path.join(dest, "fixture.yaml"), "w"), sort_keys=False)
    return meta


def list_fixtures(out_root: str) -> list[dict]:
    rows = []
    for p in sorted(glob.glob(os.path.join(out_root, "*", "fixture.yaml"))):
        m = yaml.safe_load(open(p))
        rows.append({k: m.get(k) for k in ("id", "disease", "strata", "n_files", "bytes",
                                           "model_start_found", "missing_core_upstream",
                                           "spawn_prompt_source", "flags")})
    return rows


def _self_test() -> int:
    failures = []
    def ok(c, m):
        if not c: failures.append(m)
    with tempfile.TemporaryDirectory() as td:
        run = os.path.join(td, "2026-01-01_000000_some-run"); os.makedirs(os.path.join(run, "data")); os.makedirs(os.path.join(run, "models"))
        def w(rel, txt, mtime):
            p = os.path.join(run, rel); os.makedirs(os.path.dirname(p), exist_ok=True)
            open(p, "w").write(txt); os.utime(p, (mtime, mtime))
        t0 = 1_800_000_000
        w("plan.md", "plan", t0 - 100); w("hypotheses.md", "h", t0 - 100)
        w("data/cases.csv", "a,b\n1,2\n", t0 - 50)
        w("data/derived_by_modeler.csv", "x\n", t0 + 500)          # after modeler start -> excluded
        w("models/identifiability_a_priori.yaml", "verdict: IDENTIFIABLE\n", t0 - 10)
        w("models/model.py", "x=1", t0 + 600)                       # modeler-owned -> excluded
        w("models/benchmark_targets.yaml", "targets: []", t0 - 5)   # planner-written upstream -> kept
        w("critique_methods.yaml", "x", t0 + 700); w("results.md", "r", t0 + 800)
        w("citations.md", "c", t0 + 900)                             # core, revised later -> kept + flagged
        json.dump({"question": "Allocate a measles budget across districts"}, open(os.path.join(run, "metadata.json"), "w"))
        open(os.path.join(run, "trace.jsonl"), "w").write(
            json.dumps({"type": "subagent_start", "agent_type": "planner", "ts": "2027-01-15T08:00:00"}) + "\n" +
            json.dumps({"type": "subagent_start", "agent_type": "modeler",
                        "ts": datetime.fromtimestamp(t0, tz=timezone.utc).replace(tzinfo=None).isoformat()}) + "\n")
        out = os.path.join(td, "fx")
        m = build_one("X1", run, out, utc=True)
        got = set(m["files"])
        ok("data/cases.csv" in got and "plan.md" in got, f"F1 upstream files kept: {got}")
        ok("data/derived_by_modeler.csv" not in got, "F2 post-start data excluded")
        ok("models/model.py" not in got and "models/identifiability_a_priori.yaml" in got
           and "models/benchmark_targets.yaml" in got, f"F3 models/ rule: {sorted(got)}")
        ok("critique_methods.yaml" not in got and "results.md" not in got, "F4 downstream excluded")
        ok("citations.md" in got and any("citations.md" in f for f in m["flags"]), "F5 revised core flagged")
        ok(m["disease"] == "measles" and m["allocation"], "F6 disease/allocation tags")
        sp = open(os.path.join(out, "X1", "spawn_prompt.md")).read()
        ok("{run_dir}" in sp and "measles" in sp, "F7 synthesized spawn prompt")
        ok(_templatize("Run directory: /tmp/ab/T1/runs/2026-01-01_000000_some-run/ go", run)
           == "Run directory: {run_dir}/ go", "F8 templatize run path")
        ok(len(list_fixtures(out)) == 1, "F9 list")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - "); return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["build", "list"])
    ap.add_argument("sources", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.cmd == "list":
        for r in list_fixtures(a.out):
            print(json.dumps(r))
        return 0
    for s in a.sources:
        label, _, rest = s.partition("=")
        utc = rest.endswith("@utc")
        run = rest[:-4] if utc else rest
        tr = os.path.join(os.path.dirname(os.path.dirname(os.path.normpath(run))), "claude_projects")
        m = build_one(label, run, a.out, utc, tr if os.path.isdir(tr) else None)
        print(f"{label}: {m['n_files']} files, {m['bytes']>>20} MB, start_found={m['model_start_found']}, "
              f"missing={m['missing_core_upstream']}, prompt={m['spawn_prompt_source']}, flags={len(m['flags'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
