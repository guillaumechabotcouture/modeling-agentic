"""Lead amendments of a signed spec: the amendment lock and the parent/child hand-off chain of runs.

Moved verbatim from agents/stage_machine.py by scripts/refactor/split_stage_machine.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import analysis_backend as AB

from .common import SPEC_RETHINK_REQUEST, STATE_FILE, load_state


# lead-gaps β: under --signoff lead a rethink of the signed spec with a valid patch becomes a child run (toolkit/spec/
# amend.py), signed provisionally by the lead; the parent ends with this status and the run continues in the child.
AMENDED_PROVISIONAL = "amended_provisional"
AMENDMENT_RECORD = "amendment_record.json"           # toolkit/spec/amend.py RECORD
# held by a --signoff lead run across its amendment and hand-off; under analysis/ (reserved for every run, so no
# owner-mode deliverable path changes: Codex r3) and never copied into a child (.lock)
AMEND_LOCK = "analysis/spec_amendment.lock"


class AmendmentLocked(RuntimeError):
    """Another process holds the parent's amendment lock, or has already handed the run to its child: this resume
    stops with nothing written (owner decision on #330). `run_dir`: the run whose lock is held (main.py stores a
    parent it finished up to a child another process holds: Codex r1 of #349)."""

    def __init__(self, msg: str, run_dir: str | None = None):
        super().__init__(msg)
        self.run_dir = run_dir


_LOCK_HELD = __import__("threading").local()


class _amend_lock:
    """An exclusive, non-blocking lock on <run>/analysis/spec_amendment.lock (lib.safe_io, no link followed; flock is
    per open file, so two processes, or two machines in two threads, exclude each other). Nests within one thread (a
    lead run holds it from run_v2 through its amendment and hand-off). A second holder raises AmendmentLocked."""

    def __init__(self, run_dir: str):
        self.run_dir, self.f = run_dir, None
        self.key = os.path.realpath(os.path.join(run_dir, AMEND_LOCK))

    def __enter__(self):
        held = _LOCK_HELD.__dict__.setdefault("depth", {})
        if held.get(self.key):
            held[self.key] += 1
            return self
        import fcntl
        from lib.safe_io import open_append_nofollow
        f = open_append_nofollow(self.run_dir, AMEND_LOCK)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise AmendmentLocked(f"another process is amending {self.run_dir} ({AMEND_LOCK} is held): this resume "
                                  "stops with nothing written; resume again once that process has ended",
                                  run_dir=self.run_dir) from None
        except BaseException:
            f.close()
            raise
        self.f = f
        held[self.key] = 1
        return self

    def __exit__(self, *exc):
        held = _LOCK_HELD.__dict__.setdefault("depth", {})
        held[self.key] -= 1
        if held[self.key]:
            return False
        del held[self.key]
        import fcntl
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()
        return False


def _child_of(run_dir: str, recorded, strict: bool = False) -> str | None:
    """The child dir a parent recorded, resolved beside the parent as it is now (a restored copy moves both: Codex r1),
    and only when the child's metadata names this parent (by directory name)."""
    if not isinstance(recorded, str) or not recorded:
        return None
    here = os.path.join(os.path.dirname(os.path.abspath(run_dir)), os.path.basename(recorded.rstrip(os.sep)))
    if os.path.islink(here):          # a link to another tree is not this run's child (Codex r1 of #349)
        return None
    try:
        pr = (json.load(open(os.path.join(here, "metadata.json"))) or {}).get("parent_run") or {}
    except (OSError, ValueError, AttributeError):
        return None
    ok_ = os.path.basename(str(pr.get("run_dir") or "").rstrip(os.sep)) == os.path.basename(os.path.abspath(run_dir))
    if ok_ and strict:      # a finished parent: the child binds its final state (r2); a missing digest binds none (r3)
        try:
            ok_ = isinstance(pr.get("run_state_sha256"), str) and hashlib.sha256(
                open(os.path.join(run_dir, STATE_FILE), "rb").read()).hexdigest() == pr["run_state_sha256"]
        except OSError:
            ok_ = False
    return here if ok_ and os.path.realpath(here) != os.path.realpath(run_dir) else None


def _parent_of(run_dir: str, recorded):
    """The parent a child run continues: its sibling of the recorded name when that sibling hands off to this child (a
    parent and child moved together: Codex r3 on #342, P2-12), else the recorded path."""
    if not isinstance(recorded, str) or not recorded:
        return recorded
    here = os.path.join(os.path.dirname(os.path.abspath(run_dir)), os.path.basename(recorded.rstrip(os.sep)))
    c = handoff_child(here)
    if c and os.path.realpath(c) == os.path.realpath(run_dir):
        return here
    # a manual amendment's parent keeps its status: the parent-state digest the child records (in its metadata or its
    # amendment record: Codex r2 of #349)
    for rel in ("metadata.json", AMENDMENT_RECORD):
        try:
            bound = ((json.load(open(os.path.join(run_dir, rel))) or {}).get("parent_run") or {}).get(
                "run_state_sha256")
            if not os.path.islink(here) and isinstance(bound, str) and bound == hashlib.sha256(
                    open(os.path.join(here, STATE_FILE), "rb").read()).hexdigest():
                return here
        except (OSError, ValueError, AttributeError):
            continue
    return recorded


def _own_spec_digest(run_dir: str):
    """The spec digest the run's own sign-off record binds (decision_spec.signoff.json; never a legacy brief's)."""
    from toolkit.spec import signoff as SO
    try:
        rec = json.load(open(os.path.join(run_dir, SO.SPEC_SIGNOFF)))
        m = rec.get("sha256")
        return (m.get(AB.SPEC_FILE) if isinstance(m, dict) else None) or rec.get("spec_sha256")
    except (OSError, ValueError, AttributeError):
        return None


def _made_by(child: str, req: dict, parent: str) -> bool:
    """`child` is the run toolkit/spec/amend.py made from this rethink request of `parent`: from the signed spec the
    request names, which the parent's own sign-off binds, with the request's patch (its id and content digest). A
    same-named child from another backup or another patch is not this request's (Codex r1 and r2 of #349)."""
    from toolkit.spec import amend as AM
    try:
        meta = json.load(open(os.path.join(child, "metadata.json")))
    except (OSError, ValueError):
        return False
    req = req if isinstance(req, dict) else {}
    sig, patch = req.get("signed_spec_sha256"), req.get("patch")
    if not (isinstance(meta, dict) and isinstance(sig, str) and isinstance(patch, dict)):
        return False
    return meta.get("parent_spec_sha256") == sig == _own_spec_digest(parent) \
        and meta.get("amendment") == str(patch.get("id") or "unnamed") \
        and meta.get("patch_sha256") == AM.patch_sha256(patch)


def handoff_child(run_dir: str) -> str | None:
    """The child run a provisional lead amendment handed this run to (`amended_provisional`), when it has a state."""
    st = _state_or_none(run_dir)
    if st is None or st.status != AMENDED_PROVISIONAL:
        return None
    child = _child_of(run_dir, next((h.get("amended_to") for h in reversed(st.history)
                                     if isinstance(h, dict) and h.get("amended_to")), None), strict=True)
    return child if child and os.path.isfile(os.path.join(child, STATE_FILE)) else None


def _state_or_none(run_dir: str):
    """The run's state, or None when it is missing or cannot be read as one (Codex r3 of #349: `[]` too)."""
    try:
        return load_state(run_dir)
    except Exception:  # noqa: BLE001 -- unreadable, or not a run state
        return None


def handoff_chain(run_dir: str, limit: int = 8, kept: bool = False) -> list[str]:
    """Every child run this run continued in, in order; with `kept`, also a child an amendment refused at signing
    left for the owner, or one an interrupted hand-off signed (main.py stores each with the run: Codex r1): only
    after a recorded refusal or an unfinished hand-off, and only the child this run's request made (Codex r3 on #342,
    P1-11; Codex r1 of #349)."""
    return list(iter_handoff_chain(run_dir, limit, kept))


def iter_handoff_chain(run_dir: str, limit: int = 8, kept: bool = False):
    """handoff_chain, one run at a time: main.py stores each run before it reads the next (Codex r2 of #349)."""
    out, cur = [], run_dir
    while len(out) < limit:
        nxt = handoff_child(cur)
        if nxt is None and kept:
            nxt = _kept_child(cur)
            if nxt is not None and nxt not in out:
                yield nxt
            return
        if nxt is None or nxt in out:
            return
        out.append(nxt)
        yield nxt
        cur = nxt


def _kept_child(run_dir: str) -> str | None:
    """A refused amendment's kept child, or the lead-signed child of a hand-off interrupted before the parent was saved
    FINAL; only the child this run's request made (Codex r1 and r2 of #349)."""
    try:
        req = json.load(open(os.path.join(run_dir, SPEC_RETHINK_REQUEST)))
    except (OSError, ValueError):
        return None
    la = req.get("lead_amendment") if isinstance(req, dict) else None
    if not isinstance(la, dict):
        return None
    status = la.get("status")
    if status in ("started", "signed_provisionally"):
        st = _state_or_none(run_dir)
        if st is None or st.status == AMENDED_PROVISIONAL:
            return None
    elif status != "refused":
        return None
    child = _child_of(run_dir, la.get("child"))
    if child is None or not _made_by(child, req, run_dir):
        return None
    if status != "refused":              # an unfinished hand-off: only a child with its state and the lead's signature
        from toolkit.spec import signoff as SO
        if not (_state_or_none(child) is not None and SO.status(child) == "signed"
                and (SO._spec_record(child) or {}).get("signed_by") == "lead"):
            return None
    return child
