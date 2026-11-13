#!/usr/bin/env python3
"""World-model trials (F6, owner 2026-11-13): the world model (toolkit/analysis/world_model.py) on many low-compute
questions, to see how it behaves: does a version 0 written before any simulation give testable hypotheses, how often
are they confirmed, and where does it fail.

    python3 scripts/world_model_trials.py run --total-cap-usd 1000 [--questions FILE] [--only id,id] [--batch B]
        [--limit N] [--trials-dir trials] [--task-cap-usd 2] [--llm-concurrency 5] [--push]
    python3 scripts/world_model_trials.py check-templates [--only id,id]   # inputs only: no LLM, no push
    python3 scripts/world_model_trials.py rebuild [--only id,id]           # circular flags + build again: no LLM, no engine
    python3 scripts/world_model_trials.py score [--trials-dir trials]      # trials/summary.yaml and summary.md
    python3 scripts/world_model_trials.py --self-test

Per question (scripts/world_model_trials_questions.yaml: {id, question, engine, template, shadow_price, ...}):
  1. prepare  copy the template (a decision spec and its synthetic or public inputs) into trials/runs/<id>/, load
              the spec (toolkit/spec/schema.py), build the engine design (toolkit/analysis/batch.py design) and
              store it; the design's features come from world_model.design_features, the function load_sweep uses
  2. seed     the bounded seed task (agents/v2_sdk_tasks.py seed_world_model: one SDK session, file tools, a USD
              cap, the decision design principles and the world model's own seed documentation in its prompt) writes
              world_model_seed.yaml before any result exists (it cannot read analysis/ or other runs). The harness
              checks it with world_model.validate (the validator register and build use) and against the design's
              package and feature names, and asks for one repair with every error; refused twice, the question stops
  3. register world_model register (the CLI's locked append to analysis/hypotheses_registry.jsonl)
  4. anchor   with --push: the ledger and the seed (only those two files, checked against an allow-list before
              every commit) are committed to branch wm-trials in its own git worktree (the main working tree is
              never touched), pushed without force and fetched; the build anchors on refs/remotes/origin/wm-trials.
              Without --push every confrontation is post hoc
  5. engine   the batch (toolkit/analysis/batch.py run, a local process pool of at most 2 workers, OMP_NUM_THREADS=2),
              one batch at a time across all questions; refused when a cloud scheduler is configured or the design
              exceeds --max-engine-runs; then world_model.record_first_seen (the driver records the batch's first
              appearance; batch.py does not yet)
  6. build    world_model build --without-plan [--shadow-price] --anchor-ref refs/remotes/origin/wm-trials (the CLI)
LLM steps run in parallel (--llm-concurrency, default 5); engine and build steps in one queue.

Spend. trials/spend.jsonl is an append-only, hash-chained ledger across invocations: a reservation of a task's cap
before it is launched, then its charge (the SDK-reported cost; the whole cap when none is reported). Committed spend
is the charges plus every open reservation at its cap. A task is launched only when committed spend plus its cap is
within --total-cap-usd (required); otherwise it and every later LLM step are refused and the question is recorded as
budget_refused. The engine runs locally: its compute is recorded at $0 with its backend. Spend per question is in
trials/runs/<id>/trial.json and the summary.

Resumable: trials/runs/<id>/trial.json records each step; a completed question is skipped, a partial one resumes at
its first missing step (a validated seed is never re-asked; a registered one is not re-registered). Nothing in this
file names a disease, a place or an intervention: those are in the questions file and the templates.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (REPO, os.path.join(REPO, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class TrialError(ValueError):
    """An input the trials cannot use, or a refused action (budget, push scope): fails closed with the reason."""


QUESTIONS = os.path.join(REPO, "scripts", "world_model_trials_questions.yaml")
CHEAP_ENGINES = frozenset({"toolkit.engines.compartmental.engine:CompartmentalEngine",
                           "toolkit.engines.renewal.engine:RenewalEngine",
                           "toolkit.engines.empirical.engine:PackageRateEngine"})
SEED_FILE = "world_model_seed.yaml"                      # what the seed task writes (agents/v2_sdk_tasks.SEED_FILE)
SEED = "analysis/world_model_seed.yaml"                  # the validated copy the harness registers and builds from
TRIAL = "trial.json"
BRANCH, REMOTE = "wm-trials", "origin"
HYPOTHESES = (5, 8)
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
STEPS = ("prepare", "seed", "register", "anchor", "engine", "build")
NUMBER_RE = re.compile(r"(?<![A-Za-z_\d.])[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?%?")    # a standalone number
# files the harness writes or hands to a task: never a data belief's evidence (a dataset of the run is)
HARNESS_NAMES = ("_task_input.json", "_task_output.json", TRIAL, "analysis/", "run_state.json", "budget_ledger.json",
                 SEED_FILE)
CIRCULAR_R2 = 0.99           # a package's reduction explained this well by its own declared effect parameters alone
_CLI_LOCK = threading.Lock()                     # world_model CLI calls print; their capture swaps sys.stdout
CLOUD_ENV = ("MA_COMPUTE_SCHEDULER", "MA_COMPUTE_RESOURCE", "MA_COMPUTE_TARGET_WORKERS")


class TrialError(ValueError):
    """An input the trials cannot use, or a refused action (budget, push scope): fails closed with the reason."""


def _sio():
    from lib import safe_io
    return safe_io


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _sha(x) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(x, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------- questions
def load_questions(path=QUESTIONS, root=REPO, engines=CHEAP_ENGINES) -> list[dict]:
    """The questions file's entries, checked as inputs before any run: a plain unique id, the question text, an engine
    among the cheap engines, a template directory holding decision_spec.yaml whose engine is that engine, and a
    shadow price that is a positive number or null. Any problem refuses the whole file."""
    import yaml
    try:
        doc = yaml.safe_load(open(path))
    except (OSError, yaml.YAMLError) as e:
        raise TrialError(f"{path}: unreadable ({e})")
    qs = (doc or {}).get("questions") if isinstance(doc, dict) else None
    if not isinstance(qs, list) or not qs:
        raise TrialError(f"{path}: questions: a non-empty list")
    bad, seen = [], set()
    for k, q in enumerate(qs):
        w = f"questions[{k}]"
        if not isinstance(q, dict):
            bad.append(f"{w}: a mapping")
            continue
        qid = str(q.get("id") or "")
        w = f"question {qid or k}"
        if not ID_RE.match(qid):
            bad.append(f"{w}: id a plain name ({ID_RE.pattern})")
        elif qid in seen:
            bad.append(f"{w}: duplicate id")
        seen.add(qid)
        if not str(q.get("question") or "").strip():
            bad.append(f"{w}: question text")
        if q.get("engine") not in engines:
            bad.append(f"{w}: engine {q.get('engine')!r} is not one of {sorted(engines)}")
        sp = q.get("shadow_price")
        if sp is not None and not (isinstance(sp, (int, float)) and not isinstance(sp, bool) and sp > 0):
            bad.append(f"{w}: shadow_price a positive number or null")
        tpl = os.path.realpath(os.path.join(root, str(q.get("template") or "")))
        spec = os.path.join(tpl, "decision_spec.yaml")
        if not q.get("template") or not os.path.isfile(spec) or not tpl.startswith(os.path.realpath(root) + os.sep):
            bad.append(f"{w}: template {q.get('template')!r} is not a directory under {root} holding decision_spec.yaml")
            continue
        try:
            ref = ((yaml.safe_load(open(spec)) or {}).get("engine") or {}).get("ref")
        except (OSError, yaml.YAMLError, AttributeError):
            ref = None
        if ref != q.get("engine"):
            bad.append(f"{w}: the template's engine {ref!r} is not the question's {q.get('engine')!r}")
    if bad:
        raise TrialError(f"{path}: " + "; ".join(bad))
    return qs


def select(qs, only=None, batch=None, limit=None) -> list[dict]:
    out = [q for q in qs if (not only or q["id"] in only) and (batch is None or str(q.get("batch")) == str(batch))]
    if only and set(only) - {q["id"] for q in out}:
        raise TrialError(f"--only names unknown questions {sorted(set(only) - {q['id'] for q in out})}")
    return out[:limit] if limit else out


# ---------------------------------------------------------------- spend
class SpendLedger:
    """Append-only, hash-chained spend ledger across invocations (trials/spend.jsonl). Lines: {kind: reserve, rid,
    question, task, cap_usd} before a task is launched, then {kind: charge, rid, usd, source}; {kind: compute, ...}
    records an engine step's cost. Committed spend = charges + compute + every open reservation at its cap. The chain
    (each line's sha256 and the previous line's) is verified on every read: an edited, removed or reordered line
    refuses the ledger. One lock (threads and processes) spans each read-check-append."""

    def __init__(self, path, total_cap_usd):
        if isinstance(total_cap_usd, bool) or not isinstance(total_cap_usd, (int, float)) or not total_cap_usd > 0:
            raise TrialError(f"total cap {total_cap_usd!r}: a positive number of USD")
        self.path, self.cap = os.path.abspath(path), float(total_cap_usd)
        self.root, self.rel = os.path.dirname(self.path), os.path.basename(self.path)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _locked(self):
        import fcntl
        with self._lock, _sio().open_append_nofollow(self.root, self.rel) as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                yield f
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _lines(self) -> list[dict]:
        out, prev = [], "0" * 64
        if not os.path.isfile(self.path):
            return out
        for k, raw in enumerate(open(self.path)):
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except ValueError:
                raise TrialError(f"{self.path} line {k + 1}: malformed")
            if e.get("prev") != prev or e.get("line_sha") != _sha({x: v for x, v in e.items() if x != "line_sha"}):
                raise TrialError(f"{self.path} line {k + 1}: edited, removed or reordered (the chain does not verify)")
            prev = e["line_sha"]
            out.append(e)
        return out

    def _append(self, f, lines, e):
        e = dict(e, at=_now(), total_cap_usd=self.cap, prev=lines[-1]["line_sha"] if lines else "0" * 64)
        e["line_sha"] = _sha(e)
        f.write(json.dumps(e, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
        return e

    @staticmethod
    def _committed(lines) -> float:
        charged = {e["rid"]: float(e["usd"]) for e in lines if e["kind"] == "charge"}
        return sum(charged.get(e["rid"], float(e["cap_usd"])) for e in lines if e["kind"] == "reserve") + \
            sum(float(e["usd"]) for e in lines if e["kind"] == "compute")

    def committed(self) -> float:
        with self._locked():
            return self._committed(self._lines())

    def spent(self) -> float:
        with self._locked():
            L = self._lines()
            return sum(float(e["usd"]) for e in L if e["kind"] in ("charge", "compute"))

    def reserve(self, question, task, cap_usd):
        """A reservation id, or None when committed spend plus this cap would exceed the total cap (refused)."""
        with self._locked() as f:
            L = self._lines()
            if self._committed(L) + float(cap_usd) > self.cap + 1e-9:
                return None
            rid = _sha([question, task, _now(), len(L), os.getpid(), threading.get_ident()])[:16]
            self._append(f, L, {"kind": "reserve", "rid": rid, "question": question, "task": task,
                                "cap_usd": float(cap_usd)})
            return rid

    def charge(self, rid, usd, source):
        with self._locked() as f:
            L = self._lines()
            if rid not in {e["rid"] for e in L if e["kind"] == "reserve"} or \
                    rid in {e["rid"] for e in L if e["kind"] == "charge"}:
                raise TrialError(f"spend ledger: no open reservation {rid}")
            self._append(f, L, {"kind": "charge", "rid": rid, "usd": float(usd), "source": source})

    def compute(self, question, usd, backend):
        with self._locked() as f:
            self._append(f, self._lines(), {"kind": "compute", "rid": None, "question": question, "usd": float(usd),
                                            "source": backend})

    def by_question(self) -> dict:
        with self._locked():
            L = self._lines()
        charged = {e["rid"]: float(e["usd"]) for e in L if e["kind"] == "charge"}
        out = {}
        for e in L:
            if e["kind"] == "reserve":
                q = out.setdefault(e["question"], {"llm_usd": 0.0, "compute_usd": 0.0, "open_reservations": 0})
                q["llm_usd"] += charged.get(e["rid"], 0.0)
                q["open_reservations"] += e["rid"] not in charged
            elif e["kind"] == "compute":
                q = out.setdefault(e["question"], {"llm_usd": 0.0, "compute_usd": 0.0, "open_reservations": 0})
                q["compute_usd"] += float(e["usd"])
        return out


# ---------------------------------------------------------------- the wm-trials branch
class GitAnchor:
    """The wm-trials branch in its own git worktree: commits the registration ledger and the seed of a question (only
    those, at their paths in the main working tree, which is where world_model.git_anchors looks them up), pushes
    without force and fetches, so refs/remotes/<remote>/<branch> anchors the ledger. Every commit is checked: the
    paths staged and every path the branch holds match the allow-list."""

    def __init__(self, repo, worktree, branch=BRANCH, remote=REMOTE, trials_rel="trials"):
        self.repo, self.wt = os.path.realpath(repo), os.path.abspath(worktree)
        self.branch, self.remote = branch, remote
        self.ref = f"refs/remotes/{remote}/{branch}"
        self.allowed = re.compile(rf"^{re.escape(trials_rel.strip('/'))}/runs/[A-Za-z0-9][A-Za-z0-9_.-]*/analysis/"
                                  r"(hypotheses_registry\.jsonl|world_model_seed\.yaml)$")
        self._lock = threading.Lock()

    def _git(self, *a, cwd=None, check=True):
        r = subprocess.run(["git", *a], cwd=cwd or self.repo, capture_output=True, text=True, timeout=300)
        if check and r.returncode:
            raise TrialError(f"git {' '.join(a)}: {r.stderr.strip()[:400]}")
        return r

    def ensure(self):
        if os.path.isdir(self.wt) and os.path.exists(os.path.join(self.wt, ".git")):
            cur = self._git("symbolic-ref", "--short", "HEAD", cwd=self.wt, check=False).stdout.strip()
            if cur != self.branch:
                raise TrialError(f"{self.wt} is checked out on {cur!r}, not {self.branch}")
            return
        self._git("fetch", "-q", self.remote, self.branch, check=False)
        has = lambda r: self._git("show-ref", "--verify", "-q", r, check=False).returncode == 0  # noqa: E731
        if has(f"refs/heads/{self.branch}"):
            self._git("worktree", "add", "-q", self.wt, self.branch)
        elif has(self.ref):
            self._git("worktree", "add", "-q", "-b", self.branch, self.wt, self.ref)
        else:
            self._git("worktree", "add", "-q", "--orphan", "-b", self.branch, self.wt)

    def _check(self, rel):
        if os.path.normpath(rel) != rel or not self.allowed.match(rel):
            raise TrialError(f"{rel!r}: only a question's registration ledger and seed go to {self.branch}")
        src = os.path.join(self.repo, rel)
        if os.path.islink(src) or not os.path.isfile(src):
            raise TrialError(f"{rel}: not a regular file in the working tree")

    def publish(self, rels, qid) -> dict:
        for rel in rels:
            self._check(rel)
        with self._lock:
            self.ensure()
            for rel in rels:
                data, _ = _sio().read_nofollow(self.repo, rel)
                _sio().write_nofollow(self.wt, rel, data, atomic=True)
            self._git("add", "--", *rels, cwd=self.wt)
            staged = [x for x in self._git("diff", "--cached", "--name-only", cwd=self.wt).stdout.splitlines() if x]
            held = [x for x in self._git("ls-files", cwd=self.wt).stdout.splitlines() if x]
            stray = sorted(set(staged) - set(rels)) + [x for x in held if not self.allowed.match(x)]
            if stray:
                raise TrialError(f"{self.branch}: refusing to commit paths outside the allow-list: {stray[:5]}")
            if staged:
                self._git("commit", "-q", "-m", f"wm-trials: register {qid} ({len(staged)} file(s))", cwd=self.wt)
            self._git("push", "-q", self.remote, f"{self.branch}:{self.branch}", cwd=self.wt)
            self._git("fetch", "-q", self.remote, self.branch, cwd=self.wt)
            head = self._git("rev-parse", "HEAD", cwd=self.wt).stdout.strip()
            if self._git("merge-base", "--is-ancestor", head, self.ref, check=False).returncode:
                raise TrialError(f"{head} is not reachable from {self.ref} after the push and fetch")
            at = self._git("show", "-s", "--format=%cI", head, cwd=self.wt).stdout.strip()
            return {"commit": head, "at": at, "ref": self.ref}


# ---------------------------------------------------------------- per-question steps
def _trial(run_dir) -> dict:
    p = os.path.join(run_dir, TRIAL)
    return json.load(open(p)) if os.path.isfile(p) else {}


def _save(run_dir, t):
    _sio().write_nofollow(run_dir, TRIAL, json.dumps(t, indent=1, sort_keys=True, default=str).encode(), atomic=True)


def prepare(q, run_dir, root=REPO, max_engine_runs=50) -> dict:
    """Copy the template, load the spec, build and store the engine design, and return the seed context (inputs
    only: no result exists). A design over the engine-run limit is refused here and again at run time."""
    from toolkit.analysis import batch as B
    from toolkit.analysis import world_model as W
    from toolkit.spec import schema
    tpl = os.path.join(root, q["template"])
    os.makedirs(run_dir, exist_ok=True)
    for dp, dn, fn in os.walk(tpl):
        dn[:] = [x for x in dn if x != "analysis"]
        for name in fn:
            rel = os.path.relpath(os.path.join(dp, name), tpl)
            data, _ = _sio().read_nofollow(tpl, rel)
            _sio().write_nofollow(run_dir, rel, data, atomic=True)
    spec = schema.load(os.path.join(run_dir, "decision_spec.yaml"))
    design, jobs = B.design(spec)
    if len(jobs) > max_engine_runs:
        raise TrialError(f"{q['id']}: the design has {len(jobs)} engine runs, over the limit of {max_engine_runs}")
    _sio().write_nofollow(run_dir, "analysis/batch/design.json", json.dumps(design).encode(), atomic=True)
    F, kinds = W.design_features(spec, design)
    card = spec.engine().describe().to_dict()
    attr = spec["calibration"].get("attribute")
    feats = {}
    for n, v in F.items():
        f = {"kind": "calibration" if attr and n == f"{attr} (calibration scale)" else kinds[n],
             "distinct_values": len(set(v))}
        if f["kind"] != "parameter":          # an engine effect's or prior's range is a spec input: withheld
            f["range"] = [min(v), max(v)]
        else:
            f["range_withheld"] = "an engine effect or prior value is a spec input"
        if f["kind"] == "calibration":
            f["read_by_engine"] = attr in (card.get("unit_attributes") or {})
        feats[n] = f
    datasets = []
    for dp, dn, fn in os.walk(run_dir):
        dn[:] = [x for x in dn if x != "analysis"]
        for name in sorted(fn):
            if name.endswith(".csv"):
                rel = os.path.relpath(os.path.join(dp, name), run_dir)
                with open(os.path.join(dp, name), newline="") as fh:
                    head = fh.readline().strip()
                datasets.append({"file": rel, "columns": [c.strip() for c in head.split(",") if c.strip()]})
    return {"question": q["question"],
            "withheld": "the spec's values, the engine's effect values and priors and the input data values are "
                        "withheld: the world model is built from literature and reasoning, then confronted with the "
                        "simulation, never with its own inputs",
            "engine": {"name": card.get("name"), "components": card.get("components"),
                       "mechanisms": [NUMBER_RE.sub("<value>", m) for m in card.get("mechanisms") or []],
                       "unit_attributes": sorted(card.get("unit_attributes") or {})},
            "datasets": datasets,
            "packages": {k: sorted(v) for k, v in spec.packages().items()}, "comparator": spec.comparator,
            "primary_outcome": spec.outcomes()[0],
            "sweep": {"design_points": len(design["points"]), "engine_runs": len(jobs), "features": feats,
                      "independent_features": list(design.get("independent_features") or []),
                      "shadow_price": q.get("shadow_price"), "optimised_plan": False,
                      "min_points": W.SETTINGS["min_points"], "small_sample_below": W.SETTINGS["perm_n_max"]},
            "hypotheses": {"min": HYPOTHESES[0], "max": HYPOTHESES[1]}}


def seed_problems(seed, ctx) -> list[str]:
    """Why the harness refuses a seed (empty when it is accepted): the world model's own validator (world_model.validate,
    as register and build call it), the number of hypotheses, and every package and feature a test names being one of
    the design's (a name the sweep lacks would fail the build)."""
    from toolkit.analysis import world_model as W
    try:
        W.validate(seed)
    except W.WorldModelError as e:
        msg = str(e).replace("world-model seed refused: ", "")
        return [x for x in msg.split("; ") if x]
    bad, hs = [], seed.get("hypotheses") or []
    if not HYPOTHESES[0] <= len(hs) <= HYPOTHESES[1]:
        bad.append(f"{len(hs)} hypotheses: write {HYPOTHESES[0]} to {HYPOTHESES[1]}")
    pk, ft = set(ctx["packages"]), set(ctx["sweep"]["features"])
    for h in hs:
        t = h["test"]
        for x in ("package", "a", "b"):
            if t.get(x) and t["kind"] != "plan_share" and t[x] not in pk:
                bad.append(f"hypothesis {h['id']}: package {t[x]!r} is not one of {sorted(pk)}")
        if t["kind"] == "plan_share":
            continue
        names = ([t["feature"]] if t.get("feature") else []) + list(t.get("where") or {}) + list(t.get("adjust_for")
                                                                                              or [])
        for n in names:
            if n not in ft:
                bad.append(f"hypothesis {h['id']}: feature {n!r} is not one of the sweep's {sorted(ft)}")
    files = [x["file"] for x in ctx.get("datasets") or []]
    for b in seed.get("beliefs") or []:
        for e in (b.get("evidence") or []) if b.get("source_type") == "literature" else []:
            harness = [x for x in HARNESS_NAMES if x in str(e.get("citation") or "")]
            if harness:
                bad.append(f"belief {b['id']}: literature evidence cites {harness}, a file the harness writes; cite "
                           "the paper (authors, journal, year) and its table or page")
        if b.get("source_type") != "data":
            continue
        for e in b.get("evidence") or []:
            de = str(e.get("dossier_entry") or "")
            harness = [x for x in HARNESS_NAMES if x in de]
            if harness:
                bad.append(f"belief {b['id']}: data evidence {de[:80]!r} cites {harness}, a file the harness writes; a "
                           f"data belief cites a dataset of the run ({files}) and its column")
            elif not any(f in de for f in files):
                bad.append(f"belief {b['id']}: data evidence {de[:80]!r} names no dataset of the run ({files}); cite "
                           "the file and column, or make it a literature belief with its citation")
    return bad


