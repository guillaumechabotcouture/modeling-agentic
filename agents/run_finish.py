"""What a run's finish shares with v2.

- `exit_code_for`: the exit code of a terminal status.
- `store_run_results`: the upload of a finished run directory, called by main.py's v2 path.

The rest of this module (including the v1 STAGE 7 validator `gate_command`) was removed."""

import json
import os
import re
import sys
import uuid

from agents import enforcement

REPO_ROOT = enforcement.REPO_ROOT


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
                    "awaiting_scope_signoff", "completed_scope_declined",
                    # Agent-built engines (#363): owner engine sign-off wait and non-admitted/declined terminals.
                    "awaiting_engine_signoff", "completed_engine_not_admitted", "completed_engine_declined",
                    # lead-gaps β: a --signoff lead run that continued in the child run its provisional amendment made
                    "amended_provisional"}


def exit_code_for(status: str) -> int:
    if status in SUCCESS_STATUSES:
        return 0
    if status == "interrupted":
        return 130
    if status in PARTIAL_STATUSES:
        return 2
    return 1


def plain_tag(value) -> str:
    """A label or launch id as it may appear in a run dir or blob name ('' when there is none)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(value or "")).lstrip(".")[:64]   # a supervisor label is <= 63


def _store_blob_name(run_path: str) -> str:
    """Where the run is stored. A cloud replicate (main.py told its launch and label: $MA_LAUNCH_ID, $MA_RUN_LABEL)
    is stored under its launch and label, beside its result record, so two replicates whose run dirs share a name
    never share a blob (baseline hb3 §5.1). A run with neither keeps the earlier name, outputs/<prefix>/<run>."""
    name = os.path.basename(os.path.normpath(run_path))
    prefix = os.environ.get("MA_STORAGE_PREFIX", "runs")
    launch, label = plain_tag(os.environ.get("MA_LAUNCH_ID")), plain_tag(os.environ.get("MA_RUN_LABEL"))
    if launch and label:
        return f"outputs/{prefix}/launch_{launch}/run_{label}_{name}.tar.gz"
    return f"outputs/{prefix}/{name}.tar.gz"


def _upload_own(results_store, tmp: str, blob: str, account, service, before: dict) -> tuple[str, str | None]:
    """(the blob the run went to, the name it was refused, or None). A blob is replaced only when this run stored
    it before, in the same account (metadata.json `stored`, `stored_account`: a resume), and a run stored under a
    fresh name keeps it; a name another run's blob holds is refused, never overwritten, and the run goes under a
    fresh name beside it (Codex r1 on #374)."""
    stem = blob[:-len(".tar.gz")]
    mine = before.get("stored") if before.get("stored_account") == (account or "") else None
    if isinstance(mine, str) and re.fullmatch(re.escape(stem) + r"\.[0-9a-f]{8}\.tar\.gz", mine):
        blob = mine
    try:
        results_store.upload_retry(tmp, blob, account, service=service, overwrite=mine == blob)
        return blob, None
    except results_store.BlobNameTaken:
        fresh = f"{stem}.{uuid.uuid4().hex[:8]}.tar.gz"
        print(f"[WARN] {blob} holds another run: not overwritten; this run is stored as {fresh}", file=sys.stderr,
              flush=True)
        results_store.upload_retry(tmp, fresh, account, service=service, overwrite=False)
        return fresh, blob


def store_run_results(run_path: str, service=None) -> str | None:
    """Phase 26: when a storage account is configured ($MA_STORAGE_ACCOUNT or
    $COILED_STORAGE_ACCOUNT), upload the finished run directory to
    outputs/<prefix>/<run name>.tar.gz (a cloud replicate: under its launch and
    label, _store_blob_name) so the result never depends on someone collecting it
    from the VM. Another run's blob is never overwritten (_upload_own). Answer keys
    and credential files are left out and API keys redacted. The outcome is recorded
    in metadata.json (`stored` / `store_error`, `store_collision`); a failure is loud
    but never fatal."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    import results_store
    account = results_store.configured_account()
    if not account and service is None:
        return None
    import tempfile
    name = os.path.basename(os.path.normpath(run_path))
    blob, err, taken = None, None, None
    mp = os.path.join(run_path, "metadata.json")
    try:
        before = json.load(open(mp)) if os.path.exists(mp) else {}
    except (OSError, ValueError):
        before = {}
    before = before if isinstance(before, dict) else {}
    try:
        # Bounded memory (v2 autonomy 1E-i): the archive is streamed to disk and streamed up; a large run was
        # held three times over at this point, and a run killed here for memory stored nothing.
        secrets = [os.environ.get(k, "") for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")]
        with tempfile.TemporaryDirectory() as stage:     # outside the run dir; removed whatever happens
            tmp = os.path.join(stage, f"{name}.tar.gz")
            results_store.write_archive(run_path, name, tmp, secrets, strict=True)
            blob, taken = _upload_own(results_store, tmp, _store_blob_name(run_path), account, service, before)
    except Exception as e:  # noqa: BLE001 — the local copy remains; say so loudly
        blob, err = None, f"{type(e).__name__}: {e}"[:500]
        print(f"[ERROR] RUN RESULTS NOT STORED (only the VM copy exists): {err}", file=sys.stderr, flush=True)
    try:
        meta = json.load(open(mp)) if os.path.exists(mp) else {}
        meta = meta if isinstance(meta, dict) else {}   # valid JSON that is not an object (ST6)
        meta.update({"stored": blob, "store_error": err, "stored_account": account or ""})
        if taken:
            meta["store_collision"] = taken
        json.dump(meta, open(mp, "w"), indent=2)
    except (OSError, ValueError):
        pass
    return blob
