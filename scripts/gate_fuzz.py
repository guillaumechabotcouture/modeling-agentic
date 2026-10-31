#!/usr/bin/env python3
"""One fuzz test over every gate check: bad input never crashes a check and
never makes it pass.

Phase 51 β. About a quarter of the P1 review findings on PRs #62-#81 were the
same input-handling bugs found again one module at a time: undecodable bytes,
integers too large for a float or for int-to-str, impossible YAML dates,
recursive YAML aliases, symlinks leaving the run directory, empty, truncated
or corrupt files, wrong top-level types, NaN and inf. This test runs every
bug class against every check, so a new check is covered the day it lands.

How it works (no list of checks or files is kept by hand):
  1. Discovery: every `scripts/*.py` with a module-level
     `def gate_findings(run_dir, ...)` (found by parsing the source, the same
     rule as `structure_metrics.py`'s gate_interface). `round_n`, when the
     function takes it, is passed as the fixture's round, as the STAGE 7
     validator does.
  2. Clean run: the positive-control run (`scripts/lib/positive_control.py`,
     built at round 3, when every round-gated check is enforced) is built
     once. Each check runs on a copy of it with `open` patched to record
     which run files it opens for reading (opened for writing at any point =
     the check's own output, not mutated). yaml/json/csv all read through
     `open`, so patching it is enough.
  3. Mutation: for each file a check read, each bug class that applies to
     that file's format writes a corrupt copy into a fresh copy of the run,
     and the check runs again in a forked child (fresh caches, a time limit).
  4. Assertions, per check and mutation:
       raises      gate_findings raised (the validator's run_guarded turns
                   this into a HIGH, but a direct caller crashes);
       not_list    it returned something other than a list;
       fails_open  the file is now invalid and the check reported no
                   finding it did not already report on the clean run (for a
                   clean run with no findings: it returned []);
       timeout     it did not return within the time limit.
     Which mutations make a file "invalid" is decided structurally:
       - undecodable bytes, a link out of the run, and, for YAML/JSON/CSV,
         empty, truncated (cut where it no longer parses), corrupt syntax,
         wrong top-level type, impossible date, recursive aliases, and an
         integer past the int-to-str limit (YAML/JSON no longer parse): always;
       - NaN, inf and 10**400 in values: only where the check reads values.
         Two probes replace every number in the file with finite wrong values
         (-7, 123456789); if either changes the check's findings, the check
         reads that file's values and must also react to NaN, inf and 10**400;
       - empty or large-number prose and code (.md, .py): still valid text,
         checked for `raises` only.
     A third probe deletes the file; whether the check notices is reported
     ("?" after a file name: it does not), never asserted.

Known failures are listed in `tests/gate_fuzz_expected.yaml` as
`<module>: {<mutation>: [<failure>, ...]}` so CI passes while they are the
work queue for the fail-closed I/O decision. CI fails on a failure not in
the list and on a listed entry that no longer fails (remove it). A short
allowlist (OPTIONAL_INPUTS below) names files a check documents as optional.

    python3 scripts/gate_fuzz.py              # full sweep, compare with the expected list
    python3 scripts/gate_fuzz.py --report     # print every failure, by module and class
    python3 scripts/gate_fuzz.py --write-expected   # rewrite the expected list from this sweep
    python3 scripts/gate_fuzz.py --self-test
"""
from __future__ import annotations

import argparse
import ast
import builtins
import importlib
import inspect
import io
import json
import multiprocessing as mp
import os
import re
import shutil
import signal
import sys
import tempfile
import time
import traceback

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXPECTED = os.path.join(ROOT, "tests", "gate_fuzz_expected.yaml")
FIXTURE_ROUND = 3     # every round-gated check is enforced from round 3
CASE_TIMEOUT_S = 30

# (module, run-relative file): the module documents the file as optional, so a
# corrupt copy may be treated as absent. Each entry cites where it says so.
OPTIONAL_INPUTS: dict[tuple[str, str], str] = {
    ("headline_binding", "models/effect_concordance.yaml"):
        "docstring item 3: 'when models/effect_concordance.yaml exists'; the file's own "
        "validity is effect_concordance's finding",
}

STRUCTURED = (".yaml", ".yml", ".json", ".jsonl", ".csv")
YAML_EXT = (".yaml", ".yml")
BIG_FLOAT = "1" + "0" * 400          # an int, but float() overflows
BIG_STR = "9" * 5000                 # beyond Python's 4,300-digit int<->str limit


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #

