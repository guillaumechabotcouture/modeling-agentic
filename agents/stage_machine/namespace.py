"""One namespace for the package (no behaviour change from the split).

Before the split every name of the stage machine lived in one module, so a caller that rebinds one
(`stage_machine.v2_gate = ...` in scripts/golden_traces.py; the self-tests' patches) changed what every
function there calls. Now a name is bound in each package module that imports it. share() makes the package
(__init__.py) forward an assignment to itself to every submodule that holds the same object, as one module did.

Made by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import sys
import types

_MISSING = object()
_PKG = __name__.rpartition(".")[0]


class _Shared(types.ModuleType):
    def __setattr__(self, name, value):
        old = self.__dict__.get(name, _MISSING)
        super().__setattr__(name, value)
        if old is _MISSING or old is value:
            return
        for mname, mod in list(sys.modules.items()):
            if mod is not None and mod is not self and mname.startswith(_PKG + ".") \
                    and mod.__dict__.get(name, _MISSING) is old:
                setattr(mod, name, value)


def share(module) -> None:
    """Make assignments to `module` (the package, agents/stage_machine/__init__.py) reach its submodules."""
    module.__class__ = _Shared
