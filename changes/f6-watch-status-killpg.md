---
phase: "F6"
commit: "S2-LW23"
status_group: "F6 Stage 2"
---

## Changelog

**The watch can no longer hang on a status call, and checks the spend cap every interval.** On 2026-11-12 the canary's watch logged a spend reading at 16:47 and then nothing for 80 minutes, while its cluster went past the $90 cap (stopped by hand at 18:10). Its next step was a `run_cluster.py status` call through `subprocess.run(capture_output=True, timeout=...)`, which on timeout kills the child and then waits for its pipes; a grandchild still holding them blocks that wait for ever. LW13c reproduces it with real processes: before the change the call had not returned after 30 s. `shell_runner` now runs every non-launch command in its own process group with its output in a file, and past its deadline kills the whole group and returns its output so far with a TimeoutError. Separately, the watch loop retried a timed-out status up to `retries` times (600 s each, 60 s apart) before the next spend reading, so even a working timeout could skip the cap for about an hour. The loop now tries status once per interval with a 300 s deadline (`STATUS_S`) and tries again after the next spend reading (LW13d). Self-tests LW13c-d (`cloud/launch_watch.py`), written first, failed before the change. Not yet run against a real cluster.
