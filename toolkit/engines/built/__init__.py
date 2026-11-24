"""Agent-built engines, admitted by harness checks (docs/design/agent-built-engines.md).

PR alpha: request schema, RequestedEngine stand-in, full-registry justification check,
pre-sign sheet section, sheet digest in the sign-off record, and --signoff none refusal.
The snapshot/sandbox worker and BuiltEngine adapter land in PR beta.
"""
from __future__ import annotations

BUILT_REF_PREFIX = "toolkit.engines.built"
BUILT_ADAPTER_REF = "toolkit.engines.built:BuiltEngine"
CANDIDATE_MODULE = "toolkit.engines.built.candidate"