def discover(scripts_dir: str = HERE) -> list[str]:
    """Module names under scripts_dir with a module-level gate_findings(run_dir, ...)."""
    out = []
    for fn in sorted(os.listdir(scripts_dir)):
        if not fn.endswith(".py") or fn.startswith("_"):
            continue
        try:
            with open(os.path.join(scripts_dir, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        if any(isinstance(n, ast.FunctionDef) and n.name == "gate_findings" and n.args.args
               and n.args.args[0].arg == "run_dir" for n in tree.body):
            out.append(fn[:-3])
    return out


# --------------------------------------------------------------------------- #
# Mutations. Each returns (bytes_or_None, kind) where kind is "invalid" (the
# file is no longer a valid artifact), "text" (still valid prose/code: only
# `raises` is checked), or None (does not apply to this file).
# --------------------------------------------------------------------------- #

def _ext(rel: str) -> str:
    return os.path.splitext(rel)[1].lower()


def _yaml_ok(b: bytes) -> bool:
    try:
        yaml.safe_load(b.decode("utf-8"))
        return True
    except Exception:  # noqa: BLE001 — any failure means "does not parse"
        return False


def _json_ok(b: bytes, lines: bool) -> bool:
    try:
        t = b.decode("utf-8")
        if lines:
            for ln in t.splitlines():
                if ln.strip():
                    json.loads(ln)
        else:
            json.loads(t)
        return True
    except Exception:  # noqa: BLE001
        return False


def _parses(rel: str, b: bytes) -> bool:
    e = _ext(rel)
    if e in YAML_EXT:
        return _yaml_ok(b)
    if e == ".json":
        return _json_ok(b, False)
    if e == ".jsonl":
        return _json_ok(b, True)
    return True


_NUM = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w.])")


def _sub_numbers_text(text: str, repl: str) -> tuple[str, int]:
    return _NUM.subn(repl, text)


