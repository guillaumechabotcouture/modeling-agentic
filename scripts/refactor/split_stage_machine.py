#!/usr/bin/env python3
"""Split agents/stage_machine.py mechanically and deterministically (architecture wave 1, owner yes 2026-11-22).

Many open PRs edit agents/stage_machine.py, so a split made by hand would conflict with all of them. This script
makes the split from whatever stage_machine.py is current: re-run it on the latest base and open the PR from its
output. It never edits a line of logic: every function, class and method moves verbatim (the only rewrites are
listed below), and it stops with an error, naming the code, when it meets something it cannot classify.

    python3 scripts/refactor/split_stage_machine.py --stage tests     # 1: the inline self-tests -> tests/stage_machine/
    python3 scripts/refactor/split_stage_machine.py --stage package   # 2: the module -> the package agents/stage_machine/ (+ CLI)
    python3 scripts/refactor/split_stage_machine.py --stage all       # both, in order
    python3 scripts/refactor/split_stage_machine.py --verify          # the package matches the module it came from
    python3 scripts/refactor/split_stage_machine.py --compare-cases BEFORE.txt AFTER.txt
    python3 scripts/refactor/split_stage_machine.py --self-test

Each stage is idempotent: on a file it has already split it does nothing and says so. `--repo DIR` runs on
another checkout; the default is this one. Run it with the repo's Python (CI's 3.14): free names are read with
`symtable`.

Stage `tests`. The self-test functions (`_self_test`, `_self_test_<x>`) and the helpers only they use (named
`_t_*` or `_TOY_*`) move to tests/stage_machine/: `_self_test` to sm_selftest_core.py, `_self_test_<x>` to
sm_selftest_<x>.py, the helpers to sm_selftest_common.py. `_self_test` in the module becomes a stub that runs
them against the module itself, so `python3 agents/stage_machine.py --self-test` prints the same cases, and
scripts/self_tests.py still finds it (tests/ is outside its scope; the module keeps its --self-test flag). The
moved code reads the module's names through imports. Three test idioms that rebind the module's own globals are
rewritten so they still rebind the module, by assignment to it (`_SM`): `global X` (the statement is removed and
X becomes `_SM.X`), `globals()` (becomes `SM_GLOBALS`, a mapping that writes through to the module) and
`sys.modules[Machine.__module__]` (becomes `_SM`); every other use of a name rebound this way also reads `_SM.X`.

Stage `package`. The module becomes the package agents/stage_machine/ (owner, 2026-11-22). A package shadows a
module of the same name for every importer, so `from agents.stage_machine import X`, `from agents import
stage_machine` and `import stage_machine` (agents/ on sys.path) all get the package:
  __init__.py      the import surface: re-exports every name the module had, keeps main() and the self-test
                   stub, and forwards an assignment to itself (`stage_machine.v2_gate = ...`, as
                   scripts/golden_traces.py and the self-tests do) to every package module that holds the same
                   object (namespace.py), as one module did
  common.py        paths, file names, caps, RunState and persistence (TABLE)
  edges.py         STATES, EDGES, LLM_STATES, POST_CHOICE_STATES, rethink_target, route_gate (TABLE)
  amend.py, signoff.py, findings.py, owner.py, report.py, failures.py, run.py   shared helpers (TABLE)
  machine.py       the Machine class: its loop, budget, journal and every method two states share
  states/<s>.py    one per `h_<STATE>` handler, a class registered by name (`@handler("GATE")`), holding the
                   handler and the methods only it calls; Machine inherits every registered class, so
                   `getattr(self, "h_" + state)` in Machine.step finds them as before
A module-level helper that TABLE does not name goes where its callers are, when they are all in one module
(the call graph); one used by several modules, or by none, stops the script: add it to TABLE. The file
agents/stage_machine.py stays as the command line only (`python3 agents/stage_machine.py sign ...`, which
cloud/worker_side.py and tools/run_console run): it puts agents/ on sys.path, imports main() from the package
and keeps the module's `if __name__ == "__main__"` block. Rewritten lines: `HERE` in common.py climbs one
directory more, so it is agents/ as before, and the path block of __init__.py does the same.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import os
import re
import subprocess
import symtable
import sys
import tempfile
import time
from dataclasses import dataclass, field

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODULE = "agents/stage_machine.py"
TESTS_DIR = "tests/stage_machine"
PKG = "stage_machine"
PKG_DIR = "agents/" + PKG
TARGET = "_stage_machine_under_test"      # sys.modules key the stub gives the module the moved tests run against
TEST_PREFIX = "sm_selftest_"
STAMP_TESTS = "# split_stage_machine: self-tests moved to tests/stage_machine/"
STAMP_PKG = "# split_stage_machine: the code lives in the package agents/stage_machine/"
GENERATED = "Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py."
BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__",
                                 "__builtins__", "__annotations__", "__dict__", "__debug__"}
TEST_FN = re.compile(r"^_self_test(_\w+)?$")
TEST_HELPER = re.compile(r"^(_t_\w+|_TOY_\w+)$")
HERE_LINE = "HERE = os.path.dirname(os.path.abspath(__file__))"
HERE_PKG = "HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # agents/, as before the split"
SHIM_UNITS = ("_self_test", "main")       # stay in agents/stage_machine.py (with the `if __name__` block)

# Stage 2: where each shared module-level name goes. A name not listed goes where all its callers are; a name
# used by several modules, or by none, and not listed, stops the script. A listed name that no longer exists is
# reported, not an error.
TABLE = {
    "common": ["HERE", "REPO", "_p", "STATE_FILE", "SIGNOFF_FILE", "SCOPE_SIGNOFF", "SIGNOFF_SHEET", "CRITICS",
               "PROTECTED_BY_FIX", "TASK_CAP_USD", "READBACK_CAP_USD", "MIN_TASK_ALLOWANCE_USD", "AWAITING_BUDGET",
               "AwaitingBudget", "DECISION_BEARING_KINDS", "MAX_SPEC_TRIES", "MAX_SCHEMA_REPAIRS",
               "MAX_SIGN_REPAIRS", "RunState", "_now", "load_state", "_withhold_report", "_quarantine_report",
               "_put", "save_state", "_write_json", "AWAITING_SPEC_AMENDMENT", "SPEC_RETHINK_REQUEST",
               "ACCEPTED_STATUSES", "DECLARED_FILE", "PARENT_ENV", "_remedy", "short_label", "_state_bytes",
               "_sha256"],
    "amend": ["AMENDED_PROVISIONAL", "AMENDMENT_RECORD", "AMEND_LOCK", "AmendmentLocked", "_LOCK_HELD", "_amend_lock",
              "_child_of", "_parent_of", "_own_spec_digest", "_made_by", "handoff_child", "_state_or_none",
              "handoff_chain", "iter_handoff_chain", "_kept_child"],
    "edges": ["STATES", "EDGES", "LLM_STATES", "POST_CHOICE_STATES", "rethink_target", "route_gate"],
    "signoff": ["signed_hash", "_signoff", "sign", "spec_signoff_findings", "_spec_signed_sha", "spec_signed_once",
                "evidence_gate"],
    "findings": ["harness_owned", "owned_path", "deliverable_problems", "report_claim_findings",
                 "report_acceptance_findings", "given_values", "typed_numbers", "_SUFFIX"],
    "owner": ["OWNER_REQUEST", "OWNER_REQUEST_PRODUCER", "request_owner_amendments", "_answer_counts"],
    "report": ["render_report", "_status_line", "STATUS_MARK", "provisional_lines", "_write_deliverable"],
    "failures": ["_rep_error", "_RESUME_WHY", "_KILL_SIGNALS", "analysis_failure_cause", "BATCH_DIR", "JC_DIR",
                 "_infra_batch", "resumable_failure", "_with_abs_paths", "_pre_engine_check"],
    "run": ["init_run", "run_v2", "_record_invocation", "_runtime_ids", "_parent_record"],
}
SHARED_ORDER = ("common", "edges", "amend", "signoff", "findings", "owner", "report", "failures")
DOCS = {
    "common": "Paths, run-file names, caps, the run state and its persistence.",
    "edges": "The state table: STATES, EDGES and the gate's routing (route_gate).",
    "amend": "Lead amendments of a signed spec: the amendment lock and the parent/child hand-off chain of runs.",
    "signoff": "The decision spec's sign-off: the signed hash, the record, sign() and the evidence preconditions.",
    "findings": "Harness findings on the deliverable and the report (owned paths, claims, acceptance language).",
    "owner": "Owner requests: amendments put to the owner, and the answers counted.",
    "report": "report.md: the harness's status line, provisional decisions and the deliverable.",
    "failures": "Why an analysis failed, and whether a resume may re-run it.",
    "run": "Starting and running a v2 run: init_run and run_v2.",
    "machine": "The Machine: the loop (step, run), transitions, budget, journal and the methods states share.",
}


class SplitError(Exception):
    """Code the script cannot classify or move without guessing."""


# ----------------------------------------------------------------------------------------------------- parsing

@dataclass
class Unit:
    node: ast.stmt
    first: int                 # first line of the statement (decorators included), 1-based
    last: int
    lead: int                  # first line of the text attached to it (comments and blank lines before it)
    names: list = field(default_factory=list)
    kind: str = ""             # doc, future, import, def, class, assign, other

    def text(self, lines) -> str:
        return "".join(lines[self.lead - 1:self.last])


def bound_names(node) -> list:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, (ast.Import, ast.ImportFrom)):
        return [a.asname or a.name.split(".")[0] for a in node.names]
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
        return [n.id for t in tgts for n in ast.walk(t) if isinstance(n, ast.Name)]
    if isinstance(node, ast.For):
        return [n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)]
    return []


def units_of(src: str):
    tree = ast.parse(src)
    out, prev = [], 0
    for i, n in enumerate(tree.body):
        first = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        if first <= prev:
            raise SplitError(f"line {first}: two statements share a line; split them by hand first")
        if i == 0 and isinstance(n, ast.Expr) and isinstance(getattr(n, "value", None), ast.Constant) \
                and isinstance(n.value.value, str):
            kind = "doc"
        elif isinstance(n, ast.ImportFrom) and n.module == "__future__":
            kind = "future"
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            kind = "import"
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "def"
        elif isinstance(n, ast.ClassDef):
            kind = "class"
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            kind = "assign"
        else:
            kind = "other"
        out.append(Unit(n, first, n.end_lineno, prev + 1, bound_names(n), kind))
        prev = n.end_lineno
    return tree, out


def _children_in(tab, a: int, b: int):
    return [c for c in tab.get_children() if a <= c.get_lineno() <= b]


def _table_globals(tab) -> set:
    """Names a symbol table (and every table nested in it) reads or binds as module globals."""
    out = set()
    for s in tab.get_symbols():
        if s.is_global() and (s.is_referenced() or s.is_assigned()):
            out.add(s.get_name())
    for c in tab.get_children():
        out |= _table_globals(c)
    return out


def _scope_level_names(node) -> set:
    """Names read at the statement's own scope: the code that runs where it stands (decorators, defaults, bases,
    an assignment's value), not the bodies of the functions, lambdas and classes it defines."""
    out = set()

    def walk(n):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for x in n.decorator_list + n.args.defaults + [d for d in n.args.kw_defaults if d is not None]:
                walk(x)
            return
        if isinstance(n, ast.Lambda):
            for x in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]:
                walk(x)
            return
        if isinstance(n, ast.ClassDef):
            for x in n.decorator_list + n.bases + [k.value for k in n.keywords]:
                walk(x)
            return
        if isinstance(n, ast.Name):
            out.add(n.id)
        for c in ast.iter_child_nodes(n):
            walk(c)
    walk(node)
    return out


