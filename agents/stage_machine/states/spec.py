"""The SPEC state handler, registered by name.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import analysis_backend as AB
import findings_ledger as FL
import fix_loop as FX

from ..common import (
    AWAITING_SPEC_AMENDMENT, MAX_SPEC_TRIES, REPO, SPEC_RETHINK_REQUEST, STATE_FILE, _now, _sha256, _state_bytes,
    _write_json, load_state, save_state,
)
from ..amend import (
    AMENDED_PROVISIONAL, AMENDMENT_RECORD, AmendmentLocked, _amend_lock, _child_of, _made_by, _state_or_none,
)
from ..signoff import _spec_signed_sha, signed_hash, spec_signed_once
from . import handler


# ---- provenance of inputs
_INPUT_EXT = (".csv", ".tsv", ".json", ".yaml", ".yml", ".txt", ".xlsx", ".parquet")
_TRAILING_PUNCT = ".,;:!?)]}>\"'`"
_LEADING_PUNCT = "([{<\"'`"
PROVENANCE = "data/inputs_provenance.json"


def _provenance(run_dir: str) -> dict:
    try:
        return json.load(open(os.path.join(run_dir, PROVENANCE)))
    except (OSError, ValueError):
        return {"inputs": {}, "cited": {}}


def answer_key_path(rel: str) -> bool:
    """A repository path into the evaluation tree. Compared case-insensitively: APFS and NTFS are
    case-insensitive by default, so EVALS/q/rubric.yaml is evals/q/rubric.yaml."""
    parts = [p.lower() for p in rel.replace(os.sep, "/").split("/") if p not in ("", ".")]
    return bool(parts) and (parts[0] == "evals" or "_truth" in parts)


def _path_candidate(tok: str) -> str | None:
    """A question token as a data file name, or None. A token that ends in a data extension is kept as written (a
    quoted "[draft] x.csv" keeps its bracket); otherwise its sentence punctuation comes off first ("... yields.csv."
    ends a sentence), unless a repository file has the exact name (never substituted by another). Only a leading
    "./" is dropped: "../x.csv" and "/x.csv" stay as written, so stage_inputs refuses them as outside the
    repository."""
    t = tok.strip()
    if not t.lower().endswith(_INPUT_EXT):
        if os.path.isfile(os.path.join(REPO, t)):
            return None
        t = t.rstrip(_TRAILING_PUNCT).lstrip(_LEADING_PUNCT).strip()
    while t.startswith("./"):
        t = t[2:]
    return t if t.lower().endswith(_INPUT_EXT) else None


def _question_paths(question: str) -> list[str]:
    """File names in the question, each once: quoted or backticked ones (which may hold spaces), and bare tokens. The
    text of a quoted name is not scanned again for bare tokens (a quoted evals/x y.csv never stages a repo-root
    y.csv); a quoted span that names no file (between two apostrophes) still is."""
    import re
    q = question or ""
    out, bare = [], list(q)
    for m in re.finditer(r"[`\"']([^`\"'\n]+)[`\"']", q):
        c = _path_candidate(m.group(1))
        if c is not None:
            out.append(c)
            bare[m.start():m.end()] = " " * (m.end() - m.start())
    out += [c for c in map(_path_candidate, re.findall(r"[A-Za-z0-9_./-]+", "".join(bare))) if c is not None]
    return list(dict.fromkeys(out))


def _safe_dest(run_dir: str, rel: str) -> str:
    """The destination path inside the run, refusing any symlink on the way and any existing link."""
    root = os.path.realpath(run_dir)
    cur = root
    for part in rel.split("/"):
        cur = os.path.join(cur, part)
        if os.path.islink(cur):
            raise ValueError(f"{rel}: {os.path.relpath(cur, root)} is a symlink")
    if os.path.exists(cur) and os.stat(cur).st_nlink > 1:
        raise ValueError(f"{rel}: the destination is a hard link")
    if not os.path.realpath(cur).startswith(root + os.sep):
        raise ValueError(f"{rel}: outside the run directory")
    return cur


def _copy_nofollow(src: str, dest: str, run_dir: str) -> None:
    """Copy a repo file into the run dir, published through lib.safe_io (no link at any component, atomic)."""
    data = open(src, "rb").read()
    from lib.safe_io import write_nofollow
    write_nofollow(run_dir, os.path.relpath(dest, run_dir).replace(os.sep, "/"), data, atomic=True)


def stage_inputs(run_dir: str, question: str) -> list[str]:
    """Copy every repository file the question names into data/inputs/ (same relative path), with its
    sha256 in data/inputs_provenance.json. Never the evaluation tree (evals/, _truth, any case), and
    never through a link: a symlinked directory or a linked destination is refused (ValueError)."""
    staged = []
    prov = {"inputs": {}, "cited": _provenance(run_dir).get("cited") or {}}
    root = os.path.realpath(REPO)
    for tok in _question_paths(question):
        src = os.path.realpath(os.path.join(root, tok))
        rel = os.path.relpath(src, root).replace(os.sep, "/")
        if (not src.startswith(root + os.sep) or not os.path.isfile(src) or answer_key_path(rel)
                or answer_key_path(tok) or rel.lower().startswith("runs/")):
            continue
        dest_rel = f"data/inputs/{rel}"
        dest = _safe_dest(run_dir, dest_rel)
        parent = os.path.dirname(dest)
        os.makedirs(parent, exist_ok=True)
        _safe_dest(run_dir, dest_rel)                      # nothing linked was created on the way
        _copy_nofollow(src, os.path.join(os.path.realpath(run_dir), dest_rel), os.path.realpath(run_dir))
        prov["inputs"][dest_rel] = {"source": rel, "sha256": _sha256(src), "bytes": os.path.getsize(src)}
        staged.append(dest_rel)
    _safe_dest(run_dir, PROVENANCE)
    os.makedirs(os.path.join(run_dir, "data"), exist_ok=True)
    _write_json(run_dir, PROVENANCE, prov)
    return staged


def record_cited_inputs(run_dir: str) -> list[str]:
    """Every file decision_spec.yaml cites must be inside the run directory; each is recorded with
    its sha256 in data/inputs_provenance.json. Returns the problems (pre-sign)."""
    import yaml
    import pipeline as P
    try:
        spec = yaml.safe_load(open(os.path.join(run_dir, AB.SPEC_FILE))) or {}
    except Exception as e:  # noqa: BLE001
        return [f"{AB.SPEC_FILE} unreadable: {type(e).__name__}"]
    found = P.cited_paths(spec, run_dir)       # one walk, shared with the derived-input checks
    prov, probs, root = _provenance(run_dir), [], os.path.realpath(run_dir)
    # The staged copies must still be the files the question named (the draft task may not touch
    # them; this checks it).
    for dest, e in (prov.get("inputs") or {}).items():
        p = os.path.join(run_dir, dest)
        src = os.path.join(REPO, e.get("source", ""))
        if not os.path.isfile(p) or os.path.islink(p) or _sha256(p) != e.get("sha256"):
            probs.append(f"the staged input {dest} was changed or removed after it was copied")
        elif os.path.isfile(src) and _sha256(src) != e.get("sha256"):
            probs.append(f"the staged input {dest} no longer matches its source {e.get('source')}")
    for rel in sorted(set(found)):
        p = os.path.realpath(os.path.join(run_dir, rel))
        if not p.startswith(root + os.sep):
            probs.append(f"the spec cites {rel}, outside the run directory: copy it into data/")
        elif os.path.isdir(p):
            prov.setdefault("cited", {})[os.path.relpath(p, root).replace(os.sep, "/") + "/"] = {"dir": True}
        elif not os.path.isfile(p):
            probs.append(f"the spec cites {rel}, which does not exist")
        else:
            prov.setdefault("cited", {})[os.path.relpath(p, root).replace(os.sep, "/")] = {"sha256": _sha256(p)}
    os.makedirs(os.path.join(run_dir, "data"), exist_ok=True)
    _write_json(run_dir, PROVENANCE, prov)
    return probs
LEGACY_BRIEF_SIGNOFF = ("decision_brief.signoff.json",)      # scripts/decision_brief.py SIGNOFF (the v1 brief)


def rethink_request(run_dir: str, round_n: int, signoff: str = "owner") -> dict:
    """spec_rethink_request.json: why the run asks for a spec amendment (the latest fix plan's rethink briefs, the
    open findings the spec owns, with their remedies) and the signed spec it would amend (its sha256)."""
    briefs = []
    try:          # this round's fix plan only: an earlier round's (an engine rethink) is not this request's (#261 P2-6)
        plan = json.load(open(os.path.join(run_dir, f"fix_plan_r{round_n}.json"))).get("plan") or {}
    except (OSError, ValueError, AttributeError):
        plan = {}
    briefs = [{"task": t.get("id"), "findings": t.get("findings"), "brief": t.get("brief")}
              for t in plan.get("tasks") or [] if isinstance(t, dict) and t.get("action") == "rethink"
              and t.get("owner_state") == "SPEC"]
    led = FL.load(run_dir)
    fs = [{"id": f.id, "severity": f.severity, "claim": f.claim, "fix_requires": f.fix_requires}
          for f in FL.open_findings(led) if f.owner_state == "SPEC" or any(f.id in (b["findings"] or []) for b in briefs)]
    try:
        prev = json.load(open(os.path.join(run_dir, SPEC_RETHINK_REQUEST)))
    except (OSError, ValueError):
        prev = None
    if isinstance(prev, dict) and prev.get("round") == round_n:
        return prev                  # a resume returns the request it made (Codex review of #261 P1-2)
    # lead-gaps β: the rethink's machine-readable amendment, under --signoff lead only (an owner keeps `amend --from-request
    # --patch` for their own patch: amend refuses a request patch beside --patch; Codex r1)
    patch = FX.spec_patch(plan) if signoff == "lead" else None
    doc = {"round": round_n, "requested_at": _now(), "signed_spec_sha256": _spec_signed_sha(run_dir),
           "working_spec_sha256": signed_hash(run_dir), "briefs": briefs, **({"patch": patch} if patch else {}),
           "findings": fs, "next": "amend decision_spec.yaml and relaunch with reuse (a new run dir from this one's "
                                    "spec and stored rows); this run does not redraft or re-sign its spec"}
    _write_json(run_dir, SPEC_RETHINK_REQUEST, doc)
    return doc


def _draft_file_problems(run_dir: str) -> list[str]:
    """The spec (required) and analysis_settings.yaml (optional) must each be a regular file with one link: the pre-sign
    checks read them by path, so a symbolic or hard link would show them another file's content (Codex r4 P1-5)."""
    import stat
    out = []
    for rel, required in ((AB.SPEC_FILE, True), (AB.SETTINGS_FILE, False)):
        try:
            st = os.lstat(os.path.join(run_dir, rel))
        except FileNotFoundError:
            if required:
                out.append(f"spec unreadable: {rel} does not exist")
            continue
        except OSError as e:
            out.append(f"{rel} cannot be read: {type(e).__name__}")
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            out.append(f"{rel} is a link or not a regular file: write it as a plain file")
        elif st.st_nlink > 1:
            out.append(f"{rel} has {st.st_nlink} hard links: write it as its own file")
    return out


