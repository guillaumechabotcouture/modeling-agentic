"""Unit features of the outcome emulator (`analysis.emulator.features`, F6 S2-4).

    analysis:
      emulator:
        features:
          - cov                                             # a units-table column (a number), as before
          - {attribute: season, encoding: harmonic_cos}     # an engine unit attribute, encoded as one number
          - {attribute: past_years, encoding: count}
          - {attribute: past_years, encoding: last, empty: 2000}
          - {attribute: past_type, encoding: equals, value: b}

`attribute` names an attribute bound in `decision_units.engine_attributes` (the value each unit's engine runs get);
the calibrated attribute is not one (it is the emulator's first input already). Encodings:
  value                 the attribute is one number
  count, sum            a list of numbers: its length, its sum (0 for an empty list)
  mean, sd, min, max, first, last
                        a list of numbers (sd: population sd); an empty list takes `empty` (a number), else refused
  harmonic_cos, harmonic_sin, harmonic_amplitude
                        a list of at least 2 numbers with a positive sum, read as one cycle (e.g. a seasonal profile):
                        its first harmonic, relative to the mean (a, b = 2/n sum x_k cos|sin(2 pi k / n) / mean x;
                        amplitude sqrt(a^2 + b^2)); zero for a flat list
  equals                1 when the attribute equals `value` (a category), else 0
Anything else fails closed: an unknown attribute or encoding, a value the encoding cannot read (a text for a numeric
encoding, a mapping, a non-finite number), an empty list without `empty`, or two features with one name.

A feature's name is the column, or `<attribute>.<encoding>` (`<attribute>=<value>` for equals). Features are emulator
inputs only: the design (units, Sobol values) and every batch job are unchanged by them, so a stored batch is reused
when features are added or removed.
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

SCALAR = ("value", "equals")
LIST = ("count", "sum", "mean", "sd", "min", "max", "first", "last", "harmonic_cos", "harmonic_sin",
        "harmonic_amplitude")
ENCODINGS = SCALAR + LIST
KEYS = {"attribute", "encoding", "value", "empty"}
W = "analysis.emulator.features"


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def name(f) -> str:
    if isinstance(f, str):
        return f
    return f"{f['attribute']}={f.get('value')}" if f.get("encoding") == "equals" else f"{f['attribute']}.{f['encoding']}"


def encode(f: dict, v) -> float:
    """One number from an attribute value; ValueError when the encoding cannot read it."""
    e = f["encoding"]
    if e == "equals":
        if isinstance(v, (list, dict)):
            raise ValueError(f"equals needs one value, got {v!r}")
        same = (float(v) == float(f["value"])) if _finite(v) and _finite(f["value"]) else \
            str(v).strip() == str(f["value"]).strip()
        return 1.0 if same else 0.0
    if e == "value":
        if not _finite(v):
            raise ValueError(f"value needs one finite number, got {v!r}")
        return float(v)
    if not isinstance(v, list) or not all(_finite(x) for x in v):
        raise ValueError(f"{e} needs a list of finite numbers, got {v!r}")
    xs = [float(x) for x in v]
    if e == "count":
        return float(len(xs))
    if e == "sum":
        return float(sum(xs))
    if not xs:
        if "empty" in f:
            return float(f["empty"])
        raise ValueError(f"{e} of an empty list: give `empty` (the number an empty list takes)")
    n, m = len(xs), sum(xs) / len(xs)
    if e == "mean":
        return m
    if e == "sd":
        return math.sqrt(sum((x - m) ** 2 for x in xs) / n)
    if e in ("min", "max"):
        return min(xs) if e == "min" else max(xs)
    if e in ("first", "last"):
        return xs[0] if e == "first" else xs[-1]
    if n < 2 or m <= 0:
        raise ValueError(f"{e} needs at least 2 numbers with a positive sum, got {v!r}")
    a = 2.0 / n * sum(x * math.cos(2 * math.pi * k / n) for k, x in enumerate(xs)) / m
    b = 2.0 / n * sum(x * math.sin(2 * math.pi * k / n) for k, x in enumerate(xs)) / m
    return a if e == "harmonic_cos" else b if e == "harmonic_sin" else math.hypot(a, b)


def declared(raw: dict) -> list:
    return list(((raw.get("analysis") or {}).get("emulator") or {}).get("features") or [])


def vector(spec, row: dict) -> list[float]:
    """The unit's feature values, in declaration order (ValueError naming the feature when one cannot be read).
    `spec`: a Spec (toolkit/spec/schema.py) for its unit_attributes()."""
    out, attrs = [], None
    for f in declared(spec.raw):
        if isinstance(f, str):
            out.append(float(row[f]))
            continue
        if attrs is None:
            attrs = spec.unit_attributes(row)
        try:
            out.append(encode(f, attrs[f["attribute"]]))
        except (ValueError, TypeError, KeyError) as e:
            raise ValueError(f"{W}: {name(f)}: {e}") from None
    return out


def problems(spec, cols) -> list[str]:
    """Fail closed on every feature the emulator could not read for every unit."""
    feats = declared(spec.raw)
    if not isinstance(feats, list):
        return [f"{W}: a list"]
    bad = []
    ea = (spec.raw.get("decision_units") or {}).get("engine_attributes") or {}
    cal_attr = (spec.raw.get("calibration") or {}).get("attribute")
    ok = []
    for f in feats:
        if isinstance(f, str):
            (ok.append(f) if f in cols else bad.append(f"{W}: no column {f}"))
            continue
        if not isinstance(f, dict) or set(f) - KEYS or "attribute" not in f or "encoding" not in f:
            bad.append(f"{W}: {f!r}: a column, or {{attribute, encoding[, value][, empty]}}")
            continue
        a, e = f["attribute"], f["encoding"]
        if e not in ENCODINGS:
            bad.append(f"{W}: {a}: unknown encoding {e!r} (one of {', '.join(ENCODINGS)})")
        elif a == cal_attr:
            bad.append(f"{W}: {a} is the calibrated attribute, already the emulator's first input")
        elif a not in ea:
            bad.append(f"{W}: {a} is not an attribute bound in decision_units.engine_attributes")
        elif (e == "equals") != ("value" in f):
            bad.append(f"{W}: {a}.{e}: `value` is the category `equals` compares with, and only for it")
        elif "empty" in f and (e in SCALAR or e in ("count", "sum") or not _finite(f["empty"])):
            bad.append(f"{W}: {a}.{e}: `empty` is a number, for the list encodings an empty list cannot give")
        else:
            ok.append(f)
    names = [name(f) for f in feats if isinstance(f, str) or (isinstance(f, dict) and {"attribute", "encoding"} <= set(f))]
    bad += [f"{W}: two features are named {n}" for n in sorted({n for n in names if names.count(n) > 1})]
    if bad:
        return bad
    from toolkit.spec.schema import Spec
    sub = Spec(dict(spec.raw, analysis={"emulator": {"features": ok}}), spec.base)
    try:
        units = spec.units()
    except OSError as e:
        return [f"{W}: the units table is unreadable ({e})"]
    for u in units:
        try:
            vector(sub, u)
        except (ValueError, TypeError) as e:
            return [f"{e} (unit {u.get(spec.uid)})"]
    return []


def _self_test() -> int:
    import tempfile
    import yaml
    from toolkit.spec import schema
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def raises(f, v):
        try:
            encode(f, v)
            return False
        except ValueError:
            return True
    E = lambda e, **k: dict(attribute="a", encoding=e, **k)  # noqa: E731
    xs = [2.0, 2021.0, 2023.0]
    ok([encode(E(e), xs) for e in ("count", "sum", "mean", "min", "max", "first", "last")]
       == [3.0, 4046.0, 4046.0 / 3, 2.0, 2023.0, 2.0, 2023.0] and abs(encode(E("sd"), [1.0, 3.0]) - 1.0) < 1e-12,
       "FT1 list summaries: count, sum, mean, sd, min, max, first, last")
    ok(encode(E("count"), []) == 0.0 and encode(E("sum"), []) == 0.0 and encode(E("last", empty=2000), []) == 2000.0
       and raises(E("last"), []), "FT2 an empty list: count and sum are 0; other summaries take `empty`, else refused")
    prof = [1 + 0.5 * math.cos(2 * math.pi * (k - 3) / 12) for k in range(12)]
    hc, hs, ha = (encode(E(e), prof) for e in ("harmonic_cos", "harmonic_sin", "harmonic_amplitude"))
    ok(abs(ha - 0.5) < 1e-9 and abs(math.atan2(hs, hc) - 2 * math.pi * 3 / 12) < 1e-9
       and encode(E("harmonic_amplitude"), [1.0] * 12) < 1e-12,
       f"FT3 a cycle's first harmonic: relative amplitude 0.5 and peak at index 3 recovered ({ha:.3f}); flat gives 0")
    ok(encode(E("equals", value="b"), "b") == 1.0 and encode(E("equals", value="b"), "a") == 0.0
       and encode(E("equals", value=2), 2.0) == 1.0 and encode(E("value"), 0.4) == 0.4,
       "FT4 equals gives an indicator of a category; value passes a number")
    ok(all(raises(f, v) for f, v in ((E("value"), "pyr"), (E("value"), [1.0]), (E("mean"), "1 2"),
                                     (E("mean"), [1.0, float("nan")]), (E("value"), float("inf")),
                                     (E("equals", value="b"), ["b"]), (E("harmonic_cos"), [0.0, 0.0]),
                                     (E("mean"), {"k": 1.0}))),
       "FT5 non-numeric, non-finite, wrong-shaped and mapping values are refused")
    with tempfile.TemporaryDirectory() as d:
        p = schema.toy_spec(d, n=6)
        lines = open(os.path.join(d, "units.csv")).read().splitlines()
        open(os.path.join(d, "units.csv"), "w").write("\n".join(
            [lines[0] + ",yrs,kind"] + [ln + (",none" if i == 0 else f",{2018 + i} {2020 + i}") + f",k{i % 2}"
                                        for i, ln in enumerate(lines[1:])]) + "\n")
        r = yaml.safe_load(open(p))
        r["decision_units"]["engine_attributes"] = {"group": {"column": "yrs", "split": " ", "empty": "none"}}

        def probs(feats, attrs=None):
            rr = dict(r, decision_units=dict(r["decision_units"], **({"engine_attributes": attrs} if attrs else {})),
                      analysis=dict(r["analysis"], emulator=dict(r["analysis"]["emulator"], features=feats)))
            return problems(schema.Spec(rr, d), set(lines[0].split(",")) | {"yrs", "kind"})
        good = ["cov", {"attribute": "group", "encoding": "count"}, {"attribute": "group", "encoding": "last",
                                                                      "empty": 2000}]
        ok(probs(good) == [], f"FT6 a column and attribute encodings over every unit validate: {probs(good)}")
        sp = schema.Spec(dict(r, analysis=dict(r["analysis"], emulator={"features": good})), d)
        u0, u1 = sp.units()[0], sp.units()[1]
        ok(vector(sp, u0) == [float(u0["cov"]), 0.0, 2000.0] and vector(sp, u1) == [float(u1["cov"]), 2.0, 2021.0]
           and [name(f) for f in good] == ["cov", "group.count", "group.last"],
           f"FT7 values and names per unit ({vector(sp, u1)})")
        cases = [([{"attribute": "nope", "encoding": "count"}], "not an attribute bound"),
                 ([{"attribute": "group", "encoding": "median"}], "unknown encoding"),
                 ([{"attribute": "x", "encoding": "value"}], "calibrated attribute"),
                 ([{"attribute": "group", "encoding": "last"}], "empty list"),
                 ([{"attribute": "group", "encoding": "value"}], "one finite number"),
                 ([{"attribute": "group", "encoding": "count", "empty": 1}], "`empty` is a number"),
                 ([{"attribute": "group", "encoding": "equals"}], "`value`"),
                 ([{"attribute": "group", "encoding": "count", "extra": 1}], "a column, or"),
                 (["nocol"], "no column nocol"),
                 ([{"attribute": "group", "encoding": "count"}] * 2, "two features are named group.count")]
        got = [probs(f) for f, _ in cases]
        ok(all(g and any(w in s for s in g) for g, (_, w) in zip(got, cases)),
           f"FT8 fail closed: unknown attribute or encoding, the calibrated attribute, unreadable values, misplaced "
           f"keys, duplicate names ({[g[:1] for g in got]})")
        ok(probs([{"attribute": "group", "encoding": "equals", "value": "k1"}],
                 {"group": {"column": "kind"}}) == [], "FT9 a category attribute is usable through equals")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
