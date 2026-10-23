#!/usr/bin/env python3
"""Curate stage-replay fixtures from finished or partial runs (architecture v2, step 1).

A MODEL fixture is the frozen upstream state the modeler starts from: every
file that existed in the run directory when the first modeler was spawned
(file mtime <= the first `subagent_start` of type modeler in trace.jsonl),
minus harness bookkeeping. Core upstream documents that were revised after
that point (e.g. plan.md re-written by a later planner spawn) are included at
their latest version and flagged, because the earlier version no longer
exists on disk.

    python3 scripts/fixtures.py build --out <fixtures_root> [--mode evidence|full]
                                      [--budget-mb 50] SOURCE [SOURCE ...]
        SOURCE = <label>=<run_dir>[@utc]   (@utc: trace timestamps are UTC,
                                            e.g. runs harvested from cloud VMs)
    python3 scripts/fixtures.py list --out <fixtures_root>
    python3 scripts/fixtures.py check <fixture_dir>            # coverage of code-referenced inputs
    python3 scripts/fixtures.py restore <fixture_dir> <relpath> # bring back an excluded file
    python3 scripts/fixtures.py --self-test

Modes. `evidence` (default) copies only upstream files with evidence that the
stage uses them in the SOURCE run, within a size budget:
  (a) transcript  the stage agent read/globbed/grepped or named the file in a
                  Bash command or its spawn prompt (CLI subagent transcripts;
                  trace.jsonl tool summaries as a fallback)
  (b) code        a path/glob literal in the source run's own code
                  (models/**/*.py, run-root *.py) resolves to the file or its dir
  (c) core        the documents the modeler prompt tells it to read
  (d) processed   files under a processed/derived data dir, each <= 10 MB
Everything else is listed under `excluded:` (relpath, size, sha256, original
path, reason no_evidence | over_budget) and can be restored. `full` keeps the
pre-v2 behaviour (every pre-start file): it shipped a 472 MB raw WorldPop
raster the modeler never opened.

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
DEFAULT_BUDGET_MB = 50
PROCESSED_DIRS = {"processed", "derived", "clean", "cleaned", "interim", "intermediate"}
PROCESSED_FILE_CAP = 10 * 2**20
STAGE_AGENT = {"MODEL": "modeler"}
RULE_ORDER = ("core", "transcript", "code", "processed")
_LIT = re.compile(r"""(?<![\w])[rbfu]{0,2}(["'])((?:(?!\1)[^\n\\]){2,300})\1""")
_TOKEN = re.compile(r"[\w./*{}\-]+")


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


def session_dirs(run_dir: str, transcripts_root: str | None) -> list[str]:
    """CLI session dirs (containing subagents/) for this run only.

    Harvested runs ship their own claude_projects/ (one VM = one run). For
    local runs the session id in metadata.json selects exactly this run's
    session under ~/.claude/projects, never other sessions in that folder."""
    if transcripts_root and os.path.isdir(transcripts_root):
        return sorted({os.path.dirname(os.path.dirname(m)) for m in
                       glob.glob(os.path.join(transcripts_root, "**", "subagents", "*.meta.json"),
                                 recursive=True)})
    try:
        sid = json.load(open(os.path.join(run_dir, "metadata.json"))).get("session_id")
    except (OSError, ValueError):
        sid = None
    if not sid:
        return []
    return sorted(d for d in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}"))
                  if os.path.isdir(os.path.join(d, "subagents")))


