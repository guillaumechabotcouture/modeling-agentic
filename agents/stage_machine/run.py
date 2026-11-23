"""Starting and running a v2 run: init_run and run_v2.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import hashlib
import json
import os

from .common import AWAITING_BUDGET, PARENT_ENV, RunState, STATE_FILE, _now, _write_json, load_state, save_state
from .amend import _amend_lock, handoff_child
from .machine import Machine


def _parent_record(path: str) -> dict:
    """metadata.json parent_run: the parent's dir name and its run_state.json sha256 (None, with the error, when it
    cannot be read: the link is then never trusted by scripts/harness_metrics.py)."""
    from lib.safe_io import read_nofollow
    rec = {"run_dir": os.path.basename(os.path.abspath(path)), "recorded_from": PARENT_ENV}
    try:
        rec["run_state_sha256"] = hashlib.sha256(read_nofollow(path, STATE_FILE)[0]).hexdigest()
    except Exception as e:  # noqa: BLE001 -- recorded, never raised: the run itself is not the parent's
        rec.update(run_state_sha256=None, error=type(e).__name__)
    return rec


def _runtime_ids() -> dict:
    """The model ids and the Agent SDK version this process runs with ("unknown" when they cannot be read)."""
    try:
        from agents.enforcement import MODEL_DEEP, MODEL_FAST
        models = {"deep": MODEL_DEEP, "fast": MODEL_FAST}
    except Exception:  # noqa: BLE001
        models = {"deep": "unknown", "fast": "unknown"}
    try:
        from importlib.metadata import version
        sdk = version("claude-agent-sdk")
    except Exception:  # noqa: BLE001
        sdk = "unknown"
    return {"models": models, "sdk_version": sdk}


def _record_invocation(run_dir: str, budget_usd, cap_usd) -> None:
    """This invocation's record in metadata.json: the latest model ids and SDK version at the top level, and on the
    invocation entry main.py appended (scripts/lib/pipeline_id.py stamp) the budget asked for, the cap in force, the
    model ids and the SDK version. No entry is invented when main.py wrote none. Read with no link followed; a
    metadata.json that cannot be read is left as it is (never replaced by a partial record)."""
    from lib.safe_io import read_nofollow
    try:
        meta = json.loads(read_nofollow(run_dir, "metadata.json")[0].decode())
    except Exception:  # noqa: BLE001
        return
    if not isinstance(meta, dict):
        return
    ids = _runtime_ids()
    meta.update(ids)
    inv = meta.get("invocations")
    if isinstance(inv, list) and inv and isinstance(inv[-1], dict) and "budget_usd" not in inv[-1]:
        inv[-1].update(budget_usd=budget_usd, cap_usd=cap_usd, **ids)
    _write_json(run_dir, "metadata.json", meta)


def init_run(run_dir: str, question: str, max_rounds: int = 3, signoff: str = "owner", gate_mode: str = "v2",
             budget_usd: float | None = None, derived_inputs: bool = True, derive_unsandboxed: bool = False,
             world_model: bool = True) -> RunState:
    os.makedirs(run_dir, exist_ok=True)
    st = load_state(run_dir)
    if st is not None:
        return st
    st = RunState(question=question, max_rounds=max_rounds, signoff=signoff, gate_mode=gate_mode, budget_usd=budget_usd,
                  derived_inputs=derived_inputs, derive_unsandboxed=derive_unsandboxed, world_model_stages=world_model)
    try:   # keep what main.py recorded first (run_dir, pipeline, commit, launcher: scripts/lib/pipeline_id.py)
        meta = json.load(open(os.path.join(run_dir, "metadata.json")))
    except (OSError, ValueError):
        meta = {}
    meta = meta if isinstance(meta, dict) else {}
    meta.update({"question": question, "started": meta.get("started") or _now(), "pipeline": "v2",
                 "max_rounds": max_rounds, "signoff": signoff})
    if os.environ.get(PARENT_ENV):
        meta["parent_run"] = _parent_record(os.environ[PARENT_ENV])
    _write_json(run_dir, "metadata.json", meta)
    save_state(run_dir, st)
    return st


def run_v2(question: str, run_dir: str, tasks, max_rounds: int = 3, signoff: str = "owner",
           budget_usd: float | None = None, log=print, derive_unsandboxed: bool = False, backend=None,
           cross_engine_check: bool = True, _depth: int = 0, _stamp: dict | None = None) -> str:
    """Entry point for main.py --v2 (and the dry run). Resumes when run_state.json exists, also a run that ended
    run_failed because its analysis step timed out, was killed or lost engine rows to the machine
    (Machine.reopen_interrupted_analysis). A --signoff lead run holds its amendment lock from before its state is read
    through its hand-off and the run of the child it handed to (owner decision on #330, Codex r3 on #330 and #342): a
    second process resuming it fails with AmendmentLocked before writing anything, also to that child."""
    try:
        prev = load_state(run_dir)
    except (OSError, ValueError):
        prev = None
    if signoff != "lead" and (prev is None or prev.signoff != "lead"):
        return _run_v2_chain(question, run_dir, tasks, max_rounds, signoff, budget_usd, log, derive_unsandboxed,
                             backend, cross_engine_check, _depth, _stamp)
    os.makedirs(run_dir, exist_ok=True)
    with _amend_lock(run_dir):     # held through the hand-off and the child's run (Codex r3 on #342, P1-8)
        return _run_v2_chain(question, run_dir, tasks, max_rounds, signoff, budget_usd, log, derive_unsandboxed,
                             backend, cross_engine_check, _depth, _stamp)


def _run_v2_chain(question, run_dir, tasks, max_rounds, signoff, budget_usd, log, derive_unsandboxed, backend,
                  cross_engine_check, _depth, _stamp) -> str:
    """One run dir, then the child its provisional amendment handed it to (run_v2, under the caller's lock)."""
    child, status = _run_v2_once(question, run_dir, tasks, max_rounds, signoff, budget_usd, log,
                                 derive_unsandboxed, backend, cross_engine_check, _stamp)
    if child is None or _depth >= 8:          # a bounded chain (a restored cycle cannot recurse: Codex r2)
        return status
    # lead-gaps β: the run continues in the child its provisional amendment made (same process, commit and launcher);
    # the child is stamped only once its own lock is held
    try:
        meta = json.load(open(os.path.join(run_dir, "metadata.json")))
        ident = {k: meta.get(k) for k in ("pipeline", "commit", "launcher")}
    except (OSError, ValueError, AttributeError) as e:
        log(f"[machine] the child run's identity was not read: {type(e).__name__}: {e}")
        ident = None
    cst = load_state(child)
    log(f"[machine] continuing in {child} (provisional amendment)")
    return run_v2(cst.question, child, tasks.for_run_dir(child), max_rounds=cst.max_rounds, signoff="lead",
                  budget_usd=cst.budget_usd, log=log, derive_unsandboxed=derive_unsandboxed, backend=backend,
                  cross_engine_check=cross_engine_check, _depth=_depth + 1, _stamp=ident)


def _run_v2_once(question, run_dir, tasks, max_rounds, signoff, budget_usd, log, derive_unsandboxed, backend,
                 cross_engine_check, stamp) -> tuple:
    """(the child to continue in or None, the status) of one run dir."""
    if stamp:
        try:
            from lib import pipeline_id
            pipeline_id.stamp(run_dir, stamp)
        except Exception as e:  # noqa: BLE001 -- the identity stamp only; the run records its own invocation below
            log(f"[machine] the child run's identity was not stamped: {type(e).__name__}: {e}")
    st = init_run(run_dir, question, max_rounds, signoff, "v2", budget_usd, derive_unsandboxed=derive_unsandboxed)
    if st.paused == AWAITING_BUDGET and budget_usd and budget_usd > (st.budget_usd or 0):
        # the owner resumes a run paused below the allowance floor with a higher cap: recorded, never lowered
        st.history.append({"at": _now(), "budget_raised": {"from": st.budget_usd, "to": budget_usd},
                           "spent_usd": round(st.spent_usd, 4)})
        st.budget_usd = budget_usd
        save_state(run_dir, st)
    _record_invocation(run_dir, budget_usd, st.budget_usd)
    m = Machine(run_dir, tasks, st, backend=backend, log=log, cross_engine_check=cross_engine_check)
    m.reopen_interrupted_analysis()
    status = m.run()
    return handoff_child(run_dir), status