def _annotation_names(node) -> set:
    """Names in annotations. Under `from __future__ import annotations` they are strings that nothing evaluates
    at import, but each module imports them anyway, so typing.get_type_hints() resolves as it did."""
    out = set()
    for n in ast.walk(node):
        anns = []
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = n.args
            anns = [x.annotation for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]
                    if x is not None] + [n.returns]
        elif isinstance(n, ast.AnnAssign):
            anns = [n.annotation]
        for x in anns:
            if x is not None:
                out |= {m.id for m in ast.walk(x) if isinstance(m, ast.Name)}
    return out


def unit_refs(top_table, u: Unit) -> set:
    """Every module-level name a unit reads or binds, besides its own definition."""
    refs = _scope_level_names(u.node)
    for c in _children_in(top_table, u.first, u.last):
        refs |= _table_globals(c)
    return refs


def method_tables(top_table, cls: ast.ClassDef) -> dict:
    tabs = [c for c in top_table.get_children() if c.get_name() == cls.name and c.get_lineno() == cls.lineno]
    if len(tabs) != 1:
        raise SplitError(f"class {cls.name}: no single symbol table")
    return {c.get_lineno(): c for c in tabs[0].get_children()}


# ------------------------------------------------------------------------------------------------- text edits

def apply_edits(lines: list, edits: list) -> list:
    """edits: (lineno, start_byte, end_byte, new_text) within one line (ast offsets are UTF-8 bytes);
    (lineno, None, None, "") deletes the line. Returns new lines."""
    out = list(lines)
    seen = set()
    for ln, a, b, new in sorted(edits, key=lambda e: (e[0], -1 if e[1] is None else e[1]), reverse=True):
        if (ln, a) in seen:
            raise SplitError(f"line {ln}: two rewrites at one place")
        seen.add((ln, a))
        if a is None:
            out[ln - 1] = ""
            continue
        raw = out[ln - 1].encode("utf-8")
        out[ln - 1] = (raw[:a] + new.encode("utf-8") + raw[b:]).decode("utf-8")
    return out


def wrap_import(head: str, names, width: int = 116, noqa: str = "") -> str:
    names = list(names)
    one = f"{head}{', '.join(names)}"
    if len(one) + len(noqa) <= width:
        return one + noqa + "\n"
    rows, cur = [], "    "
    for n in names:
        piece = n + ", "
        if len(cur) + len(piece) > width and cur.strip():
            rows.append(cur.rstrip())
            cur = "    "
        cur += piece
    rows.append(cur.rstrip())
    return head + "(" + noqa + "\n" + "\n".join(rows) + "\n)\n"


def render_import(node, keep) -> str:
    if isinstance(node, ast.Import):
        return "".join(f"import {a.name}" + (f" as {a.asname}" if a.asname else "") + "\n"
                       for a in node.names if (a.asname or a.name.split(".")[0]) in keep)
    if node.level:
        raise SplitError(f"line {node.lineno}: a relative import in the module")
    names = [a.name + (f" as {a.asname}" if a.asname else "") for a in node.names if (a.asname or a.name) in keep]
    return f"from {node.module} import {', '.join(names)}\n" if names else ""


def import_block(import_units, needed: set) -> str:
    return "".join(render_import(u.node, needed) for u in import_units if needed & set(u.names))


# ------------------------------------------------------------------------------------------- stage 1: tests

def classify_tests(units):
    """(test units grouped by output stem, index of the first test unit). Fails on code in the test region it
    cannot classify, and on a test-named helper that production code uses."""
    by_name = {n: u for u in units for n in u.names}
    first = next((i for i, u in enumerate(units) if u.kind in ("def", "assign") and u.names
                  and all(TEST_FN.match(n) or TEST_HELPER.match(n) for n in u.names)), None)
    if first is None:
        return {}, None
    main_i = next((i for i, u in enumerate(units) if u.names == ["main"]), None)
    if main_i is None or main_i < first:
        raise SplitError("no main() after the self-tests: the module's layout is not the one this script knows")
    groups = {}
    for u in units[first:main_i]:
        if not u.names:
            raise SplitError(f"line {u.first}: a statement with no name among the self-tests "
                             f"({ast.unparse(u.node)[:60]!r})")
        for n in u.names:
            if TEST_FN.match(n):
                stem = "core" if n == "_self_test" else n[len("_self_test_"):]
            elif TEST_HELPER.match(n):
                stem = "common"
            else:
                raise SplitError(f"line {u.first}: `{n}` sits among the self-tests but is named like neither a "
                                 f"self-test (_self_test_<x>) nor a test helper (_t_*, _TOY_*)")
            if len({("core" if TEST_FN.match(m) and m == "_self_test" else
                     m[len("_self_test_"):] if TEST_FN.match(m) else "common") for m in u.names}) > 1:
                raise SplitError(f"line {u.first}: one statement binds names for two test files")
            if u not in groups.setdefault(stem, []):     # `_TOY_A = _TOY_B = ...` moves once
                groups[stem].append(u)
    late = [n for u in units[main_i + 1:] for n in u.names if TEST_FN.match(n) or TEST_HELPER.match(n)]
    if late:
        raise SplitError(f"self-test code after main(): {late}")
    early = [n for u in units[:first] for n in u.names if TEST_FN.match(n) and n != "_self_test"]
    if early:
        raise SplitError(f"self-test functions before the self-test region: {early}")
    if "_self_test" not in by_name or by_name["_self_test"] not in groups.get("core", []):
        raise SplitError("no _self_test() in the self-test region")
    return groups, first


def _test_rewrites(tree_src: str, tests: list):
    """The edits that keep the moved tests rebinding the module, and the names they rebind."""
    rebound, edits, uses_globals = set(), [], False
    for u in tests:
        holders, g_holders = set(), set()
        for n in ast.walk(u.node):
            if isinstance(n, ast.Global):
                rebound |= set(n.names)
            if isinstance(n, ast.Name) and n.id in ("__file__", "__name__", "_SM", "SM_GLOBALS"):
                raise SplitError(f"line {n.lineno}: `{n.id}` in a self-test means something else once the test "
                                 f"moves; rewrite it by hand first")
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("exec", "eval", "vars",
                                                                                          "locals") \
                    and not (n.func.id == "vars" and n.args):
                raise SplitError(f"line {n.lineno}: `{n.func.id}()` in a self-test reads a namespace that moves")
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                v = n.value
                if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "globals":
                    holders.add(n.targets[0].id)
                    g_holders.add(n.targets[0].id)
                if _is_machine_module(v):
                    holders.add(n.targets[0].id)
        _check_namespace_uses(u.node, g_holders)
        for n in ast.walk(u.node):
            if isinstance(n, ast.Subscript) and (isinstance(n.value, ast.Name) and n.value.id in holders
                                                 or _is_globals_call(n.value)) \
                    and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                rebound.add(n.slice.value)
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in holders \
                    and isinstance(n.ctx, ast.Store):
                rebound.add(n.attr)
            if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "modules" \
                    and isinstance(n.value.value, ast.Name) and n.value.value.id == "sys" and not _is_machine_module(n):
                raise SplitError(f"line {n.lineno}: sys.modules[...] in a self-test other than "
                                 f"sys.modules[Machine.__module__]")
    _check_bindings(tests, rebound)
    shadow = sorted(rebound & set(dir(builtins)))
    if shadow:
        raise SplitError(f"a self-test rebinds the builtin(s) {shadow} in the module; patch them another way first")
    for u in tests:
        for n in ast.walk(u.node):
            if isinstance(n, ast.Global):
                line = tree_src.splitlines()[n.lineno - 1]
                if n.end_lineno != n.lineno or line.strip() != ast.unparse(n):
                    raise SplitError(f"line {n.lineno}: a `global` statement that is not alone on its line")
                edits.append((n.lineno, None, None, ""))
            elif _is_machine_module(n):
                edits.append((n.lineno, n.col_offset, n.end_col_offset, "_SM"))
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "globals" and not n.args:
                edits.append((n.lineno, n.col_offset, n.end_col_offset, "SM_GLOBALS"))
                uses_globals = True
            elif isinstance(n, ast.Name) and n.id in rebound:
                edits.append((n.lineno, n.col_offset, n.end_col_offset, "_SM." + n.id))
    return rebound, edits, uses_globals


