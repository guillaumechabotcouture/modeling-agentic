#!/usr/bin/env python3
"""A horizon with a delivery period distinct from the outcome window (malaria engine v0.6).

    python3 toolkit/engines/horizon.py --self-test

The contract's `Horizon` (toolkit/engines/contract.py) has one decision window, which bounds both the years in which
package components are delivered and the years over which outcomes are counted. A `DeliveryHorizon` adds `delivery`
= [t0, t1): the period in which the decision's components are delivered and paid for, inside the window. The window
stays the outcome window. The harness builds one only when the spec gives `objective.delivery`; an engine reads
`delivery_period(horizon)`, and declares `supports_delivery = True` when it honours the field (the spec validator
refuses the field for any other engine, since that engine would ignore it).

contract.py is unchanged: it is part of every engine's code fingerprint, so a field there would make every engine's
benchmark stamps stale. Nothing here names a disease.
"""
from __future__ import annotations

import numbers
import os
import sys
from dataclasses import dataclass

if __package__:
    from .contract import Horizon
else:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from toolkit.engines.contract import Horizon  # noqa: E402


@dataclass
class DeliveryHorizon(Horizon):
    delivery: tuple | None = None   # [t0, t1) in model years: components delivered and paid for; None = the window


def delivery_period(horizon) -> tuple:
    """(t0, t1) of the delivery period: the horizon's `delivery`, else its decision window."""
    d = getattr(horizon, "delivery", None)
    return tuple(float(x) for x in (d if d is not None else horizon.window))


def explicit_delivery(horizon) -> bool:
    return getattr(horizon, "delivery", None) is not None


def delivery_problems(horizon) -> list[str]:
    """Why the delivery period is unusable: not two numbers t0 < t1, or not inside the decision window."""
    d = getattr(horizon, "delivery", None)
    if d is None:
        return []
    # Fail closed on the shape before coercing: a string iterates as characters ("15" would read as [1.0, 5.0]), a
    # mapping as its keys, a bool as a number.
    if not (isinstance(d, (list, tuple)) and len(d) == 2
            and all(isinstance(x, numbers.Real) and not isinstance(x, bool) for x in d)):
        return [f"delivery period {d!r}: give [t0, t1], two numbers"]
    t0, t1 = (float(x) for x in d)
    w0, w1 = (float(x) for x in horizon.window)
    if not t0 < t1:
        return [f"delivery period [{t0:g}, {t1:g}]: needs t0 < t1"]
    if not (w0 <= t0 and t1 <= w1):
        return [f"delivery period [{t0:g}, {t1:g}] is not inside the outcome window [{w0:g}, {w1:g}]"]
    return []


def make_horizon(start, stop, window, windows=None, delivery=None) -> Horizon:
    """A contract Horizon, or a DeliveryHorizon when `delivery` is given (so horizons without it are unchanged). The
    window is kept as given, as the callers built it before v0.6."""
    w = tuple(window)
    if delivery is None:
        return Horizon(float(start), float(stop), w, dict(windows or {}))
    return DeliveryHorizon(float(start), float(stop), w, dict(windows or {}), tuple(float(x) for x in delivery))


def _self_test() -> int:
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    h = make_horizon(0, 5, [1, 4], {"w": [1, 2]})
    hd = make_horizon(0, 5, [1, 4], {"w": [1, 2]}, delivery=[1, 2])
    ok(type(h) is Horizon and not explicit_delivery(h) and delivery_period(h) == (1.0, 4.0),
       "H1 without a delivery period the horizon is the contract's own, and delivery is the decision window")
    ok(isinstance(hd, Horizon) and explicit_delivery(hd) and delivery_period(hd) == (1.0, 2.0) and hd.window == (1.0, 4.0),
       "H2 a DeliveryHorizon is a contract Horizon carrying the delivery period; the window stays the outcome window")
    ok(delivery_problems(hd) == [] and delivery_problems(h) == [], "H3 a delivery period inside the window is usable")
    ok(delivery_problems(make_horizon(0, 5, [1, 4], delivery=[0.5, 2])) and
       delivery_problems(make_horizon(0, 5, [1, 4], delivery=[2, 2])) and
       delivery_problems(DeliveryHorizon(0, 5, (1, 4), {}, delivery="x")),
       "H4 a delivery period outside the window, empty, or malformed is refused")
    # H5 (external review 2026-11-17): a string iterates as characters, so "15" read as [1.0, 5.0]. Anything that
    # is not a list or tuple of two real numbers is refused, never coerced.
    for bad in ("15", "1 2", b"15", {1: 0, 2: 0}, [1], [1, 2, 3], ["1", "2"], [True, 2], [1, float("nan")]):
        ok(delivery_problems(DeliveryHorizon(0, 5, (0, 5), {}, delivery=bad)),
           f"H5 a delivery period {bad!r} that is not a list or tuple of two real numbers is refused")
    ok(delivery_problems(DeliveryHorizon(0, 5, (0, 5), {}, delivery=(1, 2.5))) == [],
       "H5 a tuple of two numbers stays usable")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
