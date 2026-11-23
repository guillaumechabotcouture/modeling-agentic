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
_MODULES = sys.modules          # held here: a patch of the package's `sys` must not reach the forwarding itself
_SELF = __name__


def _submodules(pkg):
    me = _MODULES.get(_SELF)
    return [m for k, m in list(_MODULES.items())
            if m is not None and m is not pkg and m is not me and k.startswith(_PKG + ".")]


_CREATED: set = set()           # (submodule name, attribute) bindings forwarding created
_ORIGINAL: set = set()          # the package's names when share() ran: the module's own names
_CHILDREN: dict = {}            # name -> the submodule object the import system bound under it
_NO_ALL = object()


class _Shared(types.ModuleType):
    @property
    def __all__(self):
        """What `import *` took from the one module: its public names, now and after any patch, except a
        package submodule still bound under its own name (the module had none); or what a caller set."""
        own = self.__dict__.get("_namespace_all", _NO_ALL)
        if own is not _NO_ALL:
            return own
        return [n for n, v in self.__dict__.items() if not n.startswith("_") and _CHILDREN.get(n, _NO_ALL) is not v]

    @__all__.setter
    def __all__(self, value):
        self.__dict__["_namespace_all"] = value

    @__all__.deleter
    def __all__(self):
        self.__dict__.pop("_namespace_all", None)

    def __setattr__(self, name, value):
        old = self.__dict__.get(name, _MISSING)
        super().__setattr__(name, value)
        if old is value:
            return
        for mod in _submodules(self):
            # the same object, or (old missing) a name the module resolved through builtins, as one module did
            if mod.__dict__.get(name, _MISSING) is old:
                setattr(mod, name, value)
                if old is _MISSING:
                    _CREATED.add((mod.__name__, name))

    def __delattr__(self, name):
        old = self.__dict__.get(name, _MISSING)
        super().__delattr__(name)
        for mod in _submodules(self):
            created = (mod.__name__, name) in _CREATED
            _CREATED.discard((mod.__name__, name))
            # forwarding's own binding, or (an original name) the same object: gone everywhere, as in one module
            if (created or name in _ORIGINAL and old is not _MISSING) and mod.__dict__.get(name, _MISSING) is old:
                delattr(mod, name)
                if name in _ORIGINAL:
                    _CREATED.add((mod.__name__, name))      # a later assignment restores it there


def share(module) -> None:
    """Make assignments to `module` (the package, agents/stage_machine/__init__.py) reach its submodules."""
    _ORIGINAL.update(module.__dict__)
    _CHILDREN.update({k.rpartition(".")[2]: m for k, m in list(_MODULES.items())
                      if m is not None and k.rpartition(".")[0] == module.__name__})
    module.__class__ = _Shared
