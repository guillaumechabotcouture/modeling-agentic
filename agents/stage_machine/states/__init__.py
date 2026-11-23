"""The state handlers of the v2 stage machine, one module per state, each a class registered by name
(`@handler("GATE")`). Machine (machine.py) inherits every registered class, in this import order,
so `getattr(self, "h_" + state)` in Machine.step finds each handler as before the split.
"""
from __future__ import annotations

HANDLERS: dict = {}


def handler(state: str):
    """Register a state's handler class under the state's name; it must define h_<state>."""
    def register(cls):
        if "h_" + state not in vars(cls):
            raise TypeError(f"{cls.__name__} is registered for {state} but has no h_{state}")
        HANDLERS[state] = cls
        return cls
    return register


from . import (  # noqa: E402,F401
    spec, sign, evidence, engine_choice, preregister, configure, analyze, score, critique, gate, fix_loop,
    owner_decision, write, presentation,
)