def _special_files(run_dir: str, limit: int = 20000) -> list[str]:
    """Every entry in the run directory that is not a regular file, a directory or a link to one (a FIFO, a device, a
    socket), as problems: a check or a derivation that opens one can block with no writer. The whole run is walked
    (no link followed), so the result does not depend on how a path in the spec is spelled (Codex r1 on #350). Fail
    closed past `limit` entries."""
    import stat
    out, n = [], 0
    for base, dirs, files in os.walk(run_dir):
        for name in dirs + files:
            n += 1
            if n > limit:
                return out + [f"the run directory has more than {limit} entries: its files were not checked"]
            p = os.path.join(base, name)
            try:
                mode = os.stat(p).st_mode           # a link: its target's type
            except OSError:
                continue                            # a dangling link opens nothing
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                out.append(f"{os.path.relpath(p, run_dir)!r} in the run is not a regular file: remove it, or write "
                           "a plain file")
    return out


CARRIED = "from an earlier try, still unresolved: "


def _carry_feedback(errs: list, earlier: list | None, cap: int = 40) -> list:
    """The SPEC feedback for the next try: this try's problems, then the earlier try's when there is no draft on
    disk to check (baseline hb1: a try lost to the turn cap replaced the validation errors with "max turns", so
    the next try repeated them). Earlier items are marked once and never repeated."""
    out = list(errs)
    seen = {x[len(CARRIED):] if x.startswith(CARRIED) else x for x in out}
    for x in earlier or []:
        core = x[len(CARRIED):] if x.startswith(CARRIED) else x
        if core not in seen and not core.startswith("draft_spec failed:"):
            seen.add(core)
            out.append(CARRIED + core)
    return out[:max(cap, len(errs))]          # the cap trims the carried items only (Codex r1 P2-6)