def _is_globals_call(n) -> bool:
    return isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "globals" and not n.args


def _check_namespace_uses(node, holders: set):
    """globals() (or an alias of it) is used only in the ways SM_GLOBALS writes through to the module: a string
    subscript (read, write), .get(), or one alias of globals() itself. Anything else (update, keys, pop, del,
    `in`, an alias of the alias) would act on a private dict or see the moved names missing."""
    parent = {c: p for p in ast.walk(node) for c in ast.iter_child_nodes(p)}
    for n in ast.walk(node):
        if not (_is_globals_call(n) or isinstance(n, ast.Name) and n.id in holders and isinstance(n.ctx, ast.Load)):
            continue
        p = parent.get(n)
        ok_ = (isinstance(p, ast.Subscript) and p.value is n and isinstance(p.slice, ast.Constant)
               and isinstance(p.slice.value, str)) \
            or (isinstance(p, ast.Attribute) and p.attr == "get" and isinstance(parent.get(p), ast.Call)) \
            or (isinstance(p, ast.Assign) and p.value is n and _is_globals_call(n) and len(p.targets) == 1
                and isinstance(p.targets[0], ast.Name))
        if ok_ and isinstance(p, ast.Subscript) and isinstance(p.ctx, ast.Del):
            ok_ = False
        if not ok_:
            raise SplitError(f"line {n.lineno}: a use of globals() in a self-test that SM_GLOBALS cannot write "
                             f"through ({ast.unparse(p)[:60]!r}); rewrite it as a subscript first")


def _check_bindings(tests, rebound: set):
    """A name the rewrite turns into `_SM.X` must be bound in the tests only by a plain assignment (which becomes
    an assignment to the module), and `Machine` must be the module's class wherever a test names it."""
    for u in tests:
        comp_targets = set()
        for n in ast.walk(u.node):
            if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
                for g in n.generators:
                    comp_targets |= {m.id for m in ast.walk(g.target) if isinstance(m, ast.Name)}
        for n in ast.walk(u.node):
            bound = []
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound = [n.name]
            elif isinstance(n, ast.alias):
                bound = [n.asname or n.name.split(".")[0]]
            elif isinstance(n, ast.ExceptHandler) and n.name:
                bound = [n.name]
            elif isinstance(n, ast.arg):
                bound = [n.arg]
            elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
                bound = [n.name]
            elif isinstance(n, ast.MatchMapping) and n.rest:
                bound = [n.rest]
            elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)) and n.id in ("Machine",
                                                                                                    "globals"):
                bound = [n.id]
            for b in bound:
                if b in rebound or b in ("Machine", "globals"):
                    raise SplitError(f"line {getattr(n, 'lineno', u.first)}: `{b}` is bound in a self-test other "
                                     f"than by a plain assignment; rename it first")
        bad = sorted(comp_targets & (rebound | {"Machine"}))
        if bad:
            raise SplitError(f"line {u.first}: {bad} is a comprehension variable in a self-test and a name the "
                             f"tests rebind in the module; rename it first")


def _is_machine_module(n) -> bool:
    return (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Attribute) and n.value.attr == "modules"
            and isinstance(n.value.value, ast.Name) and n.value.value.id == "sys"
            and isinstance(n.slice, ast.Attribute) and n.slice.attr == "__module__"
            and isinstance(n.slice.value, ast.Name) and n.slice.value.id == "Machine")


def _check_rebound_never_local(src: str, rebound: set, tests):
    top = symtable.symtable(src, MODULE, "exec")
    for u in tests:
        for c in _children_in(top, u.first, u.last):
            stack = [c]
            while stack:
                t = stack.pop()
                for s in t.get_symbols():
                    if s.get_name() in rebound and not s.is_global() and (s.is_assigned() or s.is_parameter()
                                                                          or s.is_imported()):
                        raise SplitError(f"`{s.get_name()}` is rebound in the module by a self-test and also a "
                                         f"local name in {t.get_name()} (line {t.get_lineno()}): rename it first")
                stack += t.get_children()


def _file_globals(body: str, path: str) -> set:
    """The module-level names a generated file's body reads but does not define."""
    top = symtable.symtable(body, path, "exec")
    used, defined = set(), set()
    for s in top.get_symbols():
        if s.is_assigned() or s.is_imported() or s.is_namespace():
            defined.add(s.get_name())
        if s.is_referenced():
            used.add(s.get_name())
    for c in top.get_children():
        used |= _table_globals(c)
    return used - defined - BUILTINS


STUB = '''def _self_test() -> int:
    """The cases live in tests/stage_machine/ (moved there verbatim by scripts/refactor/split_stage_machine.py);
    they run against this module, imported there as {target}."""
    sys.modules["{target}"] = sys.modules[__name__]
    d = os.path.join(REPO, "tests", "stage_machine")
    for k, m in list(sys.modules.items()):  # a second call (another copy, another checkout) binds the cases afresh
        if k.startswith("{prefix}") and os.path.basename(os.path.dirname(getattr(m, "__file__", None) or "")) \
                == "stage_machine":
            del sys.modules[k]
    while d in sys.path:
        sys.path.remove(d)
    sys.path.insert(0, d)           # this checkout's cases, not another's
    import {prefix}core
    return {prefix}core._self_test()
'''

NS_HELPER = '''

class _ModuleGlobals(dict):
    """globals() of the stage machine module, for the moved self-tests: reads its names and writes them by
    assignment to it, so a patch reaches every module that holds the name (split_stage_machine.py)."""

    def __init__(self, module):
        super().__init__()
        self._m = module

    def __getitem__(self, k):
        try:
            return getattr(self._m, k)
        except AttributeError:
            raise KeyError(k) from None

    def __setitem__(self, k, v):
        setattr(self._m, k, v)

    def __contains__(self, k):
        return hasattr(self._m, k)

    def get(self, k, default=None):
        return getattr(self._m, k, default)


SM_GLOBALS = _ModuleGlobals(_SM)
'''


