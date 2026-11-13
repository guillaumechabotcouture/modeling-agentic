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
P_RANGE = (0.02, 0.98)              # a claim's stated probability: never 0 or 1 (it must be able to surprise)
DEFAULT_MAX_ENGINE_RUNS = 400      # 60-100 design points x up to 4 packages (owner, 2026-11-13); each batch about a minute
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
        if "shadow_price" in q:
            bad.append(f"{w}: shadow_price is not set in the questions file: the template pre-registers its willingness "
                       f"to pay ({WTP_FILE}, against {WTP_TABLE})")
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
        try:
            shadow_price_of(q, root)
        except TrialError as e:
            bad.append(f"{w}: {e}")
    if bad:
        raise TrialError(f"{path}: " + "; ".join(bad))
    return qs


WTP_TABLE = "data/world_model_trials/wtp.yaml"     # one published threshold per DALY and DALYs per outcome type
WTP_FILE = "wtp.yaml"                              # in a template: {outcome_type}; none for an estimation question


def shadow_price_of(q, root=REPO) -> dict:
    """{value, basis}: the template's pre-registered willingness to pay as a shadow price (outcome per USD) = DALYs per
    outcome of its outcome type / the threshold (USD per DALY), both from the question-independent table WTP_TABLE with
    their sources. A template without wtp.yaml has none (paired tests are then untestable). Refused (TrialError): an
    outcome type the table lacks, or a table without a positive threshold and a source."""
    import yaml
    p = os.path.join(root, str(q.get("template") or ""), WTP_FILE)
    if not os.path.isfile(p):
        return {"value": None, "basis": f"no {WTP_FILE} in the template: no willingness to pay (paired tests "
                                        "untestable)"}
    ot = (yaml.safe_load(open(p)) or {}).get("outcome_type")
    try:
        tab = yaml.safe_load(open(os.path.join(root, WTP_TABLE))) or {}
    except OSError:
        raise TrialError(f"{q.get('id')}: {WTP_TABLE} is missing")
    th, row = tab.get("threshold") or {}, (tab.get("outcome_types") or {}).get(ot)
    usd = th.get("usd_per_daly")
    if not (isinstance(usd, (int, float)) and usd > 0 and str(th.get("source") or "").strip()):
        raise TrialError(f"{WTP_TABLE}: threshold.usd_per_daly a positive number with its source")
    if not (isinstance(row, dict) and isinstance(row.get("dalys"), (int, float)) and row["dalys"] > 0 and
            str(row.get("source") or "").strip()):
        raise TrialError(f"{q.get('id')}: outcome type {ot!r} is not in {WTP_TABLE} with its DALYs and source")
    return {"value": float(row["dalys"]) / float(usd), "outcome_type": ot,
            "basis": f"{row['dalys']} DALYs per {ot} ({row['source']}) / US${usd} per DALY ({th['source']})"}


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


def prepare(q, run_dir, root=REPO, max_engine_runs=DEFAULT_MAX_ENGINE_RUNS, trials_root=None) -> dict:
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
                      # the pre-registered willingness to pay (a published threshold, question-independent) is shown
                      # from batch 4 on (owner, 2026-11-13); batches 1-3 were seeded with it withheld and keep that
                      "shadow_price": None if q.get("shadow_price") is None else
                      "withheld: net benefit is compared at a declared shadow price (outcome per USD) whose value is "
                      "not shown" if q.get("withhold_shadow_price") else
                      {"value": q["shadow_price"], "units": "outcome per USD",
                       "basis": q.get("shadow_price_basis") or "the template's pre-registered willingness to pay"},
                      "paired_tests": "testable" if q.get("shadow_price") is not None else
                      "untestable: no shadow price is declared",
                      "optimised_plan": False,
                      "min_points": W.SETTINGS["min_points"], "small_sample_below": W.SETTINGS["perm_n_max"]},
            "hypotheses": {"min": HYPOTHESES[0], "max": HYPOTHESES[1], "p_required": True},
            "prior_world_model": carried(q, trials_root)}


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
    if (ctx.get("hypotheses") or {}).get("p_required"):
        for h in hs:
            p = h.get("p")
            if isinstance(p, bool) or not isinstance(p, (int, float)) or not P_RANGE[0] <= p <= P_RANGE[1]:
                bad.append(f"hypothesis {h.get('id')}: p, the probability that the claim holds, a number in "
                           f"[{P_RANGE[0]}, {P_RANGE[1]}] (never 0 or 1: a claim must be able to surprise)")
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


def engine_read_columns(spec) -> set:
    """Units-table columns the engine reads: those bound to its unit attributes (decision_units.engine_attributes:
    column or columns). A declared feature on any other column is something the engine never reads."""
    out = set()
    for b in ((spec["decision_units"].get("engine_attributes") or {}).values()):
        if not isinstance(b, dict):
            continue
        if b.get("column"):
            out.add(str(b["column"]))
        cols = b.get("columns")
        out |= {str(c) for c in (cols.values() if isinstance(cols, dict) else cols or [])}
    return out


ENGINE_READS = "engine_reads.yaml"     # in a template: {features_read_via: {feature: bound attribute or column}}


def engine_unread_features(spec_dir, reads_dir=None) -> set:
    """Declared design features the engine never reads, from the inputs only: a feature on a units-table column
    bound to no engine attribute (decision_units.engine_attributes), unless the template declares that it reaches
    the engine through a derived bound attribute (engine_reads.yaml features_read_via, e.g. a susceptibility gap
    folded into the bound s0 groups), and a calibration axis the engine card does not list. A features_read_via
    target that is neither an engine attribute nor a bound column is refused (TrialError)."""
    import yaml
    from toolkit.analysis import features as FT
    from toolkit.spec import schema
    spec = schema.load(os.path.join(spec_dir, "decision_spec.yaml"))
    read = engine_read_columns(spec)
    attrs = set(spec["decision_units"].get("engine_attributes") or {})
    attr = spec["calibration"].get("attribute")
    via = {}
    for d in (spec_dir, reads_dir):
        if d and os.path.isfile(os.path.join(d, ENGINE_READS)):
            via = (yaml.safe_load(open(os.path.join(d, ENGINE_READS))) or {}).get("features_read_via") or {}
            break
    bad = {f: t for f, t in via.items() if t not in read | attrs | ({attr} if attr else set())}
    if bad:
        raise TrialError(f"{ENGINE_READS}: features_read_via targets {bad} are neither engine attributes nor bound "
                         f"columns ({sorted(read | attrs)})")
    unread = {FT.name(f) for f in FT.declared(spec.raw) if isinstance(f, str) and f not in read and f not in via}
    card_attrs = set(spec.engine().describe().to_dict().get("unit_attributes") or {})
    if attr and attr not in card_attrs:
        unread.add(f"{attr} (calibration scale)")
    return unread


def classify_tests(run_dir, seed, lam=None, template_dir=None) -> dict:
    """{hypothesis id: {class, why}} from the spec's inputs and the engine's own sweep (world_model.load_sweep,
    canonical_test and the sweep's reduction, as the build reads them). Classes, applied in world_model build
    (--set-aside): reported, counted apart, never a confirmation, never moving confidence.
      circular      a test of a package's effect against one of its own effect parameters (its feature); or an effect
                    band on a reduction that its own declared effect parameters explain alone (R^2 >= CIRCULAR_R2 of a
                    quadratic fit, linear below twice the quadratic's columns of points) whose range over the design
                    (which spans the declared ranges) lies inside the band: its verdict is fixed by a spec input
      prior_echo    such a band whose range is not inside the band: its claimed quantity is a declared engine prior,
                    so its verdict measures agreement with that prior, not skill
      engine_blind  a direction test on a feature the engine never reads (a units-table column bound to no engine
                    attribute, or a calibration axis the engine card does not list): untestable by this engine"""
    import numpy as np
    from toolkit.analysis import world_model as W
    sw, spec = W.load_sweep(run_dir, "current", lam, use_plan=False)
    unread = engine_unread_features(run_dir, template_dir)
    by = {b["id"]: b for b in seed.get("beliefs") or []}
    out = {}
    for h in seed.get("hypotheses") or []:
        t = W.canonical_test(h["test"], by.get(h["belief"]))
        if t["kind"] == "plan_share":
            continue
        pk = [t[x] for x in ("package", "a", "b") if t.get(x)]
        comps = {c for p in pk for c in sw.packages.get(p, [])}
        own = [f for f in sw.features if sw.kinds.get(f) == "parameter" and _own(f, comps)]
        if t.get("feature") in own:     # a test against it; adjusting for it or a region of it is not
            out[h["id"]] = {"class": "circular",
                            "why": f"tests {pk}'s effect against its own effect parameter {t['feature']}"}
            continue
        if t["kind"] == "effect_direction" and t.get("feature") in unread:
            out[h["id"]] = {"class": "engine_blind",
                            "why": f"the engine never reads {t['feature']} (bound to no engine attribute): untestable "
                                   "by this engine"}
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
        if r2 < CIRCULAR_R2:
            continue
        lo, hi = float(y.min()), float(y.max())
        inside = t["band"][0] <= lo and hi <= t["band"][1]
        out[h["id"]] = {"class": "circular" if inside else "prior_echo",
                        "why": f"{t['package']}'s relative reduction is fixed by its declared effect parameter(s) {own} "
                               f"(R^2 {r2:.4f}); its range over the design [{lo:.3g}, {hi:.3g}] "
                               + (f"lies inside the band {t['band']}" if inside else
                                  f"against the band {t['band']}: the verdict compares the seed with the prior")}
    return out


def circular_tests(run_dir, seed, lam=None) -> dict:
    """{hypothesis id: why} of the circular class of classify_tests."""
    return {k: v["why"] for k, v in classify_tests(run_dir, seed, lam).items() if v["class"] == "circular"}


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


# ---------------------------------------------------------------- citation lookup (no LLM)
S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works"
_S2_LOCK, _S2_LAST = threading.Lock(), [0.0]
TITLE_OVERLAP = 0.5           # share of the found title's words (4+ letters) in the citation, for a named first author
STRONG_OVERLAP = 0.8          # both ways, for an organisation or anonymous author (no surname to check)
S2_BACKOFF = (2, 4, 8, 16)    # seconds between Semantic Scholar retries after a 429, then the next source answers
ORG_RE = re.compile(r"\b(WHO|UNICEF|UNAIDS|CDC|ECDC|World Health Organi[sz]ation|Committee|Team|Partnership|Group|"
                    r"Union|Organi[sz]ation|Ministry|Collaborat\w*|Consortium|Network|Initiative|Agency|Institute|"
                    r"Council|Foundation|Programme|Program)\b")
