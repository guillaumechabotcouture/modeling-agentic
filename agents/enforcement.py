"""Shared enforcement helpers (environment filtering, model IDs, harness hashing, SDK deny helper)."""

from __future__ import annotations

import hashlib
import os
import re

REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

HARNESS_FILES = ("main.py", "CLAUDE.md", "CHANGELOG.md", "requirements.txt",
                 "requirements-cloud.txt", "requirements-agent.txt")   # the last: PR #116 (absent until it merges: skipped when hashed)

# Subtrees hashed for tamper evidence. `.claude/` as a whole also holds
# lock files and local settings that change legitimately, so only the
# orchestration manifests and skills are hashed.
HASHED_DIRS = ("scripts", "agents", ".claude/orchestration", ".claude/skills", "toolkit")
HASHED_FILES = HARNESS_FILES
_HASH_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
_HASH_SKIP_SUFFIXES = (".pyc", ".pyo")

# Environment variable prefixes holding cloud / third-party credentials.
# They are only passed to agent subprocesses when cloud execution is on.
CLOUD_ENV_PREFIXES = ("AZ_", "AZURE_", "OPENAI_", "AWS_", "GOOGLE_")
# SDK authentication variables kept when blanking credential patterns for the ACQUIRE task (docs/design/acquire.md §3.1).
ACQUIRE_SDK_AUTH_VARS = frozenset({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"})
_ACQUIRE_CRED_RE = re.compile(r"(?:_TOKEN$|_KEY$|SECRET|PASSWORD|^COILED_|^GH_|^GITHUB_)", re.I)


def deny_output(reason: str) -> dict:
    """PreToolUse hook response that blocks the call (SDK schema)."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"Phase 21 enforcement: {reason}",
        }
    }


def hash_harness(repo_root: str = REPO_ROOT) -> dict[str, str]:
    """sha256 of every hashed harness file, keyed by repo-relative path."""
    out: dict[str, str] = {}

    def _add(path: str) -> None:
        h = hashlib.sha256()
        try:
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 16), b""):
                    h.update(chunk)
        except OSError:
            return
        out[os.path.relpath(path, repo_root).replace(os.sep, "/")] = h.hexdigest()

    for d in HASHED_DIRS:
        root = os.path.join(repo_root, d)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(x for x in dirnames if x not in _HASH_SKIP_DIRS)
            for fname in sorted(filenames):
                if fname.endswith(_HASH_SKIP_SUFFIXES) or fname == ".DS_Store":
                    continue
                _add(os.path.join(dirpath, fname))
    for fname in HASHED_FILES:
        p = os.path.join(repo_root, fname)
        if os.path.isfile(p):
            _add(p)
    return out


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

# Phase 21 α: pinned model IDs. The bare "opus"/"sonnet" aliases are
# resolved by whatever CLI the SDK bundles; SDK 0.1.56's CLI (2.1.92)
# resolved them to the 4.x family, so runs silently used older models
# than intended and metadata could not say which. Bump these together
# with claude-agent-sdk in requirements.txt.
MODEL_DEEP = "claude-opus-5-5"     # synthesis, modeling, adversarial critique
MODEL_FAST = "claude-sonnet-5-5"   # data wrangling, presentation critique

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------

# Phase 31: the prompt cache lives 1 hour instead of 5 minutes, for the lead
# and for subagents. E7 attempt 6: 44% of the measured spend was the cache
# being re-written after 5-60 min waits (the lead waiting on subagents, the
# modeler on simulations); replayed on that run, 1 h writes would have cut
# cache spend from $300 to about $186. MA_PROMPT_CACHE_TTL=5m restores the
# default for comparison.
def cache_env(environ: dict[str, str]) -> dict[str, str]:
    ttl = environ.get("MA_PROMPT_CACHE_TTL", "1h")
    if ttl not in ("1h", "5m"):
        ttl = "1h"
    return {"CLAUDE_CODE_PROMPT_CACHE_TTL": ttl, "CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": ttl}


def filtered_env(environ: dict[str, str], allow_cloud: bool) -> dict[str, str]:
    """Overrides for ClaudeAgentOptions.env.

    The SDK merges os.environ with options.env, so variables cannot be
    removed, only overridden. Cloud credentials are blanked when cloud
    execution is off; downstream code treats empty as unset. Phase 31:
    also sets the prompt-cache TTL (cache_env).
    """
    overrides = interpreter_env(environ)
    overrides.update(cache_env(environ))
    if not allow_cloud:
        overrides.update({k: "" for k in environ if k.startswith(CLOUD_ENV_PREFIXES)})
    return overrides


def acquire_env(environ: dict[str, str]) -> dict[str, str]:
    """Stricter environment overrides for the v2 ACQUIRE task (docs/design/acquire.md §3.1, S_ACQ_ENV).

    Applies `filtered_env(..., allow_cloud=False)` and additionally blanks every environment variable
    matching credential patterns (`*_TOKEN`, `*_KEY`, `*SECRET*`, `*PASSWORD*`, `COILED_*`, `GH_*`,
    `GITHUB_*`), preserving only the SDK's own authentication variables.
    """
    overrides = filtered_env(environ, allow_cloud=False)
    for k in environ:
        if k not in ACQUIRE_SDK_AUTH_VARS and _ACQUIRE_CRED_RE.search(k):
            overrides[k] = ""
    return overrides


def interpreter_env(environ: dict[str, str],
                    executable: str | None = None) -> dict[str, str]:
    """Phase 21 α: put this interpreter's bin/ first on the agents' PATH.

    Agents call bare `python3` / `pip`. When main.py was launched as
    `.venv/bin/python3 main.py` without activating the venv, those
    resolved to the system interpreter: `pip install` failed with
    externally-managed-environment and the modeler concluded the
    requested framework (LASER) was "not installed" and silently swapped
    it for scipy (2026-10-20_150152). Derive the venv from sys.executable
    so the agents always see the interpreter that runs the harness.
    """
    import sys
    exe = executable or sys.executable
    bin_dir = os.path.dirname(exe)
    prefix = os.path.dirname(bin_dir)
    out = {"PATH": bin_dir + os.pathsep + environ.get("PATH", "")}
    if os.path.isfile(os.path.join(prefix, "pyvenv.cfg")):
        out["VIRTUAL_ENV"] = prefix
    return out