def stage_tests(repo: str, log=print) -> bool:
    """Stage 1. Returns True when it changed files, False when the module is already split."""
    path = os.path.join(repo, MODULE)
    src = open(path, encoding="utf-8").read()
    tree, units = units_of(src)
    groups, first = classify_tests(units)
    if STAMP_TESTS in src or STAMP_PKG in src:
        if groups and any(n != "_self_test" for us in groups.values() for u in us for n in u.names):
            raise SplitError("the module is stamped as split but holds inline self-tests again: re-run the script "
                             "on a base that has not been split")
        log("tests: already split, nothing to do")
        return False
    if not groups:
        raise SplitError("no self-tests found in the module")
    lines = src.splitlines(keepends=True)
    tests = [u for us in groups.values() for u in us]
    test_names = {n for u in tests for n in u.names}
    prod = [u for u in units if u not in tests]
    prod_refs = set()
    top = symtable.symtable(src, MODULE, "exec")
    for u in prod:
        if u.names == ["_self_test"]:
            continue
        prod_refs |= unit_refs(top, u)
    for u in prod:          # a name reached by string (globals()["_t_x"], getattr) counts as a use too
        if u.names != ["_self_test"]:
            prod_refs |= {c.value for c in ast.walk(u.node) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
            computed = [c for c in ast.walk(u.node) if isinstance(c, ast.Subscript) and _is_globals_call(c.value)
                        and not isinstance(c.slice, ast.Constant)]
            if computed:
                raise SplitError(f"line {computed[0].lineno}: production code reads globals() with a computed key; "
                                 f"the script cannot tell whether it reaches test code")
    used_by_prod = sorted((test_names - {"_self_test"}) & prod_refs)
    if used_by_prod:
        raise SplitError(f"production code uses test-named code: {used_by_prod}")
    rebound, edits, uses_globals = _test_rewrites(src, tests)
    _check_rebound_never_local(src, rebound, tests)
    if uses_globals:
        groups.setdefault("common", [])      # SM_GLOBALS lives there, with or without test helpers
    new_lines = apply_edits(lines, edits)
    defined_in = {n: stem for stem, us in groups.items() for u in us for n in u.names}
    imports = [u for u in units if u.kind == "import"]
    import_names = {n for u in imports for n in u.names}
    module_names = {n for u in prod for n in u.names} - import_names
    out_dir = os.path.join(repo, TESTS_DIR)
    if os.path.isdir(out_dir):
        extra = sorted(f for f in os.listdir(out_dir) if f != "__pycache__" and not f.startswith(TEST_PREFIX))
        if extra:
            raise SplitError(f"{TESTS_DIR} holds files this script did not write: {extra}")
    files, deps = {}, {}
    stems = sorted(groups, key=lambda s: (s != "common", s != "core", min([u.first for u in groups[s]] or [0])))
    for stem in stems:
        body = "".join("".join(new_lines[u.lead - 1:u.last]) for u in groups[stem])
        body = body.lstrip("\n")
        if stem == "common" and uses_globals:
            body = body.rstrip("\n") + "\n" + NS_HELPER
        fname = f"{TEST_PREFIX}{stem}.py"
        free = _file_globals(body, fname)
        need_sm = "_SM" in free or (stem == "common" and uses_globals)
        free.discard("_SM")
        from_tests, from_mod, from_imp = {}, set(), set()
        for n in sorted(free):
            if n == "SM_GLOBALS":
                from_tests.setdefault("common", set()).add(n)
            elif n in defined_in and defined_in[n] != stem:
                from_tests.setdefault(defined_in[n], set()).add(n)
            elif n in import_names:
                from_imp.add(n)
            elif n in module_names:
                from_mod.add(n)
            else:
                raise SplitError(f"{fname}: `{n}` is neither defined in the module nor a builtin")
        deps[stem] = set(from_tests)
        head = (f'"""Self-tests of agents/stage_machine.py ({stem}), run by `python3 agents/stage_machine.py '
                f'--self-test`.\n\n{GENERATED}\nThe module under test is imported as {TARGET}.\n"""\n'
                "from __future__ import annotations\n\n")
        imp = import_block(imports, from_imp)
        if need_sm:
            imp += f"import {TARGET} as _SM\n"
        if from_mod:
            imp += wrap_import(f"from {TARGET} import ", sorted(from_mod))
        for s in sorted(from_tests):
            imp += wrap_import(f"from {TEST_PREFIX}{s} import ", sorted(from_tests[s]))
        files[fname] = head + imp + "\n\n" + body.rstrip("\n") + "\n"
    _check_acyclic(deps, "test files")
    # the module: the test units go, the stub takes _self_test's place, the stamp follows the docstring
    stub = STUB.format(target=TARGET, prefix=TEST_PREFIX)
    out = []
    for u in units:
        if u in tests:
            if u.names == ["_self_test"]:
                out.append("\n\n" + stub)
            continue
        t = u.text(lines)
        if u.kind == "doc":
            t = t.rstrip("\n") + "\n" + STAMP_TESTS + "\n"
        out.append(t)
    out.append("".join(lines[units[-1].last:]))
    new_src = "".join(out)
    # the module last: its stamp marks the split done, so an interrupted write is re-run, not taken as split
    _write_tree(repo, {**{f"{TESTS_DIR}/{k}": v for k, v in files.items()}, MODULE: new_src})
    log(f"tests: moved {len(test_names)} definitions into {len(files)} files under {TESTS_DIR}/; "
        f"rewrote {len(edits)} places that rebind the module ({sorted(rebound) or 'none'})")
    return True


def _check_acyclic(deps: dict, what: str):
    state = {}

    def visit(k, path):
        if state.get(k) == 1:
            raise SplitError(f"import cycle among {what}: {' -> '.join(path + [k])}")
        if state.get(k) == 2:
            return
        state[k] = 1
        for d in sorted(deps.get(k, ())):
            visit(d, path + [k])
        state[k] = 2
    for k in sorted(deps):
        visit(k, [])


def _write_tree(repo: str, files: dict):
    for rel, text in files.items():
        p = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        compile(text, rel, "exec")            # never write a file that does not parse
        tmp = p + ".split-tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)                    # whole or not at all: a retry never meets a half-written module


# ---------------------------------------------------------------------------------------- stage 2: package

def _state_mod(state: str) -> str:
    return state.lower()


def _state_cls(state: str) -> str:
    return "".join(p.capitalize() for p in state.split("_")) + "Handler"


def plan_package(src: str, table: dict = TABLE):
    """Where every unit and Machine method goes: (assign {name: module}, methods {name: module}, units, notes)."""
    tree, units = units_of(src)
    top = symtable.symtable(src, MODULE, "exec")
    notes = []
    by_name = {}
    for u in units:
        for n in u.names:
            if n in by_name and n != "_p":
                raise SplitError(f"`{n}` is defined twice at module level (lines {by_name[n].first}, {u.first})")
            by_name[n] = u
    m = [u for u in units if u.names == ["Machine"]]
    if len(m) != 1 or not isinstance(m[0].node, ast.ClassDef):
        raise SplitError("no single `class Machine` in the module")
    mu = m[0]
    cls = mu.node
    if cls.bases or cls.keywords or cls.decorator_list:
        raise SplitError("class Machine has bases, keywords or decorators: this script expects a plain class")
    mtabs = method_tables(top, cls)
    meths, other = {}, []
    for st in cls.body:
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if st.name in meths:
                raise SplitError(f"Machine.{st.name} is defined twice")
            meths[st.name] = st
        elif not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant)) and not isinstance(st, ast.Pass):
            other.append(st)
    if other:
        raise SplitError(f"class Machine has statements other than methods and its docstring (line "
                         f"{other[0].lineno}): extend the script to place them")
    # method -> module
    meth_refs, meth_calls = {}, {}
    for name, st in meths.items():
        tab = mtabs.get(st.lineno)
        if tab is None:
            raise SplitError(f"Machine.{name}: no symbol table")
        meth_refs[name] = _table_globals(tab) | _scope_level_names(st)
        calls = set()
        for x in ast.walk(st):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "self" \
                    and x.attr in meths:
                calls.add(x.attr)
            if isinstance(x, ast.Name) and (x.id in ("super", "__class__")):
                raise SplitError(f"Machine.{name} uses `{x.id}`: it would bind to another class once moved")
            if isinstance(x, (ast.Attribute, ast.Name)):
                ident = x.attr if isinstance(x, ast.Attribute) else x.id
                if ident.startswith("__") and not ident.endswith("__"):
                    raise SplitError(f"Machine.{name} uses the mangled name `{ident}`")
        meth_calls[name] = calls - {name}
    states = [n[2:] for n in meths if n.startswith("h_")]
    for s in states:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", s):
            raise SplitError(f"handler h_{s}: a state name this script cannot turn into a module name")
    mplace = {f"h_{s}": "states." + _state_mod(s) for s in states}
    callers = {n: set() for n in meths}
    for n, cs in meth_calls.items():
        for c in cs:
            callers[c].add(n)
    changed = True
    while changed:
        changed = False
        for n in meths:
            if n in mplace:
                continue
            cs = callers[n]
            if not cs:
                mplace[n] = "machine"
                changed = True
                continue
            if any(c not in mplace for c in cs):
                continue
            mods = {mplace[c] for c in cs}
            mplace[n] = mods.pop() if len(mods) == 1 else "machine"
            changed = True
    for n in meths:            # a cycle of private methods with no placed caller: they are the machine's own
        mplace.setdefault(n, "machine")
    # module-level names -> module
    fixed = {}
    for mod, names in table.items():
        for n in names:
            if n not in by_name:
                notes.append(f"TABLE names `{n}` ({mod}), which the module no longer defines")
            fixed[n] = mod
    shim_names = set()
    for u in units:
        if u.kind in ("doc", "future", "import"):
            continue
        if any(n in SHIM_UNITS for n in u.names) or _is_main_guard(u.node):
            shim_names |= set(u.names)
            continue
    unit_users = {}
    unit_ref = {}
    for u in units:
        if u.kind in ("doc", "future", "import") or u is mu:
            continue
        unit_ref[id(u)] = unit_refs(top, u)
    users = {n: set() for n in by_name}
    for u in units:
        if u.kind in ("doc", "future", "import") or u is mu:
            continue
        for r in unit_ref[id(u)]:
            if r in users and r not in u.names:
                users[r].add(("unit", u.names[0] if u.names else f"line{u.first}"))
    for mn, refs in meth_refs.items():
        for r in refs:
            if r in users:
                users[r].add(("meth", mn))
    assign = {}
    for u in units:
        if u.kind in ("doc", "future", "import"):
            continue
        if u is mu:
            assign["Machine"] = "machine"
            continue
        if set(u.names) & shim_names or (not u.names and _is_main_guard(u.node)):
            for n in u.names:
                assign[n] = "shim"
            continue
        if not u.names:
            if _is_path_loop(u.node):
                assign["_p"] = "common"
                continue
            raise SplitError(f"line {u.first}: a module-level statement with no name ({ast.unparse(u.node)[:70]!r})")
        mods = {fixed[n] for n in u.names if n in fixed}
        if len(mods) > 1:
            raise SplitError(f"line {u.first}: one statement binds names TABLE puts in two modules: {u.names}")
        if mods:
            m_ = mods.pop()
            for n in u.names:
                assign[n] = m_
    imports = {n for u in units if u.kind == "import" for n in u.names}

    def where(user):
        k, n = user
        return mplace[n] if k == "meth" else assign.get(n)

    changed = True
    pending = [u for u in units if u.kind not in ("doc", "future", "import") and u is not mu
               and u.names and u.names[0] not in assign]
    while changed and pending:
        changed = False
        for u in list(pending):
            us = set()
            for n in u.names:
                us |= {x for x in users.get(n, ()) if not (x[0] == "unit" and x[1] in u.names)}
            if not us:
                continue
            places = {where(x) for x in us}
            if None in places:
                continue
            places.discard("shim")
            if len(places) == 1:
                p = places.pop()
                for n in u.names:
                    assign[n] = p
                pending.remove(u)
                changed = True
    if pending:
        msgs = []
        for u in pending:
            us = set()
            for n in u.names:
                us |= users.get(n, set())
            places = sorted({str(where(x)) for x in us})
            msgs.append(f"`{', '.join(u.names)}` (line {u.first}) used by "
                        + (", ".join(f"{k} {n}" for k, n in sorted(us)) + f" in {places}" if us else "nothing here"))
        raise SplitError("module-level code the call graph does not place (used by several modules or by none); "
                         "add each to TABLE in scripts/refactor/split_stage_machine.py:\n  " + "\n  ".join(msgs))
    return tree, units, assign, mplace, meths, mu, top, unit_ref, meth_refs, imports, notes


