---
phase: "v2"
commit: "bwrap"
---

## Changelog

**The agent VM installs bubblewrap for the v2 derivation sandbox (best effort).** The Coiled image has passwordless sudo and `setpriv` but no bubblewrap, so a v2 run whose SPEC state must run a derivation would fail closed there (`derivation_unsandboxed`, PR #118). Before `main.py` starts, `cloud/worker_side.py` `ensure_bwrap()` now runs `sudo -n apt-get install -y bubblewrap` when bwrap is missing on Linux, and again after `apt-get update` if the first attempt fails. It records the outcome in the run's notes and never fails the run on its own. A run past sign-off does not need it, because it only checks the record's hashes. Self-test RC61 (in `cloud/run_cluster.py`) covers the decision logic with the shell calls stubbed.