# not a reference to a published source: the engine, the question's own arithmetic, the run's data, an assumption
NOT_CITATION_RE = re.compile(r"(engine card|\bcard\b|question'?s? arithmetic|\barithmetic\b|no external source|"
                             r"\.csv\b|\bcolumn\b|decision_spec|task (input|description)|_task_input|\bharness\b|"
                             r"\bsweep\b|\bassumption\b|my reading|own reasoning)", re.I)


class RateLimited(OSError):
    """The lookup service refused for rate (HTTP 429) after its retries."""


def _throttle(pause_s):
    wait = _S2_LAST[0] + pause_s - time.time()
    if wait > 0:
        time.sleep(wait)


def _get_json(url, opener=None):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "world-model-trials"})
    with (opener or urllib.request.urlopen)(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def s2_lookup(query, hint=None, pause_s=1.1, opener=None, sleep=None) -> list[dict]:
    """Papers matching `query` from the Semantic Scholar public search API (no key; one call at a time across threads,
    at least `pause_s` apart). A 429 backs off exponentially (S2_BACKOFF), then RateLimited: the next source answers."""
    import urllib.error
    import urllib.parse
    url = S2_SEARCH + "?" + urllib.parse.urlencode({"query": query[:300], "limit": 8,
                                                    "fields": "title,year,authors,externalIds,venue"})
    sleep = sleep or time.sleep
    for k in range(len(S2_BACKOFF) + 1):
        with _S2_LOCK:
            _throttle(pause_s)
            try:
                return [dict(p, source="semantic_scholar") for p in _get_json(url, opener).get("data") or []]
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise OSError(f"Semantic Scholar: HTTP {e.code}")
            finally:
                _S2_LAST[0] = time.time()
        if k < len(S2_BACKOFF):
            sleep(S2_BACKOFF[k])
    raise RateLimited("Semantic Scholar: rate limited after exponential backoff")


def openalex_url(query, hint=None) -> str:
    """The OpenAlex search URL: within one year of the cited year when known; its polite-pool `mailto` only from the
    environment variable OPENALEX_MAILTO (never written to code or files; absent, the lookup works without it)."""
    import urllib.parse
    p = {"search": query[:300], "per-page": 8, "select": "id,display_name,publication_year,authorships,doi"}
    y = (hint or {}).get("year")
    if y:
        p["filter"] = f"publication_year:{y - 1}-{y + 1}"
    if os.environ.get("OPENALEX_MAILTO"):
        p["mailto"] = os.environ["OPENALEX_MAILTO"]
    return OPENALEX + "?" + urllib.parse.urlencode(p)


def openalex_lookup(query, hint=None, pause_s=0.2) -> list[dict]:
    """Works matching `query` from the OpenAlex public API (no key)."""
    with _S2_LOCK:
        _throttle(pause_s)
        try:
            res = _get_json(openalex_url(query, hint)).get("results") or []
        finally:
            _S2_LAST[0] = time.time()
    return [{"paperId": it.get("id"), "title": it.get("display_name") or "", "year": it.get("publication_year"),
             "source": "openalex",
             "authors": [{"name": (a.get("author") or {}).get("display_name") or ""} for a in it.get("authorships") or []]}
            for it in res]


def crossref_lookup(query, hint=None, pause_s=1.1) -> list[dict]:
    """Works matching `query` from the Crossref public API (no key); an organisation author keeps its name."""
    import urllib.parse
    p = {"query.bibliographic": query[:300], "rows": 10}
    if (hint or {}).get("author"):
        p["query.author"] = hint["author"]
    with _S2_LOCK:
        _throttle(pause_s)
        try:
            items = (_get_json(CROSSREF + "?" + urllib.parse.urlencode(p)).get("message") or {}).get("items") or []
        finally:
            _S2_LAST[0] = time.time()
    out = []
    for it in items:
        dp = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        out.append({"paperId": f"doi:{it.get('DOI')}", "title": (it.get("title") or [""])[0],
                    "year": dp[0] if dp else None, "source": "crossref",
                    "authors": [{"name": a.get("name") or f"{a.get('given', '')} {a.get('family', '')}".strip()}
                                for a in it.get("author") or []]})
    return out


def _norm(s):
    """Lower case, accents and typographic quotes and dashes folded, hyphens inside words joined (pre-exposure ->
    preexposure), other punctuation a space."""
    import unicodedata
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode("ascii") if s else ""
    s = re.sub(r"[‘’`]", "'", s).lower()
    s = re.sub(r"(?<=\w)[-‐‑](?=\w)", "", s)
    return re.sub(r"[^a-z0-9' ]+", " ", s)


def _norm_u(s):
    import unicodedata
    s = re.sub(r"[‘’`]", "'", str(s or ""))
    s = re.sub(r"[‐‑‒–—]", "-", s)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def _words(s):
    return {w for w in re.findall(r"[a-z]{4,}", _norm(_norm_u(s)))}


def _sentences(c):
    return [p.strip() for p in re.split(r"\.\s+", _norm_u(c)) if p.strip()]


def _title_guess(c):
    """The title part of an author-title-venue citation: the longest sentence between full stops (None if short)."""
    parts = _sentences(c)
    best = max(parts, key=lambda p: len(_words(p)), default="")
    return best if len(_words(best)) >= 3 and best != _norm_u(c) else None


def _author_part(c):
    """(kind, surname): 'person' with the first author's surname when the citation opens with "Surname XX" or "et
    al"; 'organisation' when its first sentence names an organisation; else 'anonymous' (it opens with its title)."""
    first = (_sentences(c) or [""])[0]
    m = re.match(r"^((?:[a-z]+['’]?[a-z]*\s)?[A-Z][A-Za-z'’\-]+(?:[\s-][A-Z][A-Za-z'’\-]+)?)\s+[A-Z]{1,3}\b",
                 first)
    if m or re.search(r"\bet al\b", first):
        name = m.group(1) if m else first.split()[0]
        return "person", _norm(name.split()[-1] if " " in name and not re.match(r"^[a-z]", name) else name).replace(" ",
                                                                                                                    "")
    if ORG_RE.search(first):
        return "organisation", None
    return "anonymous", None


def citation_class(c):
    """'not_a_citation' for a source that is not a reference to a published work (the engine card, the question's own
    arithmetic, a data column of the run, an assumption), else None."""
    return "not_a_citation" if NOT_CITATION_RE.search(str(c or "")) else None


def citation_match(citation, paper) -> bool:
    """A found paper matches a citation when its year is within one of a cited year (online versus print) and its title
    matches the citation's (case, punctuation, accents, hyphens and a subtitle after a colon folded): for a named first
    author, most of the found title's words are in the citation and the first author's surname is the citation's; for
    an organisation or an anonymous note there is no surname to check, so the titles must match closely both ways."""
    yrs = [int(m.group(0)) for m in re.finditer(r"\b(19|20)\d\d\b", citation)]
    py = paper.get("year")
    if not (yrs and py and any(abs(int(py) - y) <= 1 for y in yrs)):
        return False
    title = _norm_u(paper.get("title"))
    tw, cw = _words(title), _words(citation)
    main = _words(re.split(r"\s[-:]\s|:\s", title)[0]) or tw
    if not tw:
        return False
    fwd = max(len(tw & cw) / len(tw), len(main & cw) / len(main))
    kind, surname = _author_part(citation)
    if kind == "person":
        authors = paper.get("authors") or []
        a0 = _norm(_norm_u((authors[0] if authors else {}).get("name"))).replace(" ", "")
        return bool(surname) and surname in a0 and fwd >= TITLE_OVERLAP
    # no surname to check: the whole found title (its subtitle too: a paper about a guideline is not the guideline)
    ct = _words(_title_guess(citation) or (_sentences(citation) or [""])[0])
    back = len(ct & tw) / len(ct) if ct else 0.0
    return len(tw & cw) / len(tw) >= STRONG_OVERLAP and back >= STRONG_OVERLAP


class Lookup:
    """Sources in order (default: OpenAlex, then Semantic Scholar with exponential backoff on a 429, then Crossref),
    each asked with the citation and then its title alone, until one returns a matching paper. A source that fails is
    skipped for that citation; after `breaker` rate refusals in a row Semantic Scholar is skipped, and asked again every
    `reprobe` lookups (its public endpoint shares one rate pool)."""

    def __init__(self, sources=None, breaker=3, reprobe=25, pause_s=1.1):
        self.sources = sources or [("openalex", lambda q, h=None: openalex_lookup(q, h)),
                                   ("semantic_scholar", lambda q, h=None: s2_lookup(q, h, pause_s)),
                                   ("crossref", lambda q, h=None: crossref_lookup(q, h, pause_s))]
        self.breaker, self.reprobe, self.refusals, self.n = breaker, reprobe, 0, 0

    def find(self, citation):
        """(matching paper or None, sources tried); OSError when every source failed."""
        self.n += 1
        yrs = [int(m.group(0)) for m in re.finditer(r"\b(19|20)\d\d\b", citation)]
        kind, surname = _author_part(citation)
        hint = {"year": yrs[0] if yrs else None, "author": surname if kind == "person" else None}
        tried, errors = [], 0
        for name, fn in self.sources:
            if name == "semantic_scholar" and self.refusals >= self.breaker and self.n % self.reprobe:
                continue
            tried.append(name)
            try:
                for q in dict.fromkeys(x for x in (citation, _title_guess(citation)) if x):
                    m = next((p for p in fn(q, hint) if citation_match(citation, p)), None)
                    if m:
                        if name == "semantic_scholar":
                            self.refusals = 0
                        return dict(m, source=m.get("source") or name), tried
                if name == "semantic_scholar":
                    self.refusals = 0
            except RateLimited:
                errors += 1
                if name == "semantic_scholar":
                    self.refusals += 1
            except (OSError, ValueError):
                errors += 1
        if errors == len(tried):
            raise OSError(f"every citation source failed ({tried})")
        return None, tried


_LOOKUP = []


def _default_lookup(pause_s):
    """One Lookup per process, shared by the questions' threads, so the circuit breaker holds across questions."""
    with _S2_LOCK:
        if not _LOOKUP:
            _LOOKUP.append(Lookup(pause_s=pause_s))
        return _LOOKUP[0]


def verify_citations(seed, lookup=None, pause_s=1.1) -> tuple[dict, list[dict]]:
    """(seed, records): every literature citation classified and looked up (the default Lookup unless `lookup`, a Lookup
    or a function query -> papers, is given). Owner rule (2026-11-13): until matching is validated, not found is a flag
    and changes nothing; a citation that is not a reference to a published work lowers its belief to low; a failed
    lookup changes nothing. The claimed confidence is kept in citation_check. Data beliefs are not looked up."""
    import copy
    out, recs = copy.deepcopy(seed), []
    look = lookup or _default_lookup(pause_s)
    for b in out.get("beliefs") or []:
        if b.get("source_type") != "literature":
            continue
        claimed = (b.get("citation_check") or {}).get("confidence_claimed") or b["confidence"]
        rows = []
        for e in b.get("evidence") or []:
            c = str(e.get("citation") or "")
            r = {"belief": b["id"], "citation": c[:300]}
            if citation_class(c):
                rows.append(dict(r, status=citation_class(c)))
                continue
            try:
                if isinstance(look, Lookup):
                    m, tried = look.find(c)
                else:
                    m, tried = None, ["injected"]
                    for q in dict.fromkeys(x for x in (c, _title_guess(c)) if x):
                        m = next((p for p in look(q) if citation_match(c, p)), None)
                        if m:
                            break
                r.update(status="found" if m else "not_found", paper_id=(m or {}).get("paperId"),
                         title=(m or {}).get("title"), year=(m or {}).get("year"), source=(m or {}).get("source"),
                         sources_tried=tried)
            except (OSError, ValueError) as ex:
                r.update(status="lookup_failed", error=str(ex)[:200])
            rows.append(r)
        recs += rows
        n = {k: sum(r["status"] == k for r in rows) for k in ("found", "not_found", "not_a_citation", "lookup_failed")}
        b["citation_check"] = dict(n, confidence_claimed=claimed)
        b["confidence"] = "low" if n["not_a_citation"] else claimed
    return out, recs


def citation_step(run_dir, lookup=None) -> dict:
    """The seed's citations checked between its validation and its registration: the registered seed carries the
    check (analysis/world_model_seed.yaml) and analysis/citations.json records each lookup."""
    import yaml
    seed = yaml.safe_load(open(os.path.join(run_dir, SEED)))
    out, recs = verify_citations(seed, lookup)
    from toolkit.analysis import world_model as W
    W.validate(out)
    _sio().write_nofollow(run_dir, SEED, yaml.safe_dump(out, sort_keys=False, width=110).encode(), atomic=True)
    _sio().write_nofollow(run_dir, "analysis/citations.json", json.dumps(
        {"sources": "OpenAlex, Semantic Scholar, Crossref" if lookup is None else "injected lookup", "at": _now(),
         "rule": "not found is a flag only; not a citation lowers confidence to low", "records": recs},
        indent=1).encode(), atomic=True)
    return {k: sum(r["status"] == k for r in recs) for k in ("found", "not_found", "not_a_citation", "lookup_failed")}


def register_step(run_dir) -> dict:
    from toolkit.analysis import world_model as W
    buf = io.StringIO()
    with _CLI_LOCK, contextlib.redirect_stdout(buf):      # sys.stdout is process-wide: one at a time
        W._cli(["register", os.path.join(run_dir, SEED), run_dir])
    return {"note": buf.getvalue().strip()}


def engine_step(q, run_dir, max_engine_runs=DEFAULT_MAX_ENGINE_RUNS, workers=2) -> dict:
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
    aside = classify_tests(run_dir, seed, q.get("shadow_price"),
                           os.path.join(q.get("_root") or REPO, q["template"]) if q.get("template") else None)
    circ = {k: v["why"] for k, v in aside.items() if v["class"] == "circular"}
    _sio().write_nofollow(run_dir, "analysis/circular.json", json.dumps(circ, indent=1, sort_keys=True).encode(),
                          atomic=True)
    _sio().write_nofollow(run_dir, "analysis/set_aside.json", json.dumps(aside, indent=1, sort_keys=True).encode(),
                          atomic=True)
    argv = ["build", run_dir, "--seed", os.path.join(run_dir, SEED), "--without-plan",
            "--set-aside", os.path.join(run_dir, "analysis", "set_aside.json")]
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
    return {"statuses": st, "circular": sorted(circ), "set_aside": {k: v["class"] for k, v in sorted(aside.items())},
            "note": buf.getvalue().strip()}


def _wait_past(at):
    """Sleep until the wall clock is past `at` (an ISO time to the second), so the batch's first-seen time is after
    the anchor commit's time (a tie would make every confrontation post hoc)."""
    t = dt.datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    while time.time() < t + 1.05:
        time.sleep(0.1)


def run_trials(questions, trials_root, repo=REPO, seed_fn=None, ledger=None, anchor=None, task_cap_usd=2.0,
               llm_concurrency=5, engine_workers=2, max_engine_runs=DEFAULT_MAX_ENGINE_RUNS, engines=CHEAP_ENGINES,
               engine_probe=None,
               root=None, citation_lookup=None, engine="local") -> dict:
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
        t["shadow_price"] = shadow_price_of(q, root)
        q = dict(q, shadow_price=t["shadow_price"]["value"], shadow_price_basis=t["shadow_price"]["basis"])
        try:
            if "prepare" not in t["steps"]:
                t["context"] = prepare(q, rd, root, max_engine_runs, trials_root=trials_root)
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
            if "citations" not in t["steps"] and "register" not in t["steps"]:     # between validation and registration
                t["citations"] = citation_step(rd, citation_lookup)
                t["steps"]["citations"] = _now()
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
            if engine == "remote":           # owner, 2026-11-13: no engine compute here; a VM job runs it
                t["status"] = "awaiting_engine"
                _save(rd, t)
                return record(q["id"], t)
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
MODES = ("circular", "prior_echo", "engine_blind", "untestable", "post_hoc", "conditional_not_settled", "too_few_points", "test_too_lenient", "wrong_estimand",
         "inert_feature", "high_confidence_refuted", "contradiction_without_open_question", "citation_not_verifiable")


ASIDE = ("circular", "prior_echo", "engine_blind")


def _aside(h):
    """The set-aside class of a checked hypothesis (circular, prior_echo, engine_blind), or None."""
    c = h.get("confrontation") or {}
    return (c.get("set_aside") or {}).get("class") or ("circular" if c.get("circular") else None)


def failure_modes(hyps, seed, wm, ctx=None) -> dict:
    """{mode: [hypothesis or belief ids]} for the checked hypotheses of one question (MODES; empty modes omitted)."""
    by = {b["id"]: b for b in seed.get("beliefs") or []}
    qs = {q.get("id") for q in (wm or {}).get("open_questions") or []}
    feats = ((ctx or {}).get("sweep") or {}).get("features") or {}
    out = {m: [] for m in MODES}
    for h in hyps:
        t, st, obs = h.get("test") or {}, h.get("status"), (h.get("result") or {}).get("observed") or {}
        b = by.get(h.get("belief")) or {}
        if st == "untestable" and cls != "engine_blind":
            out["untestable"].append(h["id"])
        cls = _aside(h)
        if cls:
            out[cls].append(h["id"])
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


def _citations(run_dir):
    """Counts from analysis/citations.json (None for a run made before the citation check)."""
    p = os.path.join(run_dir, "analysis", "citations.json")
    if not os.path.isfile(p):
        return None
    recs = json.load(open(p)).get("records") or []
    return dict({k: sum(r["status"] == k for r in recs) for k in ("found", "not_found", "not_a_citation", "lookup_failed")},
                beliefs_downgraded=sorted({r["belief"] for r in recs if r["status"] == "not_found"}))


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
        if (h.get("confrontation") or {}).get("post_hoc") or _aside(h):
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
                           "by_status": st, **{c: sum(_aside(h) == c for h in hs) for c in ASIDE},
                           "post_hoc": sum(bool((h.get("confrontation") or {}).get("post_hoc"))
                                                            for h in hs)},
               confidence=conf, contradictions=contra, conflicts=conflicts,
               where_to_look={"top": wl, "decision_relevant": bool(wb.get("decision_relevant"))},
               failure_modes=failure_modes(hs, seed, wm, t.get("context")),
               seed_refusals=(t.get("seed") or {}).get("refusals") or [],
               citations=_citations(run_dir),
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
    tot = dict({"written": 0, "testable": 0, "post_hoc": 0, "by_status": {}}, **{c: 0 for c in ASIDE})
    conf = {c: {"supported": 0, "refuted": 0, "other": 0} for c in ("low", "medium", "high")}
    fm, wtl = {}, {"decision_relevant": 0, "questions": 0}
    cit = {"found": 0, "not_found": 0, "not_a_citation": 0, "lookup_failed": 0, "questions_checked": 0}
    sup = {q["rerun_of"]: i for i, q in Q.items() if q.get("rerun_of") and q["status"] == "complete"}
    for i, q in Q.items():
        if i in sup:                    # an earlier run of a question rerun under newer rules: listed, not scored
            q["superseded_by"] = sup[i]
            continue
        h = q.get("hypotheses")
        if not h:
            continue
        for k in ("written", "testable", "post_hoc") + ASIDE:
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
        if q.get("citations"):
            cit["questions_checked"] += 1
            for k in ("found", "not_found", "not_a_citation", "lookup_failed"):
                cit[k] += q["citations"][k]
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
         "overall": {"questions": statuses, "hypotheses": tot, "confidence": conf, "failure_modes": fm, "citations": cit,
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
         f"Circular tests: {o['hypotheses']['circular']} of {o['hypotheses']['written']}. "
         f"Literature citations looked up (questions run with the check: {o['citations']['questions_checked']}): "
         f"{o['citations']['found']} found, {o['citations']['not_found']} not found (a flag only: confidence kept until "
         f"matching is validated), {o['citations'].get('not_a_citation', 0)} not a citation (lowered to low confidence), "
         f"{o['citations']['lookup_failed']} lookups failed.", "",
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
        for c in ([] if q.get("superseded_by") else q.get("contradictions") or []):      # current runs only
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


# ---------------------------------------------------------------- one guard and the belief table (owner, 2026-11-13)
def _input_determined(sw, package, mask) -> tuple[bool, float | None]:
    """Is the package's relative reduction over `mask` fixed by its own declared effect parameters alone (R^2 >=
    CIRCULAR_R2, the fit classify_tests uses)? The comparator (no components) is: its reduction is zero."""
    import numpy as np
    comps = sw.packages.get(package) or []
    if not comps:
        return True, 1.0
    own = [f for f in sw.features if sw.kinds.get(f) == "parameter" and _own(f, set(comps))]
    if not own:
        return False, None
    red = sw.reduction(package)
    m = mask & np.isfinite(red)
    y, n = red[m], int(m.sum())
    if n < 10:
        return False, None
    X = np.column_stack([sw.features[f][m] for f in own])
    cols = [np.ones(n)] + [X[:, i] for i in range(X.shape[1])]
    quad = cols + [X[:, i] * X[:, j] for i in range(X.shape[1]) for j in range(i, X.shape[1])]
    A = np.column_stack(quad if n >= 2 * len(quad) else cols)
    res = y - A @ np.linalg.lstsq(A, y, rcond=None)[0]
    r2 = 1.0 - float(res @ res) / float(((y - y.mean()) ** 2).sum()) if np.ptp(y) > 0 else 1.0
    return r2 >= CIRCULAR_R2, r2


def guard_reasons(run_dir, seed, lam=None, template_dir=None) -> dict:
    """{hypothesis id: {class, why}}: the one guard. A claim cannot be tested by a simulation whose declared inputs
    determine its outcome without running the dynamics. Mechanically, from the run's own artifacts and code
    (classify_tests, world_model.load_sweep and canonical_test, batch.realized_rank_correlations):
      circular, prior_echo  the claimed quantity is a declared effect (tested against its own parameter, or a band on
                            a reduction its declared parameters explain alone)
      engine_blind          a direction claim on a feature the engine never reads
      input_determined      a paired comparison (or flip) of packages whose reductions are each fixed by their declared
                            parameters: the winner is arithmetic on declared burden, priors, prices and lambda
      confounded_independence  a conditional claim settled on a feature declared independent whose realized units
                            table correlates it with another declared feature (|Spearman| > 0.3)"""
    from toolkit.analysis import batch as B
    from toolkit.analysis import world_model as W
    from toolkit.spec import schema
    out = dict(classify_tests(run_dir, seed, lam, template_dir))
    sw, spec = W.load_sweep(run_dir, "current", lam, use_plan=False)
    ind = list(sw.design.get("independent") or [])
    worst = {}
    if ind:
        for k, v in B.realized_rank_correlations(spec, ind).items():
            for f in k.split("|"):
                worst[f] = max(worst.get(f, 0.0), abs(v))
    by = {b["id"]: b for b in seed.get("beliefs") or []}
    for h in seed.get("hypotheses") or []:
        if h["id"] in out:
            continue
        t = W.canonical_test(h["test"], by.get(h["belief"]))
        k = t["kind"]
        if k in ("paired_sign", "flip_in"):
            m = W._region(sw, t.get("where"))
            da, ra = _input_determined(sw, t["a"], m)
            db, rb = _input_determined(sw, t["b"], m)
            if da and db:
                out[h["id"]] = {"class": "input_determined",
                                "why": f"{t['a']} and {t['b']} reduce by their declared effects alone (R^2 {ra}, {rb}): "
                                       "the winner is arithmetic on declared burden, priors, prices and lambda"}
                continue
        if k in ("effect_direction", "flip_in") and t.get("scope", "conditional") == "conditional" and \
                not t.get("adjust_for") and t.get("feature") in ind and worst.get(t["feature"], 0.0) > 0.3:
            out[h["id"]] = {"class": "confounded_independence",
                            "why": f"{t['feature']} is declared independent but the realized units table correlates it "
                                   f"with another declared feature (|Spearman| {worst[t['feature']]:.2f} > 0.3)"}
    return out


OUTCOME_OF = {"supported": "confirmed", "refuted": "refuted"}


def _superseded(qs, trials_root) -> set:
    done = {q["id"] for q in qs if _trial(os.path.join(trials_root, "runs", q["id"])).get("status") == "complete"}
    return {q["rerun_of"] for q in qs if q.get("rerun_of") and q["id"] in done}


LABEL_P = {"low": 0.633, "medium": 0.864, "high": 0.833}   # batches 1r3-3's label rates (belief_rescore), a fallback


def _table_rows(rd, q):
    """(seed, rows) of one completed question: each hypothesis with its stated p (None when the seed stated a label
    only), its outcome, the guard's reason and the post-hoc flag."""
    import yaml
    t = _trial(rd)
    seed = yaml.safe_load(open(os.path.join(rd, SEED)))
    hy = yaml.safe_load(open(os.path.join(rd, "analysis", "hypotheses.yaml")))["hypotheses"]
    tpl = os.path.join(REPO, q["template"]) if q.get("template") else None
    g = guard_reasons(rd, seed, (t.get("shadow_price") or {}).get("value"), tpl)
    by = {b["id"]: b for b in seed["beliefs"]}
    stated = {h["id"]: h.get("p") for h in seed.get("hypotheses") or []}
    rows = []
    for h in hy:
        b = by[h["belief"]]
        p = stated.get(h["id"])
        rows.append({"question": q["id"], "batch": str(q.get("batch")), "id": h["id"], "belief": h["belief"],
                     "claim": h.get("claim"), "test": h["test"],
                     "label": (b.get("citation_check") or {}).get("confidence_claimed") or b.get("confidence"),
                     "stated_p": p if isinstance(p, (int, float)) and not isinstance(p, bool) else None,
                     "outcome": OUTCOME_OF.get(h["status"], "undecided"), "status": h["status"],
                     "observed": (h.get("result") or {}).get("observed"), "guard": g.get(h["id"]),
                     "post_hoc": bool((h.get("confrontation") or {}).get("post_hoc"))})
    return seed, rows


def write_belief_table(rd, q, at=None, label_p=None, rows=None, seed=None) -> dict:
    """One question's belief table (analysis/belief_table.yaml): each claim's p is the seed's stated p, else its
    label's rate (`label_p`, default LABEL_P); surprise for unguarded, pre-registered, decided claims; beliefs (p =
    the mean stated p of their claims, else the label's) updated by the stated rule; the next simulation."""
    import yaml
    from toolkit.analysis import belief_table as BT
    at = at or _now()
    if rows is None:
        seed, rows = _table_rows(rd, q)
    lp = label_p or LABEL_P
    for r in rows:
        r["p"] = r["stated_p"] if r["stated_p"] is not None else lp.get(r["label"], 0.5)
        r["p_source"] = "stated" if r["stated_p"] is not None else "label rate"
        r["surprise"] = None if r["guard"] or r["post_hoc"] else BT.surprise(r["p"], r["outcome"])
    beliefs = []
    for b in seed["beliefs"]:
        mine = [r for r in rows if r["belief"] == b["id"]]
        st = [r["stated_p"] for r in mine if r["stated_p"] is not None]
        lab = (b.get("citation_check") or {}).get("confidence_claimed") or b.get("confidence")
        ev = (b.get("evidence") or [{}])[0]
        nb = {"id": b["id"], "claim": b.get("mechanism"), "p": sum(st) / len(st) if st else lp.get(lab, 0.5),
              "basis": f"{b.get('source_type')}: " + str(ev.get("citation") or ev.get("dossier_entry") or
                                                           ev.get("artifact") or "")[:200],
              "decides": bool(b.get("decision_relevant")), "history": []}
        for r in mine:
            if not r["guard"] and not r["post_hoc"]:
                nb = BT.update(nb, r["outcome"], at, observed=f"refuted by {r['id']}: not ({r['claim']}); observed "
                                                              f"{json.dumps(r['observed'], default=str)[:200]}")
        beliefs.append(nb)
    nxt = [b["id"] for b in BT.next_simulation(beliefs)][:3]
    doc = {"schema": "analysis.belief_table/0", "producer": "scripts/world_model_trials.py write_belief_table",
           "p_rule": {"stated_p_first": True, "label_p": lp,
                      "update": {"confirm_step": BT.CONFIRM_STEP, "refuted_p": BT.REFUTED_P}},
           "rows": rows, "beliefs": beliefs, "next_simulation": nxt}
    _sio().write_nofollow(rd, "analysis/belief_table.yaml", yaml.safe_dump(doc, sort_keys=False, width=110,
                                                                            default_flow_style=None).encode(),
                          atomic=True)
    return doc


def carried(q, trials_root) -> list:
    """The earlier questions' belief tables a chained question is seeded with (q["carry_from"]): their beliefs with
    updated p and history, the claims refuted with what was observed, and the open questions. A carried question
    without a belief table is refused (TrialError)."""
    import yaml
    out = []
    for qid in q.get("carry_from") or []:
        p = os.path.join(trials_root or "", "runs", qid, "analysis", "belief_table.yaml")
        if not trials_root or not os.path.isfile(p):
            raise TrialError(f"{q['id']}: carry_from {qid} has no belief table ({p})")
        t = yaml.safe_load(open(p))
        out.append({"question": qid,
                    "question_text": (_trial(os.path.join(trials_root, "runs", qid)).get("context") or {}).get(
                        "question"),
                    "beliefs": [{k: b.get(k) for k in ("id", "claim", "p", "basis", "decides", "history")}
                                for b in t["beliefs"]],
                    "refuted": [{"claim": r["claim"], "observed": r["observed"]} for r in t["rows"]
                                if r["outcome"] == "refuted" and not r["guard"]],
                    "open_questions": [b["open_question"] for b in t["beliefs"] if b.get("open_question")]})
    return out


def belief_rescore(trials_root, qs, fit_batches=("1r3", "2", "3"), at=None) -> dict:
    """Every completed, current question rescored as a belief table from its stored artifacts (nothing re-run):
    each claim's p is its stated p, else the Laplace-smoothed confirmation rate of its belief's label over
    `fit_batches` (label claims only); the guard sets claims aside; surprise, calibration, Brier and log score per
    batch; beliefs updated by the stated rule; the next simulation per question. Writes analysis/belief_table.yaml per
    run and trials/belief_summary.yaml / .md."""
    import yaml
    from toolkit.analysis import belief_table as BT
    at = at or _now()
    sup = _superseded(qs, trials_root)
    runs, rows = {}, []
    for q in qs:
        rd = os.path.join(trials_root, "runs", q["id"])
        if _trial(rd).get("status") != "complete" or q["id"] in sup:
            continue
        seed, rs = _table_rows(rd, q)
        runs[q["id"]] = {"q": q, "seed": seed, "rows": rs}
        rows += rs
    scored = [r for r in rows if not r["guard"] and not r["post_hoc"]]
    rates = BT.label_rates([(r["label"], r["outcome"]) for r in scored
                            if r["batch"] in fit_batches and r["stated_p"] is None])
    lp = {k: v["p"] for k, v in rates.items()}
    for qid, x in runs.items():
        d = write_belief_table(os.path.join(trials_root, "runs", qid), x["q"], at, lp, x["rows"], x["seed"])
        x["next_simulation"] = d["next_simulation"]
    batches = sorted({r["batch"] for r in rows})
    guard_counts = {}
    for r in rows:
        if r["guard"]:
            guard_counts[r["guard"]["class"]] = guard_counts.get(r["guard"]["class"], 0) + 1
    S = {"schema": "trials.belief_summary/0", "written_at": at, "questions": len(runs), "claims": len(rows),
         "p_rule": {"labels": rates, "fit_batches": list(fit_batches), "stated_p_first": True},
         "overall": {"scores": BT.scores([dict(r, batch="all") for r in scored]).get("all", {}) if scored else {},
                     "calibration": BT.calibration(scored), "guard": guard_counts},
         "by_batch": {b: {"claims": sum(r["batch"] == b for r in rows),
                          "guarded": sum(r["batch"] == b and bool(r["guard"]) for r in rows),
                          "stated_p": sum(r["batch"] == b and r["stated_p"] is not None for r in rows),
                          "scores": BT.scores([r for r in scored if r["batch"] == b]).get(b),
                          "calibration": BT.calibration([r for r in scored if r["batch"] == b])} for b in batches},
         "top_surprises": sorted([{k: r[k] for k in ("question", "id", "claim", "label", "p", "outcome", "surprise")}
                                  for r in scored if r["surprise"] is not None], key=lambda r: -r["surprise"])[:10],
         "next_simulation": {qid: x["next_simulation"] for qid, x in runs.items()}}
    _sio().write_nofollow(trials_root, "belief_summary.yaml", yaml.safe_dump(S, sort_keys=False, width=110).encode(),
                          atomic=True)
    _sio().write_nofollow(trials_root, "belief_summary.md", belief_summary_md(S).encode(), atomic=True)
    return S


def belief_summary_md(S) -> str:
    L = ["# World-model trials: belief-table rescoring", "",
         f"{S['questions']} questions, {S['claims']} claims, rescored from stored artifacts (nothing re-run). p of a "
         f"claim = the Laplace-smoothed confirmation rate of its belief's stated confidence label over batches "
         f"{', '.join(S['p_rule']['fit_batches'])}: " +
         ", ".join(f"{k} {v['p']:.3f} ({v['confirmed']}/{v['n']})" for k, v in S["p_rule"]["labels"].items()) + ".",
         "", "## Guard", "", "| Class | Claims set aside |", "|---|---|"]
    for k, v in sorted(S["overall"]["guard"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {k} | {v} |")
    L += ["", "## Scores per batch (decided, unguarded, pre-registered claims)", "",
          "| Batch | Claims | Guarded | Decided | Brier | Log score (bits) |", "|---|---|---|---|---|---|"]
    for b, v in S["by_batch"].items():
        s = v["scores"] or {}
        L.append(f"| {b} | {v['claims']} | {v['guarded']} | {s.get('decided', 0)} | "
                 f"{'' if not s else round(s['brier'], 3)} | {'' if not s else round(s['log2'], 3)} |")
    L += ["", "## Calibration (all batches)", "", "| p-bin | Decided | Mean p | Observed |", "|---|---|---|---|"]
    for c in S["overall"]["calibration"]:
        L.append(f"| {c['bin'][0]:.1f}-{min(c['bin'][1], 1):.1f} | {c['decided']} | "
                 f"{'' if c['mean_p'] is None else round(c['mean_p'], 3)} | "
                 f"{'' if c['observed'] is None else round(c['observed'], 3)} |")
    L += ["", "## Top surprises", ""]
    for r in S["top_surprises"]:
        L.append(f"- {r['question']} {r['id']} ({r['label']}, p {r['p']:.2f}, {r['outcome']}, "
                 f"{r['surprise']:.2f} bits): {str(r['claim'])[:160]}")
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- the engine step in a VM job (owner, 2026-11-13)
BATCH_FILES = ("results.jsonl", "first_seen.jsonl", "points.jsonl")     # what an imported batch brings


def engine_only(qs, out_root, root=REPO, max_engine_runs=DEFAULT_MAX_ENGINE_RUNS) -> dict:
    """The VM job's step: for each question, prepare its run directory from the template exactly as the laptop did
    (the same commit's code and template give the same design), run the engine step (engine_step: the batch, then
    record_first_seen on the VM, when the batch first appears) and keep analysis/batch/ under out_root/runs/<id>/.
    No seed, no registration, no build: the laptop holds those."""
    out = {}
    for q in qs:
        rd = os.path.join(out_root, "runs", q["id"])
        try:
            prepare(dict(q, shadow_price=None), rd, root, max_engine_runs)
            e = engine_step(q, rd, max_engine_runs)
            out[q["id"]] = dict(e, ok=True)
        except Exception as ex:  # noqa: BLE001 -- recorded per question; the others run
            out[q["id"]] = {"ok": False, "error": f"{type(ex).__name__}: {ex}"[:400]}
        os.makedirs(out_root, exist_ok=True)
        _sio().write_nofollow(out_root, "engine_only.json", json.dumps(out, indent=1, default=str).encode(),
                              atomic=True)
        print(json.dumps({q["id"]: out[q["id"]]}, default=str), flush=True)
    return out


def import_engine(trials_root, src_root, qs, usd_total, source, ledger, repo=REPO, anchor_ref=None) -> dict:
    """The laptop's step after a VM job: for each question awaiting its engine, the job's batch (BATCH_FILES under
    src_root/runs/<id>/analysis/batch/) is copied into the local run directory when the job's design.json is the
    local one byte for byte (refused otherwise), the job's real USD is charged to the spend ledger in equal shares,
    and the question is built (build_step) and completed. The build then checks the rows' bindings (job sha256 and
    engine identity) and dates the evidence by the VM's first-seen record, after the anchor commit."""
    from toolkit.analysis.artifacts import sha256
    todo = [q for q in qs if _trial(os.path.join(trials_root, "runs", q["id"])).get("status") == "awaiting_engine"]
    share = float(usd_total) / len(todo) if todo else 0.0
    out = {}
    for q in todo:
        rd = os.path.join(trials_root, "runs", q["id"])
        t = _trial(rd)
        src = os.path.join(src_root, "runs", q["id"], "analysis", "batch")
        try:
            a, b = sha256(os.path.join(src, "design.json")), sha256(os.path.join(rd, "analysis", "batch",
                                                                                 "design.json"))
            if a is None or a != b:
                raise TrialError(f"{q['id']}: the job's design.json ({a}) is not the design the question was prepared "
                                 f"and seeded with ({b})")
            for name in BATCH_FILES:
                if os.path.isfile(os.path.join(src, name)):
                    data, _ = _sio().read_nofollow(src, name)
                    _sio().write_nofollow(rd, f"analysis/batch/{name}", data, atomic=True)
            ledger.compute(q["id"], share, source)
            t["engine"] = {"backend": source, "imported_from": os.path.abspath(src_root), "compute_usd": share}
            t["steps"]["engine"] = _now()
            t["build"] = build_step(dict(q, shadow_price=(t.get("shadow_price") or {}).get("value")), rd,
                                    anchor_ref if t.get("anchor") else None)
            t["steps"]["build"] = _now()
            write_belief_table(rd, q)                # what a chained question carries forward
            pq = ledger.by_question().get(q["id"]) or {}
            t.update(status="complete", spend_usd=round(pq.get("llm_usd", 0.0) + pq.get("compute_usd", 0.0), 6))
        except Exception as ex:  # noqa: BLE001
            t.update(status="failed", error=f"{type(ex).__name__}: {ex}"[:500])
        _save(rd, t)
        out[q["id"]] = {k: t.get(k) for k in ("status", "error", "spend_usd")}
    return out


def vm_commands(qs, per_job=5, cap_usd=3.0, prefix="wmt-b3-", store="outputs/wmt-b3") -> list[dict]:
    """The cloud/vm_job.py commands that run the engine step of `qs`, `per_job` questions per job (one VM start-up
    shared), each under a USD cap and a cluster name with the budget scope's prefix. Printed for the launch, never run
    here."""
    out = []
    for k in range(0, len(qs), per_job):
        ids = [q["id"] for q in qs[k:k + per_job]]
        name, job = f"{prefix}{k // per_job + 1}", f"eng{k // per_job + 1}"
        cmd = (f"python cloud/vm_job.py run {name} {job} --tree . --store {store}/{name} --cap-usd {cap_usd} -- "
               f"python3 scripts/world_model_trials.py engine-only --only {','.join(ids)} --out '$JOB_OUT'")
        out.append({"name": name, "job_id": job, "ids": ids, "cap_usd": cap_usd, "command": cmd})
    return out


def check_templates(qs, out_root, max_engine_runs=DEFAULT_MAX_ENGINE_RUNS) -> dict:
    """Each template as the run will use it (prepare, the engine step, world_model.load_sweep), with no LLM call and
    no push: a question that fails here fails before any spend."""
    from toolkit.analysis import world_model as W
    out, stamp = {}, dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S")
    for q in qs:
        rd = os.path.join(out_root, f"{q['id']}-{stamp}")          # a fresh directory per check: nothing is removed
        try:
            q = dict(q, shadow_price=shadow_price_of(q, REPO)["value"])
            ctx = prepare(q, rd, REPO, max_engine_runs)
            e = engine_step(q, rd, max_engine_runs)
            sw, _ = W.load_sweep(rd, "current", q.get("shadow_price"), use_plan=False)
            out[q["id"]] = {"ok": True, "jobs": e["jobs"], "wall_s": e["wall_s"], "features": list(ctx["sweep"][
                "features"]), "packages": list(sw.packages)}
        except Exception as e:  # noqa: BLE001
            out[q["id"]] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
        print(json.dumps({q["id"]: out[q["id"]]}, default=str))
    return out


def rebuild(trials_root, qs, anchor_ref=None, recheck_citations=False, lookup=None, root=None) -> dict:
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
            if recheck_citations and "citations" in t["steps"]:
                t["citations"] = citation_step(rd, lookup)
                t["steps"]["citations_rechecked"] = _now()
            lam = shadow_price_of(q, REPO if root is None else root) if q.get("template") else \
                (t.get("shadow_price") or {"value": None, "basis": "not recorded"})
            t["shadow_price"] = lam
            t["build"] = build_step(dict(q, shadow_price=lam["value"]), rd, anchor_ref if t.get("anchor") else None)
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
    for name in ("run", "check-templates", "rebuild", "score", "engine-only", "import-engine", "vm-commands",
                 "belief-rescore"):
        p = sub.add_parser(name)
        p.add_argument("--trials-dir", default=os.path.join(REPO, "trials"))
        if name not in ("score", "belief-rescore"):
            p.add_argument("--questions", default=QUESTIONS)
            p.add_argument("--only", help="comma-separated question ids")
            p.add_argument("--batch", help="only questions with this batch value")
            p.add_argument("--limit", type=int)
            p.add_argument("--max-engine-runs", type=int, default=DEFAULT_MAX_ENGINE_RUNS)
    sub.choices["rebuild"].add_argument("--recheck-citations", action="store_true",
                                        help="look the citations up again under the current rule before the build")
    r = sub.choices["run"]
    r.add_argument("--total-cap-usd", type=float, required=True, help="the cap on cumulative spend in the ledger")
    r.add_argument("--task-cap-usd", type=float, default=2.0)
    r.add_argument("--llm-concurrency", type=int, default=5)
    r.add_argument("--push", action="store_true", help="anchor registrations on origin/wm-trials (else post hoc)")
    r.add_argument("--engine", choices=("remote", "local"), default="remote",
                   help="remote (default; owner, 2026-11-13): stop at awaiting_engine for a VM job; local runs it here")
    sub.choices["engine-only"].add_argument("--out", required=True, help="the VM job's output directory ($JOB_OUT)")
    ie = sub.choices["import-engine"]
    ie.add_argument("--from", dest="src", required=True, help="the job's collected output directory")
    ie.add_argument("--usd", type=float, required=True, help="the job's real USD (vm_job's measured spend)")
    ie.add_argument("--source", required=True, help="the job: coiled:<name>/<job_id>")
    ie.add_argument("--total-cap-usd", type=float, required=True)
    vc = sub.choices["vm-commands"]
    vc.add_argument("--per-job", type=int, default=5)
    vc.add_argument("--cap-usd", type=float, required=True)
    vc.add_argument("--prefix", default="wmt-b3-")
    vc.add_argument("--store", default="outputs/wmt-b3")
    a = ap.parse_args(argv)
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    if a.cmd == "belief-rescore":
        S = belief_rescore(a.trials_dir, load_questions(QUESTIONS))
        print(json.dumps({k: S[k] for k in ("questions", "claims", "p_rule", "overall")}, indent=1, default=str))
        return 0
    if a.cmd == "score":
        S = score(a.trials_dir)
        print(json.dumps(S["overall"], indent=1, default=str))
        return 0
    qs = select(load_questions(a.questions), set(a.only.split(",")) if a.only else None, a.batch, a.limit)
    if a.cmd == "rebuild":
        res = rebuild(a.trials_dir, qs, f"refs/remotes/{REMOTE}/{BRANCH}", recheck_citations=a.recheck_citations)
        for i, r in res.items():
            print(json.dumps({i: r}, default=str))
        score(a.trials_dir)
        return 0 if all(r["status"] != "failed" for r in res.values()) else 1
    if a.cmd == "engine-only":
        res = engine_only(qs, os.path.expandvars(a.out), REPO, a.max_engine_runs)
        return 0 if all(v["ok"] for v in res.values()) else 1
    if a.cmd == "vm-commands":
        for c in vm_commands(qs, a.per_job, a.cap_usd, a.prefix, a.store):
            print(c["command"])
        return 0
    if a.cmd == "import-engine":
        res = import_engine(a.trials_dir, a.src, qs, a.usd, a.source,
                            SpendLedger(os.path.join(a.trials_dir, "spend.jsonl"), a.total_cap_usd), REPO,
                            f"refs/remotes/{REMOTE}/{BRANCH}")
        for i, r in res.items():
            print(json.dumps({i: r}, default=str))
        score(a.trials_dir)
        return 0 if all(r["status"] == "complete" for r in res.values()) else 1
    if a.cmd == "check-templates":
        res = check_templates(qs, os.path.join(a.trials_dir, "checks"), a.max_engine_runs)
        return 0 if all(v["ok"] for v in res.values()) else 1
    led = SpendLedger(os.path.join(a.trials_dir, "spend.jsonl"), a.total_cap_usd)
    anchor = GitAnchor(REPO, os.path.join(a.trials_dir, ".wm-trials"),
                       trials_rel=os.path.relpath(os.path.realpath(a.trials_dir), os.path.realpath(REPO))) \
        if a.push else None
    res = run_trials(qs, a.trials_dir, REPO, sdk_seed_fn(), led, anchor, a.task_cap_usd, a.llm_concurrency, 2,
                     a.max_engine_runs, engine=a.engine)
    for i, t in res.items():
        print(json.dumps({"id": i, "status": t.get("status"), "spend_usd": t.get("spend_usd"),
                          "error": t.get("error")}, default=str))
    print(json.dumps({"committed_usd": round(led.committed(), 4), "total_cap_usd": led.cap}))
    S = score(a.trials_dir)
    print(json.dumps(S["overall"]["hypotheses"], default=str))
    return 0 if all(t.get("status") in ("complete", "awaiting_engine") for t in res.values()) else 1


# ---------------------------------------------------------------- self-test
def _toy_template(d, points=12, wtp=True):
    """A trial template on the toy engine (the stub engine of these tests): the analysis self-tests' toy spec."""
    import yaml
    from toolkit.spec import schema
    os.makedirs(d, exist_ok=True)
    p = schema.toy_spec(d)
    raw = yaml.safe_load(open(p))
    raw["analysis"]["emulator"]["points"] = points
    with open(p, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    if wtp:
        with open(os.path.join(d, "wtp.yaml"), "w") as f:
            yaml.safe_dump({"outcome_type": "toy_daly"}, f)
    return d


def _toy_wtp_table(root):
    """The willingness-to-pay table of a toy repository: 10 USD per DALY, one DALY per toy outcome (lambda 0.1)."""
    import yaml
    os.makedirs(os.path.join(root, os.path.dirname(WTP_TABLE)), exist_ok=True)
    with open(os.path.join(root, WTP_TABLE), "w") as f:
        yaml.safe_dump({"threshold": {"usd_per_daly": 10.0, "source": "toy threshold"},
                        "outcome_types": {"toy_daly": {"dalys": 1.0, "source": "toy: one DALY per outcome"}}}, f)


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
    for h, p in zip(sd["hypotheses"], (0.7, 0.6, 0.55, 0.65, 0.85)):
        h["p"] = p                      # the probability each claim holds (owner, 2026-11-13: stated per claim)
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
    _toy_wtp_table(repo)
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

    only = os.environ.get("WMT_SELF_TEST_ONLY")       # one case locally (owner, 2026-11-13: CI runs the suite)

    def tryit(name, fn):
        if only and not name.startswith(only):
            return
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
            _toy_wtp_table(d)
            qp = os.path.join(d, "q.yaml")
            doc = {"questions": [{"id": "toy1", "question": "Which package per unit?", "engine": toy_ref,
                                  "template": "tpl/toy", "inputs": "synthetic"}]}
            yaml.safe_dump(doc, open(qp, "w"))
            got = load_questions(qp, root=d, engines={toy_ref})
            ok([q["id"] for q in got] == ["toy1"], f"WT2 a questions file loads: {[q['id'] for q in got]}")
            for mut, why in ((lambda q: q.update(engine="toolkit.engines.malaria.engine:MalariaEngine"),
                              "an engine that is not one of the cheap engines"),
                             (lambda q: q.update(template="tpl/none"), "a missing template"),
                             (lambda q: q.pop("question"), "no question text"),
                             (lambda q: q.update(id="a b/c"), "an id that is not a plain name"),
                             (lambda q: q.update(shadow_price=0.1), "a shadow price set in the questions file (the "
                              "template pre-registers its willingness to pay)")):
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
               "template": f"data/tpl/toy{i}", "inputs": "synthetic"} for i in range(n)]
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
                   "template": f"data/tpl/toy{i}", "inputs": "synthetic"} for i in range(3)]
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
            # batch 2 withheld the shadow price (templates had placed it from a check sweep). From batch 4 on (owner,
            # 2026-11-13) it is the pre-registered willingness to pay, question-independent: shown with its basis
            sp = ctx["sweep"].get("shadow_price") or {}
            ok(isinstance(sp, dict) and sp.get("value") == 0.1 and "toy threshold" in str(sp.get("basis"))
               and ctx["sweep"].get("paired_tests") == "testable",
               f"WT10d the seed is shown the pre-registered willingness to pay as the shadow price, with its basis: "
               f"{sp!r}")
            w = prepare({"id": "toy0", "question": "Which package per unit?", "engine": toy_ref,
                         "template": "data/tpl/toy0", "shadow_price": 0.1, "withhold_shadow_price": True},
                        os.path.join(d, "withheld"), repo)
            ok(isinstance(w["sweep"]["shadow_price"], str) and "0.1" not in w["sweep"]["shadow_price"],
               f"WT10e a question marked withhold_shadow_price (batches 1-3, seeded so) keeps it withheld: "
               f"{w['sweep']['shadow_price']!r}")
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
            got = circular_tests(rd, sd, 0.1)
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
            out = rebuild(troot, [{"id": "toy0"}, {"id": "missing"}],
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

    def wt14e():
        # batch 3, ebola_tracing_threshold H8: pop and seed_share were drawn from separate streams, yet their realized
        # rank correlation over the 24 units was 0.82, so the "independent" pop stood in for seed_share
        from toolkit.analysis import batch as B
        from toolkit.spec import schema
        with tempfile.TemporaryDirectory() as d:
            _indep_template(d, {"features": ["cov", "tgt"], "generator": "generate_units.py", "basis": "b"})
            p = os.path.join(d, "decision_spec.yaml")
            raw = yaml.safe_load(open(p))
            raw["analysis"]["emulator"]["features"] = ["cov", "tgt"]      # tgt is a function of x, as cov nearly is
            with open(p, "w") as f:
                yaml.safe_dump(raw, f, sort_keys=False)
            try:
                B.design(schema.load(p))
                ok(False, "WT14e declared features that are correlated in the realized units table are refused")
            except ValueError as e:
                ok("correlat" in str(e), f"WT14e declared features that are correlated in the realized units table are "
                                         f"refused: {str(e)[:160]}")
    tryit("WT14e realized independence", wt14e)

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
        ok(conf == {"B1": "high", "B2": "medium", "B3": "medium", "B4": "medium"} and
           out["beliefs"][1]["citation_check"]["confidence_claimed"] == "medium",
           f"WT15b (owner rule 2026-11-13) a citation not found is a flag and keeps its belief's confidence; a failed lookup is recorded "
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

    # Owner, 2026-11-13 (batch 3): citation matching fixed on round 2's false negatives; not found is a flag only;
    # "not a citation" lowers confidence; willingness to pay pre-registered; prior echo and engine-blind tests apart.
    def wt17():
        fx = json.load(open(os.path.join(REPO, "data", "world_model_trials", "_fixtures", "citation_lookups.json")))
        found = {k for k, v in fx.items()
                 if any(citation_match(v["citation"], p) for rows in v["responses"].values() for p in rows)}
        ok(found == {"baeten", "grant", "landovitz", "delany", "vanthoog", "who_ebola", "who_tb"},
           f"WT17 round 2's false negatives match their papers in the recorded OpenAlex responses (punctuation, "
           f"apostrophes, hyphens, subtitles, organisation authors); the anonymous 1978 note and the WHO "
           f"schistosomiasis guideline (only papers about it are indexed) stay not found: {sorted(found)}")
        bad = {"Celum C. Faculty Opinions recommendation", "Gatherer D. The unprecedented scale"}
        ok(not any(citation_match(fx["who_schisto"]["citation"], p) for rows in fx["who_schisto"]["responses"].values()
                   for p in rows), f"WT17b a paper about a guideline is not the guideline: {bad}")
        nac = ["engine card: rate = baseline x M(package) / M(baseline coverage)",
               "question arithmetic (coverage 0.70 vs 0.85, price 3.00 vs 4.50); no external source",
               "data: units.csv column rv_share", "Engine card in the run's task description"]
        real = [v["citation"] for v in fx.values()]
        ok(all(citation_class(c) == "not_a_citation" for c in nac) and not any(citation_class(c) for c in real),
           f"WT17c an engine card, question arithmetic or a data column is not a citation; real references are: "
           f"{[citation_class(c) for c in nac]}")
        seed = {"beliefs": [
            {"id": "B1", "source_type": "literature", "confidence": "high",
             "evidence": [{"citation": "Nobody N. An unindexed paper title here. J X 2020", "location": "Table 1"}]},
            {"id": "B2", "source_type": "literature", "confidence": "high",
             "evidence": [{"citation": nac[1], "location": "p. 1"}]}]}
        out, rec = verify_citations(seed, lookup=lambda q: [], pause_s=0)
        conf = {b["id"]: b["confidence"] for b in out["beliefs"]}
        st = {r["belief"]: r["status"] for r in rec}
        ok(conf == {"B1": "high", "B2": "low"} and st == {"B1": "not_found", "B2": "not_a_citation"}
           and out["beliefs"][0]["citation_check"]["not_found"] == 1,
           f"WT17d not found is a flag (confidence kept); not a citation lowers it to low: {conf} {st}")
        sleeps, srcs = [], []

        def s2_429(q, author=None):
            raise RateLimited("429")
        look = Lookup([("semantic_scholar", s2_429), ("openalex", lambda q, a=None: srcs.append("oa") or []),
                       ("crossref", lambda q, a=None: srcs.append("cr") or
                        [{"paperId": "doi:1", "title": "An unindexed paper title here", "year": 2020,
                          "authors": [{"name": "N Nobody"}]}])], breaker=2)
        p, tried = look.find("Nobody N. An unindexed paper title here. J X 2020")
        ok(p and p["source"] == "crossref" and tried == ["semantic_scholar", "openalex", "crossref"],
           f"WT17e sources in order: Semantic Scholar, then OpenAlex, then Crossref, until one matches: {tried}")

        class Opener:
            def __call__(self, req, timeout=None):
                import urllib.error
                raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, None)
        try:
            s2_lookup("x", pause_s=0, opener=Opener(), sleep=sleeps.append)
            ok(False, "WT17f a rate-limited Semantic Scholar backs off exponentially, then gives way")
        except RateLimited:
            ok(sleeps == [2, 4, 8, 16], f"WT17f a rate-limited Semantic Scholar backs off exponentially, then gives "
                                         f"way: {sleeps}")
    tryit("WT17 citations, round 3", wt17)

    def wt17g():
        names = [n for n, _ in Lookup().sources]
        old = os.environ.pop("OPENALEX_MAILTO", None)
        try:
            u0 = openalex_url("a title", {"year": 2020})
            os.environ["OPENALEX_MAILTO"] = "someone@example.org"
            u1 = openalex_url("a title", {"year": 2020})
        finally:
            os.environ.pop("OPENALEX_MAILTO", None)
            if old is not None:
                os.environ["OPENALEX_MAILTO"] = old
        ok(names == ["openalex", "semantic_scholar", "crossref"] and "mailto" not in u0
           and "mailto=someone%40example.org" in u1,
           f"WT17g OpenAlex first, then Semantic Scholar, then Crossref; OpenAlex's polite-pool mailto comes only from "
           f"OPENALEX_MAILTO in the environment (no address in code or files), and the lookup works without it: "
           f"{names}")
    tryit("WT17g lookup order and mailto", wt17g)

    def wt18():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed())
            t = json.load(open(os.path.join(troot, "runs", "toy0", TRIAL)))
            ok(t["shadow_price"]["value"] == 0.1 and "toy threshold" in t["shadow_price"]["basis"],
               f"WT18 the shadow price is the template's pre-registered willingness to pay (DALYs per outcome over the "
               f"published threshold), recorded with its basis: {t['shadow_price']}")
            _toy_template(os.path.join(repo, "nowtp"), wtp=False)
            lam = shadow_price_of({"template": "nowtp"}, repo)
            ok(lam["value"] is None, f"WT18b a template without a willingness to pay has no shadow price: {lam}")
            with open(os.path.join(repo, "nowtp", "wtp.yaml"), "w") as f:
                yaml.safe_dump({"outcome_type": "unknown_outcome"}, f)
            try:
                shadow_price_of({"template": "nowtp"}, repo)
                ok(False, "WT18c an outcome type missing from the willingness-to-pay table is refused")
            except TrialError:
                ok(True, "WT18c an outcome type missing from the willingness-to-pay table is refused")
    tryit("WT18 willingness to pay", wt18)

    def wt19():
        with tempfile.TemporaryDirectory() as d:
            repo = _toy_repo(d)
            tpl = os.path.join(repo, "data", "tpl", "toy0")
            _indep_template(tpl, None)
            qs = [{"id": "toy0", "question": "Which package per unit?", "engine": toy_ref, "template": "data/tpl/toy0",
                   "inputs": "synthetic"}]
            sd = _toy_seed()
            sd["beliefs"].append({"id": "B7", "mechanism": "a's effect does not depend on cov",
                                  "about": {"package": "a", "feature": "cov"}, "direction": "negligible",
                                  "source_type": "literature", "confidence": "low",
                                  "evidence": [{"citation": "Toy (2020) J Toy 1:1", "location": "Table 1"}]})
            sd["beliefs"].append({"id": "B8", "mechanism": "a's effect rises with x",
                                  "about": {"package": "a", "feature": "x (calibration scale)"}, "direction": "increases",
                                  "source_type": "literature", "confidence": "low",
                                  "evidence": [{"citation": "Toy (2020) J Toy 1:1", "location": "Table 1"}]})
            sd["hypotheses"] = [h for h in sd["hypotheses"] if h["id"] in ("H1", "H4")] + [
                {"id": "E1", "belief": "B1", "claim": "a reduces by 70-90%", "p": 0.4,
                 "test": {"kind": "effect_band", "package": "a", "band": [0.7, 0.9], "min_share": 0.7}},
                {"id": "E2", "belief": "B7", "claim": "cov does not change a's effect", "p": 0.6,
                 "test": {"kind": "effect_direction", "package": "a", "feature": "cov", "direction": "negligible",
                          "equivalence": 0.2}},
                {"id": "E3", "belief": "B8", "claim": "a's effect rises with x", "p": 0.6,
                 "test": {"kind": "effect_direction", "package": "a", "feature": "x (calibration scale)",
                          "direction": "increases", "scope": "marginal"}}]

            def seed_fn(run_dir, ctx, feedback, cap_usd):
                from agents.v2_tasks import TaskResult
                with open(os.path.join(run_dir, "world_model_seed.yaml"), "w") as f:
                    yaml.safe_dump(sd, f, sort_keys=False)
                return TaskResult(True, {"ok": True}, 0.25, "stub")
            troot = os.path.join(repo, "trials")
            res = run_trials(qs, troot, repo=repo, seed_fn=seed_fn, ledger=SpendLedger(os.path.join(troot, "s.jsonl"),
                                                                                       20.0),
                             anchor=GitAnchor(repo, os.path.join(troot, ".wm-trials")), engines={toy_ref},
                             citation_lookup=lambda q: [])
            rd = os.path.join(troot, "runs", "toy0")
            if res["toy0"]["status"] != "complete":
                print("WT19 run:", res["toy0"].get("status"), res["toy0"].get("error"), res["toy0"].get("seed"))
            hy = {h["id"]: h for h in yaml.safe_load(open(os.path.join(rd, "analysis", "hypotheses.yaml")))["hypotheses"]}
            cls = {k: (h["confrontation"].get("set_aside") or {}).get("class") for k, h in hy.items()}
            ok(res["toy0"]["status"] == "complete" and cls == {"H1": "circular", "H4": None, "E1": "prior_echo",
                                                               "E2": "engine_blind", "E3": None},
               f"WT19 a band on a reduction its declared effect prior fixes is prior echo (circular when its range lies "
               f"inside the band); a direction test on a feature the engine never reads is engine-blind: {cls}")
            ok(hy["E2"]["status"] == "untestable" and "never reads" in str(hy["E2"]["result"]["observed"]),
               f"WT19b an engine-blind test is untestable by this engine, not inconclusive: {hy['E2']['status']}")
            wm = yaml.safe_load(open(os.path.join(rd, "analysis", "world_model.yaml")))
            tr = wm["track_record"]
            b1 = [b for b in wm["beliefs"] if b["id"] == "B1"][0]
            ok("prior_echo" in tr and "engine_blind" in tr and "E1" not in (b1.get("confirmed_by") or []) +
               (b1.get("contradicted_by") or []),
               f"WT19c prior echo and engine-blind tests are counted apart and never move confidence: "
               f"{ {k: tr.get(k) for k in ('circular', 'prior_echo', 'engine_blind')} }")
            S = score(troot)
            q = S["questions"]["toy0"]["hypotheses"]
            ok(q.get("prior_echo") == 1 and q.get("engine_blind") == 1 and q.get("circular") == 1,
               f"WT19d the summary counts circular, prior-echo and engine-blind tests: {q}")
    tryit("WT19 prior echo and engine-blind", wt19)

    def wt19e():
        with tempfile.TemporaryDirectory() as d:
            tpl = os.path.join(d, "tpl")
            _indep_template(tpl, None)
            with open(os.path.join(tpl, ENGINE_READS), "w") as f:
                yaml.safe_dump({"features_read_via": {"cov": "x"}}, f)
            ok(engine_unread_features(tpl) == set(),
               "WT19e a declared feature that reaches the engine through a derived bound attribute (engine_reads.yaml "
               "features_read_via) is read, not engine-blind")
            with open(os.path.join(tpl, ENGINE_READS), "w") as f:
                yaml.safe_dump({"features_read_via": {"cov": "nothing_bound"}}, f)
            try:
                engine_unread_features(tpl)
                ok(False, "WT19f a features_read_via naming no engine attribute or bound column is refused")
            except TrialError:
                ok(True, "WT19f a features_read_via naming no engine attribute or bound column is refused")
    tryit("WT19e engine reads", wt19e)

    def wt20():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed())
            rd = os.path.join(troot, "runs", "toy0")
            sp = os.path.join(rd, SEED)
            s = yaml.safe_load(open(sp))                         # as round 2 left it: not found lowered confidence
            s["beliefs"][0].update(confidence="low", citation_check={"confidence_claimed": "high", "found": 0,
                                                                     "not_found": 1, "lookup_failed": 0})
            with open(sp, "w") as f:
                yaml.safe_dump(s, f, sort_keys=False)
            rebuild(troot, [{"id": "toy0"}], anchor_ref="refs/remotes/origin/wm-trials", recheck_citations=True,
                    lookup=lambda q: [])
            b = yaml.safe_load(open(sp))["beliefs"][0]
            ok(b["confidence"] == "high" and b["citation_check"]["not_found"] == 1,
               f"WT20 a rebuild under the new rule restores the claimed confidence of a belief whose citation was not "
               f"found, keeping the flag: {b['confidence']}, {b['citation_check']}")
    tryit("WT20 recheck citations", wt20)

    # Owner, 2026-11-13: no engine compute on the laptop. The engine step runs in a VM job (cloud/vm_job.py) on the
    # questions' prepared run directories; the laptop seeds, registers and anchors first, then imports and builds.
    def wt21():
        with tempfile.TemporaryDirectory() as d:
            repo = _toy_repo(d)
            for i in range(2):
                _toy_template(os.path.join(repo, "data", "tpl", f"toy{i}"))
            qs = [{"id": f"toy{i}", "question": "Which package per unit?", "engine": toy_ref,
                   "template": f"data/tpl/toy{i}", "inputs": "synthetic"} for i in range(2)]

            def seed_fn(run_dir, ctx, feedback, cap_usd):
                from agents.v2_tasks import TaskResult
                with open(os.path.join(run_dir, "world_model_seed.yaml"), "w") as f:
                    yaml.safe_dump(_toy_seed(), f, sort_keys=False)
                return TaskResult(True, {"ok": True}, 0.25, "stub")
            troot = os.path.join(repo, "trials")
            L = SpendLedger(os.path.join(troot, "spend.jsonl"), 20.0)
            A = GitAnchor(repo, os.path.join(troot, ".wm-trials"))
            probe = []
            res = run_trials(qs, troot, repo=repo, seed_fn=seed_fn, ledger=L, anchor=A, engines={toy_ref},
                             citation_lookup=lambda q: [], engine="remote", engine_probe=probe.append)
            rd = os.path.join(troot, "runs", "toy0")
            ok(all(r["status"] == "awaiting_engine" for r in res.values()) and not probe
               and not os.path.exists(os.path.join(rd, "analysis", "batch", "results.jsonl")),
               f"WT21 with the engine remote the laptop seeds, registers and anchors, and runs no engine: "
               f"{ {k: r['status'] for k, r in res.items()} }")
            cmds = vm_commands(qs, per_job=1, cap_usd=3.0, prefix="wmt-b3-", store="outputs/wmt-b3")
            ok(len(cmds) == 2 and all(c["name"].startswith("wmt-b3-") and "--cap-usd 3.0" in c["command"] and
                                      "engine-only" in c["command"] and "$JOB_OUT" in c["command"] for c in cmds)
               and sorted(i for c in cmds for i in c["ids"]) == ["toy0", "toy1"],
               f"WT21b the VM jobs: prefix, a USD cap, the engine-only step into $JOB_OUT, each question once: "
               f"{[c['name'] for c in cmds]}")
            vm = os.path.join(d, "vm_out")                       # what the VM job writes (run here on the toy engine)
            out = engine_only(qs, vm, root=repo)
            ok(all(v["ok"] for v in out.values()), f"WT21c the VM's engine-only step: {out}")
            res2 = import_engine(troot, vm, qs, usd_total=0.5, source="coiled:wmt-b3-1/j1", ledger=L, repo=repo,
                                 anchor_ref=A.ref)
            hy = yaml.safe_load(open(os.path.join(rd, "analysis", "hypotheses.yaml")))
            comp = [json.loads(x) for x in open(os.path.join(troot, "spend.jsonl")) if '"compute"' in x]
            ok(all(r["status"] == "complete" for r in res2.values())
               and all(h["confrontation"]["registration_status"] == "remote-anchored" for h in hy["hypotheses"])
               and sorted((e["question"], e["usd"], e["source"]) for e in comp) ==
               [("toy0", 0.25, "coiled:wmt-b3-1/j1"), ("toy1", 0.25, "coiled:wmt-b3-1/j1")],
               f"WT21d imported results are built (anchored before the VM's evidence) and the job's real USD is in the "
               f"spend ledger, split over its questions: {[(e['question'], e['usd']) for e in comp]}")
            bad = os.path.join(vm, "runs", "toy1", "analysis", "batch", "design.json")
            dd = json.load(open(bad))
            dd["points"] = dd["points"][:-1]
            with open(bad, "w") as f:
                json.dump(dd, f)
            t1 = json.load(open(os.path.join(troot, "runs", "toy1", TRIAL)))
            t1.update(status="awaiting_engine")
            t1["steps"].pop("engine", None)
            with open(os.path.join(troot, "runs", "toy1", TRIAL), "w") as f:
                json.dump(t1, f)
            res3 = import_engine(troot, vm, qs[1:], usd_total=0.0, source="x", ledger=L, repo=repo, anchor_ref=A.ref)
            ok(res3["toy1"]["status"] == "failed" and "design" in res3["toy1"]["error"],
               f"WT21e an imported batch whose design is not the one the question was prepared with is refused: "
               f"{res3['toy1']}")
    tryit("WT21 remote engine", wt21)

    def wt22():
        S = {"overall": {"questions": {"complete": 1}, "spend_usd": {"llm": 1.0, "compute": 0.0},
                         "hypotheses": {"written": 1, "circular": 0, "prior_echo": 0, "engine_blind": 0},
                         "citations": {"questions_checked": 1, "found": 3, "not_found": 2, "not_a_citation": 1,
                                       "lookup_failed": 0},
                         "confidence": {}, "failure_modes": {}, "where_to_look": {"decision_relevant": 0,
                                                                                    "questions": 0}},
             "questions": {}}
        md = summary_md(S)
        ok("set to low" not in md and "2 not found (a flag only" in md and "1 not a citation (lowered to low" in md,
           "WT22 summary.md states the citation rule in force: not found is a flag only, not a citation lowers "
           "confidence")
    tryit("WT22 summary wording", wt22)

    # Owner, 2026-11-13: one guard replaces circular / prior echo / engine-blind / leaked price / threshold arithmetic;
    # the belief table rescoring reads the runs' stored artifacts only.
    def wt23():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed())
            rd = os.path.join(troot, "runs", "toy0")
            seed = yaml.safe_load(open(os.path.join(rd, SEED)))
            g = guard_reasons(rd, seed, 0.1)
            # toy packages a and b reduce by exactly their declared effects: H1, H2, H5 bands (circular), H3 (an effect
            # against its own parameter) and H4 (a vs b at the declared costs and lambda: arithmetic on inputs)
            ok(set(g) == {"H1", "H2", "H3", "H4", "H5"} and g["H4"]["class"] == "input_determined",
               f"WT23 the guard sets aside every claim the declared inputs decide, including a paired comparison of "
               f"input-determined packages at the declared prices and lambda: { {k: v['class'] for k, v in g.items()} }")
            out = belief_rescore(troot, [{"id": "toy0", "batch": "3"}], fit_batches=("3",))
            t = yaml.safe_load(open(os.path.join(rd, "analysis", "belief_table.yaml")))
            ok(all(r.get("guard") for r in t["rows"]) and out["overall"]["scores"] == {}
               and os.path.isfile(os.path.join(troot, "belief_summary.md")),
               f"WT23b the rescoring writes the belief table and summary; guarded claims are not scored: "
               f"{[r.get('guard') for r in t['rows']]}")
    tryit("WT23 guard and belief table", wt23)

    # Owner, 2026-11-13: seeds state p per claim; a learning chain carries earlier questions' belief tables.
    def wt24():
        ctx = {"packages": {"none": [], "a": ["a"], "b": ["b"], "a+b": ["a", "b"]},
               "sweep": {"features": {"x (calibration scale)": {}, "eff_a": {}, "eff_b": {}}},
               "datasets": [{"file": "units.csv", "columns": ["id"]}], "hypotheses": {"min": 5, "max": 8,
                                                                                    "p_required": True}}
        sd = _toy_seed()
        for h in sd["hypotheses"]:
            h.pop("p", None)
        bad = seed_problems(sd, ctx)
        ok(any("p" in x and "H1" in x for x in bad), f"WT24 a hypothesis without its probability p is refused: {bad[:2]}")
        for h, p in zip(sd["hypotheses"], (0.7, 0.6, 0.55, 0.8, 0.9)):
            h["p"] = p
        ok(not seed_problems(sd, ctx), f"WT24b with p in (0, 1) per claim the seed is accepted: {seed_problems(sd, ctx)}")
        sd["hypotheses"][0]["p"] = 1.0
        ok(any("H1" in x for x in seed_problems(sd, ctx)), "WT24c p of exactly 0 or 1 is refused (a claim that cannot "
                                                          "surprise)")
    tryit("WT24 p per claim", wt24)

    def wt25():
        with tempfile.TemporaryDirectory() as d:
            repo, troot, res, calls, L, _ = e2e(d, lambda q, k: _toy_seed())
            rd0 = os.path.join(troot, "runs", "toy0")
            t = write_belief_table(rd0, {"id": "toy0", "batch": "c"}, at="2026-11-13T00:00:00+00:00")
            ok(t["rows"] and all("p" in r for r in t["rows"]), "WT25 a question's belief table is written on its own")
            q1 = {"id": "toy1", "question": "Which package per unit?", "engine": toy_ref, "template": "data/tpl/toy0",
                  "inputs": "synthetic", "carry_from": ["toy0"]}
            ctx = prepare(q1, os.path.join(troot, "runs", "toy1"), repo, trials_root=troot)
            pw = ctx.get("prior_world_model") or []
            ok(len(pw) == 1 and pw[0]["question"] == "toy0" and pw[0]["beliefs"] and "refuted" in pw[0],
               f"WT25b a chained question's seed context carries the earlier belief tables (beliefs with updated p, "
               f"refutations, open questions): {[list(x) for x in pw]}")
            try:
                prepare(dict(q1, id="toy2", carry_from=["missing"]), os.path.join(troot, "runs", "toy2"), repo,
                        trials_root=troot)
                ok(False, "WT25c a carry_from question without a belief table is refused")
            except TrialError:
                ok(True, "WT25c a carry_from question without a belief table is refused")
    tryit("WT25 learning chain", wt25)

    def wt25d():
        # chain step 2's first VM job failed: engine_only prepared the question with its carry_from, and the VM holds
        # no earlier belief table. The engine step needs the design only.
        with tempfile.TemporaryDirectory() as d:
            repo = _toy_repo(d)
            _toy_template(os.path.join(repo, "data", "tpl", "toy0"))
            q = {"id": "toyc", "question": "Which package per unit?", "engine": toy_ref, "template": "data/tpl/toy0",
                 "inputs": "synthetic", "carry_from": ["not_on_this_machine"]}
            out = engine_only([q], os.path.join(d, "vm"), root=repo)
            ok(out["toyc"]["ok"], f"WT25d the VM's engine step ignores carry_from (it needs the design only): {out}")
    tryit("WT25d engine-only with carry_from", wt25d)

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
