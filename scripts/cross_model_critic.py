#!/usr/bin/env python3
"""
Phase 21 ζ — cross-model critic (Codex CLI / gpt-6.1-sol).

Every pipeline critic is a Claude agent. In the Phase 21 review an
independent gpt-6.1-sol pass found a modeling error that five Claude
reviewers missed (a targeted intervention whose benefit scaled with the
total population while its cost scaled with the eligible population).
A different model family catches different errors, so this script runs
the Codex CLI as an extra, clean-slate adversarial critic in STAGE 6.

Division of labour (the point of the design):
  * The MODEL only judges: it returns JSON matching OUTPUT_SCHEMA
    (new findings + a re-check of each prior X- blocker).
  * The SCRIPT owns the YAML: it assigns X-NNN ids, preserves
    first_seen_round, builds carried_forward, derives the verdict, and
    validates the result with the validator's own schema function
    before writing `critique_crossmodel.yaml`. Critique-YAML schema
    repairs consumed ~20% of agent spawns in past runs; none are needed
    here because no agent writes this file.

Failure is open-but-visible: if codex is missing, not logged in, or
times out, the file is written with `verdict: UNAVAILABLE`, an
`unavailable_reason`, and prior unresolved X- blockers carried forward
unchanged (never silently dropped). The validator emits MEDIUM
`cross_model_critic_unavailable`, HIGH after 3 consecutive rounds.
Unparseable model output after one retry becomes a HIGH X-000 blocker.

PRIVACY: this sends run artifacts (plan, model code, report, ledger) to
OpenAI via the Codex CLI. Codex is confined to the run directory
(`-C <run_dir>`, read-only sandbox) and told not to read anything
outside it. main.py gates this behind its `--cross-model` flag.

Usage:
    python3 scripts/cross_model_critic.py <run_dir> --round N \\
        [--model gpt-6.1-sol] [--timeout-s 1800] [--codex codex]
    python3 scripts/cross_model_critic.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from typing import Any, Callable

import yaml

REVIEWER = "critique-crossmodel"
PREFIX = "X-"
YAML_NAME = "critique_crossmodel.yaml"
MD_NAME = "critique_crossmodel.md"
DEFAULT_MODEL = "gpt-6.1-sol"
DEFAULT_TIMEOUT_S = 1800

SEVERITIES = ["HIGH", "MEDIUM", "LOW"]
CATEGORIES = ["HARD_BLOCKER", "METHODS", "CAUSAL", "HYPOTHESES", "CITATIONS",
              "PRESENTATION", "DATA", "STRUCTURAL"]
TARGET_STAGES = ["PLAN", "DATA", "MODEL", "ANALYZE", "WRITE"]

# Strict JSON schema for `codex exec --output-schema` (structured-output
# rules: every property required, no additional properties).
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "files_reviewed", "structural_mismatch", "new_blockers",
                 "prior_rechecks"],
    "properties": {
        "summary": {"type": "string"},
        # Fail-closed (Phase 21 ζ fix): the critic must say what it read; a
        # review that opened no files is UNAVAILABLE, never PASS.
        "files_reviewed": {"type": "array", "items": {"type": "string"}},
        "structural_mismatch": {
            "type": "object",
            "additionalProperties": False,
            "required": ["detected", "description", "evidence_files"],
            "properties": {
                "detected": {"type": "boolean"},
                "description": {"type": "string"},
                "evidence_files": {"type": "array",
                                   "items": {"type": "string"}},
            },
        },
        "new_blockers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "category", "target_stage", "claim",
                             "evidence", "fix_requires"],
                "properties": {
                    "severity": {"type": "string", "enum": SEVERITIES},
                    "category": {"type": "string", "enum": CATEGORIES},
                    "target_stage": {"type": "string", "enum": TARGET_STAGES},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                    "fix_requires": {"type": "string"},
                },
            },
        },
        "prior_rechecks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "still_present", "notes"],
                "properties": {
                    "id": {"type": "string"},
                    "still_present": {"type": "boolean"},
                    "notes": {"type": "string"},
                },
            },
        },
    },
}

PROMPT_TEMPLATE = """\
You are an independent adversarial reviewer from a different model family
than the agents that built this modeling run. Your job is to find errors
that same-family critics tend to miss. Work ONLY inside the current
directory (the run directory). Do NOT read `.env`, anything in a parent
directory, or anything outside this directory. Do not modify files.

