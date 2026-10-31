"""Finishing a run: the final gate, status, report restore, store, model card and write-up.

Phase 48 γ moved this code out of main.py unchanged; main.py imports every
name back, so `main.<name>` (the self-tests, scripts/sdk_scenarios.py) still
works. Tested by `python3 main.py --self-test`.
"""

import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
)
from agents import build_agents, create_hooks, smoke_settings
from agents import enforcement
from agents.run_session import LEAD_TOOLS, log_message
from agents.run_budget import BUDGET_LEDGER, charge_session, conversation_cost, reconcile_conversation_cost, remaining_budget, write_budget_ledger

REPO_ROOT = enforcement.REPO_ROOT
VALIDATOR = os.path.join(REPO_ROOT, "scripts", "validate_critique_yaml.py")


# Terminal statuses that count as success (exit 0). Everything else is
# non-zero so cron / CI wrappers can't mistake a non-accepted run for a
# good one. See determine_status() for the full list.
SUCCESS_STATUSES = {"completed", "completed_with_report_restored"}
# Pipeline finished and wrote a report, but the final gate did not ACCEPT.
PARTIAL_STATUSES = {"completed_scope_declared", "completed_gate_not_accepted",
                    "completed_gate_error", "completed_gate_not_run",
                    "completed_no_report", "run_failed", "budget_exhausted",
                    "awaiting_signoff"}   # Phase 33: resumable with --resume once signed


_HARNESS_LOCK: dict = {}   # Phase 34 ε: modes of the harness files locked for this run (finalize_run restores them)


def detect_current_round(run_path: str) -> int | None:
    """Highest `round:` across critique_*.yaml (the gate's own input),
    falling back to pipeline_state.yaml."""
    import yaml
    rounds = []
    for path in glob.glob(os.path.join(run_path, "critique_*.yaml")):
        try:
            with open(path) as f:
                doc = yaml.safe_load(f)
            r = doc.get("round") if isinstance(doc, dict) else None
            if isinstance(r, int):
                rounds.append(r)
        except Exception:
            continue
    if rounds:
        return max(rounds)
    try:
        with open(os.path.join(run_path, "pipeline_state.yaml")) as f:
            state = yaml.safe_load(f) or {}
        r = state.get("current_round")
        return r if isinstance(r, int) else None
    except Exception:
        return None


PIPELINE_FINAL_BUDGET_S = 3600   # S1: the declared pipeline's rebuild before a final gate