def stage_texts(run_dir: str, sess_dirs: list[str], agent: str = "modeler") -> tuple[list[str], str]:
    """Tool-input text the stage agent produced in the source run: Read paths,
    Glob/Grep patterns and paths, Bash commands, and its spawn prompt.
    Returns (texts, source) with source in {transcript, trace, none}."""
    texts = []
    for sd in sess_dirs:
        for meta in glob.glob(os.path.join(sd, "subagents", "*.meta.json")):
            try:
                m = json.load(open(meta))
            except (OSError, ValueError):
                continue
            if (m.get("agentType") or m.get("subagent_type")) != agent:
                continue
            first_user = True
            for line in open(meta[:-len(".meta.json")] + ".jsonl", errors="replace"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                msg = d.get("message") or {}
                if d.get("type") == "user" and first_user:
                    c = msg.get("content")
                    texts.append(c if isinstance(c, str) else " ".join(
                        b.get("text", "") for b in (c or []) if isinstance(b, dict) and b.get("type") == "text"))
                    first_user = False
                if d.get("type") != "assistant":
                    continue
                for b in msg.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        texts.extend(str(inp.get(k)) for k in ("file_path", "path", "pattern", "command",
                                                               "notebook_path") if inp.get(k))
    if texts:
        return texts, "transcript"
    tp = os.path.join(run_dir, "trace.jsonl")
    if os.path.exists(tp):
        for line in open(tp, errors="replace"):
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") == "tool_use" and d.get("agent_type") == agent and d.get("summary"):
                texts.append(d["summary"])
    return texts, ("trace" if texts else "none")


_REDIRECT = re.compile(r"(?:>>?|\btee(?:\s+-a)?)\s*['\"]?([\w./{}$-]+\.py)\b")


def stage_written(sess_dirs: list[str], agent: str = "modeler", run_dir: str | None = None) -> set[str]:
    """Run-relative .py paths the stage agent itself wrote, from its transcripts."""
    out = set()
    for sd in sess_dirs:
        for meta in glob.glob(os.path.join(sd, "subagents", "*.meta.json")):
            try:
                if (json.load(open(meta)).get("agentType")) != agent:
                    continue
            except (OSError, ValueError):
                continue
            for line in open(meta[:-len(".meta.json")] + ".jsonl", errors="replace"):
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                for b in (d.get("message") or {}).get("content") or []:
                    if not (isinstance(b, dict) and b.get("type") == "tool_use"):
                        continue
                    inp = b.get("input") or {}
                    if b.get("name") in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
                        fp = inp.get("file_path") or inp.get("notebook_path") or ""
                        if fp.endswith(".py"):
                            out.add(_norm(fp, run_dir))
                    elif b.get("name") == "Bash":
                        out.update(_norm(x, run_dir) for x in _REDIRECT.findall(inp.get("command") or ""))
    return out


def code_literals(run_dir: str, since: float | None = None,
                  only: set[str] | None = None) -> tuple[list[str], list[str]]:
    """Path-like string literals in the source run's code (the stage's own
    produced code included), plus joins of adjacent literals on one line
    (os.path.join("data", "processed", "x.csv"), Path("data") / "x.csv").
    f-string fields become '*' so they match like globs.

    `only` (run-relative paths the stage agent wrote, from its transcripts)
    restricts the scan to the stage's own code; without transcripts, `since`
    (the stage start, epoch s) approximates it by mtime. mtime alone fails when
    an upstream stage is re-run later (critique sent the pipeline back to DATA). Upstream agents' scripts (the data
    agent's downloaders reference every raw file) are not evidence that the
    stage reads those files."""
    out, joined = set(), set()
    paths = glob.glob(os.path.join(run_dir, "models", "**", "*.py"), recursive=True) + \
        glob.glob(os.path.join(run_dir, "*.py")) + \
        glob.glob(os.path.join(run_dir, "scripts_data", "**", "*.py"), recursive=True)
    for p in paths:
        try:
            rel = os.path.relpath(p, run_dir)
            if only is not None:
                if rel not in only:
                    continue
            elif since is not None and os.stat(p).st_mtime < since - 1:
                continue
            src = open(p, errors="replace").read()
        except OSError:
            continue
        for line in src.splitlines():
            lits = [re.sub(r"\{[^}]*\}", "*", m.group(2)) for m in _LIT.finditer(line)]
            for i, lit in enumerate(lits):
                out.add(lit)
                # joins of adjacent literals may only resolve to FILES: the partial
                # join "data/raw" of os.path.join("data", "raw", "x.tif") is not
                # evidence that the code reads all of data/raw.
                if i + 1 < len(lits):
                    joined.add(lits[i].rstrip("/") + "/" + lits[i + 1].lstrip("/"))
                if i + 2 < len(lits):
                    joined.add("/".join(x.strip("/") for x in lits[i:i + 3]))
    keep = lambda xs: sorted(x for x in xs if ("/" in x or "." in x or "*" in x) and " " not in x and len(x) < 300)
    return keep(out), keep(joined - out)


def _norm(s: str, run_dir: str | None = None) -> str:
    s = s.strip().strip("'\"")
    if run_dir and s.startswith(run_dir.rstrip("/") + "/"):
        s = s[len(run_dir.rstrip("/")) + 1:]
    s = re.sub(r"^\./", "", s)
    s = re.sub(r"^.*?/runs/[^/]+/", "", s)            # absolute run-dir prefix (local or /tmp/ab/...)
    return s.replace("{run_dir}/", "")


def _specific_glob(t: str) -> bool:
    """f-strings like f"{a}/{b}" become "*/*", which would match every file.
    A glob counts as evidence only with real literal content: a named
    directory segment, or >= 4 alphanumerics outside wildcards."""
    alnum = len(re.sub(r"[^A-Za-z0-9]", "", t.replace("*", "")))
    named_dir = any(seg and "*" not in seg and re.search(r"[A-Za-z]{2}", seg) for seg in t.split("/")[:-1])
    return alnum >= 4 or named_dir


def match_refs(strings: list[str], rels: list[str], allow_dirs: bool) -> dict[str, set[str]]:
    """Map candidate relpath -> the reference strings that resolve to it."""
    from fnmatch import fnmatch
    base_count = {}
    for r in rels:
        base_count[os.path.basename(r)] = base_count.get(os.path.basename(r), 0) + 1
    hits: dict[str, set[str]] = {}
    for raw in strings:
        for tok in set(_TOKEN.findall(raw)) | {raw}:
            t = _norm(tok)
            if len(t) < 3:
                continue
            for r in rels:
                b = os.path.basename(r)
                ok = (t == r or t.endswith("/" + r) or (t == b and base_count[b] == 1 and len(b) >= 5)
                      or ("/" in t and r.endswith("/" + t)))
                if not ok and "*" in t and _specific_glob(t):
                    ok = fnmatch(r, t) or fnmatch(r, "*/" + t) or ("/" not in t and fnmatch(b, t))
                if not ok and allow_dirs and t.count("/") >= 1 and "*" not in t:
                    ok = r.startswith(t.rstrip("/") + "/")
                if ok:
                    hits.setdefault(r, set()).add(tok if len(tok) < 120 else tok[:117] + "...")
    return hits


def _processed(rel: str) -> bool:
    return any(part in PROCESSED_DIRS for part in rel.split(os.sep)[:-1])


def build_one(label: str, run_dir: str, out_root: str, utc: bool,
              transcripts_root: str | None = None, mode: str = "evidence",
              budget_mb: float = DEFAULT_BUDGET_MB) -> dict:
    run_dir = os.path.normpath(run_dir)
    t0 = modeler_start(run_dir, utc)
    fid = label
    dest = os.path.join(out_root, fid)
    inputs = os.path.join(dest, "inputs")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(inputs)
    files, flags, skipped_large, cands = {}, [], [], []
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
            if mode == "full" and st.st_size > MAX_FILE:
                skipped_large.append(rel); continue
            if core and t0 is not None and st.st_mtime > t0 + 1:
                flags.append(f"revised_after_model_start:{rel}")
            cands.append((rel, src, st.st_size, core))
    sess = session_dirs(run_dir, transcripts_root)
    evidence, excluded, code_refs, ev_source, own = {}, [], [], "n/a", None
    if mode == "full":
        chosen = [(rel, src, size) for rel, src, size, _ in cands]
    else:
        rels = [c[0] for c in cands]
        texts, ev_source = stage_texts(run_dir, sess, STAGE_AGENT.get("MODEL", "modeler"))
        t_hits = match_refs(texts, rels, allow_dirs=False)
        own = stage_written(sess, run_dir=run_dir) if ev_source == "transcript" else None
        lits, joined = code_literals(run_dir, since=t0, only=own)
        c_hits = match_refs(lits, rels, allow_dirs=True)
        for r, refs in match_refs(joined, rels, allow_dirs=False).items():
            c_hits.setdefault(r, set()).update(refs)
        code_refs = sorted(c_hits)
        for rel, src, size, core in cands:
            rules = []
            if core:
                rules.append("core")
            if rel in t_hits:
                rules.append("transcript")
            if rel in c_hits:
                rules.append("code")
            if _processed(rel) and size <= PROCESSED_FILE_CAP:
                rules.append("processed")
            evidence[rel] = rules
        budget = budget_mb * 2**20
        rank = lambda c: (min(RULE_ORDER.index(r) for r in evidence[c[0]]), c[2])
        chosen, used = [], 0
        for rel, src, size, core in sorted((c for c in cands if evidence[c[0]]), key=rank):
            if used + size > budget and not core:
                excluded.append({"relpath": rel, "bytes": size, "sha256": _sha(src),
                                 "original": src, "reason": "over_budget", "rules": evidence[rel]})
                continue
            chosen.append((rel, src, size)); used += size
        for rel, src, size, core in cands:
            if not evidence[rel]:
                excluded.append({"relpath": rel, "bytes": size, "sha256": _sha(src),
                                 "original": src, "reason": "no_evidence"})
        evidence = {r: v for r, v in evidence.items() if v and r in {c[0] for c in chosen}}
    for rel, src, size in chosen:
        dst = os.path.join(inputs, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        files[rel] = {"sha256": _sha(dst), "bytes": size}
    missing = [c for c in CORE_UPSTREAM if c not in files]
    q = _question(run_dir)
    json.dump({"question": q, "fixture": fid}, open(os.path.join(inputs, "metadata.json"), "w"), indent=1)
    prompt, prompt_src = None, "none"
    for sd in ([transcripts_root] if transcripts_root else []) + sess:
        prompt, prompt_src = _spawn_prompt(sd, run_dir)
        if prompt:
            break
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
        "spawn_prompt_source": prompt_src, "mode": mode,
        "budget_mb": budget_mb if mode != "full" else None,
        "evidence_source": ev_source, "candidates": len(cands),
        "excluded_bytes": sum(e["bytes"] for e in excluded),
        "evidence": evidence, "excluded": sorted(excluded, key=lambda e: -e["bytes"]),
        "code_refs": code_refs, "candidate_files": sorted(c[0] for c in cands),
        "stage_code": sorted(own) if mode != "full" and own is not None else None,
        "code_attribution": ("transcript" if mode != "full" and own is not None else "mtime"),
        "files": files,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    yaml.safe_dump(meta, open(os.path.join(dest, "fixture.yaml"), "w"), sort_keys=False)
    return meta


# --------------------------------------------------------------------------- CRITIQUE stage (E5)

CRITIC_AGENTS = ("critique-methods", "critique-domain", "critique-presentation", "critique-redteam")
# What a critic reads at critique time: the run state after MODEL/ANALYZE, before
# WRITE. Harness bookkeeping, earlier critiques (they would hand the replayed
# critic its answers), gate/script audit outputs and the post-WRITE report are out.
CRIT_EXCLUDE_NAMES = {
    "trace.jsonl", "harness_manifest.json", "pipeline_state.yaml", "progress.md",
    "final_gate.json", "final_gate_stderr.txt", "validator_history.jsonl", "metadata.json",
    "report.md", "report.unrendered.md", "report.snapshot.md", "run.log",
}
CRIT_EXCLUDE_PREFIXES = ("critique_", "scope_declaration", "coherence_audit", "writer_qa",
                         "effort_floors_report", "benchmark_match", "archive_", "__pycache__",
                         "claude_projects", "_replay")
CRIT_CORE = {"plan.md", "hypotheses.md", "success_criteria.yaml", "threads.yaml", "citations.md",
             "data_quality.md", "data_provenance.md", "pre_mortem.yaml", "modeling_strategy.md",
             "results.md", "decision_rule.md", "figure_rationale.md", "uncertainty_report.yaml",
             "identifiability.yaml", "model_comparison_formal.yaml", "sensitivity_analysis.yaml"}
CRIT_TEXT_EXT = (".py", ".yaml", ".yml", ".json", ".md", ".csv", ".txt")
CRIT_TEXT_CAP = 5 * 2**20
CRIT_FIG_CAP = 1 * 2**20
CRITIQUE_PROMPT = ("This is critique round 1. Research question: {question}\n"
                   "Run directory: {run_dir}\n"
                   "Review the run as it stands (plan, data, models, analysis results) per your "
                   "system prompt and write your critique markdown and critique YAML into the run "
                   "directory. There are no prior critique rounds (carried_forward must be []).")


def _crit_excluded(rel: str) -> bool:
    base = os.path.basename(rel)
    parts = rel.split(os.sep)
    if base in CRIT_EXCLUDE_NAMES or base.endswith(".pyc"):
        return True
    return any(base.startswith(p) or parts[0].startswith(p) for p in CRIT_EXCLUDE_PREFIXES)


def _crit_core(rel: str, size: int) -> bool:
    base = os.path.basename(rel)
    if rel in CRIT_CORE or (base in CRIT_CORE and rel.count(os.sep) <= 1):
        return True
    if "allocation" in base.lower() and base.endswith(".csv"):
        return size <= CRIT_TEXT_CAP
    if rel.startswith("models" + os.sep) and base.endswith(CRIT_TEXT_EXT):
        return size <= CRIT_TEXT_CAP                      # model code and rigor artifacts
    if os.sep not in rel and base.endswith(".py"):
        return size <= CRIT_TEXT_CAP                      # model code at the run root
    return False


def build_critique(label: str, run_dir: str, out_root: str, transcripts_root: str | None = None,
                   budget_mb: float = DEFAULT_BUDGET_MB) -> dict:
    """CRITIQUE-stage fixture: the state a round-1 critic reads, evidence-based.

    Rules: core (CRIT_CORE docs, model code, models/ artifacts, allocation CSVs),
    transcript (any critic agent named the file in the source run), processed
    (processed-data tables <= 10 MB), figure (figures <= 1 MB, for the
    presentation critic). Everything else is excluded with hash + reason."""
    run_dir = os.path.normpath(run_dir)
    dest = os.path.join(out_root, label)
    inputs = os.path.join(dest, "inputs")
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(inputs)
    cands = []
    for root, dirs, fnames in os.walk(run_dir):
        dirs[:] = [d for d in dirs if not _crit_excluded(os.path.relpath(os.path.join(root, d), run_dir))]
        for fn in fnames:
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, run_dir)
            if _crit_excluded(rel):
                continue
            cands.append((rel, src, os.path.getsize(src)))
    sess = session_dirs(run_dir, transcripts_root)
    texts, ev_source = [], "none"
    for agent in CRITIC_AGENTS:
        t, src_kind = stage_texts(run_dir, sess, agent)
        texts += t
        if src_kind != "none":
            ev_source = src_kind if ev_source in ("none", "trace") else ev_source
    t_hits = match_refs(texts, [c[0] for c in cands], allow_dirs=False)
    evidence = {}
    for rel, src, size in cands:
        rules = []
        if _crit_core(rel, size):
            rules.append("core")
        # Binary model outputs (.npz/.pkl/...) a critic globbed or loaded once are
        # admitted only when small: CB1's critics globbed models/out/*.npz and
        # pulled in ~45 MB of lattice arrays no critic actually inspects.
        if rel in t_hits and (rel.endswith(CRIT_TEXT_EXT + (".png", ".svg", ".pdf"))
                              or size <= CRIT_FIG_CAP):
            rules.append("transcript")
        if _processed(rel) and size <= PROCESSED_FILE_CAP:
            rules.append("processed")
        if rel.split(os.sep)[0] == "figures" and size <= CRIT_FIG_CAP:
            rules.append("figure")
        evidence[rel] = rules
    order = ("core", "transcript", "processed", "figure")
    budget = budget_mb * 2**20
    chosen, excluded, used = [], [], 0
    for rel, src, size in sorted((c for c in cands if evidence[c[0]]),
                                 key=lambda c: (min(order.index(r) for r in evidence[c[0]]), c[2])):
        if used + size > budget:
            excluded.append({"relpath": rel, "bytes": size, "sha256": _sha(src), "original": src,
                             "reason": "over_budget", "rules": evidence[rel]})
            continue
        chosen.append((rel, src, size)); used += size
    for rel, src, size in cands:
        if not evidence[rel]:
            excluded.append({"relpath": rel, "bytes": size, "sha256": _sha(src), "original": src,
                             "reason": "no_evidence"})
    files = {}
    for rel, src, size in chosen:
        dst = os.path.join(inputs, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        files[rel] = {"sha256": _sha(dst), "bytes": size}
    q = _question(run_dir)
    json.dump({"question": q, "fixture": label}, open(os.path.join(inputs, "metadata.json"), "w"), indent=1)
    open(os.path.join(dest, "spawn_prompt.md"), "w").write(CRITIQUE_PROMPT.replace("{question}", q))
    ql = q.lower()
    disease = next((d for d in ("malaria", "measles", "tuberculosis", "hiv", "polio", "cholera") if d in ql), "other")
    meta = {
        "id": label, "stage": "CRITIQUE", "source_run": run_dir, "question": q, "disease": disease,
        "allocation": bool(re.search(r"allocat|budget", ql)), "strata": [disease, "critique"],
        "n_files": len(files), "bytes": sum(f["bytes"] for f in files.values()),
        "spawn_prompt_source": "critique_template", "mode": "evidence", "budget_mb": budget_mb,
        "evidence_source": ev_source, "candidates": len(cands),
        "excluded_bytes": sum(e["bytes"] for e in excluded),
        "evidence": {r: v for r, v in evidence.items() if v and r in files},
        "excluded": sorted(excluded, key=lambda e: -e["bytes"]),
        "candidate_files": sorted(c[0] for c in cands), "files": files,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    yaml.safe_dump(meta, open(os.path.join(dest, "fixture.yaml"), "w"), sort_keys=False)
    return meta


def check(fixture_dir: str) -> dict:
    """Every input the source run's code references (rule b, recomputed now)
    must be in the fixture, or excluded as over_budget (deliberate, restorable)."""
    m = yaml.safe_load(open(os.path.join(fixture_dir, "fixture.yaml")))
    have = set(m.get("files") or {})
    over = {e["relpath"] for e in m.get("excluded") or [] if e["reason"] == "over_budget"}
    run = m["source_run"]
    rels = sorted(set(m.get("candidate_files") or []) | have | {e["relpath"] for e in m.get("excluded") or []})
    refs = []
    if os.path.isdir(run):
        own = set(m["stage_code"]) if m.get("stage_code") is not None else None
        lits, joined = code_literals(run, since=m.get("model_start_epoch"), only=own)
        refs = sorted(set(match_refs(lits, rels, allow_dirs=True)) | set(match_refs(joined, rels, allow_dirs=False)))
    gaps = [r for r in refs if r not in have and r not in over]
    return {"fixture": m["id"], "code_refs": len(refs), "present": sum(r in have for r in refs),
            "over_budget": [r for r in refs if r in over], "gaps": gaps,
            "source_available": os.path.isdir(run)}


def restore(fixture_dir: str, relpath: str) -> dict:
    p = os.path.join(fixture_dir, "fixture.yaml")
    m = yaml.safe_load(open(p))
    ex = next((e for e in m.get("excluded") or [] if e["relpath"] == relpath), None)
    if ex is None:
        raise SystemExit(f"{relpath} is not in this fixture's excluded list")
    dst = os.path.join(fixture_dir, "inputs", relpath)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(ex["original"], dst)
    if _sha(dst) != ex["sha256"]:
        os.remove(dst)
        raise SystemExit(f"{relpath}: source changed since the fixture was built (sha mismatch); not restored")
    m["excluded"] = [e for e in m["excluded"] if e["relpath"] != relpath]
    m.setdefault("files", {})[relpath] = {"sha256": ex["sha256"], "bytes": ex["bytes"]}
    m.setdefault("evidence", {})[relpath] = ["restored"]
    m["n_files"] = len(m["files"]); m["bytes"] = sum(f["bytes"] for f in m["files"].values())
    m["excluded_bytes"] = sum(e["bytes"] for e in m["excluded"])
    yaml.safe_dump(m, open(p, "w"), sort_keys=False)
    return {"restored": relpath, "bytes": ex["bytes"]}


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
        m = build_one("X1", run, out, utc=True, mode="full")
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

        # ---- evidence mode (default) -------------------------------------------
        w("data/raw/huge_raster.tif", "x" * 3_000_000, t0 - 40)       # referenced by code, over budget
        w("data/raw/unused_report.pdf", "y" * 20_000, t0 - 40)        # no evidence
        w("data/raw/read_by_modeler.csv", "a\n1\n", t0 - 40)         # named in a modeler Bash command
        w("data/raw/used_in_code.csv", "b\n2\n", t0 - 40)            # literal in model code
        w("data/pfpr/map_2019.tif", "z", t0 - 40); w("data/pfpr/map_2020.tif", "z", t0 - 40)
        w("data/processed/units.csv", "u\n", t0 - 40)                 # processed dir
        w("scripts_data/download.py", 'X = "data/raw/unused_report.pdf"\n', t0 + 900)  # data agent, re-run after start
        w("models/model.py",
          'import os\nA = "data/raw/used_in_code.csv"\nB = os.path.join("data", "raw", "huge_raster.tif")\n'
          'C = [f"data/pfpr/map_{y}.tif" for y in (2019, 2020)]\n', t0 + 600)
        proj = os.path.join(td, "claude_projects", "-k", "sid1", "subagents"); os.makedirs(proj)
        json.dump({"agentType": "modeler"}, open(os.path.join(proj, "agent-m.meta.json"), "w"))
        open(os.path.join(proj, "agent-m.jsonl"), "w").write("\n".join([
            json.dumps({"type": "user", "message": {"content": "Run directory: " + run + "\nBuild it."}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                        "input": {"command": f"head {run}/data/raw/read_by_modeler.csv"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                        "input": {"command": "ls data/raw"}}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write",
                        "input": {"file_path": f"{run}/models/model.py", "content": "..."}}]}})]))
        out2 = os.path.join(td, "fx2")
        e = build_one("E1", run, out2, utc=True, transcripts_root=os.path.join(td, "claude_projects"),
                      budget_mb=1.0)
        got = set(e["files"])
        ex = {x["relpath"]: x for x in e["excluded"]}
        ok(e["evidence_source"] == "transcript", f"E1 transcripts found: {e['evidence_source']}")
        ok(e["stage_code"] == ["models/model.py"], f"E1b stage code attributed from transcripts: {e['stage_code']}")
        ok("transcript" in e["evidence"].get("data/raw/read_by_modeler.csv", []), "E2 transcript rule")
        ok("code" in e["evidence"].get("data/raw/used_in_code.csv", []), "E3 code literal rule")
        ok({"data/pfpr/map_2019.tif", "data/pfpr/map_2020.tif"} <= got, "E4 f-string glob in code")
        ok("processed" in e["evidence"].get("data/processed/units.csv", []), "E5 processed rule")
        ok(not match_refs(["*/*", "*_*", "*-*", "*.csv"], ["data/raw/a_b.csv", "plan.md"], allow_dirs=True)
           and match_refs(["data/raw/*.csv"], ["data/raw/a_b.csv"], allow_dirs=True),
           "E5b unspecific f-string globs are not evidence; specific ones are")
        ok(e["evidence"].get("plan.md") == ["core"], "E6 core rule")
        ok(ex.get("data/raw/unused_report.pdf", {}).get("reason") == "no_evidence"
           and "data/raw/unused_report.pdf" not in got, "E7 no-evidence file excluded")
        ok(ex.get("data/raw/huge_raster.tif", {}).get("reason") == "over_budget"
           and len(ex["data/raw/huge_raster.tif"]["sha256"]) == 64, "E8 over-budget file excluded with hash")
        ok("data/cases.csv" not in got and ex.get("data/cases.csv", {}).get("reason") == "no_evidence",
           "E9 'ls data/raw' is not evidence of use")
        ok(e["bytes"] <= 1.0 * 2**20, f"E10 budget respected: {e['bytes']}")
        c = check(os.path.join(out2, "E1"))
        ok(c["gaps"] == [] and c["over_budget"] == ["data/raw/huge_raster.tif"], f"E11 coverage check: {c}")
        r = restore(os.path.join(out2, "E1"), "data/raw/huge_raster.tif")
        m2 = yaml.safe_load(open(os.path.join(out2, "E1", "fixture.yaml")))
        ok(r["bytes"] == 3_000_000 and "data/raw/huge_raster.tif" in m2["files"]
           and os.path.exists(os.path.join(out2, "E1", "inputs", "data", "raw", "huge_raster.tif")),
           "E12 restore brings the file back and updates the manifest")
        os.remove(os.path.join(out2, "E1", "inputs", "data", "raw", "used_in_code.csv"))
        m2["files"].pop("data/raw/used_in_code.csv")
        yaml.safe_dump(m2, open(os.path.join(out2, "E1", "fixture.yaml"), "w"), sort_keys=False)
        ok(check(os.path.join(out2, "E1"))["gaps"] == ["data/raw/used_in_code.csv"], "E13 check reports a gap")
        full = build_one("F2", run, os.path.join(td, "fx3"), utc=True, mode="full")
        ok("data/raw/unused_report.pdf" in full["files"], "E14 --mode full keeps the old behaviour")
    if failures:
        print("SELF-TEST FAILURES:", *failures, sep="\n  - "); return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", choices=["build", "list", "check", "restore"])
    ap.add_argument("sources", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--mode", choices=["evidence", "full"], default="evidence")
    ap.add_argument("--budget-mb", type=float, default=DEFAULT_BUDGET_MB)
    ap.add_argument("--stage", choices=["model", "critique"], default="model")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.cmd == "list":
        for r in list_fixtures(a.out):
            print(json.dumps(r))
        return 0
    if a.cmd == "check":
        bad = 0
        for fx in a.sources:
            r = check(fx); bad += bool(r["gaps"])
            print(json.dumps(r))
        return 1 if bad else 0
    if a.cmd == "restore":
        print(json.dumps(restore(a.sources[0], a.sources[1])))
        return 0
    for s in a.sources:
        label, _, rest = s.partition("=")
        utc = rest.endswith("@utc")
        run = rest[:-4] if utc else rest
        tr = os.path.join(os.path.dirname(os.path.dirname(os.path.normpath(run))), "claude_projects")
        if a.stage == "critique":
            m = build_critique(label, run, a.out, tr if os.path.isdir(tr) else None, budget_mb=a.budget_mb)
            print(f"{label}: {m['n_files']} files, {m['bytes']/2**20:.1f} MB (excluded {len(m['excluded'])} "
                  f"files, {m['excluded_bytes']/2**20:.1f} MB), evidence={m['evidence_source']}")
            continue
        m = build_one(label, run, a.out, utc, tr if os.path.isdir(tr) else None,
                      mode=a.mode, budget_mb=a.budget_mb)
        print(f"{label}: {m['n_files']} files, {m['bytes']/2**20:.1f} MB "
              f"(excluded {len(m.get('excluded') or [])} files, {m.get('excluded_bytes', 0)/2**20:.1f} MB), "
              f"evidence={m.get('evidence_source')}, start_found={m['model_start_found']}, "
              f"missing={m['missing_core_upstream']}, prompt={m['spawn_prompt_source']}, flags={len(m['flags'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