In `files_reviewed`, list every file you actually opened (relative paths).
If you could not open files (tool errors, sandbox denials), say so in the
summary and return `files_reviewed: []`; do not claim a clean review.

Review, clean-slate: plan.md, the model code (*.py here and under models/),
models/*.yaml, uncertainty_report.yaml, models/claims_ledger.yaml, any
allocation CSVs, and report.md if present. Run small read-only checks
(e.g. python snippets that call the model's functions) where that is
the fastest way to verify a suspicion.

Focus on:
  1. Math / logic errors in the model code (wrong formula, sign, index,
     time step, units, rates vs proportions, discounting).
  2. Denominator errors: an intervention's benefit computed on a
     different population than the one eligible for it / charged for it.
  3. Double counting (overlapping interventions, cases and deaths both
     credited, outputs summed across non-exclusive groups).
  4. Claims in report.md or the claims ledger that the artifacts on disk
     do not support (numbers, CIs, verdicts).
  5. Anything that would change which units get which intervention.
Report only defects you can point at with a file and line or a value.
HIGH = would change a headline number or recommendation; MEDIUM = real
but bounded; LOW = minor. Set structural_mismatch.detected only if the
delivered model cannot answer the research question as posed.

Research question: {question}
Current round: {round}
{prior_section}
Return JSON only, matching the provided schema. `new_blockers` must not
repeat a prior blocker; re-check each prior blocker in `prior_rechecks`.
"""


# ---------------------------------------------------------------------------
# Validator schema hook
# ---------------------------------------------------------------------------

def _validator_check(path: str, current_round: int) -> str | None:
    """Return None if the validator accepts `path`, else the error text."""
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import validate_critique_yaml as v  # noqa: E402
    try:
        v.validate_critique(path, REVIEWER, current_round)
        return None
    except v.SchemaError as e:
        return str(e)


# ---------------------------------------------------------------------------
# Prior state
# ---------------------------------------------------------------------------

def _load_prior(run_dir: str, current_round: int) -> dict:
    """Prior-round critique_crossmodel.yaml (or {} on round 1 / absent /
    unreadable). A file already stamped with the current round is a
    re-run of this round: use its own `carried_from` snapshot instead."""
    if current_round <= 1:
        return {}
    path = os.path.join(run_dir, YAML_NAME)
    try:
        with open(path) as f:
            doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return {}
    if not isinstance(doc, dict):
        return {}
    if doc.get("round") == current_round and isinstance(doc.get("carried_from"), dict):
        return doc["carried_from"]
    return doc


def _next_id(blockers: list[dict]) -> Callable[[], str]:
    nums = [int(m.group(1)) for b in blockers
            if (m := re.match(r"^X-(\d+)$", str(b.get("id", ""))))]
    counter = [max(nums, default=0)]

    def nxt() -> str:
        counter[0] += 1
        return f"X-{counter[0]:03d}"
    return nxt


def _question(run_dir: str) -> str:
    try:
        with open(os.path.join(run_dir, "metadata.json")) as f:
            meta = json.load(f)
        return meta.get("question") or meta.get("research_question") or ""
    except (OSError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Codex invocation
# ---------------------------------------------------------------------------

def _codex_version(codex: str, runner) -> str | None:
    try:
        p = runner([codex, "--version"], capture_output=True, text=True,
                   timeout=30)
        return (p.stdout or p.stderr).strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def run_codex(run_dir: str, prompt: str, model: str, timeout_s: int,
              codex: str = "codex", runner=subprocess.run) -> dict:
    """Invoke `codex exec` once. Returns {status, payload|reason, ...}.
    status: ok | unavailable | unparseable."""
    if runner is subprocess.run and shutil.which(codex) is None:
        return {"status": "unavailable", "reason": f"`{codex}` not found on PATH"}
    with tempfile.TemporaryDirectory() as td:
        schema_path = os.path.join(td, "schema.json")
        out_path = os.path.join(td, "last_message.json")
        with open(schema_path, "w") as f:
            json.dump(OUTPUT_SCHEMA, f)
        # Default read-only. Coiled containers can't run Codex's Linux sandbox
        # (bwrap needs network namespaces), so the replay harness sets
        # CROSS_MODEL_SANDBOX=danger-full-access on disposable VMs only, and
        # enforces integrity itself (workspace hashed before/after the review).
        sandbox = os.environ.get("CROSS_MODEL_SANDBOX", "read-only")
        if sandbox not in ("read-only", "workspace-write", "danger-full-access"):
            sandbox = "read-only"
        cmd = [codex, "exec", "-s", sandbox, "-C", os.path.abspath(run_dir),
               "-m", model, "--output-schema", schema_path, "-o", out_path,
               "--skip-git-repo-check", "--ephemeral", "-"]
        t0 = time.time()
        try:
            proc = runner(cmd, input=prompt, capture_output=True, text=True,
                          timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return {"status": "unavailable",
                    "reason": f"codex timed out after {timeout_s}s",
                    "duration_s": round(time.time() - t0, 1)}
        except OSError as e:
            return {"status": "unavailable", "reason": f"codex failed to start: {e}"}
        dur = round(time.time() - t0, 1)
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return {"status": "unavailable", "exit_status": proc.returncode,
                    "duration_s": dur,
                    "reason": f"codex exited {proc.returncode}: {' | '.join(tail)[:400]}"}
        try:
            with open(out_path) as f:
                raw = f.read()
        except OSError:
            raw = proc.stdout or ""
    payload = _parse_payload(raw)
    if payload is None:
        return {"status": "unparseable", "exit_status": 0, "duration_s": dur,
                "raw": raw[:2000]}
    return {"status": "ok", "exit_status": 0, "duration_s": dur,
            "payload": payload}


def _parse_payload(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    candidates = [raw]
    m = re.search(r"\{.*\}", raw, re.S)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            d = json.loads(c)
        except ValueError:
            continue
        if isinstance(d, dict):
            return d
    return None


# ---------------------------------------------------------------------------
# YAML assembly (deterministic; the model never writes the file)
# ---------------------------------------------------------------------------

def _norm_enum(val: Any, allowed: list[str], default: str) -> str:
    s = str(val or "").strip().upper()
    return s if s in allowed else default


def _norm_blocker(b: Any) -> dict | None:
    if not isinstance(b, dict):
        return None
    claim = str(b.get("claim") or "").strip()
    if not claim:
        return None
    return {
        "severity": _norm_enum(b.get("severity"), SEVERITIES, "MEDIUM"),
        "category": _norm_enum(b.get("category"), CATEGORIES, "METHODS"),
        "target_stage": _norm_enum(b.get("target_stage"), TARGET_STAGES, "MODEL"),
        "claim": claim,
        "evidence": str(b.get("evidence") or "").strip() or "(no evidence given)",
        "fix_requires": str(b.get("fix_requires") or "").strip() or "PATCH",
    }


def _require_review(run_dir: str, result: dict) -> dict:
    """Fail closed when the critic reviewed nothing.

    On Coiled, a broken codex sandbox made every tool call fail; the model
    still returned a schema-valid payload with no blockers, which became
    verdict PASS — an empty review indistinguishable from a clean one.
    Require at least one listed file that exists in the run directory.
    """
    if result.get("status") != "ok":
        return result
    listed = [str(f) for f in (result.get("payload") or {}).get("files_reviewed") or []]
    root = os.path.realpath(run_dir)

    def _in_run(f: str) -> bool:
        # Only files inside the run dir count: `/etc/hosts` or `../x` is not
        # a review of this run (Codex review of PR #14).
        p = os.path.realpath(f if os.path.isabs(f) else os.path.join(run_dir, f))
        return os.path.isfile(p) and os.path.commonpath([root, p]) == root
    real = [f for f in listed if _in_run(f)]
    if not real:
        return {**result, "status": "unavailable",
                "reason": ("critic reviewed no files" if not listed else
                           f"none of the {len(listed)} files the critic listed exist in the run dir")}
    return result


def build_doc(prior: dict, current_round: int, result: dict, *,
              model: str, codex_version: str | None, question: str) -> dict:
    """Assemble a schema-valid critique doc from the prior file and one
    codex result (status ok | unavailable | unparseable)."""
    prior_blockers = [b for b in (prior.get("blockers") or [])
                      if isinstance(b, dict) and str(b.get("id", "")).startswith(PREFIX)]
    prior_round = prior.get("round") if isinstance(prior.get("round"), int) else current_round - 1
    nxt = _next_id(prior_blockers)
    status = result.get("status")
    payload = result.get("payload") or {}

    rechecks = {}
    for r in payload.get("prior_rechecks") or []:
        if isinstance(r, dict) and r.get("id"):
            rechecks[str(r["id"])] = r

    blockers: list[dict] = []
    carried: list[dict] = []
    for b in prior_blockers:
        b = dict(b)
        if b.get("resolved") is True:
            blockers.append(b)            # history; no carried_forward entry
            continue
        if b["id"] == "X-000" and status == "ok":
            still, note = False, "cross-model output parsed this round"
        elif status == "ok":
            r = rechecks.get(b["id"])
            if r is None:
                still, note = True, "not re-checked by the cross-model critic; kept open"
            else:
                still = bool(r.get("still_present"))
                note = str(r.get("notes") or "").strip() or "re-checked"
        else:
            still, note = True, f"not re-checked: critic {status}"
        if not still:
            b.update(resolved=True, resolved_in_round=current_round,
                     resolved_evidence=note)
        blockers.append(b)
        carried.append({"id": b["id"], "prior_round": prior_round,
                        "still_present": still, "notes": note})

    if status == "ok":
        for nb in payload.get("new_blockers") or []:
            nb = _norm_blocker(nb)
            if nb is None:
                continue
            blockers.append({"id": nxt(), "first_seen_round": current_round,
                             "resolved": False, **nb})
    elif status == "unparseable" and not any(
            b["id"] == "X-000" and not b.get("resolved") for b in blockers):
        blockers.append({
            "id": "X-000", "severity": "HIGH", "category": "METHODS",
            "target_stage": "MODEL", "first_seen_round": current_round,
            "claim": "cross-model critic output invalid (unparseable after retry)",
            "evidence": (result.get("raw") or "")[:500] or "(empty output)",
            "fix_requires": "Re-run scripts/cross_model_critic.py next round.",
            "resolved": False,
        })

    sm_in = payload.get("structural_mismatch") if status == "ok" else None
    if isinstance(sm_in, dict) and sm_in.get("detected") is True:
        sm = {"detected": True,
              "description": str(sm_in.get("description") or "").strip()
              or "(no description given)",
              "evidence_files": [str(x) for x in (sm_in.get("evidence_files") or [])],
              "fix_requires": "RETHINK"}
    else:
        sm = {"detected": False}

    open_any = any(not b.get("resolved") for b in blockers)
    if status == "unavailable":
        verdict = "UNAVAILABLE"
    else:
        # A structural flag with no open blocker stays PASS: the
        # validator's REVISE rule needs an open blocker, and
        # structural_mismatch drives the decision on its own.
        verdict = "REVISE" if open_any else "PASS"

    history = [h for h in (prior.get("availability_history") or [])
               if isinstance(h, dict) and isinstance(h.get("round"), int)
               and h["round"] < current_round]
    history.append({"round": current_round,
                    "status": "UNAVAILABLE" if status == "unavailable" else "AVAILABLE"})

    doc: dict = {
        "reviewer": REVIEWER,
        "round": current_round,
        "verdict": verdict,
        "structural_mismatch": sm,
        "blockers": blockers,
        "carried_forward": carried if current_round > 1 else [],
        "producer": "scripts/cross_model_critic.py",
        "model": model,
        "codex_version": codex_version,
        "duration_s": result.get("duration_s"),
        "exit_status": result.get("exit_status"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "availability_history": history,
        "summary": str(payload.get("summary") or "").strip() if status == "ok" else "",
        "question": question,
    }
    if status == "unavailable":
        doc["unavailable_reason"] = result.get("reason") or "unknown"
    # Snapshot of the prior state so a same-round re-run is idempotent.
    if prior:
        doc["carried_from"] = {k: prior.get(k) for k in
                               ("round", "blockers", "availability_history")}
    return doc


def _render_md(doc: dict) -> str:
    lines = [f"# Cross-model critique — round {doc['round']}",
             "",
             f"Reviewer: {doc['model']} via Codex CLI ({doc.get('codex_version') or 'unknown version'}). "
             f"Verdict: **{doc['verdict']}**.",
             ""]
    if doc.get("unavailable_reason"):
        lines += [f"Critic unavailable: {doc['unavailable_reason']}", ""]
    if doc.get("summary"):
        lines += [doc["summary"], ""]
    if doc["structural_mismatch"].get("detected"):
        lines += ["## Structural mismatch", doc["structural_mismatch"]["description"], ""]
    open_b = [b for b in doc["blockers"] if not b.get("resolved")]
    lines.append(f"## Open blockers ({len(open_b)})")
    for b in open_b:
        lines.append(f"- **{b['id']}** [{b['severity']}/{b['category']} → {b['target_stage']}] "
                     f"{b['claim']}  \n  Evidence: {b['evidence']}  \n  Fix: {b['fix_requires']}")
    return "\n".join(lines) + "\n"


def write_outputs(run_dir: str, doc: dict) -> str | None:
    """Write YAML + MD atomically after validating with the validator's
    schema. Returns the schema error if validation still fails after the
    deterministic fallback (the fallback doc is written instead)."""
    yaml_path = os.path.join(run_dir, YAML_NAME)
    tmp = yaml_path + ".tmp"
    with open(tmp, "w") as f:
        yaml.safe_dump(doc, f, sort_keys=False, allow_unicode=True)
    err = _validator_check(tmp, doc["round"])
    if err is not None:
        # Deterministic fallback: keep prior open blockers, add X-000.
        fallback = dict(doc)
        fallback["blockers"] = [b for b in doc["blockers"] if b.get("id") != "X-000"]
        fallback["blockers"].append({
            "id": "X-000", "severity": "HIGH", "category": "METHODS",
            "target_stage": "MODEL", "first_seen_round": doc["round"],
            "claim": "cross-model critic output invalid (failed validator schema)",
            "evidence": err[:500], "fix_requires": "Re-run scripts/cross_model_critic.py.",
            "resolved": False})
        fallback["verdict"] = "REVISE"
        fallback["structural_mismatch"] = {"detected": False}
        fallback["carried_forward"] = [
            c for c in doc["carried_forward"]
            if any(b["id"] == c["id"] for b in fallback["blockers"])]
        with open(tmp, "w") as f:
            yaml.safe_dump(fallback, f, sort_keys=False, allow_unicode=True)
        doc = fallback
    os.replace(tmp, yaml_path)
    with open(os.path.join(run_dir, MD_NAME), "w") as f:
        f.write(_render_md(doc))
    return err


def run(run_dir: str, current_round: int, model: str = DEFAULT_MODEL,
        timeout_s: int = DEFAULT_TIMEOUT_S, codex: str = "codex",
        runner=subprocess.run) -> dict:
    prior = _load_prior(run_dir, current_round)
    open_prior = [b for b in (prior.get("blockers") or [])
                  if isinstance(b, dict) and not b.get("resolved")]
    if open_prior:
        listing = "\n".join(f"  - {b.get('id')} [{b.get('severity')}] {b.get('claim')}"
                            for b in open_prior)
        prior_section = ("Prior open blockers to re-check (report each in "
                         f"prior_rechecks):\n{listing}\n")
    else:
        prior_section = "No prior blockers.\n"
    question = _question(run_dir)
    prompt = PROMPT_TEMPLATE.format(question=question or "(see plan.md)",
                                    round=current_round,
                                    prior_section=prior_section)
    result = run_codex(run_dir, prompt, model, timeout_s, codex, runner)
    if result["status"] == "unparseable":
        retry = run_codex(run_dir, prompt + "\nReturn ONLY the JSON object.",
                          model, timeout_s, codex, runner)
        if retry["status"] != "unparseable":
            result = retry
    result = _require_review(run_dir, result)
    version = None if result["status"] == "unavailable" and "not found" in str(
        result.get("reason")) else _codex_version(codex, runner)
    doc = build_doc(prior, current_round, result, model=model,
                    codex_version=version, question=question)
    err = write_outputs(run_dir, doc)
    return {"status": result["status"], "verdict": doc["verdict"],
            "schema_fallback": err,
            "open_high": sum(1 for b in doc["blockers"]
                             if not b.get("resolved") and b.get("severity") == "HIGH")}


# ---------------------------------------------------------------------------
# Self-test (no real codex calls)
# ---------------------------------------------------------------------------

def _run_self_test() -> int:
    failures: list[str] = []

    def ok(cond: bool, msg: str) -> None:
        if not cond:
            failures.append(msg)

    class FakeProc:
        def __init__(self, rc=0, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def fake_runner(payloads: list, rc: int = 0, timeout: bool = False):
        calls = []

        def runner(cmd, **kw):
            calls.append(cmd)
            if cmd[-1] == "--version":
                return FakeProc(0, "codex-cli 0.0-test")
            if timeout:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            p = payloads.pop(0) if payloads else ""
            o = cmd[cmd.index("-o") + 1]
            with open(o, "w") as f:
                f.write(p if isinstance(p, str) else json.dumps(p))
            return FakeProc(rc, "", "boom" if rc else "")
        runner.calls = calls
        return runner

    def good_payload(new=None, rechecks=None, structural=False, files=("plan.md",)):
        return {"summary": "two issues", "files_reviewed": list(files),
                "structural_mismatch": {"detected": structural,
                                        "description": "wrong question" if structural else "",
                                        "evidence_files": ["model.py"] if structural else []},
                "new_blockers": new if new is not None else [
                    {"severity": "HIGH", "category": "METHODS", "target_stage": "MODEL",
                     "claim": "benefit uses total population", "evidence": "model.py:12",
                     "fix_requires": "use eligible population"},
                    {"severity": "MEDIUM", "category": "PRESENTATION", "target_stage": "WRITE",
                     "claim": "CI rounded inconsistently", "evidence": "report.md:40",
                     "fix_requires": "re-render"}],
                "prior_rechecks": rechecks or []}

    with tempfile.TemporaryDirectory() as d:
        with open(os.path.join(d, "metadata.json"), "w") as f:
            json.dump({"question": "Allocate a fixed budget across districts"}, f)
        with open(os.path.join(d, "plan.md"), "w") as f:
            f.write("# plan\n")

        # T0 (fail-closed): a schema-valid payload that reviewed nothing must
        # be UNAVAILABLE, not PASS; so must one listing only nonexistent files.
        open(os.path.join(d, "outside.txt"), "w").write("x")
        for i, (files, why) in enumerate((((), "reviewed no files"), (("ghost.py",), "none of the 1 files"),
                                          (("/etc/hosts", "../outside.txt"), "none of the 2 files"))):
            sub = os.path.join(d, f"t0_{i}"); os.makedirs(sub)
            res = run(sub, 1, runner=fake_runner([good_payload(new=[], files=files)]))
            with open(os.path.join(sub, "critique_crossmodel.yaml")) as f:
                t0 = yaml.safe_load(f)
            ok(res["verdict"] == "UNAVAILABLE" and why in (t0.get("unavailable_reason") or ""),
               f"T0 empty review must be UNAVAILABLE ({why}): {res}, {t0.get('unavailable_reason')}")

        # T1: round 1 success → schema-valid, X-001/X-002, REVISE, sandbox flags.
        r = fake_runner([good_payload()])
        out = run(d, 1, runner=r)
        doc = yaml.safe_load(open(os.path.join(d, YAML_NAME)))
        ok(out["status"] == "ok" and doc["verdict"] == "REVISE", f"T1 verdict {doc['verdict']}")
        ok([b["id"] for b in doc["blockers"]] == ["X-001", "X-002"], "T1 ids")
        ok(_validator_check(os.path.join(d, YAML_NAME), 1) is None, "T1 validator schema")
        cmd = [c for c in r.calls if "exec" in c][0]
        ok("read-only" in cmd and "-C" in cmd and cmd[cmd.index("-C") + 1] == os.path.abspath(d),
           "T1 read-only sandbox confined to run dir")
        ok(doc["model"] == DEFAULT_MODEL and doc["codex_version"] == "codex-cli 0.0-test",
           "T1 provenance recorded")
        ok(os.path.exists(os.path.join(d, MD_NAME)), "T1 md written")

        # T2: round 2 — X-001 resolved, X-002 still present, one new → carried_forward valid.
        r = fake_runner([good_payload(
            new=[{"severity": "LOW", "category": "DATA", "target_stage": "DATA",
                  "claim": "new low issue", "evidence": "data.csv", "fix_requires": "note"}],
            rechecks=[{"id": "X-001", "still_present": False, "notes": "fixed in model.py:12"},
                      {"id": "X-002", "still_present": True, "notes": "still off"}])])
        run(d, 2, runner=r)
        doc = yaml.safe_load(open(os.path.join(d, YAML_NAME)))
        by = {b["id"]: b for b in doc["blockers"]}
        ok(by["X-001"]["resolved"] and by["X-001"]["resolved_in_round"] == 2, "T2 X-001 resolved")
        ok(by["X-002"]["first_seen_round"] == 1 and not by["X-002"]["resolved"],
           "T2 first_seen preserved")
        ok("X-003" in by, "T2 new id continues numbering")
        ok({c["id"] for c in doc["carried_forward"]} == {"X-001", "X-002"}, "T2 carried_forward")
        ok(_validator_check(os.path.join(d, YAML_NAME), 2) is None, "T2 validator schema")

        # T3: round 3 unavailable (non-zero exit) → UNAVAILABLE, prior open kept open.
        r = fake_runner([""], rc=1)
        out = run(d, 3, runner=r)
        doc = yaml.safe_load(open(os.path.join(d, YAML_NAME)))
        ok(doc["verdict"] == "UNAVAILABLE" and doc.get("unavailable_reason"), "T3 UNAVAILABLE")
        ok(any(b["id"] == "X-002" and not b["resolved"] for b in doc["blockers"]),
           "T3 prior open blocker not dropped")
        ok(_validator_check(os.path.join(d, YAML_NAME), 3) is None, "T3 validator schema")
        ok(doc["availability_history"][-1] == {"round": 3, "status": "UNAVAILABLE"}, "T3 history")

        # T4: timeout → UNAVAILABLE.
        run(d, 4, runner=fake_runner([], timeout=True))
        doc = yaml.safe_load(open(os.path.join(d, YAML_NAME)))
        ok(doc["verdict"] == "UNAVAILABLE" and "timed out" in doc["unavailable_reason"], "T4 timeout")
        ok([h["status"] for h in doc["availability_history"]][-2:] == ["UNAVAILABLE"] * 2,
           "T4 consecutive history")

        # T5: unparseable twice → HIGH X-000; then next round success resolves it.
        run(d, 5, runner=fake_runner(["not json", "still not json"]))
        doc = yaml.safe_load(open(os.path.join(d, YAML_NAME)))
        x0 = [b for b in doc["blockers"] if b["id"] == "X-000"]
        ok(x0 and x0[0]["severity"] == "HIGH" and not x0[0]["resolved"], "T5 X-000 HIGH")
        ok(_validator_check(os.path.join(d, YAML_NAME), 5) is None, "T5 validator schema")
        run(d, 6, runner=fake_runner([good_payload(new=[], rechecks=[
            {"id": "X-002", "still_present": False, "notes": "fixed"},
            {"id": "X-003", "still_present": False, "notes": "fixed"}])]))
        doc = yaml.safe_load(open(os.path.join(d, YAML_NAME)))
        ok(all(b["resolved"] for b in doc["blockers"]), "T6 all resolved incl. X-000")
        ok(doc["verdict"] == "PASS", f"T6 PASS got {doc['verdict']}")
        ok(_validator_check(os.path.join(d, YAML_NAME), 6) is None, "T6 validator schema")

        # T7: same-round re-run is idempotent (uses carried_from snapshot).
        run(d, 6, runner=fake_runner([good_payload(new=[], rechecks=[])]))
        doc2 = yaml.safe_load(open(os.path.join(d, YAML_NAME)))
        ok([b["id"] for b in doc2["blockers"]] == [b["id"] for b in doc["blockers"]],
           "T7 idempotent re-run")
        ok(_validator_check(os.path.join(d, YAML_NAME), 6) is None, "T7 validator schema")

        # T8: structural mismatch → detected with fix_requires RETHINK.
        run(d, 7, runner=fake_runner([good_payload(new=[], structural=True)]))
        doc = yaml.safe_load(open(os.path.join(d, YAML_NAME)))
        ok(doc["structural_mismatch"] == {"detected": True, "description": "wrong question",
                                          "evidence_files": ["model.py"],
                                          "fix_requires": "RETHINK"}, "T8 structural")
        ok(_validator_check(os.path.join(d, YAML_NAME), 7) is None, "T8 validator schema")

        # T9: enum normalization of sloppy output.
        nb = _norm_blocker({"severity": "high", "category": "weird", "target_stage": "x",
                            "claim": "c", "evidence": "", "fix_requires": ""})
        ok(nb["severity"] == "HIGH" and nb["category"] == "METHODS"
           and nb["target_stage"] == "MODEL", "T9 normalization")

    # T10: codex absent from PATH → unavailable without spawning.
    res = run_codex(".", "p", DEFAULT_MODEL, 5, codex="definitely-not-codex-xyz")
    ok(res["status"] == "unavailable" and "not found" in res["reason"], "T10 missing binary")

    if failures:
        for m in failures:
            print(f"FAIL: {m}")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("run_dir", nargs="?")
    p.add_argument("--round", type=int)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--timeout-s", type=int, default=DEFAULT_TIMEOUT_S)
    p.add_argument("--codex", default="codex")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()
    if args.self_test:
        return _run_self_test()
    if not args.run_dir or args.round is None:
        p.error("run_dir and --round are required")
    if not os.path.isdir(args.run_dir):
        print(f"ERROR: {args.run_dir} is not a directory", file=sys.stderr)
        return 2
    out = run(args.run_dir, args.round, args.model, args.timeout_s, args.codex)
    print(json.dumps(out, indent=2))
    # Exit 0 whenever the file was written: availability is reported in
    # the YAML and gated by the validator, not by this exit code.
    return 0


if __name__ == "__main__":
    sys.exit(main())