def _own(name, comps) -> bool:
    """Is design parameter `name` one of these components' own effect parameters? By the engines' parameter naming
    (<kind>:<component>[:<outcome>], <a>+<b> for an interaction, or <kind>_<component>): a classification heuristic of
    the trials' scoring, documented as such."""
    toks = set(re.split(r"[:+]", name))
    return any(c in toks or name.endswith("_" + c) for c in comps)


def circular_tests(run_dir, seed, q) -> dict:
    """{hypothesis id: why} for tests whose verdict a spec input fixes, from the spec's inputs and the engine's own
    sweep (world_model.load_sweep, canonical_test and the sweep's reduction, as the build reads them):
      - a test of a package's effect against one of its own effect parameters (feature, region or adjustment);
      - an effect band on a package whose relative reduction its own declared effect parameters explain alone
        (R^2 >= CIRCULAR_R2 of a quadratic fit, linear below twice the quadratic's columns of points) and whose
        range over the design (which spans the declared ranges) lies inside the band.
    Applied in world_model build (--circular): reported, counted apart, never a confirmation, never moving confidence."""
    import numpy as np
    from toolkit.analysis import world_model as W
    sw, _ = W.load_sweep(run_dir, "current", q.get("shadow_price"), use_plan=False)
    by = {b["id"]: b for b in seed.get("beliefs") or []}
    out = {}
    for h in seed.get("hypotheses") or []:
        t = W.canonical_test(h["test"], by.get(h["belief"]))
        if t["kind"] == "plan_share":
            continue
        pk = [t[x] for x in ("package", "a", "b") if t.get(x)]
        comps = {c for p in pk for c in sw.packages.get(p, [])}
        own = [f for f in sw.features if sw.kinds.get(f) == "parameter" and _own(f, comps)]
        named = [t["feature"]] if t.get("feature") else []      # a test against it; adjusting for it or a region is not
        hit = [n for n in named if n in own]
        if hit:
            out[h["id"]] = f"tests {pk}'s effect against its own effect parameter(s) {hit}"
            continue
        if t["kind"] != "effect_band" or not own:
            continue
        red = sw.reduction(t["package"])
        m = W._region(sw, t.get("where")) & np.isfinite(red)
        X = np.column_stack([sw.features[f][m] for f in own])
        y, n = red[m], int(m.sum())
        if n < W.SETTINGS["min_points"]:
            continue
        cols = [np.ones(n)] + [X[:, i] for i in range(X.shape[1])]
        quad = cols + [X[:, i] * X[:, j] for i in range(X.shape[1]) for j in range(i, X.shape[1])]
        A = np.column_stack(quad if n >= 2 * len(quad) else cols)
        res = y - A @ np.linalg.lstsq(A, y, rcond=None)[0]
        r2 = 1.0 - float(res @ res) / float(((y - y.mean()) ** 2).sum()) if np.ptp(y) > 0 else 1.0
        lo, hi = float(y.min()), float(y.max())
        if r2 >= CIRCULAR_R2 and t["band"][0] <= lo and hi <= t["band"][1]:
            out[h["id"]] = (f"{t['package']}'s relative reduction is fixed by its declared effect parameter(s) {own} "
                            f"(R^2 {r2:.4f}); its range over the design [{lo:.3g}, {hi:.3g}] lies inside the band "
                            f"{t['band']}")
    return out


