"""No cloud call from a self-test (F6 S1-8, Codex round 3 on #161).

Every `--self-test` entry that can reach the cloud (cloud/run_cluster.py, cloud/vm_job.py, and through them
cloud/worker_side.py) runs inside `refusing()`: `coiled` is replaced by a module that raises on any use, so a
case that forgets its fake fails instead of calling Coiled. A case that installs its own fake (mock.patch.dict on
sys.modules) still gets it.
"""
from __future__ import annotations

import contextlib
import sys
import types
from unittest import mock

MODULES = ("coiled",)


class _Refusing(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise RuntimeError(f"self-test reached the real {self.__name__}.{name}: every case must use a fake")


@contextlib.contextmanager
def refusing():
    with mock.patch.dict(sys.modules, {m: _Refusing(m) for m in MODULES}):
        yield


def active() -> bool:
    return all(isinstance(sys.modules.get(m), _Refusing) for m in MODULES)