def _rebuild_derived(run_path: str, integrity=None) -> None:
    """Phase 39 β: regenerate stale figures before a final gate; S1: first rebuild the
    run's declared pipeline (models/pipeline.yaml) as the harness. Never raises."""
    try:
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import derived_rebuild
        if integrity is not None and os.path.exists(os.path.join(run_path, derived_rebuild.pipeline.SPEC)):
            try:
                rep = derived_rebuild.pipeline.harness_build(run_path, integrity, PIPELINE_FINAL_BUDGET_S)
                print("[PIPELINE] " + derived_rebuild.pipeline.summary_line(rep), flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"[PIPELINE] failed: {type(e).__name__}: {e}", flush=True)
        print("[REBUILD] " + derived_rebuild.summary_line(derived_rebuild.rebuild(run_path, integrity=integrity)),
              flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[REBUILD] failed: {type(e).__name__}: {e}", flush=True)


def run_final_gate(run_path: str, max_rounds: int,
                   validator: str = VALIDATOR, timeout_s: int = 900) -> dict | None:
    """Phase 21 α: re-run the STAGE 7 validator after the session ends.

    The lead was supposed to re-run it after WRITE, but nothing enforced
    that, so post-WRITE HIGHs (coherence, sufficiency) never reached a
    gate. Returns None when there are no critique YAMLs to gate on;
    otherwise a dict with action / exit_code / counts. Writes
    final_gate.json and final_gate_stderr.txt into the run dir.
    """
    current_round = detect_current_round(run_path)
    if current_round is None:
        return None
    cmd = [sys.executable, validator, run_path,
           "--max-rounds", str(max_rounds), "--current-round", str(current_round),
           "--spec-compliance", "--parameter-registry", "--rigor-artifacts",
           "--json"]
    # Phase 21 β: --post-write makes coherence / sufficiency / writer-QA
    # HIGHs blocking. Only meaningful once a report exists; without one
    # determine_status() reports completed_no_report anyway.
    if os.path.exists(os.path.join(run_path, "report.md")):
        cmd.append("--post-write")
    gate = {"current_round": current_round, "max_rounds": max_rounds,
            "ran_at": datetime.now().isoformat(), "action": None}
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True,
                              text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as e:
        gate.update(exit_code=None, error=f"{type(e).__name__}: {e}")
        return gate
    gate["exit_code"] = proc.returncode
    try:
        with open(os.path.join(run_path, "final_gate_stderr.txt"), "w") as f:
            f.write(proc.stderr)
    except OSError:
        pass
    decision = None
    out = proc.stdout.strip()
    if out:
        try:
            decision = json.loads(out[out.index("{"):])
        except (ValueError, json.JSONDecodeError):
            decision = None
    if isinstance(decision, dict):
        gate["action"] = decision.get("action")
        if "schema_errors" in decision:
            gate["schema_errors"] = decision["schema_errors"]
        unresolved = decision.get("unresolved_high")
        if isinstance(unresolved, list):
            gate["n_unresolved_high"] = len(unresolved)
        try:
            with open(os.path.join(run_path, "final_gate.json"), "w") as f:
                json.dump(decision, f, indent=2)
        except OSError:
            pass
    else:
        gate["error"] = (proc.stderr.strip().splitlines() or ["no JSON on stdout"])[-1][:500]
    return gate


def determine_status(session_outcome: str, gate: dict | None, has_report: bool,
                     report_restored: bool, harness_diff: dict | None,
                     integrity_violations: int = 0) -> str:
    """Single source of truth for metadata.json["status"].

    Phase 11 Commit η (F1) introduced status discrimination; Phase 21 α
    stops equating "the stream ended" with "completed". Order matters:
    tampering beats everything, then session-level failures, then the
    final gate's verdict on what was delivered.
    """
    if harness_diff and enforcement.harness_changed(harness_diff):
        return "harness_tampered"
    if integrity_violations:
        # Phase 23 α: decided from the harness's in-memory record, so a
        # report file an agent made unwritable cannot hide it.
        return "artifact_tampered"
    if session_outcome != "session_completed":
        return session_outcome
    action = (gate or {}).get("action")
    if action == "RUN_FAILED":
        return "run_failed"
    if not has_report:
        return "completed_no_report"
    if gate is None:
        return "completed_gate_not_run"
    if action == "ACCEPT":
        return "completed_with_report_restored" if report_restored else "completed"
    if action == "DECLARE_SCOPE":
        return "completed_scope_declared"
    if action is None or action == "VALIDATOR_ERROR":
        return "completed_gate_error"
    return "completed_gate_not_accepted"


def exit_code_for(status: str) -> int:
    if status in SUCCESS_STATUSES:
        return 0
    if status == "interrupted":
        return 130
    if status in PARTIAL_STATUSES:
        return 2
    return 1


def restore_report_if_missing(run_path: str) -> bool:
    """Phase 12 δ safety net, Phase 21 α rewiring: the PostToolUse hook
    snapshots report.md every time it changes, so if it is missing at
    the end but a snapshot exists, restore it. (The Phase 12 version
    only snapshotted at run end, after the disappearance it guarded
    against, and keyed on writer_qa CLEAN, which writer_qa also
    reported for a missing report.)"""
    report_path = os.path.join(run_path, "report.md")
    snapshot_path = os.path.join(run_path, "report.snapshot.md")
    if os.path.exists(report_path):
        return False
    if os.path.exists(os.path.join(run_path, "manuscript.md")):
        # Phase 25: the writer's draft exists but the render step never ran.
        # Build report.md from it (render_claims falls back to the draft
        # itself when references cannot be resolved).
        try:
            r = subprocess.run([sys.executable, os.path.join(REPO_ROOT, "scripts", "render_claims.py"), run_path],
                               capture_output=True, text=True, timeout=600)
        except (subprocess.TimeoutExpired, OSError) as e:
            r = subprocess.CompletedProcess([], 1, "", f"{type(e).__name__}: {e}")
        if os.path.exists(report_path):
            print("[WARNING] report.md was missing at run end; rendered from manuscript.md.",
                  file=sys.stderr, flush=True)
            return True
        # Rendering failed (e.g. no claims ledger): the draft itself is the
        # report; writer QA and the gate then judge it as written.
        try:
            shutil.copyfile(os.path.join(run_path, "manuscript.md"), report_path)
            print(f"[WARNING] report.md was missing at run end; rendering failed "
                  f"({(r.stdout + r.stderr).strip()[-300:]}); copied manuscript.md.",
                  file=sys.stderr, flush=True)
            return True
        except OSError as e:
            print(f"[ERROR] report.md missing; copying manuscript.md failed: {e}",
                  file=sys.stderr, flush=True)
    if not os.path.exists(snapshot_path):
        return False
    try:
        shutil.copyfile(snapshot_path, report_path)
    except OSError as e:
        print(f"[ERROR] report.md missing AND restore failed: {e}",
              file=sys.stderr, flush=True)
        return False
    print(f"[WARNING] report.md was missing at run end; restored from "
          f"{snapshot_path}. Investigate trace.jsonl for the deletion path.",
          file=sys.stderr, flush=True)
    return True


VERDICT_BANNER_MARK = "<!-- harness: final gate verdict -->"


def add_verdict_banner(run_path: str, gate: dict | None) -> bool:
    """If the final gate did not ACCEPT, make sure report.md opens with the
    verdict (a report written before the final rejection would otherwise
    read as accepted). Harness-written and labelled as such; idempotent."""
    rp = os.path.join(run_path, "report.md")
    action = (gate or {}).get("action")
    if not os.path.exists(rp) or action == "ACCEPT":
        return False
    body = open(rp).read()
    if VERDICT_BANNER_MARK in body:
        return False
    n = gate.get("unresolved_high")
    n = len(n) if isinstance(n, list) else gate.get("n_unresolved_high")
    banner = (f"{VERDICT_BANNER_MARK}\n> **Status: not accepted by the final gate** "
              f"(verdict: {action or gate.get('error') or 'not run'}"
              + (f"; {n} unresolved blocking finding(s)" if n is not None else "")
              + "; see `final_gate.json`). Added by the harness after the report was written: "
              "read the findings below as what was built, not as a recommendation.\n\n")
    open(rp, "w").write(banner + body)
    return True


# methods_critic / cross_model decide which critiques the validator requires
# (Codex review of PR #15: flipping methods_critic dropped a requirement).
_PROTECTED_META = ("question", "started", "run_dir", "methods_critic", "cross_model")


def _restore_protected_meta(metadata_path: str, original_meta: dict) -> bool:
    """The planner may add decision_year to metadata.json; it may not change
    the question spec-compliance is checked against. Put protected fields
    back (recording what was there) and rewrite the file."""
    try:
        with open(metadata_path) as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            raise ValueError("metadata.json is not an object")
    except (OSError, ValueError):
        # Missing or corrupted: rebuild it from the originals rather than
        # let finalization write a metadata.json without the question.
        meta = {"restored_fields": {"_metadata_unreadable": True}}
    if not isinstance(meta.get("restored_fields"), dict):
        meta["restored_fields"] = {}      # null / wrong type must not crash finalization
    changed = not os.path.exists(metadata_path) or "_metadata_unreadable" in meta["restored_fields"]
    for key in _PROTECTED_META:
        if key in original_meta and meta.get(key) != original_meta[key]:
            meta["restored_fields"][key] = meta.get(key)
            meta[key] = original_meta[key]
            changed = True
    if changed:
        with open(metadata_path, "w") as f:
            json.dump(meta, f, indent=2)
    return changed


def finalize_run(run_path, run_dir_rel, run_start, max_rounds, session_outcome,
                 session_num, session_id, session_records, harness_before,
                 harness_prior_diff, run_gate: bool = True,
                 original_meta: dict | None = None, integrity=None) -> str:
    """Restore report if needed, run the final gate, check the harness
    hash, and write metadata.json. Runs in a `finally`, so it executes on
    Ctrl-C and on unexpected exceptions too."""
    original_meta = original_meta or {}
    # Last integrity scan FIRST: pending tool changes are judged against the
    # previous baseline before the harness writes anything (Copilot, PR #15).
    # Phase 39 β (Codex): regenerate stale figures while the integrity watch is
    # still on, so anything a figure script changes is judged by the final scan.
    if run_gate:
        _rebuild_derived(run_path, integrity)
    if integrity is not None:
        integrity.final_check()
        integrity.stop_watch()
    report_restored = restore_report_if_missing(run_path)
    if report_restored and integrity is not None:
        for rel in ("report.md", "report.unrendered.md", "models/claims_ledger.yaml", "claims_ledger.yaml"):
            integrity.harness_wrote(rel)
    has_report = os.path.exists(os.path.join(run_path, "report.md"))
    # Restore the protected fields BEFORE the gate: the validator reads the
    # question from metadata.json (Codex review of PR #14: a planner edit to
    # the question would otherwise be gated, then silently undone).
    if _restore_protected_meta(os.path.join(run_path, "metadata.json"), original_meta) \
            and integrity is not None:
        integrity.harness_wrote("metadata.json")
    if integrity is not None and integrity.open_events:
        print(f"[INTEGRITY] {len(integrity.open_events)} unresolved change(s) to protected "
              f"run artifacts; see {os.path.join(run_dir_rel, 'artifact_integrity.json')}",
              flush=True)

    gate = None
    # A budget stop is still gated: the report shows what the money bought.
    if run_gate:          # Phase 29: also after max_sessions / crashes: the verdict is evidence
        print("\nRunning final STAGE 7 gate...", flush=True)
        gate = run_final_gate(run_path, max_rounds)
        if gate is None:
            # No critique round was reached: record that explicitly (Codex, Phase 29).
            gate = {"action": None, "error": "no critique round was reached; nothing to gate",
                    "ran_at": datetime.now().isoformat()}
            json.dump(gate, open(os.path.join(run_path, "final_gate.json"), "w"), indent=2)
        add_verdict_banner(run_path, gate)
        # Phase 44 α (E1): the model card, from the gated artifacts, before storage.
        write_model_card(run_path, integrity)
        # The card ran the run's code: put back protected metadata it may have changed
        # (any change is already an integrity violation).
        _restore_protected_meta(os.path.join(run_path, "metadata.json"), original_meta)

    harness_diff = enforcement.diff_harness(harness_before, enforcement.hash_harness())
    enforcement.unlock_harness(_HARNESS_LOCK)
    status = determine_status(session_outcome, gate, has_report,
                              report_restored, harness_diff,
                              integrity_violations=len(integrity.open_events) if integrity else 0)

    elapsed = (datetime.now() - run_start).total_seconds()
    metadata_path = os.path.join(run_path, "metadata.json")
    try:
        with open(metadata_path) as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            raise ValueError("metadata.json is not an object")
    except (OSError, ValueError):
        meta = {}
    # Phase 11 Commit η (F1): `completed` is the timestamp the run loop
    # finished (kept for backward compat with existing readers).
    # `status` is the authoritative discriminator; see determine_status()
    # for the values. Automation should consult `status` (or the process
    # exit code), not the presence of `completed`.
    meta["completed"] = datetime.now().isoformat()
    meta["status"] = status
    meta["session_outcome"] = session_outcome
    meta["elapsed_s"] = elapsed
    meta["sessions"] = session_num
    meta["has_report"] = has_report
    meta["report_restored"] = report_restored
    meta["final_gate"] = gate
    if integrity is not None:
        meta["artifact_integrity_violations"] = len(integrity.open_events)
        meta["artifact_integrity_resolved"] = len(integrity.events) - len(integrity.open_events)
    if session_records:
        meta.setdefault("session_results", []).extend(session_records)
    meta["total_cost_usd"] = conversation_cost(meta.get("session_results", []))
    if enforcement.harness_changed(harness_diff):
        meta["harness_diff"] = harness_diff
    if harness_prior_diff and enforcement.harness_changed(harness_prior_diff):
        meta["harness_changed_since_last_invocation"] = harness_prior_diff
    if session_id:
        meta["session_id"] = session_id
    with open(metadata_path, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"\n{'='*60}", flush=True)
    print(f"RUN {status.upper()}", flush=True)
    print(f"  Duration: {elapsed:.0f}s ({elapsed/60:.1f} min)", flush=True)
    print(f"  Sessions: {session_num}", flush=True)
    print(f"  Report: {'written' if has_report else 'not written'}", flush=True)
    if gate is not None:
        print(f"  Final gate: {gate.get('action') or gate.get('error')}", flush=True)
    if meta.get("total_cost_usd") is not None:
        print(f"  Cost: ${meta['total_cost_usd']:.2f}", flush=True)
    if status == "harness_tampered":
        print(f"  HARNESS CHANGED DURING RUN: {harness_diff}", flush=True)
    if status == "artifact_tampered":
        print(f"  PROTECTED RUN ARTIFACTS CHANGED OUTSIDE THEIR WRITERS: "
              f"{[e['file'] for e in integrity.open_events]}", flush=True)
    print(f"  Results: {run_dir_rel}/", flush=True)
    # Phase 29: the run's post-mortem (autopsy.md / autopsy.json), before storage
    # so it travels with the results; plus one row in the cross-run register.
    try:
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        import run_autopsy
        _a = run_autopsy.write(run_path)
        print(f"  Autopsy: primary layer {_a.get('primary_layer') or 'none (accepted)'}", flush=True)
        _row = run_autopsy.register_row(_a)
        # Lessons ledger: known error classes that came back, plus this run's
        # expert checklist (lessons_check.md).
        import lessons
        _row["lessons"] = lessons.register_fields(run_path)
        print(f"  Lessons: {_row['lessons']}", flush=True)
        import results_store
        if results_store.configured_account():
            _rp = os.path.join(run_path, "register_row.json")
            json.dump(_row, open(_rp, "w"), indent=1, default=str)
            results_store.upload_retry(_rp, f"outputs/register/{_a['run']}.json", attempts=2)
    except Exception as e:  # noqa: BLE001 — never block finalize
        print(f"[WARNING] autopsy/register failed: {type(e).__name__}: {e}", flush=True)
    stored = store_run_results(run_path)
    if stored:
        print(f"  Stored: {stored}", flush=True)
    print(f"{'='*60}", flush=True)
    return status


def _store_blob_name(run_path: str) -> str:
    name = os.path.basename(os.path.normpath(run_path))
    return f"outputs/{os.environ.get('MA_STORAGE_PREFIX', 'runs')}/{name}.tar.gz"


def write_model_card(run_path: str, integrity=None, timeout: float | None = None) -> dict | None:
    """Phase 44 α (E1): model_card.yaml / model_card.md, written by the harness
    from the run's own artifacts (scripts/model_card.py; the model runs in a
    separate process). Recorded as harness writes so the integrity record
    accepts them. Never blocks finalize."""
    card = None
    own = ("model_card.yaml", "model_card.md")
    # The card runs the run's own code after the final integrity check: anything it
    # changes in the run's watched files besides the card is a violation (Codex review).
    guard = integrity.harness_runs_code("model_card", allowed=own) if integrity is not None \
        else contextlib.nullcontext()
    try:
        with guard:
            sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
            import model_card
            import results_store
            card = model_card.write(
                run_path,
                timeout=timeout or float(os.environ.get("MA_MODEL_CARD_TIMEOUT_S", model_card.DEFAULT_TIMEOUT_S)),
                stored_blob=_store_blob_name(run_path) if results_store.configured_account() else None,
                run_commit=model_card._git_head())
        print(f"  Model card: {card['status']}"
              + "".join(f"\n    - {r}" for r in card["not_usable_reasons"][:3]), flush=True)
    except Exception as e:  # noqa: BLE001 — never block finalize
        print(f"[WARNING] model card failed: {type(e).__name__}: {e}", flush=True)
    if integrity is not None:
        for rel in ("model_card.yaml", "model_card.md"):
            if os.path.exists(os.path.join(run_path, rel)):
                integrity.harness_wrote(rel)
    return card


def store_run_results(run_path: str, service=None) -> str | None:
    """Phase 26: when a storage account is configured ($MA_STORAGE_ACCOUNT or
    $COILED_STORAGE_ACCOUNT), upload the finished run directory to
    outputs/<prefix>/<run name>.tar.gz so the result never depends on someone
    collecting it from the VM. Answer keys and credential files are left out
    and API keys redacted. The outcome is recorded in metadata.json
    (`stored` / `store_error`); a failure is loud but never fatal."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    import results_store
    account = results_store.configured_account()
    if not account and service is None:
        return None
    name = os.path.basename(os.path.normpath(run_path))
    blob, err = None, None
    try:
        secrets = [os.environ.get(k, "") for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")]
        data, skipped = results_store.archive_dir(run_path, name, secrets)
        tmp = os.path.join(os.path.dirname(os.path.normpath(run_path)), f".{name}.store.tar.gz")
        with open(tmp, "wb") as f:
            f.write(data)
        blob = _store_blob_name(run_path)
        results_store.upload_retry(tmp, blob, account, service=service)
        os.remove(tmp)
    except Exception as e:  # noqa: BLE001 — the local copy remains; say so loudly
        blob, err = None, f"{type(e).__name__}: {e}"[:500]
        print(f"[ERROR] RUN RESULTS NOT STORED (only the VM copy exists): {err}", file=sys.stderr, flush=True)
    try:
        mp = os.path.join(run_path, "metadata.json")
        meta = json.load(open(mp)) if os.path.exists(mp) else {}
        meta.update({"stored": blob, "store_error": err})
        json.dump(meta, open(mp, "w"), indent=2)
    except (OSError, ValueError):
        pass
    return blob


WRITEUP_PROMPT = """The modeling sessions have ended (outcome: {outcome}) without a report.
Spawn the `writer` agent exactly once for run directory {run_dir}. Tell it:

- This is the run's final write-up. The final gate's verdict is in
  {run_dir}/final_gate.json (action: {action}; unresolved blocking findings:
  {n_high}). If the action is not ACCEPT, the report's first section is
  "Status: not accepted by the final gate": the verdict and each unresolved
  blocking finding in plain words. The model is then presented as what was
  built and where it fell short, never as a recommendation; any allocation
  is labelled illustrative.
- Write the draft to {run_dir}/manuscript.md from the artifacts that exist
  (plan, data, models, results, critiques). Missing pieces are stated as
  missing, not invented.

Do nothing else; the harness renders report.md afterwards."""


def _harness_wrote_gate_outputs(integrity) -> None:
    """The validator (run by the harness) rewrites its own outputs: record them
    as harness writes so they are not flagged as tampering (Codex, Phase 29)."""
    if integrity is None:
        return
    from agents.integrity import SCRIPT_PRODUCES
    for rel in set(SCRIPT_PRODUCES.get("validate_critique_yaml.py", ())) | {
            "final_gate.json", "final_gate_stderr.txt", "metadata.json", BUDGET_LEDGER}:
        integrity.harness_wrote(rel)


async def final_writeup(run_path, run_dir_rel, question, max_rounds, budget, cap, session_num,
                        outcome, integrity=None, query_fn=None, live_state=None) -> dict:
    """Phase 29: when the modeling sessions end without a report (final-round
    RUN_FAILED, budget, sessions, crash), run the final gate and one writer
    session paid from the write-up reserve, so every run leaves a report that
    states its verdict. Never raises into finalize."""
    info = {"ran": False}
    try:
        _rebuild_derived(run_path, integrity)
        gate = run_final_gate(run_path, max_rounds) or {}
        _harness_wrote_gate_outputs(integrity)
        n_high = len(gate.get("unresolved_high") or []) if isinstance(gate.get("unresolved_high"), list) \
            else gate.get("n_unresolved_high")
        left = remaining_budget(cap, budget["sessions"])
        if left is not None and left <= 0:
            info.update(skipped="no budget left for the write-up")
            return info
        agents = {"writer": build_agents(question=question, run_dir=run_dir_rel)["writer"]}
        trace = open(os.path.join(run_path, "trace.jsonl"), "a")
        hooks = create_hooks(run_path, trace, datetime.now(), registered_agents={"writer"},
                             max_turns={"writer": agents["writer"].maxTurns}, integrity=integrity,
                             hold_writer=False)   # the harness's own write-up is never held
        options = ClaudeAgentOptions(
            system_prompt="You are the harness's final write-up step. Follow the instructions literally.",
            agents=agents, model=(smoke_settings() or {}).get("model", enforcement.MODEL_DEEP),
            max_buffer_size=enforcement.MAX_BUFFER_SIZE, settings=enforcement.SESSION_SETTINGS, tools=LEAD_TOOLS, allowed_tools=LEAD_TOOLS,
            permission_mode="bypassPermissions", setting_sources=["project"], hooks=hooks,
            cwd=REPO_ROOT, env=enforcement.filtered_env(dict(os.environ), False), max_turns=8)
        if left is not None:
            options.max_budget_usd = left
        prompt = WRITEUP_PROMPT.format(outcome=outcome, run_dir=run_dir_rel,
                                       action=gate.get("action") or gate.get("error") or "not run",
                                       n_high=n_high if n_high is not None else "unknown")
        print(f"\nFINAL WRITE-UP (gate: {gate.get('action')}; budget left ${left})", flush=True)
        result, sid, err = None, None, None
        try:
            async for m in (query_fn or query)(prompt=prompt, options=options):
                log_message(m, trace)
                if live_state is not None:
                    live_state.on_message(m)         # stage snapshot of the writer, live status
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    sid = (m.data or {}).get("session_id") or sid
                elif isinstance(m, ResultMessage):
                    result, sid = m, m.session_id or sid
        except Exception as e:  # noqa: BLE001 — still charged below, then recorded
            err = f"{type(e).__name__}: {e}"[:300]
        source = "final write-up"
        if result is None and sid:
            result = await reconcile_conversation_cost(options, sid, query_fn=query_fn)
            source = "final write-up (crashed; cost read back)"
        # unreadable cost: the whole remaining allowance (cap holds)
        charge_session(budget, session_num, result, left, session_id=sid, elapsed_s=None, source=source)
        write_budget_ledger(run_path, budget, cap, integrity)
        if err:
            info["writer_error"] = err
        info.update(ran=True, gate_action=gate.get("action"),
                    manuscript=os.path.exists(os.path.join(run_path, "manuscript.md")))
    except Exception as e:  # noqa: BLE001 — finalize still runs; the gap is recorded
        info.update(error=f"{type(e).__name__}: {e}"[:300])
        print(f"[ERROR] final write-up failed: {info['error']}", flush=True)
    try:
        mp = os.path.join(run_path, "metadata.json")
        meta = json.load(open(mp))
        meta["final_writeup"] = info
        json.dump(meta, open(mp, "w"), indent=2)
        _harness_wrote_gate_outputs(integrity)
    except (OSError, ValueError):
        pass
    return info


PRESENTATION_FIX_PROMPT = """The modeling sessions have ended. The presentation critic's round-{round}
review of the draft still lists {n} HIGH item(s) for the writer that nobody
has re-checked:

{items}

For run directory {run_dir}, do exactly these three steps, then stop:
1. Spawn the `writer` agent once. Tell it: "This is the final presentation
   fix pass. Start from {run_dir}/{source}, the draft that still carries the
   [CLAIM:id] references. Fix only the items listed below; leave any that are
   already fixed. Keep every number as a [CLAIM:id] reference, change no
   finding, verdict or conclusion, and write the revised draft to
   {run_dir}/manuscript.md." Then give it the items above verbatim.
2. Run `python3 scripts/render_claims.py {run_dir}`.
3. Spawn `critique-presentation` with a prompt that begins
   `This is critique round {round}.` Ask it to re-review {run_dir}/report.md,
   carry every earlier blocker forward (still_present true or false, with
   resolved_evidence), and rewrite critique_presentation.yaml and
   critique_presentation.md.

Do nothing else. The harness runs the final gate afterwards."""


def _render_if_stale(run_path: str) -> None:
    """The writer's draft is newer than report.md: render it (as the lead was told to)."""
    draft, report = os.path.join(run_path, "manuscript.md"), os.path.join(run_path, "report.md")
    try:
        if os.path.exists(report) and os.path.getmtime(draft) <= os.path.getmtime(report):
            return
        subprocess.run([sys.executable, os.path.join(REPO_ROOT, "scripts", "render_claims.py"), run_path],
                       capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        pass


async def presentation_fix_pass(run_path, run_dir_rel, question, budget, cap, session_num,
                                integrity=None, query_fn=None, live_state=None) -> dict:
    """Phase 49 δ (A23): when the sessions end with open presentation HIGHs for
    the writer (scripts/presentation_pass.py), run once per run a short session
    paid from what is left of the budget, the write-up reserve included: the
    writer fixes them, the claims are re-rendered and critique-presentation
    re-reviews the draft, so the final gate reads a checked verdict. Never raises."""
    info = {"ran": False}
    try:
        meta_path = os.path.join(run_path, "metadata.json")
        try:
            prior = (json.load(open(meta_path)).get("presentation_fix_pass") or {})
        except (OSError, ValueError):
            prior = {}
        if prior.get("ran"):
            return prior                        # once per run (a resumed run does not repeat it)
        from presentation_pass import pending   # scripts/ is on sys.path (agents/__init__.py)
        p = pending(run_path)
        if not p["items"]:
            return {"ran": False, "skipped": p["reason"]}   # nothing to record
        info["items"] = [i["id"] for i in p["items"]]
        left = remaining_budget(cap, budget["sessions"])
        if left is not None and left <= 0:
            info.update(skipped="no budget left for the presentation fix pass")
        else:
            await _presentation_session(run_path, run_dir_rel, question, budget, cap, session_num, p, left,
                                        info, integrity, query_fn, live_state)
    except Exception as e:  # noqa: BLE001 — finalize still runs; the gap is recorded
        info.update(error=f"{type(e).__name__}: {e}"[:300])
        print(f"[ERROR] presentation fix pass failed: {info['error']}", flush=True)
    try:
        mp = os.path.join(run_path, "metadata.json")
        meta = json.load(open(mp))
        meta["presentation_fix_pass"] = info
        json.dump(meta, open(mp, "w"), indent=2)
        if integrity is not None:
            integrity.harness_wrote("metadata.json")
    except (OSError, ValueError):
        pass
    return info


async def _presentation_session(run_path, run_dir_rel, question, budget, cap, session_num, p, left,
                                info, integrity, query_fn, live_state) -> None:
    """One harness session: writer fix, render, presentation re-review; charged to the ledger."""
    from presentation_pass import pending
    agents_ = build_agents(question=question, run_dir=run_dir_rel)
    agents = {k: agents_[k] for k in ("writer", "critique-presentation")}
    trace = open(os.path.join(run_path, "trace.jsonl"), "a")
    hooks = create_hooks(run_path, trace, datetime.now(), registered_agents=set(agents),
                         max_turns={k: a.maxTurns for k, a in agents.items()}, integrity=integrity,
                         hold_writer=False)   # the harness's own pass is never held
    options = ClaudeAgentOptions(
        system_prompt="You are the harness's presentation fix step. Follow the instructions literally.",
        agents=agents, model=(smoke_settings() or {}).get("model", enforcement.MODEL_DEEP),
        max_buffer_size=enforcement.MAX_BUFFER_SIZE, settings=enforcement.SESSION_SETTINGS, tools=LEAD_TOOLS,
        allowed_tools=LEAD_TOOLS, permission_mode="bypassPermissions", setting_sources=["project"],
        hooks=hooks, cwd=REPO_ROOT, env=enforcement.filtered_env(dict(os.environ), False), max_turns=10)
    if left is not None:
        options.max_budget_usd = left
    source = "report.unrendered.md" if os.path.exists(os.path.join(run_path, "report.unrendered.md")) \
        else "manuscript.md"
    items = "\n".join(f"- {i['id']}: {i['claim'][:600]}"
                      + (f" Fix: {i['fix_requires'][:400]}" if i["fix_requires"] else "") for i in p["items"])
    prompt = PRESENTATION_FIX_PROMPT.format(round=p["round"] or "N", n=len(p["items"]), items=items,
                                            run_dir=run_dir_rel, source=source)
    print(f"\nPRESENTATION FIX PASS ({', '.join(info['items'])}; budget left ${left})", flush=True)
    result, sid, err = None, None, None
    try:
        async for m in (query_fn or query)(prompt=prompt, options=options):
            log_message(m, trace)
            if live_state is not None:
                live_state.on_message(m)
            if isinstance(m, SystemMessage) and m.subtype == "init":
                sid = (m.data or {}).get("session_id") or sid
            elif isinstance(m, ResultMessage):
                result, sid = m, m.session_id or sid
    except Exception as e:  # noqa: BLE001 — still charged below, then recorded
        err = f"{type(e).__name__}: {e}"[:300]
    src = "presentation fix pass"
    if result is None and sid:
        result = await reconcile_conversation_cost(options, sid, query_fn=query_fn)
        src = "presentation fix pass (crashed; cost read back)"
    charge_session(budget, session_num, result, left, session_id=sid, elapsed_s=None, source=src)
    write_budget_ledger(run_path, budget, cap, integrity)
    _render_if_stale(run_path)
    after = pending(run_path)
    info.update(ran=True, still_open=[i["id"] for i in after["items"]],
                rechecked=not after["draft_after_critique"])
    if err:
        info["error"] = err