def _is_main_guard(node) -> bool:
    return isinstance(node, ast.If) and "__name__" in ast.unparse(node.test) and "__main__" in ast.unparse(node.test)


def _is_path_loop(node) -> bool:
    return isinstance(node, ast.For) and "sys.path" in ast.unparse(node)


def _rel(src_mod: str, dst_mod: str) -> str:
    """The relative import prefix from module src_mod (e.g. 'states.gate') to dst_mod (e.g. 'common')."""
    depth = src_mod.count(".")
    return "." * (depth + 1) + dst_mod


def stage_package(repo: str, table: dict = TABLE, log=print) -> bool:
    path = os.path.join(repo, MODULE)
    src = open(path, encoding="utf-8").read()
    if STAMP_PKG in src:
        if not os.path.isdir(os.path.join(repo, PKG_DIR)):
            raise SplitError(f"{MODULE} is the shim but {PKG_DIR}/ is missing")
        log("package: already split, nothing to do")
        return False
    if STAMP_TESTS not in src:
        raise SplitError("run --stage tests first (the module still holds its inline self-tests)")
    if os.path.exists(os.path.join(repo, PKG_DIR)):
        raise SplitError(f"{PKG_DIR}/ exists already; remove it or start from a clean base")
    (tree, units, assign, mplace, meths, mu, top, unit_ref, meth_refs, imports,
     notes) = plan_package(src, table)
    for n in notes:
        log("NOTE: " + n)
    lines = src.splitlines(keepends=True)
    import_units = [u for u in units if u.kind == "import"]
    future = [u for u in units if u.kind == "future"]
    if not future:
        raise SplitError("the module has no `from __future__ import annotations`")
    defined = {n: assign[n] for n in assign}
    # module bodies, in source order
    bodies, needs = {}, {}

    def add(mod, text, refs):
        bodies.setdefault(mod, []).append(text)
        needs.setdefault(mod, set()).update(refs)

    for u in units:
        if u.kind in ("doc", "future", "import") or u is mu:
            continue
        mod = assign.get(u.names[0]) if u.names else ("common" if _is_path_loop(u.node) else "shim")
        if mod == "shim" or mod is None:
            continue
        t = u.text(lines)
        if u.names == ["HERE"]:
            if t.strip().splitlines()[-1].strip() != HERE_LINE:
                raise SplitError(f"`HERE` is not `{HERE_LINE}`: the shim's paths would change; update the script")
            t = t.replace(HERE_LINE, HERE_PKG)
        add(mod, t, unit_ref[id(u)] - set(u.names))
    # Machine: core methods stay in the class; each state's methods go into its handler class
    cls = mu.node
    cls_lines = lines[mu.lead - 1:mu.last]
    header_end = cls.body[0].lineno - 1          # the line before the first body statement
    first_meth = next(st for st in cls.body if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)))
    meth_text = {}
    body_stmts = cls.body
    for i, st in enumerate(body_stmts):
        lead = (body_stmts[i - 1].end_lineno + 1) if i else header_end + 1
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            meth_text[st.name] = "".join(lines[lead - 1:st.end_lineno])
    # class header + docstring: everything up to the first method's attached text
    i_first = body_stmts.index(first_meth)
    head_end = body_stmts[i_first - 1].end_lineno if i_first else header_end
    class_head = "".join(lines[mu.lead - 1:head_end])
    if not re.search(r"^class Machine:\s*$", "".join(lines[cls.lineno - 1:cls.lineno]), re.M):
        raise SplitError("the class line is not `class Machine:`")
    class_head = class_head.replace("class Machine:", "class Machine(*HANDLERS.values()):", 1)
    core = [n for n in meths if mplace[n] == "machine"]
    machine_refs = set().union(*[meth_refs[n] for n in core]) if core else set()
    # module-level units placed in machine.py keep their side of the class
    mach_before = [u for u in units if u.lead < mu.lead and u.names and assign.get(u.names[0]) == "machine"]
    mach_after = [u for u in units if u.lead > mu.lead and u.names and assign.get(u.names[0]) == "machine"]
    machine_text = ("".join(u.text(lines) for u in mach_before) + class_head
                    + "".join(meth_text[n] for n in meths if mplace[n] == "machine")
                    + "".join(u.text(lines) for u in mach_after))
    mach_unit_refs = set()
    for u in mach_before + mach_after:
        mach_unit_refs |= unit_ref[id(u)] - set(u.names)
    bodies.pop("machine", None)
    needs["machine"] = machine_refs | mach_unit_refs
    state_order = [n[2:] for n in meths if n.startswith("h_")]
    state_meths = {s: [n for n in meths if mplace[n] == "states." + _state_mod(s)] for s in state_order}
    for s, ms in state_meths.items():
        mod = "states." + _state_mod(s)
        txt = "".join(meth_text[n] for n in ms).lstrip("\n")
        cls_text = f'\n\n@handler("{s}")\nclass {_state_cls(s)}:\n' + txt
        bodies.setdefault(mod, []).append(cls_text)
        needs.setdefault(mod, set()).update(*[meth_refs[n] for n in ms])
    stray = sorted(m for m in set(mplace.values()) - {"machine"} if m.split(".", 1)[1].upper() not in state_meths)
    if stray:
        raise SplitError(f"methods placed in modules with no handler: {stray}")
    # annotation names, imported where they resolve (see _annotation_names)
    ann_needs = {}
    for u in units:
        if u.kind in ("def", "class", "assign") and u is not mu and u.names and assign.get(u.names[0]) != "shim":
            ann_needs.setdefault(assign[u.names[0]], set()).update(_annotation_names(u.node))
    for n, st in meths.items():
        ann_needs.setdefault(mplace[n], set()).update(_annotation_names(st))
    # imports per module
    import_names = imports
    files, deps = {}, {}
    all_mods = list(SHARED_ORDER) + [m for m in sorted(set(assign.values()) - set(SHARED_ORDER))
                                     if m not in ("shim", "machine", "run") and not m.startswith("states.")]
    if any(not m.startswith("states.") and m not in DOCS and m not in ("shim",) for m in all_mods):
        raise SplitError(f"modules with no description: {[m for m in all_mods if m not in DOCS]}")
    mods = [m for m in all_mods if m in bodies] + ["machine"] + \
        ["states." + _state_mod(s) for s in state_order] + (["run"] if "run" in bodies else [])
    for mod in mods:
        if mod == "machine":
            body = machine_text
        else:
            body = "".join(bodies.get(mod, []))
        own = {n for n, m in assign.items() if m == mod}
        if mod.startswith("states."):
            own |= set()
        need = set(needs.get(mod, set())) - own - BUILTINS
        need |= {n for n in ann_needs.get(mod, set()) - own - BUILTINS
                 if (n in import_names or assign.get(n) not in (None, "shim")) and n != "HANDLERS"}
        if mod == "machine":
            need.discard("HANDLERS")
        imp_std = need & import_names
        from_mods = {}
        for n in sorted(need - import_names):
            m = assign.get(n)
            if m is None or m == "shim":
                raise SplitError(f"{mod}: needs `{n}`, which " + ("stays in the shim" if m == "shim" else
                                                                   "is not defined at module level"))
            if m != mod:
                from_mods.setdefault(m, set()).add(n)
        deps[mod] = set(from_mods) | ({"states"} if mod == "machine" else set())
        # states.<s> imports `handler` from states/__init__, which defines it before it imports states.<s>:
        # not a cycle at import time, so that edge is not checked
        doc = (DOCS[mod] if mod in DOCS else
               f"The {mod.split('.', 1)[1].upper()} state handler, registered by name.")
        head = f'"""{doc}\n\n{GENERATED}\n"""\nfrom __future__ import annotations\n\n'
        rel = ""
        for m in sorted(from_mods, key=lambda x: (x not in SHARED_ORDER, mods.index(x) if x in mods else 99, x)):
            rel += wrap_import(f"from {_rel(mod, m)} import ", sorted(from_mods[m]))
        if mod == "machine":
            rel += "from .states import HANDLERS\n"
        if mod.startswith("states."):
            rel += "from . import handler\n"
        imp = "\n".join(p for p in (import_block(import_units, imp_std), rel) if p)
        files[f"{PKG_DIR}/{mod.replace('.', '/')}.py"] = head + imp + "\n\n" + body.lstrip("\n").rstrip("\n") + "\n"
    deps["states"] = {"states." + _state_mod(s) for s in state_order}
    _check_acyclic({k: {d for d in v if d != k} for k, v in deps.items()}, "package modules")
    files[f"{PKG_DIR}/states/__init__.py"] = _states_init(state_order)
    children = {m.split(".", 1)[0] for m in mods} | {"states", "namespace"}
    clash = sorted(children & {n for u in units for n in u.names})
    if clash:
        raise SplitError(f"the module defines {clash}, which the package's submodules would shadow on import: "
                         f"rename the module or the name first")
    files[f"{PKG_DIR}/__init__.py"] = _init(units, lines, assign, mods)
    files[f"{PKG_DIR}/namespace.py"] = NAMESPACE_PY
    files[MODULE] = _cli(units, lines)
    _write_tree(repo, files)
    log(f"package: {len(files) - 1} files under {PKG_DIR}/ and the CLI; {len(state_order)} state modules; "
        f"{sum(1 for n in meths if mplace[n] == 'machine')} methods stay in Machine")
    return True


