---
phase: "Agent-built engines (#363 follow-up: bwrap stdlib & symlink mount coverage)"
commit: "cover encodings/os/sysconfig/pyvenv.cfg/opt-subroot stdlib paths in minimal-mount bwrap worker and order --tmpfs /tmp before binds"
---

## Changelog

- `toolkit/engines/built/worker.py`:
  - Expand `_ro_mount_prefixes()` to explicitly cover `encodings.__file__`, `os.__file__`, `sysconfig.get_paths()` and stdlib config vars, `sys._base_executable`, `pyvenv.cfg` `home`, core runtime/scientific package symlink targets (`math`, `_json`, `zlib`, `numpy`, `scipy`, `yaml`, `starsim`), zipped stdlib (`python*.zip`), `PYTHONHOME`, and `/opt/<subroot>` package stores when the interpreter runtime lives under `/opt/` (while still excluding `/`, `/tmp`, `/var`, `/home`, `/root`, the repo directory, run directories, and arbitrary `sys.path` entries).
  - Order `--dev /dev --proc /proc --tmpfs /tmp` before `--ro-bind` in `_bwrap_engine_cmd()` so private `/tmp` never shadows a mounted runtime prefix while keeping host `/tmp` isolated.
  - Forward `PYTHONHOME` in `_minimal_env()` when covered by mounted prefixes, enrich `_probe_engine_bwrap()` failure diagnostics with interpreter/stdlib/mount paths, and add self-test `BE18d`.