@handler("SPEC")
class SpecHandler:
    def _geometry_problems(self, rd: str | None = None) -> list[str]:
        """Before SIGN: a HIGH geometry finding not accepted by name in the spec, or a check that failed on its
        declared inputs. Writes data/input_checks.json, which the sign-off sheet reads."""
        from toolkit.spec import geometry as GEO
        return [f"{p['code']}: {p['claim']}" for p in GEO.problems(GEO.run(rd or self.run_dir))]

    # -------------------------------------------------- handlers
    def h_SPEC(self):
        if self.st.signoff == "lead" and (self.st.tries.get("spec_amendment") or spec_signed_once(self.run_dir)) \
                and not getattr(self, "_in_amend_lock", False):
            # owner decision on #330 (Codex r3): the request is written only under the amendment lock, so a resumer that
            # loses the lock writes nothing
            with _amend_lock(self.run_dir):
                self._in_amend_lock = True
                try:
                    return self.h_SPEC()
                finally:
                    self._in_amend_lock = False
        if self.st.tries.get("spec_amendment") or spec_signed_once(self.run_dir):
            # F6 run 3 (gap 2): a rethink of a signed spec is the owner's amendment, never a redraft in this run (an
            # amended spec cannot be re-signed in a signed run dir): the request, then a pause; the owner amends and
            # relaunches with reuse
            req = rethink_request(self.run_dir, self.st.round, self.st.signoff)
            sha = _spec_signed_sha(self.run_dir)
            ev = "present" if sha not in (None, "unreadable") else ("unreadable" if sha else "missing")
            if req.get("signing_evidence") != ev:        # a warning on the request, never a block (lead follow-up)
                req = dict(req, signing_evidence=ev)
                _write_json(self.run_dir, SPEC_RETHINK_REQUEST, req)
            if ev != "present":
                self.log(f"[machine] WARNING: the spec's sign-off record is {ev}; the amendment pause holds, but the "
                         "relaunch must start from a signed spec")
            if self.st.signoff == "lead" and ev == "present":
                done = self._lead_amend(req)
                if done:
                    return done
            self.st.tries["spec_amendment"] = True     # honoured on resume, whatever a restored copy lost (#261 P1-2)
            self.st.paused, self.st.status = AWAITING_SPEC_AMENDMENT, None
            save_state(self.run_dir, self.st)
            self.log(f"[machine] SPEC rethink of a signed spec: nothing redrafted. Read {SPEC_RETHINK_REQUEST} "
                     f"({len(req['findings'])} finding(s)); amend the spec and relaunch with reuse")
            return "SPEC"
        n = self.st.tries.get("spec", 0)
        if n >= MAX_SPEC_TRIES:
            return self._go("FINAL", "the spec did not validate", "run_failed")
        rd = self.run_dir
        stage_inputs(rd, self.st.question)
        fb = self.st.tries.get("spec_feedback")
        # The key is fixed before the call and the try is counted only once the call returns, so an
        # interrupted draft resumes under the same key (charged once by _settle_interrupted).
        res = self._task(f"SPEC#{self._visit()}:try{n + 1}", "draft_spec", self.st.question, rd, fb)
        self.st.tries["spec"] = n + 1
        if res is None:
            return self._budget_out("SPEC")
        errs = [f"draft_spec failed: {res.note}"] if not res.ok else []
        # baseline hb1: a try cut off (the turn cap) is checked on whatever draft is on disk, written or repaired in
        # this try or left by an earlier one: the checks say what holds now, with no before/after comparison to get
        # wrong on a resume or a rewrite (Codex r1 P1-3/P1-4, r2 P1-5/P1-7/P2-8). Only when the try's feedback is
        # what these checks wrote (or none): reasons they cannot verify (a rethink, a refused sign-off, the evidence
        # stage) are never cleared by an old draft that passes them; they are carried (Codex r3 P1-2).
        recheckable = (not fb or fb == self.st.tries.get("spec_feedback_checked")
                       or all(str(x).startswith("draft_spec failed:") for x in fb))   # r1 P1-5: a run saved before
        drafted = res.ok or (recheckable and os.path.lexists(os.path.join(rd, AB.SPEC_FILE)))
        if drafted:
            try:
                errs = self._spec_checks(rd)
                if not errs and self.st.derived_inputs:
                    self._write_sheet(rd)
            except Exception as e:  # noqa: BLE001
                errs = [f"spec unreadable: {e}"]
            if errs and not res.ok:
                errs = [f"draft_spec failed: {res.note} (its draft was checked)"] + errs
        if errs:
            self.st.tries["spec_feedback"] = _carry_feedback(errs, fb if not drafted else None)
            if drafted or recheckable:
                # these checks' own output (or, with no draft on disk, what they wrote before plus that note): a capped
                # next try may be re-checked against it (Codex r4 P1-3: a first try with no draft blocked every later one)
                self.st.tries["spec_feedback_checked"] = list(self.st.tries["spec_feedback"])
            else:
                self.st.tries.pop("spec_feedback_checked", None)
            return self._go("SPEC", f"spec invalid: {'; '.join(errs)[:200]}")
        self.st.tries.pop("spec_feedback", None)
        self.st.tries.pop("spec_feedback_checked", None)
        if not self._review_conditions(self.st.signoff, defer=False):
            # Codex narrow round P1: the conditions exist before the run ever asks for a signature (owner 2026-11-21)
            self.st.tries["spec_feedback"] = ["review conditions could not be prepared for signing"]
            return self._go("SPEC", "review conditions could not be prepared for signing")
        return self._go("EVIDENCE", "spec drafted and valid")

    def _spec_checks(self, rd: str) -> list[str]:
        """Every pre-sign check class on the draft, each run whatever the others found, so one try hears every problem
        (baseline hb1: the chain stopped at the first class that failed, and MAX_SPEC_TRIES is small). A class that
        raises is named in the problems and the others still run. A spec or settings file that is a link (symbolic or
        hard) or not a regular file, or a spec that does not parse, is the problem and nothing else runs (Codex r4
        P1-5: the checks read these files by path)."""
        import yaml
        bad = _draft_file_problems(rd) + _special_files(rd)     # before anything opens a file (Codex r1 on #350)
        if bad:
            return bad
        try:
            yaml.safe_load(open(os.path.join(rd, AB.SPEC_FILE)))
            from toolkit.analysis import conflicts as CF     # owner default D5: declare, test and continue
            if CF.ensure_default(rd):                        # after the link checks: it replaces the file
                self.log("[machine] the draft states no conflicts policy: declare_and_test (owner default D5)")
        except Exception as e:  # noqa: BLE001
            return [f"spec unreadable: {type(e).__name__}: {e}"]
        out: list[str] = []

        def run(name, fn) -> bool:
            try:
                got = list(fn() or [])
            except Exception as e:  # noqa: BLE001 -- fail closed, and the other classes still run
                got = [f"the {name} check could not run: {type(e).__name__}: {e}"]
            out.extend(x for x in got if x not in out)
            return not got
        if self.st.derived_inputs:
            run("derivation", lambda: self._derive(rd))
        run("cited-input", lambda: record_cited_inputs(rd))      # records the hashes before a check reads a cited file
        outside = self._cites_outside(rd)
        if outside:             # Codex r1 P1-1: no later check opens a file the cited-input check refused
            out.extend(x for x in outside if x not in out)       # r2 P1-2: never empty, so never a pass
            return out
        if self.st.derived_inputs:
            # Workstream B's schema runs the derived-input checks itself (and per candidate engine); the spike adapter
            # does not know them, so the harness runs them for it.
            if not hasattr(self.backend, "candidate_rows"):
                import pipeline as P
                run("derived-input", lambda: [f"{p['code']}: {p['claim']}" for p in P.derivation_problems(
                    rd, self._spec(rd), required=True)])
            signable = run("pre-sign", lambda: self.backend.pre_sign_problems(rd, derived=True))
        else:
            signable = run("pre-sign", lambda: self.backend.pre_sign_problems(rd))
        run("geometry", lambda: self._geometry_problems(rd))
        if signable and run("signed-configuration", lambda: self._config_problems(rd)):
            # Codex r1 P1-2, r2 P1-3: the assessment builds the engine, only after the pre-sign guards have passed; the
            # configuration guard's own problems go back to the drafter (r3 P1-1)
            run("evidence", lambda: self._evidence_assess(rd)[1]["spec_problems"])   # tb R2: heard one stage late
        return out

    @staticmethod
    def _config_problems(rd: str) -> list[str]:
        """The signed engine configuration's guard (analysis_backend.signed_config_problems) on the draft, whatever the
        backend: no engine is built from a draft it refuses (an error is reported by _spec_checks, fail closed)."""
        import yaml
        return list(AB.signed_config_problems(yaml.safe_load(open(os.path.join(rd, AB.SPEC_FILE))) or {}))

    @staticmethod
    def _cites_outside(rd: str) -> list[str]:
        """The spec's citations no later check may open, as problems: one that resolves outside the run directory
        (record_cited_inputs refuses it), or one that exists but is neither a regular file nor a directory (a FIFO or a
        device blocks the reader: Codex r4 P1-4). Fail closed: when the citations cannot be listed, that is the
        problem."""
        import stat
        import yaml
        import pipeline as P
        try:
            spec = yaml.safe_load(open(os.path.join(rd, AB.SPEC_FILE))) or {}
            root, out = os.path.realpath(rd), []
            for r in P.cited_paths(spec, rd):
                p = os.path.realpath(os.path.join(rd, r))
                if not p.startswith(root + os.sep):
                    out.append(f"the spec cites {r}, outside the run directory: copy it into data/")
                    continue
                try:
                    mode = os.stat(p).st_mode
                except FileNotFoundError:
                    continue                    # record_cited_inputs says it does not exist; nothing can open it
                if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    out.append(f"the spec cites {r}, which is not a regular file: write the table as a plain file")
            return out
        except Exception as e:  # noqa: BLE001
            return [f"the spec's cited paths could not be listed, so no check reads them: {type(e).__name__}: {e}"]

    def _lead_amend(self, req: dict) -> str | None:
        """The lead amendment under the parent's amendment lock (owner decision on #330): the request and the parent's
        state are read again once the lock is held, so a run another process already handed to its child, or is
        amending now, stops with AmendmentLocked and nothing written."""
        with _amend_lock(self.run_dir):
            disk = load_state(self.run_dir)
            if disk is not None and disk.status == AMENDED_PROVISIONAL:
                raise AmendmentLocked(f"{self.run_dir} was already handed to its child by another process: this "
                                      "resume stops with nothing written", run_dir=self.run_dir)
            try:
                cur = json.load(open(os.path.join(self.run_dir, SPEC_RETHINK_REQUEST)))
            except (OSError, ValueError):
                cur = None
            return self._lead_amend_locked(cur if isinstance(cur, dict) else req)

    def _lead_amend_locked(self, req: dict) -> str | None:
        """lead-gaps β (S7 under --signoff lead only): the rethink's machine-readable patch is applied by toolkit/spec/
        amend.py (--from-request) to a child run, the child's spec is signed by the lead, provisionally, and this run
        ends `amended_provisional`; run_v2 continues in the child, whose report heads with the amendment. None (the
        run pauses for the owner, as before) when there is no patch, it does not apply or validate, the amended spec
        would not sign, the parent's signature no longer holds, no budget is left, this run already continues a
        provisional amendment, or an earlier attempt was refused. The child's path is recorded before it is made, so
        an interrupted handoff is finished on resume, never repeated (Codex r1)."""
        from toolkit.spec import amend as AM, signoff as SO
        la, why, rec = req.get("lead_amendment") or {}, None, None
        child = la.get("child")
        if la.get("status") in ("started", "signed_provisionally"):
            # resolved beside this run only (a moved copy: r2); never the recorded path, which may be the original tree
            # of a restored copy (Codex r3 on #342, P1-5); and only the child this request made (Codex r1 of #349)
            child = _child_of(self.run_dir, child)
            child = child if child and _made_by(child, req, self.run_dir) else None
        if la.get("status") == "refused":
            return None                   # the first refusal stands, with its reason and any kept child
        if any(os.path.lexists(os.path.join(self.run_dir, f)) for f in LEGACY_BRIEF_SIGNOFF):
            # owner decision on #330: in v2 a legacy decision-brief signature (which may bind other files than the spec)
            # never satisfies the lead's check, also when recovering an interrupted hand-off (Codex r3); the owner decides
            why = "the parent carries a legacy decision brief signature, which a lead amendment never relies on in v2"
        elif la.get("status") in ("started", "signed_provisionally"):
            # an interrupted handoff: finished when the parent's signature still holds (Codex r3 on #342, P1-6) and the
            # child is signed by the lead, else refused (the owner decides)
            if self._signed_by() is None:
                why = "the parent's sign-off no longer holds (a signed input changed since): the owner decides"
            elif not (child and os.path.isfile(os.path.join(child, STATE_FILE)) and SO.status(child) == "signed"
                    and (SO._spec_record(child) or {}).get("signed_by") == "lead"):
                why = "an interrupted amendment left no lead-signed child"
            else:
                try:
                    rec = json.load(open(os.path.join(child, AMENDMENT_RECORD)))
                except (OSError, ValueError) as e:
                    why = f"the interrupted amendment's record cannot be read ({type(e).__name__})"
        elif not isinstance(req.get("patch"), dict):
            why = "the rethink wrote no machine-readable patch"
        elif self._provisional_amendment():
            why = "this run continues a provisional amendment; a second one waits for the owner"
        elif not callable(getattr(self.tasks, "for_run_dir", None)):
            why = "the run's tasks cannot continue in a child run"
        elif self._signed_by() is None:
            why = "the parent's sign-off no longer holds (a signed input changed since): the owner decides"
        elif isinstance(self.st.budget_usd, (int, float)) and round(self.st.budget_usd - self.st.spent_usd, 4) <= 0:
            why = "no budget is left for a child run"
        else:
            child = AM._next_child(self.run_dir)
            _write_json(self.run_dir, SPEC_RETHINK_REQUEST, dict(req, lead_amendment={"at": _now(), "status": "started",
                                                                                      "child": child}))
            self.st.tries["spec_amendment"] = True
            self.st.paused, self.st.status = AWAITING_SPEC_AMENDMENT, None     # amend amends a paused or finished run
            save_state(self.run_dir, self.st)
            try:
                rec = AM.amend(self.run_dir, from_request=True, out=child, signoff="lead", log=lambda *a: None)
                if not (rec.get("dry_sign") or {}).get("ok"):
                    why = f"the amended spec would not sign: {(rec.get('dry_sign') or {}).get('error')}"
                else:
                    self._mark_child(child, rec.get("id"))     # before the signature: a signed child always says so (r2)
                    SO.sign(child, "lead", f"Provisional (--signoff lead): amendment {rec['id']} of "
                            f"{self.run_dir}, made from its rethink request (round {req.get('round')}); the owner has "
                            f"not reviewed it. Reason: {' '.join(str(rec.get('reason')).split())[:500]}",
                            expect_sha256=rec["spec_sha256"], expect_sheet_sha256=rec["sheet_sha256"],
                            expect_derivations_sha256=rec["derivations_sha256"])
            except Exception as e:  # noqa: BLE001 -- AmendError (refused, no child) or a signing refusal: owner pause
                why = f"{type(e).__name__}: {e}"
        if why is None:
            try:
                self._mark_child(child, rec.get("id"))
            except Exception as e:  # noqa: BLE001 -- an unreadable child state: refused, the owner decides (r2)
                why = f"the child's run state cannot be read ({type(e).__name__})"
        kept = child if child and os.path.isdir(child) else None
        req = dict(req, lead_amendment={"at": _now(), "status": "refused" if why else "signed_provisionally",
                                        **({"why": why[:2000]} if why else {"id": rec.get("id")}),
                                        **({"child": kept} if kept else {})})
        _write_json(self.run_dir, SPEC_RETHINK_REQUEST, req)
        if why:
            self.log(f"[machine] the lead does not amend the signed spec ({why[:300]}): the run waits for the owner")
            return None
        self.st.paused = None
        # the child is bound to the parent's final state before that state is saved, so no interruption leaves a FINAL
        # parent whose child binds its paused state (Codex r3 on #342, P1-4); until that save the parent is at SPEC and
        # a resume finishes the hand-off again
        # a failure before the parent's FINAL state is saved (its metadata, the lineage) puts this machine and the
        # parent's metadata back as they were (Codex r2 and r3 of #349): a retry, on this machine or a new one, finishes
        # the hand-off
        import copy
        saved = copy.deepcopy(self.st)
        meta_p = os.path.join(self.run_dir, "metadata.json")
        meta0 = open(meta_p, "rb").read() if os.path.isfile(meta_p) else None
        try:
            return self._go("FINAL", f"provisional amendment {rec.get('id')} by the lead: the run continues in {child}",
                            AMENDED_PROVISIONAL, extra={"amended_to": child}, before_save=lambda: self._relink(child))
        except BaseException:
            disk = _state_or_none(self.run_dir)
            if disk is None or disk.status != AMENDED_PROVISIONAL:
                self.st = saved
                from lib.safe_io import remove_nofollow, write_nofollow
                if meta0 is not None:
                    write_nofollow(self.run_dir, "metadata.json", meta0, atomic=True)
                else:
                    remove_nofollow(self.run_dir, "metadata.json")
            raise

    def _mark_child(self, child: str, rid) -> None:
        """The child's state, once: not paused, what is left of the cap (always > 0 here), the handoff event."""
        cst = load_state(child)
        if cst is None:
            raise ValueError("no run state")
        if not any(isinstance(h, dict) and h.get("provisional_amendment") for h in cst.history):
            cst.paused = None
            if isinstance(self.st.budget_usd, (int, float)):    # one cap: the child gets what is left
                cst.budget_usd = round(self.st.budget_usd - self.st.spent_usd, 4)
            cst.history.append({"at": _now(), "provisional_amendment": rid, "by": "lead", "parent_run": self.run_dir})
            save_state(child, cst)

    def _provisional_amendment(self) -> bool:
        """This run was made by a lead's provisional amendment: its amendment record not signed by the owner, or the
        handoff event in its history (kept when the record is lost: Codex r1)."""
        return (os.path.lexists(os.path.join(self.run_dir, AMENDMENT_RECORD)) and self._signed_by() != "owner") or \
            any(isinstance(h, dict) and h.get("provisional_amendment") for h in self.st.history)

    def _relink(self, child: str) -> None:
        """The child's parent_run binds the parent's final run_state.json (amend recorded it while paused), so the
        chain still links (scripts/harness_metrics.py chains): the bytes about to be saved, from the state in memory. A
        failure is raised before the parent is saved FINAL, so a resume finishes the hand-off (Codex r1 of #349)."""
        sha = hashlib.sha256(_state_bytes(self.st)).hexdigest()
        for rel in ("metadata.json", AMENDMENT_RECORD):
            doc = json.load(open(os.path.join(child, rel)))
            if isinstance(doc.get("parent_run"), dict):
                doc["parent_run"]["run_state_sha256"] = sha
                _write_json(child, rel, doc)

    def _derive(self, rd: str | None = None) -> list[str]:
        """The draft's declared derivations, run by the harness (scripts/pipeline.py derive). Their problems go back
        to the planner with the script's stderr tail: the planner never needs a shell to learn whether its code
        works."""
        import pipeline as P
        rep = P.derive(rd or self.run_dir, unsandboxed_ok=self.st.derive_unsandboxed)
        return [f"{p['code']}: {p['claim']}" for p in rep["problems"]]
