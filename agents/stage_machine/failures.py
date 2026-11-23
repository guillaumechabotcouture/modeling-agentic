"""Why an analysis failed, and whether a resume may re-run it.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import json
import os
import re
import analysis_backend as AB

from .common import RunState


def _rep_error(rep: dict) -> str:
    errs = list(rep.get("errors") or []) + [f"{s.get('id')}: {_step_error(s.get('error'))}"
                                            for s in rep.get("steps") or [] if s.get("status") != "ok"]
    return "; ".join(str(e) for e in errs)[:300]


_TB_FRAME = re.compile(r'^[ |+]*File "([^"]+)", line (\d+)(?:, in (\S+))?\s*$')
_TB_EXC = re.compile(r"^[A-Za-z_][\w.]*(?::|$)")
_TB_HEAD = re.compile(r"[ |+]*(?:Exception Group )?Traceback \(most recent call last\):")


def _step_error(err) -> str:
    """A failed step's error, short enough for the history line. A Python traceback in it (the step's log tail; its
    header may have fallen outside the tail) is reduced to the frame that raised and the exception's first line,
    after whatever preceded the traceback (a timeout's own text); "Traceback at" keeps it read as a crash. The first
    300 characters had held only the outer frames, so T16's CI flakes recorded no exception (T9b). Any other error is
    kept as it is (T9c)."""
    text = str(err)
    lines = text.splitlines()
    fr = [(k, m) for k, m in ((k, _TB_FRAME.match(ln)) for k, ln in enumerate(lines)) if m]
    if not fr:
        return text
    k, m = fr[-1]
    depth = lambda ln: len(ln) - len(ln.lstrip(" |+-"))  # noqa: E731 -- indent, ExceptionGroup borders included
    exc = next((ln.lstrip(" |+-") for ln in lines[k + 1:] if depth(ln) < depth(lines[k])
                and _TB_EXC.match(ln.lstrip(" |+-"))), "no exception line in the log tail")
    hdr = _TB_HEAD.search(text)
    head = text[:hdr.start()] if hdr else ""      # a timeout's own text, before the traceback it shows
    where = f"{os.path.basename(m.group(1))}:{m.group(2)}" + (f" in {m.group(3)}" if m.group(3) else "")
    return f"{head.strip()[:120]} Traceback at {where}: {exc[:150]}".strip()


# Machine.reopen_interrupted_analysis's history line, per resumable cause.
_RESUME_WHY = {"timeout": "the analysis step had timed out", "killed": "the analysis step had been killed",
               "infra": "engine rows had failed on lost workers (kind infra)"}

# A step killed by these signals was stopped from outside (out of memory, a stop); a crash (SIGSEGV...) is not.
_KILL_SIGNALS = (9, 15, 2, 1)     # SIGKILL, SIGTERM, SIGINT, SIGHUP


def analysis_failure_cause(rep: dict, run_dir: str | None = None) -> str | None:
    """'timeout', 'killed' or 'infra' when the pipeline report's only failures are steps that ran out of wall-clock
    time (scripts/pipeline.py `_run_cmd`), died of an external signal, or exited with an error because their engine
    batch lost rows to the machine (`_infra_batch`, read from `run_dir`), with the steps below them skipped; None
    for anything else (another error exit, a crash, missing inputs, a validation error)."""
    if not isinstance(rep, dict) or rep.get("errors"):
        return None
    cause = None
    for s in rep.get("steps") or []:
        if not isinstance(s, dict) or s.get("status") == "ok":
            continue
        rc, err = s.get("returncode"), str(s.get("error") or "")
        if s.get("status") == "skipped" and err.startswith("upstream step not rebuilt"):
            continue
        if s.get("status") == "failed" and rc is None and err.startswith("timed out after "):
            cause = cause or "timeout"
        elif s.get("status") == "failed" and type(rc) is int and -rc in _KILL_SIGNALS:
            cause = cause or "killed"
        elif s.get("status") == "failed" and type(rc) is int and rc > 0 and run_dir and _infra_batch(run_dir, rep):
            cause = cause or "infra"
        else:
            return None
    return cause


# Where the analysis's engine batch keeps compute.py's per-call summaries (toolkit/analysis/batch.py).
BATCH_DIR = os.path.join("analysis", "batch")
# ... and the joint calibration's (toolkit/analysis/joint_calibration.py OUT_REL)
JC_DIR = os.path.join("analysis", "joint_calibration")


def _infra_batch(run_dir: str, rep: dict) -> bool:
    """2026-11-02 (Nigeria v2 full run F3): whether the engine batch this pipeline run computed failed only with
    kind infra (lost workers; scripts/compute.py has already retried them within the call). Read from the summaries
    compute.py wrote under analysis/batch/ during this run, i.e. after the run's start (the report's mtime minus its
    `seconds`, on the same clock as theirs): at least one has failed rows, and every one with failed rows has only
    infra ones. Never read from the error text. Anything unreadable is False (not resumable).
    F6 (2026-11-11): also the joint calibration's primary batches (JC_DIR: bench_call*_phase*.jsonl and
    call<N>/points.jsonl or results.jsonl directly under it; never sensitivity_* or another subdirectory, whose
    batches the failing step does not depend on). When the report carries `pipeline_run`, only summaries with the
    same id count (untagged ones are ignored), so a run dir copied or restored from a checkpoint (mtimes reset) is
    still classified; the mtime rule applies only to a report without an id."""
    run_id = rep.get("pipeline_run") if isinstance(rep, dict) else None
    try:
        started = os.path.getmtime(os.path.join(run_dir, "models", "pipeline_report.json")) - float(rep["seconds"]) - 2.0
    except (OSError, KeyError, TypeError, ValueError):
        return False
    import glob
    import re
    paths = sorted(glob.glob(os.path.join(run_dir, BATCH_DIR, "*.summary.json")))
    jc = os.path.join(run_dir, JC_DIR)   # Codex on #200: only the primary calibration's batches, never sensitivity_*
    paths += sorted(glob.glob(os.path.join(jc, "bench_call*_phase*.jsonl.summary.json")))
    paths += sorted(p for p in glob.glob(os.path.join(jc, "call*", "*.jsonl.summary.json"))
                    if re.fullmatch(r"call\d+", os.path.basename(os.path.dirname(p)))
                    and os.path.basename(p) in ("points.jsonl.summary.json", "results.jsonl.summary.json"))
    failed = 0
    for p in paths:
        try:
            with open(p) as f:
                summ = json.load(f)
            if run_id:                            # Codex on #200: the run id decides; an untagged summary is not ours
                if summ.get("pipeline_run") != run_id:
                    continue
            elif os.path.getmtime(p) < started:
                continue                          # an earlier pipeline run's batch (no run id: the mtime rule)
            kinds, n_failed = summ.get("failed_by_kind"), int(summ.get("n_failed") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            return False
        if not n_failed:
            continue
        if not isinstance(kinds, dict) or set(kinds) != {"infra"} or kinds["infra"] != n_failed:
            return False                          # a model error or a stopped row needs more than a rerun
        failed += n_failed
    return failed > 0


def resumable_failure(run_dir: str, st: RunState) -> str | None:
    """Why this run may be resumed at ANALYZE ('timeout' | 'killed' | 'infra'), or None: only a run that ended run_failed on
    its ANALYZE -> FINAL transition, whose pipeline report (models/pipeline_report.json, written by the harness's
    run) says the analysis was interrupted. Read from the report, so a run that failed before the history carried
    the `resumable` marker is classified the same way."""
    if st.state != "FINAL" or st.status != "run_failed" or not st.history:
        return None
    last = st.history[-1]
    if not isinstance(last, dict) or last.get("from") != "ANALYZE" or last.get("to") != "FINAL":
        return None
    try:
        with open(os.path.join(run_dir, "models", "pipeline_report.json")) as f:
            rep = json.load(f)
    except (OSError, ValueError):
        return None
    return analysis_failure_cause(rep, run_dir)


def _with_abs_paths(spec: dict, run_dir: str) -> dict:
    return AB._abs_spec(spec, run_dir)


def _pre_engine_check(spec: dict) -> list[str]:
    """Before an engine is chosen: the sections every engine needs are present."""
    from spike.analysis.spec import REQUIRED
    return [f"missing section {k}" for k in REQUIRED if k != "engine" and k not in spec]