def seed_step(q, run_dir, ctx, seed_fn, ledger, task_cap_usd) -> dict:
    """The seed task, repaired once. Each session's cap is reserved before it is launched (refused: budget) and
    charged with the reported cost after it."""
    import yaml
    out = {"sessions": 0, "repairs": 0, "usd": 0.0, "notes": []}
    feedback = None
    for attempt in range(2):
        rid = ledger.reserve(q["id"], "seed" if attempt == 0 else "seed repair", task_cap_usd)
        if rid is None:
            return dict(out, status="budget_refused")
        try:
            r = seed_fn(run_dir, ctx, feedback, task_cap_usd)
        except Exception as e:  # noqa: BLE001 -- the reservation is charged in full, then the question stops
            ledger.charge(rid, task_cap_usd, f"crashed: {type(e).__name__}: whole cap")
            return dict(out, status="seed_failed", error=f"{type(e).__name__}: {e}"[:300], usd=out["usd"] + task_cap_usd)
        usd = float(r.usd) if r is not None and isinstance(r.usd, (int, float)) else task_cap_usd
        ledger.charge(rid, usd, "sdk reported" if r is not None else "no result: whole cap")
        out["sessions"] += 1
        out["usd"] += usd
        out["notes"].append(getattr(r, "note", ""))
        # the seed file is what the harness judges, fresh on every attempt (a session that ran out of budget after
        # writing a valid seed still gives a valid seed; its missing notes file does not matter)
        path, seed = os.path.join(run_dir, SEED_FILE), None
        if not os.path.isfile(path) or os.path.islink(path):
            feedback = [f"no {SEED_FILE} was written" + (f" (the session: {r.note})" if r is not None and r.note
                                                         else "")]
        else:
            try:
                data, _ = _sio().read_nofollow(run_dir, SEED_FILE)
                seed = yaml.safe_load(data.decode("utf-8"))
                feedback = seed_problems(seed, ctx) if isinstance(seed, dict) else [f"{SEED_FILE}: a mapping"]
            except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
                feedback = [f"{SEED_FILE} is not readable YAML: {e}"[:300]]
        if not feedback:
            data, _ = _sio().read_nofollow(run_dir, SEED_FILE)
            _sio().write_nofollow(run_dir, SEED, data, atomic=True)
            return dict(out, status="ok", hypotheses=len(seed["hypotheses"]), refusals=out.get("refusals", []))
        out.setdefault("refusals", []).append(feedback)
        if attempt == 0:
            out["repairs"] = 1
    return dict(out, status="seed_refused")


def register_step(run_dir) -> dict:
    from toolkit.analysis import world_model as W
    buf = io.StringIO()
    with _CLI_LOCK, contextlib.redirect_stdout(buf):      # sys.stdout is process-wide: one at a time
        W._cli(["register", os.path.join(run_dir, SEED), run_dir])
    return {"note": buf.getvalue().strip()}


def engine_step(q, run_dir, max_engine_runs=50, workers=2) -> dict:
    """The batch on the stored design (re-derived from the spec and required to be identical), on a local pool of at
    most `workers` processes; a cloud scheduler in the environment or a design over the limit is refused. Then the
    batch's first appearance is recorded (world_model.record_first_seen)."""
    from toolkit.analysis import batch as B
    from toolkit.analysis import world_model as W
    from toolkit.spec import schema
    if any(os.environ.get(k) for k in CLOUD_ENV):
        raise TrialError(f"a cloud scheduler is configured ({[k for k in CLOUD_ENV if os.environ.get(k)]}); the "
                         "trials run locally unless the owner approves a cloud backend")
    if workers > 2:
        raise TrialError("at most 2 worker processes (owner's laptop rule)")
    spec = schema.load(os.path.join(run_dir, "decision_spec.yaml"))
    design, jobs = B.design(spec)
    stored = json.load(open(os.path.join(run_dir, "analysis", "batch", "design.json")))
    if json.loads(json.dumps(design)) != stored:
        raise TrialError(f"{q['id']}: the design re-derived from the spec differs from the one the seed was told")
    if len(jobs) > max_engine_runs:
        raise TrialError(f"{q['id']}: {len(jobs)} engine runs, over the limit of {max_engine_runs}")
    t0 = time.time()
    r = B.run(run_dir, jobs, spec.engine(), max_workers=workers)
    s = r["summary"]
    if s.get("backend") != "local" or (s.get("workers") or 0) > workers:
        raise TrialError(f"{q['id']}: the batch ran on {s.get('backend')} with {s.get('workers')} workers")
    if r["problems"] or s.get("n_failed"):
        raise TrialError(f"{q['id']}: {s.get('n_failed')} engine runs failed, {len(r['problems'])} rows refused "
                         f"({r['problems'][:2]})")
    first = W.record_first_seen(run_dir)
    return {"jobs": len(jobs), "wall_s": round(time.time() - t0, 2), "backend": s.get("backend"),
            "workers": s.get("workers"), "first_seen": first, "compute_usd": 0.0}


def build_step(q, run_dir, anchor_ref=None) -> dict:
    import yaml
    from toolkit.analysis import world_model as W
    seed = yaml.safe_load(open(os.path.join(run_dir, SEED)))
    circ = circular_tests(run_dir, seed, q)
    _sio().write_nofollow(run_dir, "analysis/circular.json", json.dumps(circ, indent=1, sort_keys=True).encode(),
                          atomic=True)
    argv = ["build", run_dir, "--seed", os.path.join(run_dir, SEED), "--without-plan",
            "--circular", os.path.join(run_dir, "analysis", "circular.json")]
    if q.get("shadow_price") is not None:
        argv += ["--shadow-price", repr(float(q["shadow_price"]))]
    if anchor_ref:
        argv += ["--anchor-ref", anchor_ref]
    buf = io.StringIO()
    with _CLI_LOCK, contextlib.redirect_stdout(buf):      # sys.stdout is process-wide: one at a time
        W._cli(argv)
    hy = yaml.safe_load(open(os.path.join(run_dir, "analysis", "hypotheses.yaml")))
    st = {}
    for h in hy["hypotheses"]:
        st[h["status"]] = st.get(h["status"], 0) + 1
    return {"statuses": st, "circular": sorted(circ), "note": buf.getvalue().strip()}


def _wait_past(at):
    """Sleep until the wall clock is past `at` (an ISO time to the second), so the batch's first-seen time is after
    the anchor commit's time (a tie would make every confrontation post hoc)."""
    t = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    while time.time() < t + 1.05:
        time.sleep(0.1)