def _states_init(state_order) -> str:
    mods = ", ".join(_state_mod(s) for s in state_order)
    return ('"""The state handlers of the v2 stage machine, one module per state, each a class registered by name\n'
            '(`@handler("GATE")`). Machine (machine.py) inherits every registered class, in this import order,\n'
            'so `getattr(self, "h_" + state)` in Machine.step finds each handler as before the split.\n"""\n'
            "from __future__ import annotations\n\nHANDLERS: dict = {}\n\n\n"
            "def handler(state: str):\n"
            '    """Register a state\'s handler class under the state\'s name; it must define h_<state>."""\n'
            "    def register(cls):\n"
            '        if "h_" + state not in vars(cls):\n'
            '            raise TypeError(f"{cls.__name__} is registered for {state} but has no h_{state}")\n'
            "        HANDLERS[state] = cls\n"
            "        return cls\n"
            "    return register\n\n\n"
            + wrap_import("from . import ", [_state_mod(s) for s in state_order], noqa="  # noqa: E402,F401"))


NAMESPACE_PY = '''"""One namespace for the package (no behaviour change from the split).

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
            if mod is not None and mod is not self and mname.startswith(_PKG + ".") \\
                    and mod.__dict__.get(name, _MISSING) is old:
                setattr(mod, name, value)


def share(module) -> None:
    """Make assignments to `module` (the package, agents/stage_machine/__init__.py) reach its submodules."""
    module.__class__ = _Shared
'''


def _init(units, lines, assign, mods) -> str:
    """agents/stage_machine/__init__.py, the import surface: the module's docstring (main() reads __doc__ for
    --help), its imports, its path block (private names, one directory up: HERE and REPO themselves come from
    common, the one object a patch of them reaches), every name re-exported from where it now lives, the namespace
    forwarding, then the units that stay here (_self_test, main) verbatim."""
    doc = next(u for u in units if u.kind == "doc")
    path_u = _first_path_unit(units)
    text = doc.text(lines).replace(STAMP_TESTS + "\n", "")
    if text.startswith("#!"):
        text = text.split("\n", 1)[1]
    out = [text, STAMP_PKG + "\n"]
    out.append("".join(u.text(lines) for u in units if u.kind == "future"))
    out.append("".join(u.text(lines) for u in units if u.kind == "import" and u.lead < path_u.lead))
    out.append("\n_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # agents/, as before\n"
               "_REPO = os.path.dirname(_HERE)\n"
               'for _p in (_REPO, os.path.join(_REPO, "scripts"), _HERE):     # the module\'s own order\n'
               "    if _p not in sys.path:\n        sys.path.insert(0, _p)\n\n")
    out.append("".join(render_import(u.node, set(u.names)).rstrip("\n") + "  # noqa: E402,F401\n"
                       for u in units if u.kind == "import" and u.lead > path_u.lead))
    by_mod = {}
    for n, m in assign.items():
        if m != "shim" and n != "_p":
            by_mod.setdefault(m, set()).add(n)
    for m in mods:
        if m in by_mod:
            out.append(wrap_import(f"from .{m} import ", sorted(by_mod[m]), noqa="  # noqa: E402,F401"))
    out.append("from . import namespace as _namespace  # noqa: E402\n\n"
               "_namespace.share(sys.modules[__name__])     # an assignment to this module reaches the package\n")
    for u in units:
        if u.kind in ("doc", "future", "import") or _is_main_guard(u.node):
            continue
        if u.names and assign.get(u.names[0]) == "shim":
            out.append(u.text(lines))
    return "".join(out).rstrip("\n") + "\n"


CLI_DOC = '''"""The command line of the v2 stage machine. The code is the package agents/stage_machine/ (split by
scripts/refactor/split_stage_machine.py); this file is never imported, since the package shadows it.

    python3 agents/stage_machine.py status <run_dir>
    python3 agents/stage_machine.py sign <run_dir> --by owner [--note "..."]      # the owner signs the spec
    python3 agents/stage_machine.py --self-test
"""
'''


def _cli(units, lines) -> str:
    """agents/stage_machine.py: the module's path block verbatim, at the point the module ran it (so sys.path is
    what it was when the module ran as a script, and `stage_machine` is the package), main() from the package,
    and the module's own `if __name__ == "__main__"` block."""
    guard = [u for u in units if _is_main_guard(u.node)]
    if len(guard) != 1:
        raise SplitError('the module has no single `if __name__ == "__main__"` block')
    first = _first_path_unit(units)
    loop = next(u for u in units if u.names == ["_p"] and _is_path_loop(u.node))
    head = lines[0] if lines[0].startswith("#!") else ""
    std = [u for u in units if u.kind == "import" and u.lead < first.lead and set(u.names) & {"os", "sys"}]
    return (head + CLI_DOC + STAMP_PKG + "\n" + "".join(render_import(u.node, {"os", "sys"}) for u in std) + "\n"
            + "".join(lines[first.first - 1:loop.last])
            + "\nfrom stage_machine import main  # noqa: E402  (agents/stage_machine/, the package)\n"
            + guard[0].text(lines))


def _first_path_unit(units):
    return next(u for u in units if u.names == ["HERE"])


# --------------------------------------------------------------------------------------------- verification

def verify_package(repo: str, original_src: str, table: dict = TABLE) -> list:
    """Problems: a moved definition whose AST differs from the original, a name the shim lost, a missing handler."""
    probs = []
    (_t, units, assign, mplace, meths, mu, *_rest) = plan_package(original_src, table)
    orig = {}
    for u in units:
        for n in u.names:
            if u.kind in ("def", "class", "assign") and n != "Machine":
                orig[n] = ast.dump(u.node)
    new = {}
    meth_new = {}
    for root, _d, fs in os.walk(os.path.join(repo, PKG_DIR)):
        for f in fs:
            if not f.endswith(".py"):
                continue
            t = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
            for st in t.body:
                for n in bound_names(st):
                    new.setdefault(n, []).append(ast.dump(st))
                if isinstance(st, ast.ClassDef) and (st.name == "Machine" or st.name.endswith("Handler")):
                    for m in st.body:
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            meth_new.setdefault(m.name, []).append(ast.dump(m))
    cli = ast.parse(open(os.path.join(repo, MODULE), encoding="utf-8").read())
    guards = [ast.dump(u.node) for u in units if _is_main_guard(u.node)]
    if [ast.dump(st) for st in cli.body if _is_main_guard(st)] != guards:
        probs.append(f"{MODULE}: not the module's own `if __name__ == \"__main__\"` block")
    if any(isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) for st in cli.body):
        probs.append(f"{MODULE}: defines code; the command line only imports main() from the package")
    for n, d in orig.items():
        got = new.get(n, [])
        if n == "HERE":
            if len([x for x in got if not x.startswith(("ImportFrom(", "Import("))]) != 1:
                probs.append("HERE is not defined once in the package")
            continue
        if assign.get(n) == "shim":
            if d not in got:
                probs.append(f"{n}: not the original definition in the shim")
            continue
        if n == "_p":
            continue
        if got.count(d) != 1:
            probs.append(f"{n}: the package does not hold the original definition exactly once")
    for n, st in meths.items():
        if meth_new.get(n, []).count(ast.dump(st)) != 1:
            probs.append(f"Machine.{n}: not moved verbatim")
    probs += _extra_code(repo, units, meths, mu)
    return probs


def _extra_code(repo: str, units, meths, mu) -> list:
    """The other direction: every statement in the package is an original one, an import, or one of the few the
    split writes (the handler wrappers and registry, the namespace module, the path block of __init__.py, the
    rewritten HERE line, the namespace call)."""
    probs = []
    orig = {ast.dump(u.node) for u in units if u.kind not in ("doc", "future", "import")}
    orig_meths = {ast.dump(st) for st in meths.values()}
    cls_doc = [ast.dump(st) for st in mu.node.body if isinstance(st, ast.Expr)]
    written = {ast.dump(st) for st in ast.parse(HERE_PKG + "\n").body}
    written |= {ast.dump(st) for st in ast.parse(
        '_HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n_REPO = os.path.dirname(_HERE)\n'
        'for _p in (_REPO, os.path.join(_REPO, "scripts"), _HERE):\n    if _p not in sys.path:\n'
        '        sys.path.insert(0, _p)\n_namespace.share(sys.modules[__name__])\n').body}
    states = [n[2:] for n in meths if n.startswith("h_")]
    fixed = {f"{PKG_DIR}/namespace.py": NAMESPACE_PY, f"{PKG_DIR}/states/__init__.py": _states_init(states)}
    for rel, text in fixed.items():
        if open(os.path.join(repo, rel), encoding="utf-8").read() != text:
            probs.append(f"{rel}: not the text the script writes")
    for root, _d, fs in os.walk(os.path.join(repo, PKG_DIR)):
        for f in sorted(fs):
            rel = os.path.relpath(os.path.join(root, f), repo).replace(os.sep, "/")
            if not f.endswith(".py") or rel in fixed:
                continue
            t = ast.parse(open(os.path.join(root, f), encoding="utf-8").read())
            for i, st in enumerate(t.body):
                if isinstance(st, (ast.Import, ast.ImportFrom)) or (i == 0 and isinstance(st, ast.Expr)):
                    continue
                if isinstance(st, ast.ClassDef) and (st.name == "Machine" or (
                        len(st.decorator_list) == 1 and ast.unparse(st.decorator_list[0]).startswith("handler("))):
                    for m in st.body:
                        if ast.dump(m) not in orig_meths and not (st.name == "Machine" and ast.dump(m) in cls_doc):
                            probs.append(f"{rel}: {st.name} holds code that is not an original method "
                                         f"(line {m.lineno})")
                    continue
                if ast.dump(st) not in orig and ast.dump(st) not in written:
                    probs.append(f"{rel}: line {st.lineno} is not an original statement")
    return probs


