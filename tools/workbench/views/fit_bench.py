#!/usr/bin/env python3
"""What the Fit modules' self-tests share (views/fit.py, fit_adapters.py, fit_charts.py): fixture runs built once
each by fixtures.py, and readers of a view result that say what is wrong.

    python3 tools/workbench/views/fit_bench.py --self-test
"""
from __future__ import annotations

import os
import sys

_WB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO = os.path.dirname(os.path.dirname(_WB))
for _p in (_REPO, os.path.join(_REPO, "scripts"), _WB):     # _REPO: tools.workbench.fixtures; scripts: lib.safe_io
    if _p not in sys.path:
        sys.path.insert(0, _p)
from lib.safe_io import read_nofollow, write_nofollow  # noqa: E402


class Bench:
    """What the Fit modules' self-tests share: fixture runs built once each (fixtures.py), the function under test
    (it takes a workbench.Run and returns a view result), and readers of that result that say what is wrong."""

    def __init__(self, view):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory(prefix="fit-self-test-")      # removed by done(), or when collected
        self.view, self.tmp, self.built, self.fails = view, self._tmp.name, {}, []

    def folder(self, method, defect=None, upto=None):
        """A built fixture run's folder (built once per variant; never edited in place)."""
        fixtures = fixture_builder()
        key = (method, defect, upto)
        if key not in self.built:
            self.built[key] = os.path.join(self.tmp, f"run{len(self.built)}")
            fixtures.build(self.built[key], upto=upto, defect=defect, method=method)
        return self.built[key]

    def edited(self, method, edit, defect=None):
        """A copy of a built run with one file rewritten or removed by `edit(folder)`. The builder writes plain files
        only; each is read and written below the bench's folder with no link followed (lib.safe_io)."""
        src, name = self.folder(method, defect), f"edited{len(os.listdir(self.tmp))}"
        os.makedirs(os.path.join(self.tmp, name))
        for at, _, files in os.walk(src):
            for f in files:
                rel = os.path.relpath(os.path.join(at, f), src).replace(os.sep, "/")
                write_nofollow(self.tmp, f"{name}/{rel}", read_nofollow(self.tmp, f"{os.path.basename(src)}/{rel}")[0])
        edit(os.path.join(self.tmp, name))
        return os.path.join(self.tmp, name)

    def look_at(self, d):
        import workbench
        v = self.view(workbench.Run(d))
        need(isinstance(v, dict) and set(v) >= {"checks", "facts", "tables", "charts", "notes"},
             "view returns {checks, facts, tables, charts, notes}")
        return v

    def case(self, name, fn):
        try:
            fn()
            why = None
        except Exception as e:      # a case reports and the next one runs
            why = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        print(f"  {'ok  ' if why is None else 'FAIL'} {name}" + (f": {why[:300]}" if why else ""))
        if why is not None:
            self.fails.append(name.split()[0])

    def done(self):
        self._tmp.cleanup()
        print("OK: all self-test cases passed." if not self.fails else f"FAILED: {self.fails}")
        return 1 if self.fails else 0


def fixture_builder():
    """tools/workbench/fixtures.py, imported by its full name from the repo root: scripts/ holds another module
    named fixtures, so the bare name would depend on the order of sys.path."""
    from tools.workbench import fixtures
    if os.path.dirname(os.path.abspath(fixtures.__file__)) != _WB:
        raise ImportError(f"tools.workbench.fixtures was loaded from {fixtures.__file__}, not from {_WB}")
    return fixtures


def need(cond, why):
    if not cond:
        raise AssertionError(why)


def close(a, b):
    return isinstance(a, (int, float)) and not isinstance(a, bool) and abs(a - b) <= 1e-6 * max(1.0, abs(b))


def data(d, rel):
    import yaml
    with open(os.path.join(d, rel), encoding="utf-8") as f:
        return yaml.safe_load(f)["data"]


def rewrite(rel, change):
    """An edit for Bench.edited: the YAML file `rel` with `change(document)` applied."""
    def edit(d):
        import yaml
        with open(os.path.join(d, rel), encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        change(doc)
        write_nofollow(d, rel, yaml.safe_dump(doc, sort_keys=False).encode("utf-8"))
    return edit


def check(v, name):
    found = [c for c in v["checks"] if str(c.get("id")).split(".")[-1] == name]
    need(len(found) == 1, f"one check named {name} (found {len(found)})")
    return found[0]


def statuses(v):
    return {str(c.get("id")).split(".")[-1]: c.get("status") for c in v["checks"]}


def table(v, name):
    from views.fit_adapters import COLUMNS
    t, columns = v["tables"].get(name), COLUMNS[name]
    need(isinstance(t, dict) and t.get("columns") == columns,
         f"tables.{name} has columns {columns} (got {t.get('columns') if isinstance(t, dict) else t})")
    need(all(isinstance(r, list) and len(r) == len(columns) for r in t["rows"]),
         f"tables.{name} rows are lists in column order")
    return [dict(zip(columns, r)) for r in t["rows"]]


def row(v, name, key, value):
    found = [r for r in table(v, name) if r[key] == value]
    need(len(found) == 1, f"one row of tables.{name} with {key} {value!r} (found {len(found)})")
    return found[0]


def absent(v, name):       # §3a: a table the method does not record is present with the value null
    return name in v["tables"] and v["tables"][name] is None


def says(c):
    return " ".join(str(c.get("says") or "").lower().replace("_", " ").split())


def _self_test():
    b = Bench(lambda run: {"checks": [{"id": "fit.a", "status": "ok", "says": "Some_Thing  said"}], "facts": {}, "charts": [],
                           "notes": {}, "tables": {"offsets": {"columns": ["key", "mean", "sd", "accepted_share"],
                                                               "rows": [["k", 0.1, 0.2, 1.0]]}, "series": None}})

    def builds():
        d = b.folder("history_matching")
        need(d == b.folder("history_matching") and os.path.exists(os.path.join(d, "run_state.json")), "a variant is built once")
        e = b.edited("history_matching", lambda folder: os.remove(os.path.join(folder, "run_state.json")))
        need(e != d and not os.path.exists(os.path.join(e, "run_state.json")) and os.path.exists(os.path.join(d, "run_state.json")),
             "an edit is made on a copy, never in the built run")
        need(hasattr(fixture_builder(), "DEFECTS"), "the builder is tools/workbench/fixtures.py, not another module of that name")

    def reads():
        v = b.look_at(b.folder("history_matching"))
        need(row(v, "offsets", "key", "k")["accepted_share"] == 1.0 and absent(v, "series") and not absent(v, "offsets"), "table rows by column")
        need(statuses(v) == {"a": "ok"} and says(check(v, "a")) == "some thing said" and close(1.0000001, 1.0) and not close(True, 1), "checks")
        for wrong in (lambda: row(v, "offsets", "key", "none"), lambda: check(v, "none"), lambda: table(v, "series")):
            try:
                wrong()
            except (AssertionError, AttributeError):
                continue
            raise AssertionError("a reader that finds nothing must say so")

    b.case("B1 a fixture run is built once per variant, and edited only as a copy", builds)
    b.case("B2 the readers return rows and checks by name, and fail with a reason when there is none", reads)
    return b.done()


if __name__ == "__main__":
    if "--self-test" in sys.argv[1:]:
        sys.exit(_self_test())
    sys.exit("usage: fit_bench.py --self-test")
