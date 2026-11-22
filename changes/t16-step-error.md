---
phase: "v2"
commit: "T16 step error"
---

## Changelog

**A failed analysis step's reason keeps its exception (T16 CI flakes).** When a pipeline step failed with a Python traceback, the run's FINAL reason (`_rep_error`) kept the first 300 characters of the step's log tail. Those characters are only the traceback's outer frames, so T16's CI failures (11-20 and 11-21, both shard 2, an analyze step in the reference or a resumed run) recorded "Traceback ... line 776, in m" and no exception, through three investigations. `_step_error` now reduces a traceback to its last exception and the frame that raised it (for example `Traceback at run.py:120 in solve: RuntimeError: optimizer status feasible`), after any text before the traceback (a timeout keeps reading as a timeout first); "Traceback" keeps the run console reading it as a crash; any other error is kept as it is. Codex round 1: frames are whole lines (a message quoting a frame is not one), the header may be outside the tail, the exception is the first exception line after the raising frame (trailing output, multi-line messages, SyntaxError locations and exception groups handled), and the frame comes before the message so the 300-character cut keeps it. T16's assertion and message are unchanged (its message already carries the FINAL reason, #322). The flake's cause is still open: 408 T16 reference runs on GitHub-hosted runners (2 vCPU, beside the compute self-test, as in shard 2) all completed, so the next CI failure will name it. Self-tests T9b-T9e (T9d on real interpreter output).