def check_imports(repo: str, python: str, original_names: list) -> list:
    """Import the shim both ways importers do, in a fresh interpreter, and check every original name and every
    attribute importers read; then check each state handler is on Machine."""
    attrs = set()
    out = subprocess.run(["git", "-C", repo, "grep", "-hoE", r"(SM|_sm[0-9]?|stage_machine)\.[A-Za-z_][A-Za-z0-9_]*",
                          "--", "*.py", f":!{MODULE}", f":!{PKG_DIR}/"], capture_output=True, text=True)
    for m in out.stdout.split():
        a = m.split(".", 1)[1]
        if a != "py":
            attrs.add(a)
    frm = subprocess.run(["git", "-C", repo, "grep", "-hoE",
                          r"from (agents\.)?stage_machine import [A-Za-z_][A-Za-z0-9_, ]*", "--", "*.py"],
                         capture_output=True, text=True)
    for m in frm.stdout.splitlines():
        attrs |= {x.strip().split(" as ")[0] for x in m.split(" import ", 1)[1].split(",") if x.strip()}
    names = sorted(set(original_names) | attrs)
    # an attribute importers name that the module never had (a comment's `stage_machine.X`) was not there before
    # either: it is not checked
    names = sorted(set(original_names) | (attrs & set(original_names))) if original_names else names
    code = ("import sys, json\nsys.path.insert(0, sys.argv[1])\nfrom agents import stage_machine as A\n"
            "sys.path.insert(0, sys.argv[1] + '/agents')\nimport stage_machine as B\n"
            "names = json.loads(sys.argv[2])\n"
            "miss = [f'{m.__name__}.{n}' for m in (A, B) for n in names if not hasattr(m, n)]\n"
            "hs = [n for n in A.EDGES if n != 'FINAL' and not hasattr(A.Machine, 'h_' + n)]\n"
            "print(json.dumps({'missing': miss, 'no_handler': hs, 'same_machine': A.Machine is B.Machine}))\n")
    import json
    r = subprocess.run([python, "-c", code, repo, json.dumps(names)], capture_output=True, text=True, cwd=repo)
    if r.returncode != 0:
        return [f"importing the shim failed: {r.stderr.strip()[-800:]}"]
    res = json.loads(r.stdout.strip().splitlines()[-1])
    probs = [f"missing attribute {m}" for m in res["missing"]]
    probs += [f"no handler for state {s}" for s in res["no_handler"]]
    return probs


CASE = re.compile(r"^(PASS|FAIL) (\S+)")


def case_list(text: str) -> list:
    return [m.groups() for m in (CASE.match(line) for line in text.splitlines()) if m]


def compare_cases(before: str, after: str) -> tuple[bool, str]:
    b, a = case_list(before), case_list(after)
    def finished(text):
        tail = [x.strip() for x in text.splitlines() if x.strip() and not x.startswith("rc=")]
        return bool(tail) and tail[-1] == "OK: all self-test cases passed."
    ok_b, ok_a = finished(before), finished(after)
    if b == a and ok_b == ok_a and a and ok_a:
        n_pass = sum(1 for s, _ in a if s == "PASS")
        return True, (f"identical: {len(a)} cases in the same order, {n_pass} PASS, {len(a) - n_pass} FAIL; "
                      f"final line {'OK' if ok_a else 'not OK'} in both")
    import difflib
    diff = "\n".join(difflib.unified_diff([" ".join(x) for x in b], [" ".join(x) for x in a], "before", "after",
                                          lineterm="", n=1))
    return False, (f"DIFFERENT ({len(b)} before, {len(a)} after; OK line {ok_b} -> {ok_a}"
                   + ("; an empty or unfinished run proves nothing" if not (a and ok_a and ok_b) else "")
                   + f"):\n{diff}")


# ----------------------------------------------------------------------------------------------- self-test