def run_trials(questions, trials_root, repo=REPO, seed_fn=None, ledger=None, anchor=None, task_cap_usd=2.0,
               llm_concurrency=5, engine_workers=2, max_engine_runs=50, engines=CHEAP_ENGINES, engine_probe=None,
               root=None) -> dict:
    """Every question through prepare, seed, register, anchor, engine and build (module docstring). Returns {id:
    trial record}; each record is also trials/runs/<id>/trial.json."""
    import queue
    from concurrent.futures import ThreadPoolExecutor
    if ledger is None or seed_fn is None:
        raise TrialError("run_trials needs a spend ledger and a seed task")
    root = root or repo
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    rel_root = os.path.relpath(os.path.realpath(trials_root), os.path.realpath(repo)).replace(os.sep, "/")
    if anchor is not None and (rel_root.startswith("..") or os.path.isabs(rel_root)):
        raise TrialError(f"{trials_root} must be inside {repo} for the anchor to find the ledger at its path")
    if anchor is not None:
        anchor.allowed = GitAnchor(repo, anchor.wt, anchor.branch, anchor.remote, rel_root).allowed
    results, lock, stop = {}, threading.Lock(), {"budget": False}
    work = queue.Queue()

    def record(qid, t):
        with lock:
            results[qid] = t

    def llm_part(q):
        rd = os.path.join(trials_root, "runs", q["id"])
        os.makedirs(rd, exist_ok=True)
        t = _trial(rd)
        if t.get("status") == "complete":
            return record(q["id"], dict(t, skipped=True))
        t = dict(t, id=q["id"], engine_ref=q["engine"], rerun_of=q.get("rerun_of"),
                 steps=dict(t.get("steps") or {}), status="running")
        try:
            if "prepare" not in t["steps"]:
                t["context"] = prepare(q, rd, root, max_engine_runs)
                t["steps"]["prepare"] = _now()
                _save(rd, t)
            if "seed" not in t["steps"]:
                if stop["budget"]:
                    t["status"] = "budget_refused"
                    _save(rd, t)
                    return record(q["id"], t)
                s = seed_step(q, rd, t["context"], seed_fn, ledger, task_cap_usd)
                t["seed"] = s
                if s["status"] != "ok":
                    stop["budget"] = stop["budget"] or s["status"] == "budget_refused"
                    t["status"] = s["status"]
                    _save(rd, t)
                    return record(q["id"], t)
                t["steps"]["seed"] = _now()
                _save(rd, t)
            if "register" not in t["steps"]:
                t["register"] = register_step(rd)
                t["steps"]["register"] = _now()
                _save(rd, t)
            if "anchor" not in t["steps"]:
                if anchor is not None:
                    rels = [f"{rel_root}/runs/{q['id']}/{SEED}",
                            f"{rel_root}/runs/{q['id']}/analysis/hypotheses_registry.jsonl"]
                    t["anchor"] = anchor.publish(rels, q["id"])
                    _wait_past(t["anchor"]["at"])
                else:
                    t["anchor"] = None
                t["steps"]["anchor"] = _now()
                _save(rd, t)
            work.put((q, rd, t))
        except Exception as e:  # noqa: BLE001 -- recorded on the question; the others carry on
            t.update(status="failed", error=f"{type(e).__name__}: {e}"[:500])
            _save(rd, t)
            record(q["id"], t)

    def engine_worker():
        while True:
            item = work.get()
            if item is None:
                return
            q, rd, t = item
            try:
                if "engine" not in t["steps"]:
                    if engine_probe:
                        engine_probe("start")
                    try:
                        t["engine"] = engine_step(q, rd, max_engine_runs, engine_workers)
                    finally:
                        if engine_probe:
                            engine_probe("end")
                    ledger.compute(q["id"], t["engine"]["compute_usd"], t["engine"]["backend"])
                    t["steps"]["engine"] = _now()
                    _save(rd, t)
                t["build"] = build_step(q, rd, anchor.ref if anchor is not None else None)
                t["steps"]["build"] = _now()
                pq = ledger.by_question().get(q["id"]) or {}
                t.update(status="complete", spend_usd=round(pq.get("llm_usd", 0.0) + pq.get("compute_usd", 0.0), 6))
            except Exception as e:  # noqa: BLE001
                t.update(status="failed", error=f"{type(e).__name__}: {e}"[:500])
            _save(rd, t)
            record(q["id"], t)

    ew = threading.Thread(target=engine_worker, daemon=True)
    ew.start()
    with ThreadPoolExecutor(max_workers=max(1, int(llm_concurrency))) as ex:
        list(ex.map(llm_part, questions))
    work.put(None)
    ew.join()
    return results


# ---------------------------------------------------------------- scoring
# The scoring's own review heuristics (not world-model rules; the verdicts are the build's): a band test is lenient
# when its band spans at least LENIENT_WIDTH of relative reduction or its threshold share is at most LENIENT_SHARE;
# a negligible claim when its equivalence bound is at least LENIENT_EQUIVALENCE. A literature citation is machine-
# verifiable only with a year and a location naming a table, figure, page or section.
SCORING = {"lenient_width": 0.6, "lenient_share": 0.6, "lenient_equivalence": 0.3}
YEAR_RE = re.compile(r"\b(19|20)\d\d\b")
LOC_RE = re.compile(r"\b(table|tab\.|fig|figure|p\.|pp\.|page|section|supplement|appendix|abstract|results|methods|"
                    r"discussion|introduction|main text|summary of findings|eq\.|equation)", re.I)
MODES = ("circular", "untestable", "post_hoc", "conditional_not_settled", "too_few_points", "test_too_lenient", "wrong_estimand",
         "inert_feature", "high_confidence_refuted", "contradiction_without_open_question", "citation_not_verifiable")


def failure_modes(hyps, seed, wm, ctx=None) -> dict:
    """{mode: [hypothesis or belief ids]} for the checked hypotheses of one question (MODES; empty modes omitted)."""
    by = {b["id"]: b for b in seed.get("beliefs") or []}
    qs = {q.get("id") for q in (wm or {}).get("open_questions") or []}
    feats = ((ctx or {}).get("sweep") or {}).get("features") or {}
    out = {m: [] for m in MODES}
    for h in hyps:
        t, st, obs = h.get("test") or {}, h.get("status"), (h.get("result") or {}).get("observed") or {}
        b = by.get(h.get("belief")) or {}
        if st == "untestable":
            out["untestable"].append(h["id"])
        if (h.get("confrontation") or {}).get("circular"):
            out["circular"].append(h["id"])
        if (h.get("confrontation") or {}).get("post_hoc"):
            out["post_hoc"].append(h["id"])
        if isinstance(obs, dict) and obs.get("marginal_only"):
            out["conditional_not_settled"].append(h["id"])
        if st == "inconclusive" and isinstance(obs, dict) and ("usable points" in str(obs.get("reason") or "") or
                                                               "usable points" in str(obs.get("why") or "")):
            out["too_few_points"].append(h["id"])
        k = t.get("kind")
        lenient = (k == "effect_band" and (t["band"][1] - t["band"][0] >= SCORING["lenient_width"] or
                                           t["min_share"] <= SCORING["lenient_share"])) or \
            (k == "paired_sign" and t["min_share"] <= SCORING["lenient_share"]) or \
            (k == "effect_direction" and t.get("direction") == "negligible" and
             t.get("equivalence", 0) >= SCORING["lenient_equivalence"])
        if lenient:
            out["test_too_lenient"].append(h["id"])
        mg = b.get("magnitude") or {}
        if k == "effect_band" and mg and mg.get("scale") != "relative_reduction":
            out["wrong_estimand"].append(h["id"])
        named = [t.get("feature")] + list(t.get("where") or {}) if k != "plan_share" else []
        if any((feats.get(n) or {}).get("read_by_engine") is False for n in named if n):
            out["inert_feature"].append(h["id"])
        if st == "refuted" and b.get("confidence") == "high":
            out["high_confidence_refuted"].append(h["id"])
        if st == "refuted" and f"Q.{h['id']}" not in qs:
            out["contradiction_without_open_question"].append(h["id"])
    for b in seed.get("beliefs") or []:
        if b.get("source_type") == "literature" and not all(
                YEAR_RE.search(str(e.get("citation") or "")) and LOC_RE.search(str(e.get("location") or ""))
                for e in b.get("evidence") or []):
            out["citation_not_verifiable"].append(b["id"])
    return {m: v for m, v in out.items() if v}


def _yaml(p):
    import yaml
    try:
        return yaml.safe_load(open(p)) if os.path.isfile(p) else None
    except (OSError, yaml.YAMLError):
        return None


def score_question(run_dir) -> dict:
    t = _trial(run_dir)
    out = {"status": t.get("status", "not_started"), "engine": t.get("engine_ref"), "rerun_of": t.get("rerun_of"),
           "seed": {k: (t.get("seed") or {}).get(k) for k in ("sessions", "repairs", "usd", "status")}}
    seed, hy, wm = (_yaml(os.path.join(run_dir, p)) for p in (SEED, "analysis/hypotheses.yaml",
                                                              "analysis/world_model.yaml"))
    if not (seed and hy and wm):
        return out
    hs = hy["hypotheses"]
    st = {}
    for h in hs:
        st[h["status"]] = st.get(h["status"], 0) + 1
    by = {b["id"]: b for b in seed["beliefs"]}
    conf = {c: {"supported": 0, "refuted": 0, "other": 0} for c in ("low", "medium", "high")}
    for h in hs:
        if (h.get("confrontation") or {}).get("post_hoc") or (h.get("confrontation") or {}).get("circular"):
            continue
        c = by[h["belief"]]["confidence"]
        conf[c][h["status"] if h["status"] in ("supported", "refuted") else "other"] += 1
    for c in conf.values():
        n = c["supported"] + c["refuted"]
        c["share_confirmed"] = round(c["supported"] / n, 3) if n else None
    qs = {q.get("id"): q for q in wm.get("open_questions") or []}
    contra = [{"hypothesis": h["id"], "belief": h["belief"], "claim": h.get("claim"),
               "open_question": (qs.get(f"Q.{h['id']}") or {}).get("question")} for h in hs if h["status"] == "refuted"]
    conflicts = [{"belief": b["id"], "with": [c.get("belief") for c in b.get("conflicts") or []]}
                 for b in wm["beliefs"] if b.get("conflicts") and not str(b["id"]).startswith("sim.")]
    wl = (wm.get("where_to_look") or [None])[0]
    wb = {b["id"]: b for b in wm["beliefs"]}.get((wl or {}).get("belief")) or {}
    out.update(hypotheses={"written": len(hs), "testable": sum(h["status"] != "untestable" for h in hs),
                           "by_status": st, "circular": sum(bool((h.get("confrontation") or {}).get("circular")) for h in hs),
                           "post_hoc": sum(bool((h.get("confrontation") or {}).get("post_hoc"))
                                                            for h in hs)},
               confidence=conf, contradictions=contra, conflicts=conflicts,
               where_to_look={"top": wl, "decision_relevant": bool(wb.get("decision_relevant"))},
               failure_modes=failure_modes(hs, seed, wm, t.get("context")),
               seed_refusals=(t.get("seed") or {}).get("refusals") or [],
               track_record={k: (wm.get("track_record") or {}).get(k) for k in ("confirmed", "contradicted",
                                                                                 "inconclusive", "untestable",
                                                                                 "share_confirmed")})
    return out


def score(trials_root) -> dict:
    """Per question and overall (module docstring); writes trials/summary.yaml and trials/summary.md."""
    import yaml
    runs = os.path.join(trials_root, "runs")
    ids = sorted(x for x in os.listdir(runs) if os.path.isdir(os.path.join(runs, x))) if os.path.isdir(runs) else []
    Q = {i: score_question(os.path.join(runs, i)) for i in ids}
    spend = {}
    if os.path.isfile(os.path.join(trials_root, "spend.jsonl")):
        L = SpendLedger(os.path.join(trials_root, "spend.jsonl"), 1e12)      # read only: the cap is not used
        spend = L.by_question()
        for i, q in Q.items():
            q["spend_usd"] = spend.get(i, {"llm_usd": 0.0, "compute_usd": 0.0})
    tot = {"written": 0, "testable": 0, "circular": 0, "post_hoc": 0, "by_status": {}}
    conf = {c: {"supported": 0, "refuted": 0, "other": 0} for c in ("low", "medium", "high")}
    fm, wtl = {}, {"decision_relevant": 0, "questions": 0}
    sup = {q["rerun_of"]: i for i, q in Q.items() if q.get("rerun_of") and q["status"] == "complete"}
    for i, q in Q.items():
        if i in sup:                    # an earlier run of a question rerun under newer rules: listed, not scored
            q["superseded_by"] = sup[i]
            continue
        h = q.get("hypotheses")
        if not h:
            continue
        for k in ("written", "testable", "circular", "post_hoc"):
            tot[k] += h[k]
        for s, n in h["by_status"].items():
            tot["by_status"][s] = tot["by_status"].get(s, 0) + n
        for c, v in q["confidence"].items():
            for k in ("supported", "refuted", "other"):
                conf[c][k] += v[k]
        for m, v in q["failure_modes"].items():
            fm.setdefault(m, {"count": 0, "questions": []})
            fm[m]["count"] += len(v)
            fm[m]["questions"].append(i)
        wtl["questions"] += q["where_to_look"]["top"] is not None
        wtl["decision_relevant"] += q["where_to_look"]["decision_relevant"]
    for c in conf.values():
        n = c["supported"] + c["refuted"]
        c["share_confirmed"] = round(c["supported"] / n, 3) if n else None
    statuses = {}
    for q in Q.values():
        statuses[q["status"]] = statuses.get(q["status"], 0) + 1
    S = {"schema": "trials.world_model/0", "producer": "scripts/world_model_trials.py", "written_at": _now(),
         "scoring": dict(SCORING, note="review heuristics of this scoring, not world-model rules; verdicts are the "
                                       "build's own"),
         "overall": {"questions": statuses, "hypotheses": tot, "confidence": conf, "failure_modes": fm,
                     "where_to_look": wtl,
                     "spend_usd": {"llm": round(sum(v["llm_usd"] for v in spend.values()), 4),
                                   "compute": round(sum(v["compute_usd"] for v in spend.values()), 4)}},
         "questions": Q}
    _sio().write_nofollow(trials_root, "summary.yaml", yaml.safe_dump(S, sort_keys=False, width=110).encode(),
                          atomic=True)
    _sio().write_nofollow(trials_root, "summary.md", summary_md(S).encode(), atomic=True)
    return S


