"""What a run's finish shares with v2.

- `gate_command`: the STAGE 7 validator command, used by the stage machine's legacy gate mode and by the
  golden traces.
- `exit_code_for`: the exit code of a terminal status.
- `store_run_results`: the upload of a finished run directory, called by main.py's v2 path.

The rest of this module, the v1 lead session's final gate, write-up, presentation pass and model card, was
removed with pipeline v1 (owner, 2026-11-20; docs/design/v1-retirement.md)."""

import json
import os
import sys

from agents import enforcement

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
                    "awaiting_signoff",   # Phase 33: resumable with --resume once signed
                    # Phase 49 β: waiting for the owner on decision-bearing declarations
                    # (resumable), and the owner declined one with no rounds left.
                    "awaiting_scope_signoff", "completed_scope_declined"}


def gate_command(run_path: str, max_rounds: int, current_round: int, post_write: bool,
                 validator: str = VALIDATOR, history: bool = True) -> list[str]:
    """The STAGE 7 validator command, exactly as the final gate runs it (v2 C:
    shared with agents/stage_machine.py's legacy gate and the golden traces)."""
    cmd = [sys.executable, validator, run_path,
           "--max-rounds", str(max_rounds), "--current-round", str(current_round),
           "--spec-compliance", "--parameter-registry", "--rigor-artifacts",
           "--json"]
    if post_write:
        cmd.append("--post-write")
    if not history:
        cmd.append("--no-history")
    return cmd


def exit_code_for(status: str) -> int:
    if status in SUCCESS_STATUSES:
        return 0
    if status == "interrupted":
        return 130
    if status in PARTIAL_STATUSES:
        return 2
    return 1


def _store_blob_name(run_path: str) -> str:
    name = os.path.basename(os.path.normpath(run_path))
    return f"outputs/{os.environ.get('MA_STORAGE_PREFIX', 'runs')}/{name}.tar.gz"


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
    import tempfile
    name = os.path.basename(os.path.normpath(run_path))
    blob, err = None, None
    try:
        # Bounded memory (v2 autonomy 1E-i): the archive is streamed to disk and streamed up; a large run was
        # held three times over at this point, and a run killed here for memory stored nothing.
        secrets = [os.environ.get(k, "") for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")]
        with tempfile.TemporaryDirectory() as stage:     # outside the run dir; removed whatever happens
            tmp = os.path.join(stage, f"{name}.tar.gz")
            results_store.write_archive(run_path, name, tmp, secrets, strict=True)
            blob = _store_blob_name(run_path)
            results_store.upload_retry(tmp, blob, account, service=service)
    except Exception as e:  # noqa: BLE001 — the local copy remains; say so loudly
        blob, err = None, f"{type(e).__name__}: {e}"[:500]
        print(f"[ERROR] RUN RESULTS NOT STORED (only the VM copy exists): {err}", file=sys.stderr, flush=True)
    try:
        mp = os.path.join(run_path, "metadata.json")
        meta = json.load(open(mp)) if os.path.exists(mp) else {}
        meta = meta if isinstance(meta, dict) else {}   # valid JSON that is not an object (ST6)
        meta.update({"stored": blob, "store_error": err})
        json.dump(meta, open(mp, "w"), indent=2)
    except (OSError, ValueError):
        pass
    return blob