def _yaml_numbers(text: str, repl: str) -> tuple[str, int]:
    """Replace every numeric scalar in a YAML document, keeping its layout:
    numbers are found by the parser's event marks, not by a text pattern."""
    try:
        events = list(yaml.parse(text, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return text, 0
    spans = []
    resolver = yaml.resolver.Resolver()
    for ev in events:
        if isinstance(ev, yaml.ScalarEvent) and ev.implicit[0] and ev.style is None:
            tag = resolver.resolve(yaml.ScalarNode, ev.value, (True, False))
            if tag in ("tag:yaml.org,2002:int", "tag:yaml.org,2002:float"):
                spans.append((ev.start_mark.index, ev.end_mark.index))
    for s, e in reversed(spans):
        text = text[:s] + repl + text[e:]
    return text, len(spans)


def _csv_numbers(text: str, repl: str) -> tuple[str, int]:
    out, n = [], 0
    lines = text.splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if i == 0:
            out.append(ln)
            continue
        cells = ln.rstrip("\r\n").split(",")
        for j, c in enumerate(cells):
            if _NUM.fullmatch(c.strip()):
                cells[j] = repl
                n += 1
        out.append(",".join(cells) + ln[len(ln.rstrip("\r\n")):])
    return "".join(out), n


def _json_numbers(text: str, repl: str, lines: bool) -> tuple[str, int]:
    # JSON numbers outside strings: walk the text, skipping string literals.
    out, i, n, inside = [], 0, 0, False
    while i < len(text):
        ch = text[i]
        if inside:
            out.append(ch)
            if ch == "\\":
                out.append(text[i + 1:i + 2])
                i += 2
                continue
            if ch == '"':
                inside = False
            i += 1
            continue
        if ch == '"':
            inside = True
            out.append(ch)
            i += 1
            continue
        m = _NUM.match(text, i) if (ch.isdigit() or ch == "-") else None
        if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
            out.append(repl)
            n += 1
            i = m.end()
            continue
        out.append(ch)
        i += 1
    return "".join(out), n


def _numbers(rel: str, text: str, repl_yaml: str, repl_json: str, repl_csv: str):
    e = _ext(rel)
    if e in YAML_EXT:
        return _yaml_numbers(text, repl_yaml)
    if e == ".json":
        return _json_numbers(text, repl_json, False)
    if e == ".jsonl":
        return _json_numbers(text, repl_json, True)
    if e == ".csv":
        return _csv_numbers(text, repl_csv)
    return text, 0


def _mut_numbers(repl_yaml, repl_json, repl_csv, prose_repl=None):
    def f(rel, b):
        try:
            text = b.decode("utf-8")
        except UnicodeDecodeError:
            return None, None
        if _ext(rel) in STRUCTURED:
            t, n = _numbers(rel, text, repl_yaml, repl_json, repl_csv)
            if not n:
                return None, None
            # A YAML/JSON integer past the int-to-str limit no longer parses: the
            # file is invalid. Otherwise only the values changed ("value").
            return t.encode(), ("value" if _parses(rel, t.encode()) else "invalid")
        if prose_repl is None:
            return None, None
        t, n = _sub_numbers_text(text, prose_repl)
        return (t.encode(), "text") if n else (None, None)
    return f


def _mut_undecodable(rel, b):
    return b"\xff\xfe\xc3\x28 " + b, "invalid"


def _mut_empty(rel, b):
    """An empty artifact is invalid; an empty prose or code file is still valid
    text (a check that searches it for a pattern may rightly find nothing)."""
    if not b.strip():
        return None, None
    return b"", ("invalid" if _ext(rel) in STRUCTURED else "text")


def _mut_truncated(rel, b):
    """Cut the file inside a line so that the result is no longer valid in its
    format (a truncated YAML that still parses is not a truncated file)."""
    e = _ext(rel)
    if e not in STRUCTURED or len(b) < 4:
        return None, None
    for cut in range(len(b) // 2, 0, -1):
        if b[cut - 1:cut] in (b"\n",) or b[cut:cut + 1] in (b"\n",):
            continue
        t = b[:cut]
        if e == ".csv":
            lines = t.decode("utf-8", "replace").splitlines()
            if len(lines) >= 2 and lines[-1].count(",") < lines[0].count(","):
                return t, "invalid"
            continue
        if not _parses(rel, t):
            return t, "invalid"
    return None, None


def _mut_corrupt(rel, b):
    e = _ext(rel)
    if e in YAML_EXT or e in (".json", ".jsonl"):
        t = b + b"\n]]] {{{ : : 'unclosed\n"
        return (t, "invalid") if not _parses(rel, t) else (None, None)
    return None, None


def _load(rel, b):
    e = _ext(rel)
    t = b.decode("utf-8")
    if e in YAML_EXT:
        return yaml.safe_load(t)
    if e == ".json":
        return json.loads(t)
    if e == ".jsonl":
        return [json.loads(ln) for ln in t.splitlines() if ln.strip()]
    raise ValueError


def _mut_wrong_type(rel, b):
    e = _ext(rel)
    if e not in YAML_EXT + (".json", ".jsonl"):
        return None, None
    try:
        doc = _load(rel, b)
    except Exception:  # noqa: BLE001
        return None, None
    if e == ".jsonl":
        return b"[1, 2]\n[3]\n", "invalid"
    new = ["fuzz", 1] if isinstance(doc, dict) else {"fuzz": 1}
    return (yaml.safe_dump(new) if e in YAML_EXT else json.dumps(new)).encode(), "invalid"


def _mut_bad_date(rel, b):
    if _ext(rel) not in YAML_EXT:
        return None, None
    try:
        doc = yaml.safe_load(b.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, None
    if isinstance(doc, dict):
        return b + b"\nfuzz_date: 2026-02-30\n", "invalid"
    if isinstance(doc, list):
        return b + b"\n- 2026-02-30\n", "invalid"
    return None, None


def _mut_alias_loop(rel, b):
    """Every top-level value becomes a list that contains itself: the keys a
    check looks for are present, their values recurse forever."""
    if _ext(rel) not in YAML_EXT:
        return None, None
    try:
        doc = yaml.safe_load(b.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None, None
    if isinstance(doc, dict) and doc:
        lines = []
        for i, k in enumerate(doc):
            lines.append(f"{json.dumps(str(k))}: &fz{i} [*fz{i}]")
        return ("\n".join(lines) + "\n").encode(), "invalid"
    if isinstance(doc, list):
        return b"&fz [*fz]\n", "invalid"
    return None, None


MUTATIONS = {
    "undecodable": _mut_undecodable,
    "empty": _mut_empty,
    "truncated": _mut_truncated,
    "corrupt": _mut_corrupt,
    "wrong_type": _mut_wrong_type,
    "big_int_float": _mut_numbers(BIG_FLOAT, BIG_FLOAT, BIG_FLOAT, prose_repl=BIG_FLOAT),
    "big_int_str": _mut_numbers(BIG_STR, BIG_STR, BIG_STR, prose_repl=BIG_STR),
    "bad_date": _mut_bad_date,
    "alias_loop": _mut_alias_loop,
    "nan": _mut_numbers(".nan", "NaN", "nan"),
    "inf": _mut_numbers(".inf", "Infinity", "inf"),
    "symlink_escape": None,      # handled in _apply: the file becomes a link out of the run
}

# Probes are not bug classes and are never failures. They tell the oracle what
# a check does with a file: whether finite wrong values in it change the
# check's findings (then NaN, inf and 10**400 there must too), and, for the
# report only, whether the check notices the file missing.
PROBES = {
    "probe_negative": _mut_numbers("-7", "-7", "-7"),
    "probe_large": _mut_numbers("123456789", "123456789", "123456789"),
    "probe_missing": None,       # handled in _apply: the file is deleted
}
VALUE_MUTATIONS = ("nan", "inf", "big_int_float", "big_int_str")


# --------------------------------------------------------------------------- #
# Running a check
# --------------------------------------------------------------------------- #

def _call(mod, run_dir: str, round_n: int):
    fn = mod.gate_findings
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    return fn(run_dir, round_n=round_n) if "round_n" in params else fn(run_dir)


def _fkey(f) -> str:
    if isinstance(f, dict):
        return f"{f.get('kind')}|{f.get('severity')}"
    return repr(f)[:80]


class _Recorder:
    """Patch open() to record run files opened for reading and for writing."""

    def __init__(self, run_dir: str):
        self.root = os.path.realpath(run_dir)
        self.reads, self.writes = set(), set()
        self._orig = builtins.open

    def __enter__(self):
        orig = self._orig

        def rec_open(file, mode="r", *a, **k):
            fh = orig(file, mode, *a, **k)
            try:
                if not isinstance(file, int):
                    p = os.path.abspath(os.fspath(file))
                    p = os.path.join(os.path.realpath(os.path.dirname(p)), os.path.basename(p))
                    if p.startswith(self.root + os.sep):
                        rel = os.path.relpath(p, self.root)
                        (self.writes if any(c in mode for c in "wax+") else self.reads).add(rel)
            except TypeError:
                pass
            return fh

        builtins.open = rec_open
        io.open = rec_open
        return self

    def __exit__(self, *exc):
        builtins.open = self._orig
        io.open = self._orig
        return False


def _alarm(*_):
    raise TimeoutError("case timed out")


def _child(conn, fn, args):
    try:
        signal.signal(signal.SIGALRM, _alarm)
        signal.alarm(CASE_TIMEOUT_S)
        conn.send(fn(*args))
    except BaseException as e:  # noqa: BLE001 — report, never hang the parent
        conn.send({"status": "harness_error", "detail": f"{type(e).__name__}: {e}"})
    finally:
        conn.close()


def _in_child(fn, *args, timeout=CASE_TIMEOUT_S + 10):
    ctx = mp.get_context("fork")
    a, b = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_child, args=(b, fn, args))
    p.start()
    b.close()
    res = None
    if a.poll(timeout):
        try:
            res = a.recv()
        except EOFError:
            res = None
    p.join(5)
    if p.is_alive():
        p.kill()
        p.join()
    if res is None:
        res = {"status": "timeout" if p.exitcode in (None, -9) else "raises",
               "detail": f"child exit {p.exitcode}"}
    return res


def _observe(modname: str, base: str, work: str, round_n: int) -> dict:
    run = os.path.join(work, "run")
    shutil.copytree(base, run, symlinks=True)
    mod = importlib.import_module(modname)
    with _Recorder(run) as rec:
        try:
            res = _call(mod, run, round_n)
            err = None
        except Exception as e:  # noqa: BLE001
            res, err = None, f"{type(e).__name__}: {e}"
    return {"reads": sorted(rec.reads - rec.writes), "writes": sorted(rec.writes),
            "findings": sorted({_fkey(f) for f in res}) if isinstance(res, list) else None,
            "error": err}


def _apply(run: str, rel: str, mutation: str, outside: str):
    path = os.path.join(run, rel)
    with open(path, "rb") as f:
        b = f.read()
    if mutation == "symlink_escape":
        os.makedirs(outside, exist_ok=True)
        target = os.path.join(outside, rel.replace(os.sep, "__"))
        with open(target, "wb") as f:
            f.write(b)
        os.remove(path)
        os.symlink(target, path)
        return "invalid"
    if mutation == "probe_missing":
        os.remove(path)
        return "probe"
    new, kind = {**MUTATIONS, **PROBES}[mutation](rel, b)
    if new is None:
        return None
    with open(path, "wb") as f:
        f.write(new)
    return kind


def _case(modname: str, base: str, work: str, rel: str, mutation: str, clean: list, round_n: int) -> dict:
    run = os.path.join(work, "run")
    shutil.copytree(base, run, symlinks=True)
    kind = _apply(run, rel, mutation, os.path.join(work, "outside"))
    if kind is None:
        return {"status": "n/a"}
    mod = importlib.import_module(modname)
    try:
        res = _call(mod, run, round_n)
    except RecursionError as e:
        return {"status": "raises", "detail": f"RecursionError: {str(e)[:120]}"}
    except Exception as e:  # noqa: BLE001
        tb = traceback.extract_tb(e.__traceback__)
        where = f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno}" if tb else "?"
        return {"status": "raises", "detail": f"{type(e).__name__} at {where}: {str(e)[:120]}"}
    if not isinstance(res, list):
        return {"status": "not_list", "detail": type(res).__name__}
    new = {_fkey(f) for f in res} - set(clean)
    return {"status": "returned", "kind": kind, "new": bool(new), "n": len(res)}


def _classify(cases: list[dict]) -> list[dict]:
    """Turn raw outcomes into ok / fails_open, using the probes of the same
    (module, file): a value mutation can fail open only where a finite wrong
    value changed the check's findings."""
    sensitive, required = set(), set()
    for c in cases:
        key = (c["module"], c["file"])
        if c["mutation"] in ("probe_negative", "probe_large") and c.get("new"):
            sensitive.add(key)
        if c["mutation"] == "probe_missing" and (c.get("new") or c["status"] != "returned"):
            required.add(key)
    out = []
    for c in cases:
        c = dict(c)
        key = (c["module"], c["file"])
        c["file_required"] = key in required
        if c["mutation"] in PROBES:
            c["status"] = "probe"
        elif c["status"] == "returned":
            kind, new = c.pop("kind"), c.pop("new")
            judged = kind == "invalid" or (kind == "value" and key in sensitive)
            if judged and not new and key not in OPTIONAL_INPUTS:
                c["status"] = "fails_open"
                c["detail"] = f"{c.pop('n')} finding(s), none new"
            else:
                c["status"] = "ok"
                c.pop("n", None)
        out.append(c)
    return out


def sweep(scripts_dir: str = HERE, modules=None, base: str | None = None, round_n: int = FIXTURE_ROUND,
          workers: int | None = None, log=print) -> dict:
    """Run every (module, file read, mutation) case. Returns
    {"modules": {mod: {"reads": [...], "findings": [...], "error": ...}},
     "cases": [{"module", "file", "mutation", "status", "detail"}]}."""
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    modules = modules if modules is not None else discover(scripts_dir)
    out = {"modules": {}, "cases": []}
    with tempfile.TemporaryDirectory(prefix="gate_fuzz_") as td:
        if base is None:
            from lib import positive_control as pc
            base = os.path.join(td, "base")
            with open(os.devnull, "w") as dn:
                old = sys.stdout
                sys.stdout = dn
                try:
                    pc.build(base, round_n=round_n)
                finally:
                    sys.stdout = old
            shutil.rmtree(os.path.join(base, "models", "__pycache__"), ignore_errors=True)
        for m in modules:       # import once in the parent: children fork with it loaded
            try:
                importlib.import_module(m)
            except Exception as e:  # noqa: BLE001
                out["modules"][m] = {"reads": [], "findings": None, "error": f"import: {e}"}
        jobs = []
        for m in modules:
            if m in out["modules"]:
                continue
            obs = _in_child(_observe, m, base, os.path.join(td, f"obs_{m}"), round_n)
            out["modules"][m] = obs
            if obs.get("status"):
                continue
            for rel in obs["reads"]:
                if not os.path.isfile(os.path.join(base, rel)):
                    continue
                for mu in (*MUTATIONS, *PROBES):
                    jobs.append((m, rel, mu, obs.get("findings") or []))
        workers = workers or max(1, min(8, (os.cpu_count() or 2)))
        t0 = time.time()
        running = []
        idx = 0

        def launch(j, k):
            m, rel, mu, clean = j
            return (j, _in_child_async(_case, m, base, os.path.join(td, f"c{k}"), rel, mu, clean, round_n))

        pending = list(enumerate(jobs))
        while pending or running:
            while pending and len(running) < workers:
                k, j = pending.pop(0)
                running.append(launch(j, k))
            done = [r for r in running if r[1].ready()]
            if not done:
                time.sleep(0.005)
                continue
            for r in done:
                running.remove(r)
                (m, rel, mu, _), h = r
                res = h.result()
                if res.get("status") != "n/a":
                    out["cases"].append({"module": m, "file": rel, "mutation": mu, **res})
                idx += 1
        out["cases"] = _classify(out["cases"])
        out["seconds"] = round(time.time() - t0, 1)
    return out


class _Async:
    def __init__(self, fn, args):
        ctx = mp.get_context("fork")
        self.a, b = ctx.Pipe(duplex=False)
        self.p = ctx.Process(target=_child, args=(b, fn, args))
        self.p.start()
        b.close()
        self.t0 = time.time()
        self.res = None

    def ready(self) -> bool:
        if self.res is not None:
            return True
        if self.a.poll(0):
            try:
                self.res = self.a.recv()
            except EOFError:
                self.res = {"status": "raises", "detail": "child died"}
        elif not self.p.is_alive():
            self.res = {"status": "raises", "detail": f"child exit {self.p.exitcode}"}
        elif time.time() - self.t0 > CASE_TIMEOUT_S + 10:
            self.p.kill()
            self.res = {"status": "timeout", "detail": f">{CASE_TIMEOUT_S}s"}
        if self.res is not None:
            self.p.join(5)
            self.a.close()
        return self.res is not None

    def result(self):
        return self.res


def _in_child_async(fn, *args):
    return _Async(fn, args)


# --------------------------------------------------------------------------- #
# Expected failures
# --------------------------------------------------------------------------- #

FAILS = ("raises", "not_list", "fails_open", "timeout", "harness_error")


def failures(res: dict) -> dict:
    """{module: {mutation: sorted failure statuses}} from a sweep."""
    out: dict = {}
    for c in res["cases"]:
        if c["status"] in FAILS:
            out.setdefault(c["module"], {}).setdefault(c["mutation"], set()).add(c["status"])
    for m, d in res["modules"].items():
        if d.get("error") or d.get("status"):
            out.setdefault(m, {}).setdefault("clean_run", set()).add("raises")
    return {m: {mu: sorted(s) for mu, s in sorted(d.items())} for m, d in sorted(out.items())}


def load_expected(path: str = EXPECTED) -> dict:
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict) or not isinstance(doc.get("expected_failures"), dict):
        raise ValueError(f"{path}: needs a mapping 'expected_failures'")
    exp = doc["expected_failures"]
    for m, d in exp.items():
        if not isinstance(d, dict) or not all(isinstance(v, list) for v in d.values()):
            raise ValueError(f"{path}: expected_failures.{m} must map mutation -> [failure, ...]")
    return exp


def compare(found: dict, expected: dict) -> tuple[list[str], list[str]]:
    """(new failures, stale expected entries)."""
    new, stale = [], []
    for m, d in found.items():
        for mu, sts in d.items():
            for s in sts:
                if s not in (expected.get(m) or {}).get(mu, []):
                    new.append(f"{m}: {mu}: {s}")
    for m, d in expected.items():
        for mu, sts in (d or {}).items():
            for s in sts:
                if s not in (found.get(m) or {}).get(mu, []):
                    stale.append(f"{m}: {mu}: {s}")
    return new, stale


def report(res: dict) -> str:
    lines = [f"{len(res['modules'])} checks, {len(res['cases'])} cases, {res.get('seconds')} s"]
    for m, d in sorted(res["modules"].items()):
        lines.append(f"  {m}: reads {len(d.get('reads') or [])} run file(s)"
                     + (f"; clean run error: {d.get('error') or d.get('detail')}" if d.get("error") or d.get("status") else ""))
    by: dict = {}
    for c in res["cases"]:
        if c["status"] in FAILS:
            by.setdefault((c["module"], c["mutation"], c["status"]), []).append(c)
    for (m, mu, s), cs in sorted(by.items()):
        # "?" marks a file whose absence the check does not notice either.
        files = sorted({c["file"] + ("" if c.get("file_required") else "?") for c in cs})
        lines.append(f"{m} | {mu} | {s} | {', '.join(files)} | {cs[0].get('detail', '')}")
    return "\n".join(lines)


def write_expected(found: dict, path: str = EXPECTED) -> None:
    head = ("# Known gate_fuzz failures on main (scripts/gate_fuzz.py), the work queue for the\n"
            "# fail-closed I/O decision. CI fails on a failure missing here and on an entry here\n"
            "# that no longer fails. Regenerate with: python3 scripts/gate_fuzz.py --write-expected\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        yaml.safe_dump({"expected_failures": found}, f, sort_keys=True, allow_unicode=True, width=100,
                       default_flow_style=None)


# --------------------------------------------------------------------------- #
# Self-test: synthetic checks with known behaviour
# --------------------------------------------------------------------------- #

_FAKE = {
    # reads a.yaml with fail-closed handling: every mutation yields a finding
    "gf_closed": '''
import os, math, yaml
def gate_findings(run_dir):
    p = os.path.join(run_dir, "a.yaml")
    if os.path.islink(p) or not os.path.realpath(p).startswith(os.path.realpath(run_dir) + os.sep):
        return [{"kind": "a_invalid", "severity": "HIGH"}]
    try:
        with open(p, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        if not isinstance(doc, dict):
            raise ValueError("type")
        v = float(doc["n"])
        if not math.isfinite(v) or len(doc) != 2:
            raise ValueError("value")
        if not isinstance(doc["items"], list) or any(x is doc["items"] for x in doc["items"]):
            raise ValueError("items")
    except Exception:
        return [{"kind": "a_invalid", "severity": "HIGH"}]
    return []
''',
    # the historical pattern: crash on bad input, silent on unreadable
    "gf_open": '''
import os, yaml
def gate_findings(run_dir):
    try:
        doc = yaml.safe_load(open(os.path.join(run_dir, "a.yaml"))) or {}
    except (yaml.YAMLError, OSError):
        return []
    return [] if float(doc["n"]) > 0 else [{"kind": "n_low", "severity": "HIGH"}]
''',
    # reads only whether a key is there: NaN in its values is not its concern
    "gf_keys": '''
import os, yaml
def gate_findings(run_dir):
    try:
        with open(os.path.join(run_dir, "a.yaml"), encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except Exception:
        return [{"kind": "a_invalid", "severity": "HIGH"}]
    if os.path.islink(os.path.join(run_dir, "a.yaml")):
        return [{"kind": "a_invalid", "severity": "HIGH"}]
    if not isinstance(doc, dict) or set(doc) != {"n", "items"} or not isinstance(doc["items"], list) \\
            or any(x is doc["items"] for x in doc["items"]) or not isinstance(doc["n"], (int, float)):
        return [{"kind": "a_invalid", "severity": "HIGH"}]
    return []
''',
    "gf_round": '''
def gate_findings(run_dir, round_n=None):
    return [] if round_n == 3 else [{"kind": "wrong_round"}]
''',
    "gf_notlist": '''
def gate_findings(run_dir):
    open(run_dir + "/a.yaml").read()
    return None
''',
    "gf_other": '''
def gate_findings(result):
    return []
''',
}


def _self_test() -> int:
    fails = []

    def ok(cond, label):
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            fails.append(label)

    with tempfile.TemporaryDirectory() as td:
        sd = os.path.join(td, "scripts")
        os.makedirs(sd)
        for name, src in _FAKE.items():
            with open(os.path.join(sd, name + ".py"), "w") as f:
                f.write(src)
        base = os.path.join(td, "run")
        os.makedirs(base)
        with open(os.path.join(base, "a.yaml"), "w") as f:
            f.write("n: 3\nitems:\n  - 1\n  - 2\n")
        mods = discover(sd)
        ok(mods == ["gf_closed", "gf_keys", "gf_notlist", "gf_open", "gf_round"],
           f"GF1 discovery finds gate_findings(run_dir, ...) only, not gate_findings(result) ({mods})")
        res = sweep(sd, mods, base=base, workers=4, log=lambda *_: None)
        ok(res["modules"]["gf_closed"]["reads"] == ["a.yaml"], "GF2 the clean run records the file a check reads")
        found = failures(res)
        ok("gf_closed" not in found, f"GF3 a fail-closed check passes every mutation ({found.get('gf_closed')})")
        fo = found.get("gf_open", {})
        want_open = {"corrupt": ["fails_open"], "truncated": ["fails_open"], "symlink_escape": ["fails_open"],
                     "inf": ["fails_open"]}
        want_raise = {m: ["raises"] for m in ("empty", "undecodable", "wrong_type", "big_int_str",
                                              "big_int_float", "bad_date", "alias_loop")}
        ok(all(fo.get(k) == v for k, v in want_open.items()),
           f"GF4 the silent-except pattern fails open on corrupt, truncated, linked-out and inf input ({fo})")
        ok(all(fo.get(k) == v for k, v in want_raise.items()),
           "GF5 it raises on empty (KeyError), undecodable, wrong type, 10**400, 5000 digits, bad date, alias loop")
        ok("nan" not in fo, "GF6 NaN that fails `> 0` yields a new finding, so it is not fails_open")
        ok("gf_round" not in found and res["modules"]["gf_round"]["findings"] == [],
           "GF7 round_n is passed as the fixture round when the check takes it")
        ok("not_list" in found.get("gf_notlist", {}).get("empty", []), "GF8 a non-list return is reported")
        ok("gf_keys" not in found, f"GF8b a check that reads keys, not values, is not held to NaN/inf ({found.get('gf_keys')})")
        OPTIONAL_INPUTS[("gf_open", "a.yaml")] = "self-test"
        try:
            fo2 = failures(sweep(sd, ["gf_open"], base=base, workers=4)).get("gf_open", {})
        finally:
            del OPTIONAL_INPUTS[("gf_open", "a.yaml")]
        ok(not any("fails_open" in v for v in fo2.values()) and "raises" in fo2.get("wrong_type", []),
           "GF8c an allowlisted optional file is not held to fails_open, still to raises")
        new, stale = compare(found, {"gf_open": fo, "gf_notlist": found.get("gf_notlist", {}),
                                     "gf_gone": {"empty": ["raises"]}})
        ok(new == [] and stale == ["gf_gone: empty: raises"], f"GF9 expected list: listed passes, stale reported ({new}, {stale})")
        new2, _ = compare(found, {})
        ok(len(new2) >= 5, "GF10 an unlisted failure is new")
        p = os.path.join(td, "exp.yaml")
        write_expected(found, p)
        ok(load_expected(p) == found, "GF11 the expected list round-trips")
    # Mutation helpers keep layout and stay format-specific.
    t, n = _yaml_numbers("a: 1\nb: [2, x3]\nc: '4'\nd: 1.5e+3\n", ".nan")
    ok(t == "a: .nan\nb: [.nan, x3]\nc: '4'\nd: .nan\n" and n == 3, f"GF12 YAML numbers found by the parser ({t!r})")
    t, n = _json_numbers('{"a": 1, "b": "x2", "c": [-3.5]}', "NaN", False)
    ok(t == '{"a": NaN, "b": "x2", "c": [NaN]}' and n == 2, f"GF13 JSON numbers outside strings ({t!r})")
    tb, kind = _mut_truncated("x.yaml", b"a: 1\nb:\n  - [1, 2, 3]\n  - c\n")
    ok(kind == "invalid" and not _yaml_ok(tb), "GF14 truncation is cut where the YAML no longer parses")
    ok(_mut_truncated("x.md", b"text text") == (None, None), "GF15 prose is not truncated (still valid)")
    t, kind = _mut_alias_loop("x.yaml", b"a: 1\nb: 2\n")
    d = yaml.safe_load(t)
    ok(d["a"][0] is d["a"], "GF16 alias loop keeps the keys, values contain themselves")
    if fails:
        print(f"FAILED: {len(fails)} self-test case(s).")
        return 1
    print("OK: all self-test cases passed.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--report", action="store_true", help="print every failure")
    ap.add_argument("--write-expected", action="store_true")
    ap.add_argument("--json", metavar="PATH", help="write the full sweep result as JSON")
    ap.add_argument("--module", action="append", help="only these modules")
    ap.add_argument("--workers", type=int)
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    res = sweep(HERE, a.module, workers=a.workers)
    found = failures(res)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1)
    if a.report:
        print(report(res))
    if a.write_expected:
        write_expected(found)
        print(f"Wrote {EXPECTED}")
        return 0
    try:
        expected = load_expected()
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"ERROR: cannot read the expected list: {e}")
        return 1
    if a.module:
        expected = {m: d for m, d in expected.items() if m in a.module}
    new, stale = compare(found, expected)
    print(f"gate_fuzz: {len(res['modules'])} checks, {len(res['cases'])} cases in {res.get('seconds')} s; "
          f"{sum(len(s) for d in found.values() for s in d.values())} known failure entries")
    for x in new:
        print(f"NEW FAILURE: {x}")
    for x in stale:
        print(f"FIXED (remove from {os.path.relpath(EXPECTED, ROOT)}): {x}")
    if new or stale:
        return 1
    print("OK: no failures beyond the expected list.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
