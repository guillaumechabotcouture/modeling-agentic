"""Agent-built engines lane (docs/design/agent-built-engines.md, #363).

PR alpha: request schema and `RequestedEngine` stand-in (`toolkit.engines.built.requested`).
PR beta: immutable content-addressed snapshots and S1-S4 static checks (`snapshot.py`), minimal-mount
bubblewrap sandbox and verified-bytes worker (`worker.py`), and the `BuiltEngine` adapter (`adapter.py`).
PR gamma: oracle library (`oracles.py`), admission gate A1-A8/A11 (`admission.py`), and bounded
try tool (`try_tool.py`).
"""
from __future__ import annotations

BUILT_REF_PREFIX = "toolkit.engines.built"
BUILT_ADAPTER_REF = "toolkit.engines.built:BuiltEngine"

from toolkit.engines.built.adapter import BuiltEngine, BuiltEngineRefused  # noqa: E402

__all__ = ["BUILT_REF_PREFIX", "BUILT_ADAPTER_REF", "BuiltEngine", "BuiltEngineRefused"]
