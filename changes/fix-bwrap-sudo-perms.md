---
phase: "Agent-built engines & ACQUIRE narrowed bwrap-sudo permissions fix"
commit: "pre-create minimal-mount bwrap ancestor directories with 0755 and /tmp with 1777 so bwrap-sudo + setpriv can traverse mounted runtimes"
---

## Changelog

- `toolkit/engines/built/worker.py`:
  - Add `_ancestor_dirs()` and emit `--perms 0755 --dir <anc>` before `--ro-bind` in `_bwrap_engine_cmd()` so `bwrap 0.12+` running under `sudo -n bwrap` + `setpriv` does not synthesize root-owned `0700` parent directories (such as `/opt` or `/etc`) that cause `EACCES` / `ModuleNotFoundError: No module named 'encodings'` after privilege drop.
  - Pass `--perms 1777 --tmpfs /tmp` and `--perms 0755` on `/harness` and its subdirectories; add self-test `BE18e`.
- `scripts/pipeline.py`:
  - Apply the same ancestor `--perms 0755 --dir` and `--perms 1777 --tmpfs /tmp` fix to `_bwrap_narrow_cmd()` (narrowed sandbox for derivations reading acquired inputs).