def summary_md(S) -> str:
    o = S["overall"]
    L = ["# World-model trials: summary", "",
         f"Questions by status: {', '.join(f'{k} {v}' for k, v in sorted(o['questions'].items()))}. "
         f"Spend: LLM ${o['spend_usd']['llm']:.2f}, compute ${o['spend_usd']['compute']:.2f}.", "",
         "Verdicts are the world model build's own (toolkit/analysis/world_model.py). Post hoc means not anchored "
         "in remote git before the evidence existed; post-hoc results are not in the confirmation shares. "
         "Circular means the verdict is fixed by a spec input (analysis/circular.json): reported, never a confirmation, never moving confidence. "
         f"Circular tests: {o['hypotheses']['circular']} of {o['hypotheses']['written']}.", "",
         "## Per question", "",
         "| Question | Engine | Status | Written | Testable | Supported | Refuted | Inconclusive | Untestable | "
         "Circular | Post hoc | Repairs | Spend (USD) |", "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, q in S["questions"].items():
        h = q.get("hypotheses") or {}
        b = h.get("by_status") or {}
        sp = q.get("spend_usd") or {}
        st = q["status"] + (f" (superseded by {q['superseded_by']})" if q.get("superseded_by") else "")
        L.append(f"| {i} | {str(q.get('engine') or '').split(':')[-1]} | {st} | {h.get('written', '')} | "
                 f"{h.get('testable', '')} | {b.get('supported', 0) if h else ''} | {b.get('refuted', 0) if h else ''}"
                 f" | {b.get('inconclusive', 0) if h else ''} | {b.get('untestable', 0) if h else ''} | "
                 f"{h.get('circular', '')} | {h.get('post_hoc', '')} | {(q.get('seed') or {}).get('repairs') or 0} | "
                 f"{sp.get('llm_usd', 0) + sp.get('compute_usd', 0):.2f} |")
    L += ["", "## Confirmation by the seed's confidence (pre-registered only)", "",
          "| Confidence | Supported | Refuted | Other | Share confirmed |", "|---|---|---|---|---|"]
    for c, v in o["confidence"].items():
        L.append(f"| {c} | {v['supported']} | {v['refuted']} | {v['other']} | "
                 f"{'' if v['share_confirmed'] is None else v['share_confirmed']} |")
    L += ["", "## Failure modes", "", "| Mode | Count | Questions |", "|---|---|---|"]
    for m, v in sorted(o["failure_modes"].items(), key=lambda kv: -kv[1]["count"]):
        L.append(f"| {m} | {v['count']} | {', '.join(v['questions'])} |")
    w = o["where_to_look"]
    L += ["", f"where_to_look's top item concerns a belief the seed marked decision-relevant in "
              f"{w['decision_relevant']} of {w['questions']} questions.", "", "## Contradictions", ""]
    for i, q in S["questions"].items():
        for c in q.get("contradictions") or []:
            L.append(f"- {i} {c['hypothesis']} ({c['claim']}): open question recorded: "
                     f"{'yes' if c['open_question'] else 'no'}")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- CLI
def sdk_seed_fn():
    """The real seed task: one bounded SDK session per call (agents/v2_sdk_tasks.py), capped at the reserved USD."""
    from agents.v2_sdk_tasks import SdkTasks

    def fn(run_dir, ctx, feedback, cap_usd):
        t = SdkTasks(run_dir, task_cap_usd=cap_usd)
        t.set_allowance(cap_usd)
        return t.seed_world_model(ctx, feedback)
    return fn


def check_templates(qs, out_root, max_engine_runs=50) -> dict:
    """Each template as the run will use it (prepare, the engine step, world_model.load_sweep), with no LLM call and
    no push: a question that fails here fails before any spend."""
    from toolkit.analysis import world_model as W
    out, stamp = {}, dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    for q in qs:
        rd = os.path.join(out_root, f"{q['id']}-{stamp}")          # a fresh directory per check: nothing is removed
        try:
            ctx = prepare(q, rd, REPO, max_engine_runs)
            e = engine_step(q, rd, max_engine_runs)
            sw, _ = W.load_sweep(rd, "current", q.get("shadow_price"), use_plan=False)
            out[q["id"]] = {"ok": True, "jobs": e["jobs"], "wall_s": e["wall_s"], "features": list(ctx["sweep"][
                "features"]), "packages": list(sw.packages)}
        except Exception as e:  # noqa: BLE001
            out[q["id"]] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
        print(json.dumps({q["id"]: out[q["id"]]}, default=str))
    return out


def rebuild(trials_root, qs, anchor_ref=None) -> dict:
    """The circular classification and the build again for each completed question (after a scoring or flag fix): no
    LLM call, no engine run, no new registration. The batch, the ledger and its anchor are as they were, so verdicts
    and registration status are re-derived, not changed. A question that is not complete is skipped."""
    out = {}
    for q in qs:
        rd = os.path.join(trials_root, "runs", q["id"])
        t = _trial(rd)
        if t.get("status") != "complete":
            out[q["id"]] = {"status": "skipped", "why": f"status {t.get('status', 'not started')}"}
            continue
        try:
            t["build"] = build_step(q, rd, anchor_ref if t.get("anchor") else None)
            t["steps"]["rebuild"] = _now()
            out[q["id"]] = {"status": "rebuilt", "statuses": t["build"]["statuses"], "circular": t["build"]["circular"]}
        except Exception as e:  # noqa: BLE001
            out[q["id"]] = {"status": "failed", "error": f"{type(e).__name__}: {e}"[:300]}
            t["rebuild_error"] = out[q["id"]]["error"]
        _save(rd, t)
    return out


def main(argv=None) -> int:
    import argparse
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--self-test"]:
        return _self_test()
    ap = argparse.ArgumentParser(prog="world_model_trials.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "check-templates", "rebuild", "score"):
        p = sub.add_parser(name)
        p.add_argument("--trials-dir", default=os.path.join(REPO, "trials"))
        if name != "score":
            p.add_argument("--questions", default=QUESTIONS)
            p.add_argument("--only", help="comma-separated question ids")
            p.add_argument("--batch", help="only questions with this batch value")
            p.add_argument("--limit", type=int)
            p.add_argument("--max-engine-runs", type=int, default=50)
    r = sub.choices["run"]
    r.add_argument("--total-cap-usd", type=float, required=True, help="the cap on cumulative spend in the ledger")
    r.add_argument("--task-cap-usd", type=float, default=2.0)
    r.add_argument("--llm-concurrency", type=int, default=5)
    r.add_argument("--push", action="store_true", help="anchor registrations on origin/wm-trials (else post hoc)")
    a = ap.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    if a.cmd == "score":
        S = score(a.trials_dir)
        print(json.dumps(S["overall"], indent=1, default=str))
        return 0
    qs = select(load_questions(a.questions), set(a.only.split(",")) if a.only else None, a.batch, a.limit)
    if a.cmd == "rebuild":
        res = rebuild(a.trials_dir, qs, f"refs/remotes/{REMOTE}/{BRANCH}")
        for i, r in res.items():
            print(json.dumps({i: r}, default=str))
        score(a.trials_dir)
        return 0 if all(r["status"] != "failed" for r in res.values()) else 1
    if a.cmd == "check-templates":
        res = check_templates(qs, os.path.join(a.trials_dir, "checks"), a.max_engine_runs)
        return 0 if all(v["ok"] for v in res.values()) else 1
    led = SpendLedger(os.path.join(a.trials_dir, "spend.jsonl"), a.total_cap_usd)
    anchor = GitAnchor(REPO, os.path.join(a.trials_dir, ".wm-trials"),
                       trials_rel=os.path.relpath(os.path.realpath(a.trials_dir), os.path.realpath(REPO))) \
        if a.push else None
    res = run_trials(qs, a.trials_dir, REPO, sdk_seed_fn(), led, anchor, a.task_cap_usd, a.llm_concurrency, 2,
                     a.max_engine_runs)
    for i, t in res.items():
        print(json.dumps({"id": i, "status": t.get("status"), "spend_usd": t.get("spend_usd"),
                          "error": t.get("error")}, default=str))
    print(json.dumps({"committed_usd": round(led.committed(), 4), "total_cap_usd": led.cap}))
    S = score(a.trials_dir)
    print(json.dumps(S["overall"]["hypotheses"], default=str))
    return 0 if all(t.get("status") == "complete" for t in res.values()) else 1


# ---------------------------------------------------------------- self-test
def _toy_template(d, points=12):
    """A trial template on the toy engine (the stub engine of these tests): the analysis self-tests' toy spec."""
    import yaml
    from toolkit.spec import schema
    os.makedirs(d, exist_ok=True)
    p = schema.toy_spec(d)
    raw = yaml.safe_load(open(p))
    raw["analysis"]["emulator"]["points"] = points
    with open(p, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    return d


def _toy_seed(bad=None):
    """A valid seed on the toy sweep's names (packages none, a, b, a+b; features 'x (calibration scale)', eff_a,
    eff_b); `bad` plants a defect: 'schema' (a test without its threshold) or 'name' (a feature the sweep lacks)."""
    lit = [{"citation": "Toy et al. (2020) J Toy 1:1", "location": "Table 2"}]
    sd = {"beliefs": [
        {"id": "B1", "mechanism": "a reduces the outcome", "about": {"package": "a"}, "direction": "reduces",
         "magnitude": {"band": [0.2, 0.8], "scale": "relative_reduction"}, "source_type": "literature",
         "evidence": lit, "confidence": "high", "decision_relevant": True},
        {"id": "B2", "mechanism": "b reduces the outcome less than a", "about": {"package": "b"},
         "direction": "reduces", "magnitude": {"band": [0.1, 0.5], "scale": "relative_reduction"},
         "source_type": "literature", "evidence": lit, "confidence": "low"},
        {"id": "B3", "mechanism": "a's effect grows with its efficacy", "about": {"package": "a", "feature": "eff_a"},
         "direction": "increases", "source_type": "literature", "evidence": lit, "confidence": "medium"},
        {"id": "B4", "mechanism": "a beats b at the shadow price", "about": {"pair": ["a", "b"]},
         "direction": "flips", "source_type": "literature", "evidence": lit, "confidence": "low",
         "decision_relevant": True}],
        "open_questions": [{"id": "Q1", "question": "does b's effect depend on x?", "beliefs": ["B2"],
                            "decision_relevant": True}],
        "hypotheses": [
            {"id": "H1", "belief": "B1", "claim": "a reduces by 20-80%",
             "test": {"kind": "effect_band", "package": "a", "band": [0.2, 0.8], "min_share": 0.7}},
            {"id": "H2", "belief": "B2", "claim": "b reduces by 10-50%",
             "test": {"kind": "effect_band", "package": "b", "band": [0.1, 0.5], "min_share": 0.7}},
            {"id": "H3", "belief": "B3", "claim": "a's reduction increases with eff_a",
             "test": {"kind": "effect_direction", "package": "a", "feature": "eff_a", "direction": "increases",
                      "scope": "marginal"}},
            {"id": "H4", "belief": "B4", "claim": "a beats b across x",
             "test": {"kind": "paired_sign", "a": "a", "b": "b", "feature": "x (calibration scale)",
                      "range": [-2.0, 2.0], "sign": "positive", "min_share": 0.7}},
            {"id": "H5", "belief": "B1", "claim": "a reduces by at least a tenth almost everywhere",
             "test": {"kind": "effect_band", "package": "a", "band": [0.1, 1.0], "min_share": 0.9}}]}
    if bad == "schema":
        del sd["hypotheses"][0]["test"]["min_share"]
    if bad == "name":
        sd["hypotheses"][2]["test"]["feature"] = "no_such_feature"
        sd["beliefs"][2]["about"]["feature"] = "no_such_feature"
    return sd


def _git(*a, cwd):
    import subprocess
    r = subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(a)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _toy_repo(d):
    """A repository with a bare origin (no network): trials/ ignored on its main branch, as in the real repo."""
    origin, repo = os.path.join(d, "origin.git"), os.path.join(d, "repo")
    os.makedirs(repo)
    _git("init", "--bare", "-q", origin, cwd=d)
    _git("init", "-q", "-b", "main", cwd=repo)
    for k, v in (("user.name", "Test"), ("user.email", "test@example.org"), ("commit.gpgsign", "false")):
        _git("config", k, v, cwd=repo)
    with open(os.path.join(repo, ".gitignore"), "w") as f:
        f.write("trials/\n")
    _git("add", ".gitignore", cwd=repo)
    _git("commit", "-q", "-m", "init", cwd=repo)
    _git("remote", "add", "origin", origin, cwd=repo)
    _git("push", "-q", "origin", "main", cwd=repo)
    return repo


def _self_test() -> int:
    import tempfile
    import threading
    import time
    import yaml
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    fails = []

    def ok(c, m):
        print(("PASS " if c else "FAIL ") + m)
        if not c:
            fails.append(m)

    def tryit(name, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- the failure is the finding
            ok(False, f"{name}: {type(e).__name__}: {e}")

    toy_ref = "toolkit.engines.contract:ToyEngine"

    def wt1():
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "spend.jsonl")
            L = SpendLedger(p, 5.0)
            r1 = L.reserve("q1", "seed", 2.0)
            L.charge(r1, 0.75, "sdk reported")
            r2 = L.reserve("q2", "seed", 2.0)              # outstanding: counted at its whole cap
            ok(r1 and r2 and abs(L.committed() - 2.75) < 1e-9 and abs(L.spent() - 0.75) < 1e-9,
               f"WT1 an outstanding reservation counts at its cap, a charged one at its charge: committed "
               f"{L.committed()}, spent {L.spent()}")
            L2 = SpendLedger(p, 5.0)                       # a later invocation reads the same ledger
            ok(L2.reserve("q3", "seed", 2.5) is None and L2.reserve("q3", "seed", 2.0) is not None,
               "WT1b the cap holds across invocations: a task whose cap would take committed spend past the total is "
               "refused, one that fits is reserved")
            lines = open(p).read().splitlines()
            bad = json.loads(lines[1])
            bad["usd"] = 0.01
            lines[1] = json.dumps(bad, sort_keys=True)
            with open(p, "w") as f:
                f.write("\n".join(lines) + "\n")
            try:
                SpendLedger(p, 5.0).committed()
                ok(False, "WT1c an edited spend ledger line is refused (append-only chain)")
            except TrialError:
                ok(True, "WT1c an edited spend ledger line is refused (append-only chain)")
            try:
                SpendLedger(os.path.join(d, "x.jsonl"), 0)
                ok(False, "WT1d a total cap that is not a positive number is refused")
            except TrialError:
                ok(True, "WT1d a total cap that is not a positive number is refused")
    tryit("WT1 spend ledger", wt1)

    def wt2():
        with tempfile.TemporaryDirectory() as d:
            _toy_template(os.path.join(d, "tpl", "toy"))
            qp = os.path.join(d, "q.yaml")
            doc = {"questions": [{"id": "toy1", "question": "Which package per unit?", "engine": toy_ref,
                                  "template": "tpl/toy", "shadow_price": 0.1, "inputs": "synthetic"}]}
            yaml.safe_dump(doc, open(qp, "w"))
            got = load_questions(qp, root=d, engines={toy_ref})
            ok([q["id"] for q in got] == ["toy1"], f"WT2 a questions file loads: {[q['id'] for q in got]}")
            for mut, why in ((lambda q: q.update(engine="toolkit.engines.malaria.engine:MalariaEngine"),
                              "an engine that is not one of the cheap engines"),
                             (lambda q: q.update(template="tpl/none"), "a missing template"),
                             (lambda q: q.pop("question"), "no question text"),
                             (lambda q: q.update(id="a b/c"), "an id that is not a plain name")):
                bad = json.loads(json.dumps(doc))
                mut(bad["questions"][0])
                yaml.safe_dump(bad, open(qp, "w"))
                try:
                    load_questions(qp, root=d, engines={toy_ref})
                    ok(False, f"WT2b refused: {why}")
                except TrialError:
                    ok(True, f"WT2b refused: {why}")
            dup = {"questions": doc["questions"] * 2}
            yaml.safe_dump(dup, open(qp, "w"))
            try:
                load_questions(qp, root=d, engines={toy_ref})
                ok(False, "WT2b refused: a duplicate id")
            except TrialError:
                ok(True, "WT2b refused: a duplicate id")
        real = load_questions(os.path.join(REPO, "scripts", "world_model_trials_questions.yaml"))
        evals = [q for q in real if q.get("eval_question") and not q.get("rerun_of")]
        ok(len(real) >= 10 and len(evals) == 5 and len({q["engine"] for q in real}) >= 3,
           f"WT2c the shipped questions file: {len(real)} questions, {len(evals)} from the eval suite, engines "
           f"{sorted({q['engine'].split(':')[-1] for q in real})}")
    tryit("WT2 questions", wt2)

    def e2e(d, seeds, cap=20.0, task_cap=2.0, n=1, push=True):
        """One trials run on toy questions: (repo, trials root, results, calls, ledger)."""
        repo = _toy_repo(d)
        for i in range(n):
            _toy_template(os.path.join(repo, "data", "tpl", f"toy{i}"))
        qs = [{"id": f"toy{i}", "question": "Which package per unit?", "engine": toy_ref,
               "template": f"data/tpl/toy{i}", "shadow_price": 0.1, "inputs": "synthetic"} for i in range(n)]
        calls, live = [], {"llm": 0, "llm_max": 0, "eng": 0, "eng_max": 0}
        lock = threading.Lock()

        def seed_fn(run_dir, ctx, feedback, cap_usd):
            from agents.v2_tasks import TaskResult
            with lock:
                live["llm"] += 1
                live["llm_max"] = max(live["llm_max"], live["llm"])
            try:
                calls.append({"run": os.path.basename(run_dir), "feedback": feedback, "cap": cap_usd,
                              "ctx": ctx})
                k = sum(c["run"] == os.path.basename(run_dir) for c in calls)
                sd = seeds(os.path.basename(run_dir), k)
                with open(os.path.join(run_dir, "world_model_seed.yaml"), "w") as f:
                    yaml.safe_dump(sd, f, sort_keys=False)
                time.sleep(0.05)
                return TaskResult(True, {"ok": True}, 0.25, "stub")
            finally:
                with lock:
                    live["llm"] -= 1

        def engine_probe(phase):
            with lock:
                live["eng"] += 1 if phase == "start" else -1
                live["eng_max"] = max(live["eng_max"], live["eng"])
        troot = os.path.join(repo, "trials")
        L = SpendLedger(os.path.join(troot, "spend.jsonl"), cap)
        anchor = GitAnchor(repo, os.path.join(troot, ".wm-trials")) if push else None
        res = run_trials(qs, troot, repo=repo, seed_fn=seed_fn, ledger=L, anchor=anchor, task_cap_usd=task_cap,
                         llm_concurrency=2, engine_workers=2, max_engine_runs=50, engines={toy_ref},
                         engine_probe=engine_probe, citation_lookup=lambda query: [])     # no network in self-tests
        return repo, troot, res, calls, L, live

    def wt3():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, live = e2e(d, lambda q, k: _toy_seed())
            r = res["toy0"]
            rd = os.path.join(troot, "runs", "toy0")
            hy = yaml.safe_load(open(os.path.join(rd, "analysis", "hypotheses.yaml")))
            regs = {h["id"]: h["confrontation"]["registration_status"] for h in hy["hypotheses"]}
            ok(r["status"] == "complete" and set(regs.values()) == {"remote-anchored"},
               f"WT3 one question end to end (stub seed, toy engine, local bare origin): {r['status']}, every "
               f"hypothesis remote-anchored before its evidence: {regs}")
            tree = _git("ls-tree", "-r", "--name-only", "refs/remotes/origin/wm-trials", cwd=repo).splitlines()
            ok(sorted(tree) == ["trials/runs/toy0/analysis/hypotheses_registry.jsonl",
                                "trials/runs/toy0/analysis/world_model_seed.yaml"],
               f"WT3b the wm-trials branch holds the ledger and the seed only, never run data: {tree}")
            untracked = [x[3:] for x in _git("status", "--porcelain", cwd=repo).splitlines()]
            ok(untracked == ["data/"] and _git("status", "--porcelain", "--untracked-files=no", cwd=repo) == "" and
               _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo) == "main",
               "WT3c the main working tree is untouched (the wm-trials branch lives in its own worktree)")
            ctx = calls[0]["ctx"]
            from toolkit.analysis import world_model as W
            sw, _ = W.load_sweep(rd, "current", 0.1, use_plan=False)
            ok(sorted(ctx["sweep"]["features"]) == sorted(sw.features) and ctx["packages"] == sw.packages,
               f"WT3d the seed task is told the sweep's own feature and package names (world_model's, from the "
               f"design, before any result): {sorted(ctx['sweep']['features'])}")
            ok(not any("result" in k.lower() for k in json.dumps(ctx).split('"') if k.endswith(".jsonl")),
               "WT3e the seed context names no results file")
            first = [json.loads(x) for x in open(os.path.join(rd, "analysis", "batch", "first_seen.jsonl"))]
            ok(len(first) == 1 and r["spend_usd"] == 0.25 and abs(L.spent() - 0.25) < 1e-9,
               f"WT3f the batch's first-seen time is recorded once by the driver, and spend is per question "
               f"({r['spend_usd']}) and in the ledger ({L.spent()})")
            wm = yaml.safe_load(open(os.path.join(rd, "analysis", "world_model.yaml")))
            ok(wm["track_record"]["registry"]["anchors"] and r["engine"]["jobs"] == 48,
               f"WT3g the build reads the remote anchor; the sweep ran {r['engine']['jobs']} engine jobs")
    tryit("WT3 end to end", wt3)

    def wt4():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed("schema" if k == 1 else None))
            fb = calls[1]["feedback"] if len(calls) > 1 else None
            ok(len(calls) == 2 and fb and any("min_share" in x for x in fb) and res["toy0"]["status"] == "complete"
               and res["toy0"]["seed"]["repairs"] == 1,
               f"WT4 a seed the world model's validator refuses is repaired once with the validator's errors: "
               f"{fb}")
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed("schema"))
            reg = os.path.join(troot, "runs", "toy0", "analysis", "hypotheses_registry.jsonl")
            ok(len(calls) == 2 and res["toy0"]["status"] == "seed_refused" and not os.path.exists(reg),
               f"WT4b refused twice: the question stops, nothing is registered ({res['toy0']['status']})")
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed("name" if k == 1 else None))
            fb = calls[1]["feedback"] if len(calls) > 1 else []
            ok(len(calls) == 2 and any("no_such_feature" in x for x in fb or []),
               f"WT4c a test naming a feature or package the sweep does not have is repaired: {fb}")
    tryit("WT4 repair", wt4)

    def wt5():
        with tempfile.TemporaryDirectory() as d:
            repo = _toy_repo(d)
            A = GitAnchor(repo, os.path.join(repo, "trials", ".wm-trials"))
            os.makedirs(os.path.join(repo, "trials", "runs", "q1", "analysis", "batch"))
            for rel in ("trials/runs/q1/analysis/batch/results.jsonl", "trials/runs/q1/decision_spec.yaml"):
                with open(os.path.join(repo, rel), "w") as f:
                    f.write("{}\n")
                try:
                    A.publish([rel], "q1")
                    ok(False, f"WT5 publishing {rel} is refused (ledgers and seeds only)")
                except TrialError:
                    ok(True, f"WT5 publishing {rel} is refused (ledgers and seeds only)")
            try:
                A.publish(["trials/runs/q1/analysis/../../../../.gitignore"], "q1")
                ok(False, "WT5b a path that climbs out of the trials tree is refused")
            except TrialError:
                ok(True, "WT5b a path that climbs out of the trials tree is refused")
    tryit("WT5 push scope", wt5)

    def wt6():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, live = e2e(d, lambda q, k: _toy_seed(), n=3)
            ok(all(r["status"] == "complete" for r in res.values()) and live["llm_max"] <= 2 and live["eng_max"] == 1,
               f"WT6 three questions: LLM steps in parallel at most llm_concurrency ({live['llm_max']}), engine "
               f"steps one batch at a time on 2 worker processes ({live['eng_max']})")
            n0 = len(calls)
            L2 = SpendLedger(os.path.join(troot, "spend.jsonl"), 20.0)
            qs = [{"id": f"toy{i}", "question": "Which package per unit?", "engine": toy_ref,
                   "template": f"data/tpl/toy{i}", "shadow_price": 0.1, "inputs": "synthetic"} for i in range(3)]
            res2 = run_trials(qs, troot, repo=repo, seed_fn=lambda *a: (_ for _ in ()).throw(AssertionError("called")),
                              ledger=L2, anchor=GitAnchor(repo, os.path.join(troot, ".wm-trials")),
                              engines={toy_ref})
            ok(len(calls) == n0 and all(r["status"] == "complete" and r.get("skipped") for r in res2.values()),
               "WT6b a second invocation skips completed questions (no LLM call, no engine run)")
    tryit("WT6 concurrency and resume", wt6)

    def wt7():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed(), cap=2.5, task_cap=2.0, n=2, push=False)
            st = sorted(r["status"] for r in res.values())
            ok(st == ["budget_refused", "complete"] and len(calls) == 1,
               f"WT7 with a total cap of $2.50 and a $2 task cap, the second question is not launched: {st}")
            ok(all(h["confrontation"]["registration_status"] == "post hoc" for h in yaml.safe_load(open(os.path.join(
                troot, "runs", [k for k, r in res.items() if r["status"] == "complete"][0], "analysis",
                "hypotheses.yaml")))["hypotheses"]),
               "WT7b without a push the registrations are not anchored: every confrontation is post hoc")
    tryit("WT7 budget stop", wt7)

    def wt8():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed(), n=1)
            S = score(troot)
            q = S["questions"]["toy0"]
            hy = yaml.safe_load(open(os.path.join(troot, "runs", "toy0", "analysis", "hypotheses.yaml")))
            st = {}
            for h in hy["hypotheses"]:
                st[h["status"]] = st.get(h["status"], 0) + 1
            ok(q["hypotheses"]["written"] == 5 and q["hypotheses"]["by_status"] == st
               and q["hypotheses"]["post_hoc"] == 0,
               f"WT8 the score reads the build's own statuses: {q['hypotheses']}")
            ok(set(S["overall"]) >= {"hypotheses", "confidence", "failure_modes", "where_to_look", "spend_usd"}
               and os.path.isfile(os.path.join(troot, "summary.yaml"))
               and os.path.isfile(os.path.join(troot, "summary.md")),
               "WT8b overall scores and the failure-mode taxonomy are written to trials/summary.yaml and summary.md")
            ok(isinstance(q["where_to_look"].get("decision_relevant"), bool),
               f"WT8c where_to_look's top item is classified as decision-relevant or not: {q['where_to_look']}")
        # planted outcomes: a lenient band, a high-confidence refutation and an untestable test are classified
        fake = {"hypotheses": [
            {"id": "H1", "belief": "B1", "status": "supported", "test": {"kind": "effect_band", "package": "a",
                                                                          "band": [0.0, 0.95], "min_share": 0.55},
             "result": {"observed": {"coverage": 1.0}}, "confrontation": {"post_hoc": False}},
            {"id": "H2", "belief": "B2", "status": "refuted", "test": {"kind": "effect_band", "package": "b",
                                                                        "band": [0.4, 0.5], "min_share": 0.8},
             "result": {"observed": {"coverage": 0.0}}, "confrontation": {"post_hoc": False}},
            {"id": "H3", "belief": "B1", "status": "untestable", "test": {"kind": "plan_share", "package": "a",
                                                                           "where": {"x": [0, 1]}, "min_share": 0.5},
             "result": {"observed": {"reason": "no optimised plan (analysis/point.yaml)"}},
             "confrontation": {"post_hoc": False}}]}
        seed = {"beliefs": [{"id": "B1", "confidence": "low", "source_type": "literature",
                             "evidence": [{"citation": "Someone", "location": "p. 3"}]},
                            {"id": "B2", "confidence": "high", "source_type": "literature",
                             "evidence": [{"citation": "Doe J et al. Lancet 2019;393:1-9", "location": "Table 2"}]}],
                "hypotheses": fake["hypotheses"]}
        fm = failure_modes(fake["hypotheses"], seed, {"beliefs": seed["beliefs"], "open_questions": []})
        ok(fm.get("test_too_lenient") == ["H1"] and fm.get("untestable") == ["H3"]
           and fm.get("high_confidence_refuted") == ["H2"] and fm.get("citation_not_verifiable") == ["B1"]
           and fm.get("contradiction_without_open_question") == ["H2"],
           f"WT8d the failure-mode taxonomy classifies planted outcomes: {fm}")
    tryit("WT8 scoring", wt8)

    def wt9():
        import pipeline as P
        src = open(os.path.abspath(__file__)).read()
        src = src[:src.index("# ---------------------------------------------------------------- self-test")]
        ok(not P.domain_terms(src), f"WT9 the driver names no disease, place or intervention (pipeline.domain_terms, "
                                    f"the repository's own check): {P.domain_terms(src)}")
    tryit("WT9 disease-agnostic", wt9)

    # Owner, 2026-11-13 (after the tb_acf smoke): the world model is built from literature and reasoning, then
    # confronted with the simulation, never with its own inputs.
    def wt10():
        import csv
        from agents.v2_sdk_tasks import _seed_read_denied
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed())
            ctx = calls[0]["ctx"]
            rd = os.path.join(troot, "runs", "toy0")
            par = {n: f for n, f in ctx["sweep"]["features"].items() if f["kind"] == "parameter"}
            ok(par and all("range" not in f for f in par.values()) and "notes" not in ctx["engine"]
               and not any(NUMBER_RE.search(m) for m in ctx["engine"].get("mechanisms") or []),
               f"WT10 the seed context withholds the engine's effect and prior values: no parameter range "
               f"({par}), no card notes, mechanisms in words")
            vals = set()
            for row in csv.DictReader(open(os.path.join(rd, "units.csv"))):
                vals |= {v for k, v in row.items() if k not in ("id",) and re.search(r"\d\.\d", v or "")}
            blob = json.dumps(ctx)
            leaked = sorted(v for v in vals if v in blob)
            ok(not leaked and ctx.get("datasets") and all("columns" in x and "rows" not in x for x in ctx["datasets"]),
               f"WT10b no input data value is in the seed context (datasets by file and column only): leaked "
               f"{leaked[:5]}, datasets {ctx.get('datasets')}")
            got = {p: _seed_read_denied("Read", {"file_path": p}, rd) for p in
                   ("decision_spec.yaml", "units.csv", "_task_input.json", "world_model_seed.yaml",
                    os.path.join(REPO, "data", "world_model_trials", "tb_acf", "decision_spec.yaml"),
                    os.path.join(REPO, "toolkit", "analysis", "world_model.py"))}
            want = {"decision_spec.yaml": True, "units.csv": True, "_task_input.json": False,
                    "world_model_seed.yaml": False,
                    os.path.join(REPO, "data", "world_model_trials", "tb_acf", "decision_spec.yaml"): True,
                    os.path.join(REPO, "toolkit", "analysis", "world_model.py"): False}
            ok({k: bool(v) for k, v in got.items()} == want,
               f"WT10c the seed task can read only its input and its own seed in the run, and no template or input "
               f"data in the repository: {got}")
    tryit("WT10 inputs withheld", wt10)

    def wt11():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed())
            rd = os.path.join(troot, "runs", "toy0")
            hy = yaml.safe_load(open(os.path.join(rd, "analysis", "hypotheses.yaml")))
            wm = yaml.safe_load(open(os.path.join(rd, "analysis", "world_model.yaml")))
            circ = {h["id"] for h in hy["hypotheses"] if (h.get("confrontation") or {}).get("circular")}
            # the toy engine's package a reduces by exactly eff_a and b by eff_b (spec inputs): H1, H2, H5 are bands on
            # a declared effect whose range lies inside the band; H3 tests a's effect against its own parameter
            ok(circ == {"H1", "H2", "H3", "H5"},
               f"WT11 circular tests are classified from the spec's inputs and the engine's own sweep: {sorted(circ)}")
            sup = [h["id"] for h in hy["hypotheses"] if h["status"] == "supported" and h["id"] not in circ]
            tr = wm["track_record"]
            ok(tr["confirmed"] == len(sup) and tr.get("circular", {}).get("confirmed", 0) >= 1,
               f"WT11b circular verdicts are reported apart and never counted as confirmations: confirmed "
               f"{tr['confirmed']} (non-circular supported {sup}), circular {tr.get('circular')}")
            b2 = [b for b in wm["beliefs"] if b["id"] == "B2"][0]
            ok(b2["confidence"] == "low" and "confidence_seed" not in b2 and not b2.get("confirmed_by"),
               f"WT11c a circular confirmation does not move confidence: B2 {b2['confidence']}, "
               f"{b2.get('confirmed_by')}")
            S = score(troot)
            q = S["questions"]["toy0"]
            ok(q["hypotheses"]["circular"] == 4 and sorted(q["failure_modes"].get("circular") or []) == sorted(circ)
               and S["overall"]["hypotheses"]["circular"] == 4,
               f"WT11d the summary counts circular tests ({q['hypotheses']}) and lists them as a failure mode")
            dec = [h["id"] for h in hy["hypotheses"] if h["id"] not in circ and h["status"] in ("supported", "refuted")]
            ok(sum(v["supported"] + v["refuted"] for v in q["confidence"].values()) == len(dec),
               f"WT11e the confirmation-by-confidence table leaves circular verdicts out: {q['confidence']}")
    tryit("WT11 circular", wt11)

    def wt12():
        def seeds(cite):
            def f(q, k):
                sd = _toy_seed()
                sd["beliefs"].append({"id": "B5", "mechanism": "the units differ in size", "about": {"feature": "pop"},
                                      "direction": "none", "source_type": "data", "confidence": "medium",
                                      "evidence": [{"dossier_entry": cite if k == 1 else "units.csv column pop"}]})
                return sd
            return f
        for cite, why in (("_task_input.json engine.notes", "the task's own input file"),
                          ("analysis/batch/design.json", "a harness artifact"),
                          ("my reading of the question", "no dataset of the run")):
            with tempfile.TemporaryDirectory() as d:
                repo, troot, res, calls, L, _ = e2e(d, seeds(cite))
                fb = calls[1]["feedback"] if len(calls) > 1 else []
                ok(len(calls) == 2 and any("B5" in x for x in fb or []) and res["toy0"]["status"] == "complete",
                   f"WT12 a data belief citing {why} is refused and repaired (then a dataset of the run is accepted): "
                   f"{fb}")
    tryit("WT12 data evidence", wt12)

    # From the first 10 (2026-11-13): an own effect parameter in adjust_for or a region is not a test against it; a
    # literature belief citing a harness file is refused; a section name (Results, Abstract) is a location; rebuild.
    def wt13():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed())
            rd = os.path.join(troot, "runs", "toy0")
            sd = _toy_seed()
            sd["hypotheses"] = [
                {"id": "A1", "belief": "B3", "claim": "x adjusted for eff_a",
                 "test": {"kind": "effect_direction", "package": "a", "feature": "eff_a", "direction": "increases",
                          "adjust_for": ["x (calibration scale)"]}},
                {"id": "A2", "belief": "B2", "claim": "b's band where eff_b is high",
                 "test": {"kind": "effect_band", "package": "b", "band": [0.0, 0.05], "min_share": 0.7,
                          "where": {"eff_b": [0.0, 1.0]}}}]
            sd["beliefs"].append({"id": "B6", "mechanism": "x does not change a's effect, given eff_a",
                                  "about": {"package": "a", "feature": "x (calibration scale)"},
                                  "direction": "negligible", "source_type": "literature", "confidence": "low",
                                  "evidence": [{"citation": "Toy (2020) J Toy 1:1", "location": "Table 1"}]})
            sd["hypotheses"].append({"id": "A3", "belief": "B6", "claim": "x is negligible for a, adjusting eff_a",
                                     "test": {"kind": "effect_direction", "package": "a",
                                              "feature": "x (calibration scale)", "direction": "negligible",
                                              "equivalence": 0.2, "adjust_for": ["eff_a"]}})
            got = circular_tests(rd, sd, {"shadow_price": 0.1})
            ok(set(got) == {"A1"}, f"WT13 only a test against its own effect parameter is circular, not one that "
                                   f"adjusts for it or restricts its region to it (or a band its range is not inside): "
                                   f"{sorted(got)}")
            ctx = calls[0]["ctx"]
            bad = _toy_seed()
            bad["beliefs"][0]["evidence"] = [{"citation": "_task_input.json engine card (2026)", "location": "p. 1"}]
            ok(any("B1" in x and "_task_input.json" in x for x in seed_problems(bad, ctx)),
               "WT13b a literature belief citing a file the harness writes is refused")
            fm = failure_modes([], {"beliefs": [
                {"id": "L1", "source_type": "literature", "evidence": [{"citation": "Doe J. NEJM 2019", "location":
                                                                         "Results, primary outcome"}]},
                {"id": "L2", "source_type": "literature", "evidence": [{"citation": "Doe J. NEJM 2019", "location":
                                                                         "Abstract"}]},
                {"id": "L3", "source_type": "literature", "evidence": [{"citation": "Doe J. NEJM 2019", "location":
                                                                         "somewhere"}]}]}, {})
            ok(fm.get("citation_not_verifiable") == ["L3"],
               f"WT13c a paper section (Results, Abstract) is a verifiable location; 'somewhere' is not: {fm}")
            before = yaml.safe_load(open(os.path.join(rd, "analysis", "hypotheses.yaml")))
            n0 = len(calls)
            out = rebuild(troot, [{"id": "toy0", "shadow_price": 0.1}, {"id": "missing", "shadow_price": 0.1}],
                          anchor_ref="refs/remotes/origin/wm-trials")
            after = yaml.safe_load(open(os.path.join(rd, "analysis", "hypotheses.yaml")))
            ok(len(calls) == n0 and out["toy0"]["status"] == "rebuilt" and out["missing"]["status"] == "skipped"
               and [h["status"] for h in after["hypotheses"]] == [h["status"] for h in before["hypotheses"]]
               and all(h["confrontation"]["registration_status"] == "remote-anchored" for h in after["hypotheses"]),
               f"WT13d rebuild re-runs only the circular classification and the build (no LLM, no engine), keeping "
               f"verdicts and anchoring: {out}")
    tryit("WT13 circular scope, harness citations, rebuild", wt13)

    # Owner, 2026-11-13 (after the first 10): independent features by construction, citation lookup, tighter bands.
    def _indep_template(d, decl):
        _toy_template(d)
        p = os.path.join(d, "decision_spec.yaml")
        raw = yaml.safe_load(open(p))
        raw["analysis"]["emulator"]["features"] = ["cov"]
        if decl is not None:
            raw["analysis"]["emulator"]["independent_features"] = decl
        with open(p, "w") as f:
            yaml.safe_dump(raw, f, sort_keys=False)
        with open(os.path.join(d, "generate_units.py"), "w") as f:
            f.write("# toy generator: cov drawn independently of every other column\n")

    def wt14():
        from toolkit.analysis import batch as B
        from toolkit.analysis import world_model as W
        from toolkit.spec import schema
        good = {"features": ["cov"], "generator": "generate_units.py",
                "basis": "the generator draws cov independently of every other column (synthetic units table)"}
        with tempfile.TemporaryDirectory() as d:
            _indep_template(d, good)
            design, _ = B.design(schema.load(os.path.join(d, "decision_spec.yaml")))
            ok(design.get("independent_features") == ["cov"] and
               (design.get("independent_features_basis") or {}).get("generator_sha256"),
               f"WT14 a declared independent feature of a synthetic units table is a design property, with its basis "
               f"and the generator's sha256: {design.get('independent_features')}, "
               f"{design.get('independent_features_basis')}")
            os.makedirs(os.path.join(d, "analysis", "batch"))
            with open(os.path.join(d, "analysis", "batch", "design.json"), "w") as f:
                json.dump(design, f)
            _, jobs = B.design(schema.load(os.path.join(d, "decision_spec.yaml")))
            B.run(d, jobs, schema.load(os.path.join(d, "decision_spec.yaml")).engine(), max_workers=2)
            sw, _ = W.load_sweep(d, "current", 0.1, use_plan=False)
            ok(sw.design.get("independent") == ["cov"], f"WT14b world_model reads it as independent_features: "
                                                         f"{sw.design}")
        for bad, why in (({"features": ["tgt"], "generator": "generate_units.py", "basis": "b"},
                          "a column that is not a declared design feature"),
                         ({"features": ["cov"], "generator": "generate_units.py"}, "no basis"),
                         ({"features": ["cov"], "basis": "b"}, "no generator"),
                         ({"features": ["cov"], "generator": "nope.py", "basis": "b"}, "a generator that is not there")):
            with tempfile.TemporaryDirectory() as d:
                _indep_template(d, bad)
                try:
                    B.design(schema.load(os.path.join(d, "decision_spec.yaml")))
                    ok(False, f"WT14c an independence declaration with {why} is refused")
                except ValueError:
                    ok(True, f"WT14c an independence declaration with {why} is refused")
        with tempfile.TemporaryDirectory() as d:
            _indep_template(d, None)
            design, _ = B.design(schema.load(os.path.join(d, "decision_spec.yaml")))
            ok("independent_features" not in design and "independent_features_basis" not in design,
               "WT14d without a declaration the design is unchanged (no independent_features key)")
    tryit("WT14 independent features", wt14)

    def wt15():
        seen = []

        def lookup(query):
            seen.append(query)
            if "Doe" in query:
                return [{"paperId": "abc123", "title": "A trial of things in places", "year": 2019,
                         "authors": [{"name": "Jane Doe"}, {"name": "R Roe"}]}]
            if "Boom" in query:
                raise OSError("network down")
            return []
        seed = {"beliefs": [
            {"id": "B1", "source_type": "literature", "confidence": "high",
             "evidence": [{"citation": "Doe J et al. A trial of things in places. Lancet 2019;393:1-9",
                           "location": "Table 2"}]},
            {"id": "B2", "source_type": "literature", "confidence": "medium",
             "evidence": [{"citation": "Imaginary A. Results that never were. J Nothing 2021", "location": "p. 3"}]},
            {"id": "B3", "source_type": "literature", "confidence": "medium",
             "evidence": [{"citation": "Boom B. Network failure as method. 2020", "location": "Table 1"}]},
            {"id": "B4", "source_type": "data", "confidence": "medium",
             "evidence": [{"dossier_entry": "units.csv column pop"}]}]}
        out, rec = verify_citations(seed, lookup=lookup, pause_s=0)
        conf = {b["id"]: b["confidence"] for b in out["beliefs"]}
        st = {r["belief"]: r["status"] for r in rec}
        ok(st == {"B1": "found", "B2": "not_found", "B3": "lookup_failed"} and
           [r["paper_id"] for r in rec if r["belief"] == "B1"] == ["abc123"],
           f"WT15 each literature citation is looked up (title, first author and year must match): {st}")
        ok(conf == {"B1": "high", "B2": "low", "B3": "medium", "B4": "medium"} and
           out["beliefs"][1]["citation_check"]["confidence_claimed"] == "medium",
           f"WT15b a citation not found downgrades its belief to low (the claim is kept); a failed lookup is recorded "
           f"and changes nothing; data beliefs are not looked up: {conf}")
        wrong = [{"paperId": "x", "title": "A trial of things in places", "year": 2011,
                  "authors": [{"name": "Jane Doe"}]}]
        _, rec2 = verify_citations({"beliefs": [seed["beliefs"][0]]}, lookup=lambda q: wrong, pause_s=0)
        ok(rec2[0]["status"] == "not_found", f"WT15c a title match with another year is not a match: {rec2}")
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed())
            rd = os.path.join(troot, "runs", "toy0")
            led = yaml.safe_load(open(os.path.join(rd, SEED)))
            cites = json.load(open(os.path.join(rd, "analysis", "citations.json")))
            S = score(troot)
            ok(cites["records"] and all(b.get("citation_check") for b in led["beliefs"]
                                        if b["source_type"] == "literature")
               and S["questions"]["toy0"]["citations"]["not_found"] == len(cites["records"])
               and "citations" in S["overall"],
               f"WT15d in a run the check sits between the seed and its registration (the registered seed carries it) "
               f"and the summary counts it: {S['questions']['toy0'].get('citations')}")
    tryit("WT15 citations", wt15)

    def wt16():
        from agents.v2_sdk_tasks import seed_prompt_text
        sp = seed_prompt_text()
        ok("as tight as the cited evidence supports" in sp and "lenient" in sp,
           "WT16 the seed prompt asks for bands as tight as the evidence supports and says a wider band is lenient")
        ok(DEFAULT_MAX_ENGINE_RUNS >= 400, f"WT16b designs of 60-100 points fit the run limit "
                                           f"({DEFAULT_MAX_ENGINE_RUNS} engine runs)")
    tryit("WT16 prompt and design size", wt16)

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