TOY = '''#!/usr/bin/env python3
"""Toy stage machine.

    python3 agents/stage_machine.py --self-test
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _p in (REPO, os.path.join(REPO, "scripts"), HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import toyhelp as TH  # noqa: E402

STATES = ("A", "B", "FINAL")
EDGES = {"A": ("B",), "B": ("FINAL",), "FINAL": ()}
STATE_FILE = "state.json"


@dataclass
class RunState:
    state: str = "A"
    log: list = field(default_factory=list)


def _now():
    return "t"


def route_gate(x):
    return "FINAL" if x else "A"


def a_helper(v):
    return TH.double(v) + 1


def shared(v):
    return v


class Machine:
    """The toy machine."""

    def __init__(self, st=None):
        self.st = st or RunState()

    def _go(self, nxt):
        assert nxt in EDGES[self.st.state]
        self.st.log.append((_now(), nxt))
        self.st.state = nxt
        return nxt

    def step(self):
        return getattr(self, "h_" + self.st.state)()

    # ---- handlers
    def h_A(self):
        return self._go("B" if self._a_ok() else "B")

    def _a_ok(self):
        return a_helper(shared(1)) == 3

    def h_B(self):
        return self._go(route_gate(shared(True)))


def run_toy(st=None):
    m = Machine(st)
    while m.st.state != "FINAL":
        m.step()
    return m.st


def _t_toy():
    return 7


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)
    ok(run_toy().state == "FINAL", "X1 the toy run ends FINAL")
    ok(_t_toy() == 7, "X2 a helper moves with the tests")
    g_ = globals()
    orig = g_["route_gate"]
    seen = []
    g_["route_gate"] = lambda x: seen.append(x) or "FINAL"
    try:
        ok(run_toy().log[-1][1] == "FINAL" and seen == [True], "X3 a patch through globals() reaches the handler")
    finally:
        g_["route_gate"] = orig
    _self_test_more(ok)
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def _self_test_more(ok) -> None:
    global REPO
    old, REPO = REPO, "/nowhere"
    try:
        ok(REPO == "/nowhere", "X4 `global REPO` still rebinds the module")
        mod = sys.modules[Machine.__module__]
        vg = mod.a_helper
        mod.a_helper = lambda v: 99
        try:
            ok(not Machine()._a_ok(), "X5 a patch through sys.modules[Machine.__module__] reaches the state")
        finally:
            mod.a_helper = vg
    finally:
        REPO = old
    ok(Machine()._a_ok() and REPO == old, "X6 the patches are undone")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    print(json.dumps(STATES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

TOY_TABLE = {"common": ["HERE", "REPO", "_p", "STATE_FILE", "RunState", "_now", "shared"],
             "edges": ["STATES", "EDGES", "route_gate"], "run": ["run_toy"]}


def _self_test() -> int:
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    py = sys.executable
    quiet = lambda *a: None  # noqa: E731
    with tempfile.TemporaryDirectory() as td:
        def toy_repo(name, src=TOY):
            d = os.path.join(td, name)
            os.makedirs(os.path.join(d, "agents"))
            os.makedirs(os.path.join(d, "scripts"))
            open(os.path.join(d, MODULE), "w").write(src)
            open(os.path.join(d, "scripts", "toyhelp.py"), "w").write("def double(v):\n    return 2 * v\n")
            subprocess.run(["git", "init", "-q", d], check=True)
            subprocess.run(["git", "-C", d, "add", "-A"], check=True)
            return d

        def selftest(d):
            r = subprocess.run([py, MODULE, "--self-test"], cwd=d, capture_output=True, text=True)
            return r.returncode, r.stdout + r.stderr

        d = toy_repo("a")
        rc0, before = selftest(d)
        ok(rc0 == 0 and len(case_list(before)) == 6, f"S1 the toy module's own self-test passes ({rc0})")
        ok(stage_tests(d, log=quiet), "S2 stage tests changes the toy")
        rc1, after = selftest(d)
        same, why = compare_cases(before, after)
        ok(rc1 == 0 and same, f"S3 after stage tests the same cases pass: {why}")
        files = sorted(f for f in os.listdir(os.path.join(d, TESTS_DIR)) if f.endswith(".py"))
        ok(files == ["sm_selftest_common.py", "sm_selftest_core.py", "sm_selftest_more.py"],
           f"S4 one file per self-test function, helpers in common: {files}")
        mod_src = open(os.path.join(d, MODULE)).read()
        ok("def _self_test_more" not in mod_src and "_t_toy" not in mod_src and STAMP_TESTS in mod_src
           and "--self-test" in mod_src, "S5 the module keeps a stub and its --self-test flag, not the tests")
        snap = {f: open(os.path.join(d, TESTS_DIR, f)).read() for f in files}
        ok(not stage_tests(d, log=quiet) and snap == {f: open(os.path.join(d, TESTS_DIR, f)).read() for f in files},
           "S6 stage tests is idempotent")
        core = snap["sm_selftest_core.py"]
        ok('g_ = SM_GLOBALS' in core and "    global REPO\n" not in snap["sm_selftest_more.py"]
           and "_SM.REPO" in snap["sm_selftest_more.py"] and "mod = _SM" in snap["sm_selftest_more.py"],
           "S7 the three rebinding idioms are rewritten to assign to the module")
        d2 = toy_repo("b")
        stage_tests(d2, log=quiet)
        ok(all(open(os.path.join(d2, TESTS_DIR, f)).read() == snap[f] for f in files)
           and open(os.path.join(d2, MODULE)).read() == mod_src, "S8 the output is deterministic")
        subprocess.run(["git", "-C", d, "add", "-A"], check=True)
        ok(stage_package(d, table=TOY_TABLE, log=quiet), "S9 stage package changes the toy")
        rc2, after2 = selftest(d)
        same2, why2 = compare_cases(before, after2)
        ok(rc2 == 0 and same2, f"S10 after stage package the same cases pass (patches reach the states): {why2}"
           + ("" if rc2 == 0 else after2[-600:]))
        pk = sorted(os.path.relpath(os.path.join(r, f), d) for r, _x, fs in os.walk(os.path.join(d, PKG_DIR))
                    for f in fs if f.endswith(".py"))
        ok(pk == [f"{PKG_DIR}/{f}" for f in ("__init__.py", "common.py", "edges.py", "machine.py", "namespace.py",
                                             "run.py", "states/__init__.py", "states/a.py", "states/b.py")]
           and PKG_DIR == "agents/stage_machine", f"S11 the package layout: {pk}")
        sa = open(os.path.join(d, PKG_DIR, "states", "a.py")).read()
        ok('@handler("A")' in sa and "def _a_ok" in sa and "def a_helper" in sa,
           "S12 a state module holds its handler, the method only it calls and the helper only it uses")
        subprocess.run(["git", "-C", d, "add", "-A"], check=True)
        probs = verify_package(d, mod_src, TOY_TABLE)
        ok(probs == [], f"S13 verify: every definition moved verbatim {probs}")
        probs = check_imports(d, py, ["Machine", "run_toy", "route_gate", "RunState", "a_helper", "_t_toy"][:5])
        ok(probs == [], f"S14 both import paths resolve every name {probs}")
        r = subprocess.run([py, MODULE], cwd=d, capture_output=True, text=True)
        ok(r.returncode == 0 and '"A"' in r.stdout, f"S15 the CLI still runs ({r.returncode})")
        r = subprocess.run([py, "-c", "import sys; sys.path.insert(0, 'agents'); import stage_machine as m, "
                            "stage_machine.edges as e; f = m.route_gate; m.route_gate = f; m.route_gate = 1; "
                            "print(e.route_gate == 1); m.route_gate = f; print(e.route_gate is f)"],
                           cwd=d, capture_output=True, text=True)
        ok(r.stdout.split() == ["True", "True"], "S22 rebinding a name to the object it holds is a no-op, and a "
                                                 f"patch and its undo reach the submodule ({r.stderr[-200:]})")
        ok(not stage_package(d, table=TOY_TABLE, log=quiet), "S16 stage package is idempotent")
        pa = os.path.join(d, PKG_DIR, "states", "a.py")
        open(pa, "w").write(sa.replace("== 3", "== 4") + "\nX_NEW = 1\n")
        probs = verify_package(d, mod_src, TOY_TABLE)
        ok(any("_a_ok" in p for p in probs) and any("not an original statement" in p for p in probs),
           f"S21 verify catches an edited method and an added statement: {probs}")
        open(pa, "w").write(sa)
        # fail loudly
        d3 = toy_repo("c", TOY.replace("def shared(v):", "def orphan():\n    return 1\n\n\ndef shared(v):"))
        stage_tests(d3, log=quiet)
        try:
            stage_package(d3, table=TOY_TABLE, log=quiet)
            ok(False, "S17 an unplaced helper stops the script")
        except SplitError as e:
            ok("orphan" in str(e) and "TABLE" in str(e), f"S17 an unplaced helper stops the script: {str(e)[:90]}")
        d4 = toy_repo("d", TOY.replace("def _self_test_more(ok)", "X_ODD = 1\n\n\ndef _self_test_more(ok)"))
        try:
            stage_tests(d4, log=quiet)
            ok(False, "S18 unclassifiable code among the self-tests stops the script")
        except SplitError as e:
            ok("X_ODD" in str(e), f"S18 unclassifiable code among the self-tests stops the script: {str(e)[:80]}")
        d5 = toy_repo("e", TOY.replace("ok(_t_toy() == 7,", "ok(eval('_t_toy()') == 7,"))
        try:
            stage_tests(d5, log=quiet)
            ok(False, "S19 eval() in a self-test stops the script")
        except SplitError as e:
            ok("eval" in str(e), "S19 eval() in a self-test stops the script")
        refuse = {  # Codex r1 on #354: each would have split silently into a test that checks something else
            "S23 a comprehension variable named like a rebound name":
                ("        ok(REPO == \"/nowhere\",", "        _ = [REPO for REPO in 'ab']\n        ok(REPO == \"/nowhere\","),
            "S24 a def binding a rebound name": ("    old, REPO = REPO,", "    def REPO(): pass\n    old, REPO = REPO,"),
            "S25 a local name Machine": ("        mod = sys.modules", "        Machine = int\n        mod = sys.modules"),
            "S26 globals().update": ("    g_ = globals()\n", "    globals().update(X=1)\n    g_ = globals()\n"),
            "S27 production code reaching a test helper by name": (
                "def shared(v):\n    return v", "def shared(v):\n    return v if v else globals()['_t_toy']()"),
            # Codex r2 on #354
            "S29 an alias of the globals() alias": ("    g_ = globals()\n", "    g_ = globals()\n    h_ = g_\n"),
            "S30 del globals()[...]": ("    g_ = globals()\n", "    globals()['zz'] = 1\n    del globals()['zz']\n"
                                                             "    g_ = globals()\n"),
            "S31 `in globals()`": ("    _self_test_more(ok)\n", "    if '_self_test_more' in globals():\n"
                                                              "        _self_test_more(ok)\n"),
            "S32 a local callable named globals": ("    g_ = globals()\n", "    globals = dict\n    g_ = globals()\n"),
            "S33 a test rebinding a builtin": ("    g_ = globals()\n", "    globals()['open'] = open\n    g_ = globals()\n"),
            "S34 a mapping-pattern capture of a rebound name": (
                "    old, REPO = REPO,", "    match {}:\n        case {**REPO}:\n            pass\n    old, REPO = REPO,"),
            "S35 production globals() with a computed key": (
                "def shared(v):\n    return v", "def shared(v):\n    return v if v else globals()['_t_' + 'toy']()"),
        }
        for i, (what, (old, new)) in enumerate(refuse.items()):
            assert old in TOY, what
            dd = toy_repo(f"r{i}", TOY.replace(old, new, 1))
            try:
                stage_tests(dd, log=quiet)
                ok(False, f"{what} stops the script")
            except SplitError as e:
                ok(True, f"{what} stops the script: {str(e)[:70]}")
        d6 = toy_repo("g", TOY.replace("    ok(_t_toy() == 7, \"X2 a helper moves with the tests\")\n", "")
                      .replace("def _t_toy():\n    return 7\n\n\n", ""))
        os.makedirs(os.path.dirname(os.path.join(d6, TESTS_DIR)))
        open(os.path.join(d6, TESTS_DIR), "w").write("a file where the directory goes")
        try:
            stage_tests(d6, log=quiet)
        except (OSError, SplitError):
            pass
        unstamped = STAMP_TESTS not in open(os.path.join(d6, MODULE)).read()
        os.remove(os.path.join(d6, TESTS_DIR))
        retried = stage_tests(d6, log=quiet)
        rc6, out6 = selftest(d6)
        ok(unstamped and retried and rc6 == 0 and len(case_list(out6)) == 5,
           f"S28 an interrupted split leaves the module unsplit, and the retry (globals() with no test helper) "
           f"runs ({rc6}, {out6[-200:]!r})")
        ok(compare_cases("PASS T1 a\nPASS T2 b\n", "PASS T1 x\nFAIL T2 b\n")[0] is False
           and compare_cases("PASS T1 a\nOK: all self-test cases passed.", "PASS T1 b\nOK: all self-test cases "
                             "passed.")[0] and not compare_cases("", "")[0]
           and not compare_cases("PASS T1 a\n", "PASS T1 a\n")[0],
           "S20 the case comparison keys on status and id, in order, and needs a finished run with cases")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=["tests", "package", "all"])
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--verify", action="store_true", help="check the package against the stage-1 module (git HEAD)")
    ap.add_argument("--original", default=None, help="--verify: the stage-1 module to compare with "
                                                     "(default: git show HEAD:agents/stage_machine.py)")
    ap.add_argument("--compare-cases", nargs=2, metavar=("BEFORE", "AFTER"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    if a.compare_cases:
        same, why = compare_cases(open(a.compare_cases[0]).read(), open(a.compare_cases[1]).read())
        print(why)
        return 0 if same else 1
    try:
        t0 = time.time()
        if a.stage in ("tests", "all"):
            stage_tests(a.repo)
        if a.stage in ("package", "all"):
            stage_package(a.repo)
        if a.stage:
            print(f"done in {time.time() - t0:.1f}s")
        if a.verify:
            orig = (open(a.original).read() if a.original else
                    subprocess.run(["git", "-C", a.repo, "show", f"HEAD:{MODULE}"], capture_output=True, text=True,
                                   check=True).stdout)
            probs = verify_package(a.repo, orig)
            _t, units = units_of(orig)
            names = [n for u in units for n in u.names if n not in ("_p",)]
            probs += check_imports(a.repo, sys.executable, names)
            for p in probs:
                print("PROBLEM: " + p)
            print("verify: " + ("OK, every definition moved verbatim and every name resolves" if not probs
                                else f"{len(probs)} problem(s)"))
            return 1 if probs else 0
    except SplitError as e:
        print(f"split_stage_machine: cannot split: {e}", file=sys.stderr)
        return 2
    if not (a.stage or a.verify):
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
