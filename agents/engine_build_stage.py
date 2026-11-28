"""Agent-built engine lifecycle mixin, narrow run lock (BE31), and constants (Wave B2).

Imports `agents/report_render` (`_withhold_report`, `_write_json`) and never imports `stage_machine` at
module scope (DAG: stage_machine -> engine_build_stage -> report_render -> v2_gate).
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS = os.path.join(_ROOT, "scripts")
for _p in (_ROOT, _SCRIPTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import analysis_backend as AB
from agents import findings_ledger as FL
from agents import report_render as RR

_put = RR._put
_write_json = RR._write_json
_withhold_report = RR._withhold_report
_spec_signed_sha = RR._spec_signed_sha
signed_hash = RR.signed_hash
SPEC_RETHINK_REQUEST = RR.SPEC_RETHINK_REQUEST

POST_CHOICE_STATES = (
    "ENGINE_BUILD", "ENGINE_ADMIT", "ENGINE_REVIEW", "ENGINE_SIGN",
    "ANALYZE", "CRITIQUE", "GATE", "FIX_LOOP", "OWNER_DECISION", "WRITE",
)
POST_ENGINE_SIGN_STATES = (
    "ANALYZE", "CRITIQUE", "GATE", "FIX_LOOP", "OWNER_DECISION", "WRITE",
)

MAX_ENGINE_BUILD_TRIES = 2
MAX_ENGINE_REPAIRS = 1
ENGINE_BUILD_CAP_USD = 40.0
ENGINE_BUILD_CAP_MAX_USD = 200.0
ENGINE_BUILD_TRY_CAP_USD = 15.0
ENGINE_REVIEW_CAP_USD = 3.0
ENGINE_ADMIT_COMPUTE_CAP_USD = 15.0
AWAITING_ENGINE_SIGNOFF = "awaiting_engine_signoff"
ENGINE_NOT_ADMITTED = "completed_engine_not_admitted"
ENGINE_DECLINED = "completed_engine_declined"

AWAITING_BUDGET = "awaiting_budget"
MIN_TASK_ALLOWANCE_USD = 0.50
MAX_CRITIQUE_ROUNDS = 3

RUN_LOCK = "analysis/run.lock"
_LOCK_HELD = threading.local()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class RunLocked(RuntimeError):
    """Another process holds the built-engine run lock (`analysis/run.lock`): resume stops with nothing written."""

    def __init__(self, msg: str, run_dir: str | None = None):
        super().__init__(msg)
        self.run_dir = run_dir


class _run_lock:
    """Exclusive, non-blocking flock on `<run>/analysis/run.lock` (lib.safe_io, no link followed), reentrant per thread."""

    def __init__(self, run_dir: str):
        self.run_dir, self.f = run_dir, None
        self.key = os.path.realpath(os.path.join(run_dir, RUN_LOCK))

    def __enter__(self):
        held = _LOCK_HELD.__dict__.setdefault("depth", {})
        if held.get(self.key):
            held[self.key] += 1
            return self
        import fcntl
        from lib.safe_io import open_append_nofollow
        f = open_append_nofollow(self.run_dir, RUN_LOCK)
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.close()
            raise RunLocked(f"another process is running built-engine resume on {self.run_dir} ({RUN_LOCK} is held): "
                            "this resume stops with nothing written; resume again once that process has ended",
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


def spec_has_engine_build(run_dir: str) -> bool:
    """True when `decision_spec.yaml` in `run_dir` declares an `engine.build` request (single-link no-follow read)."""
    import yaml
    from lib.safe_io import read_nofollow
    from toolkit.spec import schema as _SCH
    try:
        raw, st_f = read_nofollow(run_dir, AB.SPEC_FILE)
        if st_f.st_nlink > 1:
            return False
        doc = yaml.safe_load(raw.decode("utf-8"))
        return isinstance(doc, dict) and _SCH.engine_has_build(doc.get("engine"))
    except Exception:  # noqa: BLE001
        return False


class EngineBuildMixin:
    """Built-engine lifecycle handlers and helpers mixed into `Machine`."""

    def _has_built_engine(self) -> bool:
        return bool(isinstance(self.st.engine_build, dict) and self.st.engine_build.get("id"))

    def _engine_repairs_left(self) -> bool:
        return int((self.st.engine_build or {}).get("repairs") or 0) < MAX_ENGINE_REPAIRS

    def _engine_tamper_problems(self) -> list[str]:
        """Verify signed built-engine snapshot bytes after ENGINE_SIGN (§5.1). Empty when clean or not signed."""
        eb = self.st.engine_build if isinstance(self.st.engine_build, dict) else {}
        if eb.get("status") != "signed":
            return []
        from toolkit.engines.built import snapshot as BSNAP
        return BSNAP.verify_admitted_engine(self.run_dir, require_signoff=True)[1]

    def _max_engine_build_tries(self) -> int:
        repairs = int((self.st.engine_build or {}).get("repairs") or 0)
        return MAX_ENGINE_BUILD_TRIES + repairs

    def _update_pending_choice_status(self, build_id: str, bstatus: str) -> None:
        try:
            from lib.safe_io import read_nofollow
            cur = json.loads(read_nofollow(self.run_dir, AB.ENGINE_CHOICE)[0].decode("utf-8"))
            if not isinstance(cur, dict):
                cur = {}
        except (OSError, ValueError):
            cur = {}
        cur.pop("engine", None)
        cur["at"] = _now()
        cur["build"] = {"id": build_id, "status": bstatus}
        _write_json(self.run_dir, AB.ENGINE_CHOICE, cur)

    def _build_engine_packet(self, build_decl: dict, feedback: list[str] | None) -> dict:
        """Build the `_task_input.json` packet for `build_engine` (§3.5)."""
        import inspect
        from toolkit.engines import contract as _C
        from toolkit.engines.built import oracles as BORC
        from toolkit.engines.built import snapshot as BSNAP
        oracle_sigs = {name: str(inspect.signature(fn)) for name, fn in sorted(BORC.ORACLE_REGISTRY.items())}
        return {
            "build": build_decl,
            "contract_docstring": inspect.getdoc(_C) or "",
            "allowed_imports": {
                "top_level": sorted(BSNAP.ALLOWED_TOP_IMPORTS),
                "qualified": sorted(BSNAP.ALLOWED_QUALIFIED_IMPORTS),
            },
            "oracle_signatures": oracle_sigs,
            "layout": {
                "work_dir": f"models/ext/engine_{build_decl['id']}/",
                "required_files": ["engine.py", "card.yaml", "ENGINE_NOTES.md"],
            },
            "checks": {
                "S1-S3": "file/link/size audit, AST import allowlist, banned calls/dunders/top-level execution",
                "A1": "conformance with EngineCard, card.yaml, exact request parameters, non-negative finite outputs",
                "A2": "determinism across processes, PYTHONHASHSEED, case orders, opaque IDs, and seed_test_setting",
                "A4": "declared oracle reductions + absent-component identity + per_capita_invariant_to_N",
                "A5": "compartment mass balance, non-negativity, conservation, transition-count witnesses (starsim_module)",
                "A6": "dead-parameter read tracking, read_outcomes phase check, non-trivial output movement, and output liveness",
                "A8": "structured trial benchmarks under built coverage policy",
            },
            "feedback": list(feedback or []),
        }

    def _write_request_for_owner(self, reason: str, problems: list[str]) -> None:
        eb = self.st.engine_build or {}
        lines = [
            "# Agent-built engine not admitted — owner follow-up",
            "",
            f"- Engine id: `{eb.get('id')}` (`{eb.get('kind')}`)",
            f"- Tries used: `{eb.get('tries', 0)}` (repairs: `{eb.get('repairs', 0)}`)",
            f"- Snapshot: `{eb.get('snapshot_sha256') or 'none'}`",
            f"- Reason: {reason}",
            "",
            "## Problems",
            "",
        ]
        lines += [f"- {p}" for p in (problems or ["see engine_build/admission.json"])]
        _put(self.run_dir, "engine_build/request_for_owner.md", "\n".join(lines) + "\n")

    def _ensure_hidden_seed(self, snap_sha: str) -> tuple[bytes, str]:
        """Draw and commit the hidden seed once per snapshot before any hidden case runs, reusing the exact
        committed seed if admission resumes mid-way after a crash (BE31). Fails closed if a commitment was
        already recorded for `snap_sha` in run_state.json but `engine_build/.hidden/seed.json` is missing
        or mismatched (never silently swaps a committed hidden seed)."""
        import secrets
        from lib.safe_io import read_nofollow
        seed_rel = "engine_build/.hidden/seed.json"
        eb = self.st.engine_build
        prior_snap = eb.get("hidden_seed_snapshot")
        prior_commit = eb.get("hidden_commitment")
        if prior_commit and prior_snap in (snap_sha, None):
            try:
                raw, st_f = read_nofollow(self.run_dir, seed_rel)
                if st_f.st_nlink != 1:
                    raise RuntimeError(f"{seed_rel} has multiple hard links")
                doc = json.loads(raw.decode("utf-8"))
                shex = doc.get("seed_hex") if isinstance(doc, dict) else None
                if not isinstance(shex, str) or len(shex) != 64:
                    raise RuntimeError(f"{seed_rel} missing valid 64-hex seed_hex")
                sb = bytes.fromhex(shex)
                commit = hashlib.sha256(shex.encode("ascii")).hexdigest()
                if commit != prior_commit:
                    raise RuntimeError(f"{seed_rel} commitment mismatch ({commit[:12]} != {prior_commit[:12]})")
                eb["hidden_seed_snapshot"] = snap_sha
                return sb, commit
            except (FileNotFoundError, OSError, ValueError) as e:
                raise RuntimeError(f"committed hidden seed for {snap_sha[:12]} missing or unreadable: {e}") from None
        try:
            raw, st_f = read_nofollow(self.run_dir, seed_rel)
            if st_f.st_nlink == 1:
                doc = json.loads(raw.decode("utf-8"))
                shex = doc.get("seed_hex") if isinstance(doc, dict) else None
                if (isinstance(doc, dict) and doc.get("snapshot_sha256") == snap_sha
                        and isinstance(shex, str) and len(shex) == 64):
                    sb = bytes.fromhex(shex)
                    commit = hashlib.sha256(shex.encode("ascii")).hexdigest()
                    eb["hidden_commitment"] = commit
                    eb["hidden_seed_snapshot"] = snap_sha
                    self._save()
                    return sb, commit
        except (FileNotFoundError, OSError, ValueError):
            pass
        sb = secrets.token_bytes(32)
        shex = sb.hex()
        commit = hashlib.sha256(shex.encode("ascii")).hexdigest()
        _write_json(self.run_dir, seed_rel, {
            "snapshot_sha256": snap_sha,
            "seed_hex": shex,
            "commitment": commit,
            "committed_at": _now(),
        })
        eb["hidden_commitment"] = commit
        eb["hidden_seed_snapshot"] = snap_sha
        self._save()
        return sb, commit

    def _reveal_hidden_seed(self) -> None:
        """Reveal hidden seed into `engine_build/hidden_reveal.json` at FINAL for post-run audits (§5.4, §7 Q5)."""
        from lib.safe_io import read_nofollow
        seed_rel = "engine_build/.hidden/seed.json"
        if not os.path.lexists(os.path.join(self.run_dir, seed_rel)):
            return
        try:
            raw, st_f = read_nofollow(self.run_dir, seed_rel)
            if st_f.st_nlink > 1:
                return
            doc = json.loads(raw.decode("utf-8"))
            if isinstance(doc, dict):
                _write_json(self.run_dir, "engine_build/hidden_reveal.json", dict(doc, revealed_at=_now()))
        except (OSError, ValueError):
            return

    def _invalidate_engine_admission(self) -> None:
        """Clear stale admission/review/signoff artifacts before rebuilding a built engine."""
        from lib.safe_io import remove_nofollow
        eb = self.st.engine_build
        if isinstance(eb, dict):
            eb["status"] = "building"
            eb["snapshot_sha256"] = None
            eb["admission_sha256"] = None
            eb["hidden_commitment"] = None
            eb["hidden_seed_snapshot"] = None
            eb["compute_reservation_id"] = None
        for stale_rel in ("engine_build/admission.json", "engine_build/admission_sheet.md",
                          "engine_build/signoff.json", "engine_build/decline.json",
                          "engine_build/review_status.json", "engine_build/.hidden/seed.json",
                          "critique_engine.yaml"):
            remove_nofollow(self.run_dir, stale_rel, missing_ok=True)

    def _begin_engine_repair(self, why: str, findings: list[str] | None = None):
        """Route a GATE/FIX_LOOP finding with `target_stage: ENGINE` into the single allowed engine repair
        (§6.1, BE30): clears post_write, increments round_pending, withholds report, removes old sign-off,
        resets engine_choice.json to pending-build, and transitions to ENGINE_BUILD."""
        eb = self.st.engine_build
        eb["repairs"] = int(eb.get("repairs") or 0) + 1
        self._invalidate_engine_admission()
        self.st.round_pending = True
        self.st.post_write = False
        self.st.engine = None
        _withhold_report(self.run_dir)
        self._update_pending_choice_status(str(eb["id"]), "building")
        fb = list(findings or []) or [f"post-analysis engine repair: {why}"]
        self.st.tries["engine_build_feedback"] = fb
        return self._go("ENGINE_BUILD", f"engine repair {eb['repairs']}/{MAX_ENGINE_REPAIRS}: {why}")

    def h_ENGINE_BUILD(self):
        from toolkit.engines.built import snapshot as BSNAP
        eb = self.st.engine_build
        if not isinstance(eb, dict) or not eb.get("id"):
            return self._go("FINAL", "ENGINE_BUILD entered with no build request", "run_failed")
        bid = str(eb["id"])
        try:
            raw_spec = self._spec()
            build_decl = dict(raw_spec["engine"]["build"])
        except Exception as e:  # noqa: BLE001
            return self._go("FINAL", f"cannot read engine.build from decision_spec.yaml: {e}", "run_failed")
        n_used = int(eb.get("tries") or 0)
        max_tries = self._max_engine_build_tries()
        snaps_dir = os.path.join(self.run_dir, "engine_build", "snapshots")
        if n_used >= max_tries:
            self._write_request_for_owner("maximum engine build tries exhausted",
                                          list(self.st.tries.get("engine_build_feedback") or []))
            eb["status"] = "refused"
            return self._go("FINAL", f"engine build exhausted {max_tries} try/tries", ENGINE_NOT_ADMITTED)

        wdir_rel = f"models/ext/engine_{bid}"
        wdir = os.path.join(self.run_dir, wdir_rel)
        if os.path.isdir(wdir) and not os.path.islink(wdir):
            from lib.safe_io import remove_nofollow
            for entry in list(os.listdir(wdir)):
                if not BSNAP._allowed_filename(entry):
                    ep = os.path.join(wdir, entry)
                    if not os.path.isdir(ep) or os.path.islink(ep):
                        remove_nofollow(self.run_dir, f"{wdir_rel}/{entry}", missing_ok=True)
        fb = list(self.st.tries.get("engine_build_feedback") or [])
        packet = self._build_engine_packet(build_decl, fb)
        try_num = n_used + 1
        res = self._task(
            f"ENGINE_BUILD#{self._visit()}:try{try_num}",
            "build_engine",
            self.run_dir,
            packet,
            bid,
            cap_override=ENGINE_BUILD_TRY_CAP_USD,
            build_budget=True,
        )
        if res is None:
            return self._budget_out("ENGINE_BUILD")
        eb["tries"] = try_num
        static_probs: list[str] = []
        try:
            files = BSNAP.read_working_copy(wdir)
            static_probs.extend(BSNAP.check_static(files, request=build_decl))
        except BSNAP.SnapshotError as e:
            static_probs.append(f"working copy invalid: {e}")
        if static_probs and not res.ok:
            static_probs.insert(0, f"build_engine task failed: {res.note}")
        if static_probs:
            self.st.tries["engine_build_feedback"] = static_probs[:40]
            if try_num < max_tries:
                return self._go("ENGINE_BUILD", f"static check failed on try {try_num}: {static_probs[0][:160]}")
            self._write_request_for_owner("static checks failed on final try", static_probs)
            eb["status"] = "refused"
            return self._go("FINAL", f"engine build failed static checks after {try_num} tries", ENGINE_NOT_ADMITTED)

        man = BSNAP.create_snapshot(wdir, snaps_dir, run_static=True, request=build_decl)
        snap_sha = man["snapshot_sha256"]
        eb["snapshot_sha256"] = snap_sha
        eb["status"] = "building"
        self.st.tries.pop("engine_build_feedback", None)
        self._update_pending_choice_status(bid, "building")
        self._save()
        return self._go("ENGINE_ADMIT", f"snapshot {snap_sha[:12]} built (try {try_num}/{max_tries})")

    def h_ENGINE_ADMIT(self):
        from agents import run_budget as RB
        from toolkit.engines.built import admission as BADM
        eb = self.st.engine_build
        snap_sha = eb.get("snapshot_sha256")
        if not isinstance(snap_sha, str) or len(snap_sha) != 64:
            return self._go("FINAL", "ENGINE_ADMIT entered without a snapshot", "run_failed")
        try:
            spec_raw = self._spec()
            build_decl = dict(spec_raw["engine"]["build"])
        except Exception as e:  # noqa: BLE001
            return self._go("FINAL", f"cannot read decision_spec.yaml for admission: {e}", "run_failed")

        # BE33: reserve admission compute against the run balance BEFORE any dispatch, reusing an
        # existing active reservation (including an orphaned one after crash-before-save_state).
        cap_usd = self.st.budget_usd
        res_id = None
        prev_res_id = eb.get("compute_reservation_id")
        ledger0 = RB.load_budget_ledger(self.run_dir)
        for sess in reversed(ledger0.get("sessions") or []):
            if not isinstance(sess, dict) or sess.get("status") != "reserved":
                continue
            if prev_res_id and sess.get("session_id") == prev_res_id:
                if float(sess.get("reserved_usd") or 0.0) + 1e-9 >= ENGINE_ADMIT_COMPUTE_CAP_USD:
                    res_id = prev_res_id
                    break
            elif (not prev_res_id and sess.get("stage") == "ENGINE_ADMIT"
                  and sess.get("label") == "admission_compute"
                  and float(sess.get("reserved_usd") or 0.0) + 1e-9 >= ENGINE_ADMIT_COMPUTE_CAP_USD):
                res_id = str(sess.get("session_id"))
                eb["compute_reservation_id"] = res_id
                eb["compute_reserved_usd"] = float(sess.get("reserved_usd") or ENGINE_ADMIT_COMPUTE_CAP_USD)
                self._save()
                break
        if res_id is None:
            if cap_usd is not None:
                rem_state = round(cap_usd - self.st.spent_usd, 4)
                if rem_state + 1e-9 < ENGINE_ADMIT_COMPUTE_CAP_USD:
                    self.st.paused, self.st.status = AWAITING_BUDGET, None
                    self.st.history.append({
                        "at": _now(), "paused": AWAITING_BUDGET, "reason": "engine_admit_compute",
                        "state": "ENGINE_ADMIT", "need_usd": ENGINE_ADMIT_COMPUTE_CAP_USD,
                        "left_usd": rem_state, "cap_usd": cap_usd, "spent_usd": round(self.st.spent_usd, 4),
                    })
                    self._save()
                    self.log(f"[machine] ENGINE_ADMIT paused before dispatch: remaining ${rem_state:.2f} < "
                             f"${ENGINE_ADMIT_COMPUTE_CAP_USD:.2f} admission compute cap")
                    return "ENGINE_ADMIT"
            ok_res, res_or_why = RB.reserve_compute(
                self.run_dir, ENGINE_ADMIT_COMPUTE_CAP_USD, cap_usd=cap_usd,
                stage="ENGINE_ADMIT", label="admission_compute",
            )
            if not ok_res:
                self.st.paused, self.st.status = AWAITING_BUDGET, None
                self.st.history.append({
                    "at": _now(), "paused": AWAITING_BUDGET, "reason": "engine_admit_compute",
                    "state": "ENGINE_ADMIT", "need_usd": ENGINE_ADMIT_COMPUTE_CAP_USD,
                    "left_usd": None, "why": str(res_or_why), "cap_usd": cap_usd,
                    "spent_usd": round(self.st.spent_usd, 4),
                })
                self._save()
                return "ENGINE_ADMIT"
            res_id = str(res_or_why)
            eb["compute_reservation_id"] = res_id
            eb["compute_reserved_usd"] = ENGINE_ADMIT_COMPUTE_CAP_USD
            self._save()

        # BE31: draw or reuse committed hidden seed for this snapshot (fails closed if missing/tampered)
        try:
            _seed_bytes, commit = self._ensure_hidden_seed(snap_sha)
        except RuntimeError as e:
            RB.reconcile_compute(self.run_dir, res_id, 0.0, cap_usd=cap_usd)
            eb["compute_reservation_id"] = None
            eb["compute_reserved_usd"] = 0.0
            _withhold_report(self.run_dir)
            return self._go("FINAL", f"hidden seed tampered: {e}", "artifact_tampered")

        # Load verified independent A11 reference snapshot (fail closed if a recorded snapshot was tampered;
        # only allow working reference.py when NO reference_snapshot_sha256 was ever recorded).
        ref_files = None
        ref_sha = eb.get("reference_snapshot_sha256")
        ref_snaps = os.path.join(self.run_dir, "engine_build", "reference", "snapshots")
        if isinstance(ref_sha, str) and len(ref_sha) == 64:
            from toolkit.engines.built import snapshot as BSNAP
            try:
                ref_files = BSNAP.read_verified_snapshot(ref_snaps, ref_sha)
            except (BSNAP.SnapshotError, OSError) as e:
                RB.reconcile_compute(self.run_dir, res_id, 0.0, cap_usd=cap_usd)
                eb["compute_reservation_id"] = None
                eb["compute_reserved_usd"] = 0.0
                _withhold_report(self.run_dir)
                return self._go("FINAL", f"reference snapshot tampered: {e}", "artifact_tampered")
        elif eb.get("reference_status") != "failed":
            ref_dir = os.path.join(self.run_dir, "engine_build", "reference")
            if os.path.isdir(ref_dir) and os.path.isfile(os.path.join(ref_dir, "reference.py")):
                try:
                    from lib.safe_io import read_nofollow
                    ref_raw, ref_st = read_nofollow(self.run_dir, "engine_build/reference/reference.py")
                    if ref_st.st_nlink == 1:
                        ref_files = {"engine.py": ref_raw, "card.yaml": b"name: ref\n", "ENGINE_NOTES.md": b"# ref\n"}
                except (OSError, ValueError):
                    ref_files = None
        dossier_bytes = None
        doss_rel = ((spec_raw.get("evidence") or {}).get("dossier")
                    if isinstance(spec_raw.get("evidence"), dict) else None) or "evidence_dossier.yaml"
        if isinstance(doss_rel, str) and os.path.lexists(os.path.join(self.run_dir, doss_rel)):
            try:
                from lib.safe_io import read_nofollow
                d_raw, d_st = read_nofollow(self.run_dir, doss_rel)
                if d_st.st_nlink == 1:
                    dossier_bytes = d_raw
            except OSError:
                dossier_bytes = None
        from toolkit.engines import benchmark_coverage as BC
        from toolkit.engines.built import try_tool as BTRY
        req_ref = bool(eb.get("require_reference"))
        cfg_map = (build_decl.get("request") or {}).get("configs") or {}
        default_cfg = dict(cfg_map.get("default") or {}) if isinstance(cfg_map, dict) else {}
        actual_compute_usd = 0.0
        try:
            adm = BADM.admit(
                self.run_dir,
                snap_sha,
                build_decl,
                spec_raw,
                config=default_cfg,
                reference_files=ref_files,
                require_reference=req_ref,
                dossier_bytes=dossier_bytes,
                departures_status=BC.signature(self.run_dir),
                unsandboxed_ok=self.st.engine_unsandboxed,
            )
        finally:
            RB.reconcile_compute(self.run_dir, res_id, actual_compute_usd, cap_usd=cap_usd)
            eb["compute_reservation_id"] = None
            eb["compute_reserved_usd"] = 0.0
            self.st.spent_usd = round(self.st.spent_usd + actual_compute_usd, 4)

        from lib.safe_io import read_nofollow
        adm_sha = hashlib.sha256(read_nofollow(self.run_dir, "engine_build/admission.json")[0]).hexdigest()
        eb["admission_sha256"] = adm_sha
        eb["hidden_commitment"] = adm.get("hidden_commitment") or commit

        # Check SPEC amendment routing BEFORE accepting admission (A8).
        checks = adm.get("checks") or {}
        a8 = checks.get("A8") or {}
        other_gating_failed = [
            cid for cid in ("A1", "A2", "A4", "A5", "A6", "A11")
            if (checks.get(cid) or {}).get("status") not in ("pass", "skipped", "not_applicable")
        ]
        a8_missing_bm = any("no benchmark" in str(p) for p in (a8.get("problems") or []))
        n_used = int(eb.get("tries") or 0)
        max_tries = self._max_engine_build_tries()
        a8_amend = (
            a8.get("status") == "fail"
            and a8.get("routing") == "spec_amendment"
            and (a8_missing_bm or not other_gating_failed or n_used >= max_tries)
        )
        if a8_amend:
            probs = list(a8.get("problems") or [])
            claim_txt = "; ".join(probs[:6])
            fix_req = "Owner amendment: add signed engine_departures or decision_test"
            led = FL.load(self.run_dir)
            led["engine_admit_amendment"] = FL.Finding(
                id="engine_admit_amendment", kind="engine_admit_amendment", severity="HIGH",
                source="engine-admit", owner_state="SPEC", target_stage="SPEC",
                resolution_type="owner_decision", status="open", blocking=True,
                scope_declarable=False, first_seen_round=self.st.round, last_seen_round=self.st.round,
                claim=claim_txt, fix_requires=fix_req,
            )
            FL.save(self.run_dir, led)
            req_doc = {
                "round": self.st.round,
                "requested_at": _now(),
                "source": "ENGINE_ADMIT",
                "signed_spec_sha256": _spec_signed_sha(self.run_dir),
                "working_spec_sha256": signed_hash(self.run_dir),
                "briefs": [{"task": "ENGINE_ADMIT", "findings": ["A8"],
                            "brief": "; ".join(probs[:4])}],
                "findings": [{"id": "engine_admit_amendment", "severity": "HIGH",
                              "claim": claim_txt, "fix_requires": fix_req}],
                "next": "amend decision_spec.yaml (add engine_departures or decision_test) and relaunch with reuse",
            }
            _write_json(self.run_dir, SPEC_RETHINK_REQUEST, req_doc)
            self.st.tries["spec_amendment"] = True
            return self._go("SPEC", "admission requires signed spec amendment (A8)")

        if adm.get("status") == "admitted":
            eb["status"] = "admitted"
            self._update_pending_choice_status(str(eb["id"]), "admitted")
            return self._go("ENGINE_REVIEW", f"admitted snapshot {snap_sha[:12]}")

        # Failed admission: fail fast if A11 is permanently unavailable because reference build failed;
        # otherwise retry ENGINE_BUILD if tries remain, else FINAL completed_engine_not_admitted.
        # NEVER copy hidden check problem strings (`adm["checks"][cid]["problems"]`) into builder feedback (§4.6).
        fb_lines: list[str] = []
        if adm.get("static_problems"):
            fb_lines += [f"static: {p}" for p in adm["static_problems"][:12]]
        failed_ids = list((adm.get("feedback") or {}).get("failed_checks") or [])
        if (checks.get("A11") or {}).get("status") == "unavailable" and eb.get("reference_status") == "failed":
            fb_lines.append("A11 independent reference implementation failed to build; cannot admit without reference")
            eb["status"] = "refused"
            self._write_request_for_owner(f"admission failed on checks {failed_ids}", fb_lines)
            return self._go("FINAL", "independent reference unavailable after retries (A11)", ENGINE_NOT_ADMITTED)
        pub_checks = [cid for cid in failed_ids if cid in BTRY.VALID_PUBLIC_CHECKS]
        if pub_checks:
            fb_lines.append(
                f"admission failed on check(s): {failed_ids} — run mcp__engine__try(checks={pub_checks!r}) on your "
                "working copy to inspect public diagnostics"
            )
        elif failed_ids:
            fb_lines.append(f"admission failed on check(s): {failed_ids}")
        if n_used < max_tries:
            self.st.tries["engine_build_feedback"] = fb_lines
            return self._go("ENGINE_BUILD", f"admission failed ({', '.join(failed_ids[:4])}); try {n_used}/{max_tries}")
        eb["status"] = "refused"
        self._write_request_for_owner(f"admission failed on checks {failed_ids}", fb_lines)
        return self._go("FINAL", f"engine not admitted after {n_used} tries ({', '.join(failed_ids[:4])})",
                        ENGINE_NOT_ADMITTED)

    def h_ENGINE_REVIEW(self):
        from lib.safe_io import remove_nofollow
        from toolkit.engines.built import signoff as BSIG
        eb = self.st.engine_build
        snap_sha = eb.get("snapshot_sha256")
        v = self._visit()
        repairs = int(self.st.tries.get("engine_review_repairs") or 0)
        fb = self.st.tries.get("engine_review_feedback")
        jkey = f"ENGINE_REVIEW#{v}:r{self.st.round}:a{repairs + 1}"
        if repairs == 0 and (self.st.journal.get(jkey) or {}).get("status") != "completed":
            # Remove any stale critique_engine.yaml from a prior snapshot before a fresh review task runs
            remove_nofollow(self.run_dir, BSIG.REVIEW_FILE, missing_ok=True)
        res = self._task(
            jkey,
            "critique_engine",
            self.run_dir,
            self.st.round,
            fb,
            cap_override=ENGINE_REVIEW_CAP_USD,
            build_budget=True,
        )
        if res is None:
            return self._budget_out("ENGINE_REVIEW")
        cpath = os.path.join(self.run_dir, BSIG.REVIEW_FILE)
        doc, errs = (BSIG.validate_engine_critique(cpath, self.st.round)
                     if os.path.isfile(cpath)
                     else (None, [f"critique_engine task failed: {res.note or 'no output'}"]))
        if doc is None and not res.ok:
            errs = [f"critique_engine task failed: {res.note or 'no output'}"] + errs
        if doc is None:
            if repairs < 1:
                self.st.tries["engine_review_repairs"] = repairs + 1
                self.st.tries["engine_review_feedback"] = errs
                return self._go("ENGINE_REVIEW", f"critique_engine.yaml malformed ({errs[0][:120]}); 1 repair")
            self.st.tries.pop("engine_review_repairs", None)
            self.st.tries.pop("engine_review_feedback", None)
            _write_json(self.run_dir, BSIG.REVIEW_STATUS_REL, {
                "snapshot_sha256": snap_sha, "review_unavailable": True, "errors": errs, "recorded_at": _now(),
            })
            eb["status"] = "reviewed"
            return self._go("ENGINE_SIGN", "engine review unavailable after repair (blocks lead sign-off)")

        self.st.tries.pop("engine_review_repairs", None)
        self.st.tries.pop("engine_review_feedback", None)
        open_high = [b for b in (doc.get("blockers") or []) if not b.get("resolved") and b.get("severity") == "HIGH"]
        open_med = [b for b in (doc.get("blockers") or []) if not b.get("resolved") and b.get("severity") == "MEDIUM"]
        _write_json(self.run_dir, BSIG.REVIEW_STATUS_REL, {
            "snapshot_sha256": snap_sha,
            "review_unavailable": False,
            "verdict": doc.get("verdict"),
            "high_count": len(open_high),
            "medium_count": len(open_med),
            "recorded_at": _now(),
        })
        n_used = int(eb.get("tries") or 0)
        if open_high and n_used < self._max_engine_build_tries():
            self._invalidate_engine_admission()
            self.st.tries["engine_build_feedback"] = [
                f"engine reviewer {b['id']} ({b['severity']}): {b['claim']} Required: {b['fix_requires']}"
                for b in open_high
            ]
            return self._go("ENGINE_BUILD", f"critique-engine raised {len(open_high)} HIGH finding(s); rebuilding")
        eb["status"] = "reviewed"
        return self._go("ENGINE_SIGN", f"engine review {doc.get('verdict')} (HIGH={len(open_high)})")

    def h_ENGINE_SIGN(self):
        from toolkit.engines.built import BUILT_ADAPTER_REF
        from toolkit.engines.built import signoff as BSIG
        eb = self.st.engine_build
        st_sig = BSIG.status(self.run_dir)
        if st_sig == "declined":
            dec, _ = BSIG._read_json_optional(self.run_dir, BSIG.DECLINE_REL)
            dec_by = (dec or {}).get("by")
            if self.st.signoff == "owner" and dec_by != "owner":
                self.st.paused = AWAITING_ENGINE_SIGNOFF
                self._save()
                return "ENGINE_SIGN"
            note = (dec or {}).get("note") or "declined"
            n_used = int(eb.get("tries") or 0)
            if n_used < self._max_engine_build_tries():
                self._invalidate_engine_admission()
                self.st.tries["engine_build_feedback"] = [f"owner/lead declined admitted engine: {note}"]
                return self._go("ENGINE_BUILD", f"engine declined with try left: {note[:120]}")
            eb["status"] = "declined"
            return self._go("FINAL", f"admitted engine declined: {note[:200]}", ENGINE_DECLINED)

        if st_sig == "awaiting" and self.st.signoff == "lead":
            try:
                BSIG.sign(self.run_dir, "lead", note="lead provisional engine sign-off (--signoff lead)")
                st_sig = BSIG.status(self.run_dir)
            except ValueError as e:
                # e.g. review_unavailable or unresolved review HIGH -> pause for owner
                self.st.paused = AWAITING_ENGINE_SIGNOFF
                self._save()
                self.log(f"[machine] lead cannot sign built engine ({e}); paused for owner at ENGINE_SIGN")
                return "ENGINE_SIGN"

        if st_sig == "signed":
            so, _ = BSIG._read_json_optional(self.run_dir, BSIG.SIGNOFF_REL)
            by = (so or {}).get("by")
            if self.st.signoff == "owner" and by != "owner":
                self.st.paused = AWAITING_ENGINE_SIGNOFF
                self._save()
                return "ENGINE_SIGN"
            if by == "lead" and BSIG.review_blocks_lead(self.run_dir):
                self.st.paused = AWAITING_ENGINE_SIGNOFF
                self._save()
                return "ENGINE_SIGN"
            # Write final engine_choice.json resolving to BuiltEngine adapter (§5.1)
            try:
                raw_spec = self._spec()
                cfg_map = ((raw_spec.get("engine") or {}).get("build") or {}).get("request", {}).get("configs") or {}
                default_cfg = dict(cfg_map.get("default") or {}) if isinstance(cfg_map, dict) else {}
            except Exception:  # noqa: BLE001
                default_cfg = {}
            prev_ch = {}
            try:
                from lib.safe_io import read_nofollow
                prev_ch = json.loads(read_nofollow(self.run_dir, AB.ENGINE_CHOICE)[0].decode("utf-8"))
                if not isinstance(prev_ch, dict):
                    prev_ch = {}
            except (OSError, ValueError):
                prev_ch = {}
            cands = prev_ch.get("candidates") or []
            choice_doc = {
                "at": _now(),
                "candidates": cands,
                "build": {"id": eb.get("id"), "status": "signed", "snapshot_sha256": eb.get("snapshot_sha256")},
                "engine": {"ref": BUILT_ADAPTER_REF, "config": default_cfg},
                "why": f"admitted and {by}-signed agent-built engine {eb.get('id')}",
            }
            if "engine_departures" in prev_ch:
                choice_doc["engine_departures"] = prev_ch["engine_departures"]
            _write_json(self.run_dir, AB.ENGINE_CHOICE, choice_doc)
            eb["status"] = "signed"
            self.st.engine = BUILT_ADAPTER_REF
            return self._go("ANALYZE", f"built engine {eb.get('id')} signed by {by}")

        if st_sig == "changed":
            _withhold_report(self.run_dir)
            return self._go("FINAL", "admitted built-engine evidence changed before/after sign-off", "artifact_tampered")

        self.st.paused = AWAITING_ENGINE_SIGNOFF
        self._save()
        self.log(f"[machine] built engine {eb.get('id')} awaiting owner sign-off: inspect "
                 f"engine_build/admission_sheet.md and run "
                 f"python3 toolkit/engines/built/signoff.py sign {self.run_dir} --by owner")
        return "ENGINE_SIGN"


def _self_test_built_engine(ok, td: str) -> None:
    """BE28-BE31, BE33 (PR delta of #363): agent-built engine lifecycle, pause/sign/decline/retry,
    A8 spec-amendment routing, single post-analysis ENGINE repair edge, committed hidden-seed reuse
    + run-lock enforcement in owner mode, pre-dispatch compute reservation, build-cap pause/resume,
    lead provisional sign-off + first-paragraph report disclaimer, and snapshot tamper detection."""
    import shutil
    import threading
    from agents import run_finish as RF
    from toolkit.engines.built import BUILT_ADAPTER_REF
    from toolkit.engines.built import admission as BADM
    from toolkit.engines.built import signoff as BSIG
    from toolkit.engines.built import worker as BWORK
    from agents.v2_tasks import StubTasks
    from agents.stage_machine import (
        AWAITING_SPEC_AMENDMENT, Machine, RunLocked, SIGNOFF_FILE, _run_lock, _signoff, init_run, load_state, run_v2,
        save_state, signed_hash,
    )

    real_b = AB.backend()
    if type(real_b).__name__ != "_ToolkitApi":
        ok(True, "BE28-BE33 skipped: toolkit backend not active")
        return

    dev_un = BWORK.engine_sandbox_kind() is None

    def _cf_build(*, include_trial_oob: bool = False) -> dict:
        lo_t, hi_t = (0.80, 0.90) if include_trial_oob else (0.50, 0.75)
        return {
            "id": "cf_mod",
            "kind": "closed_form",
            "why": "Needs custom_coupled_hazard_scaling not represented on any registered engine card",
            "mechanisms": ["custom_coupled_hazard_scaling", "exponential_survival"],
            "request": {
                "stochastic": False,
                "per_capita_invariant_to_N": True,
                "components": {"comp_a": {"values": [0, 1], "absent": 0}},
                "unit_attributes": {
                    "N": {"type": "continuous", "range": [1000.0, 10000.0]},
                    "haz": {"type": "continuous", "range": [0.1, 0.5]},
                },
                "outputs": {
                    "per_capita": ["cases"],
                    "windows": {"w0": ["surv"]},
                    "cost_drivers": ["units_delivered"],
                },
                "age_groups": ["all"],
                "parameters": [
                    {"name": "base_scale", "value": 1.0, "kind": "fixed", "lo": 0.5, "hi": 1.5,
                     "units": "ratio", "source": "dossier:E1"},
                ],
                "attribute_binding": {"N": "required", "haz": "required"},
                "decision_inputs": {
                    "comp_a": {"inputs": [], "none_reason": "synthetic self-test component"},
                },
                "expected_effects": {
                    "comp_a": {"cases": "-", "surv": "+", "units_delivered": "+"},
                    "base_scale": {"cases": "+", "surv": "-"},
                    "haz": {"cases": "+", "surv": "-"},
                },
                "inert": {"N": {"reason": "per-capita outcomes invariant to N"}},
                "seed_test_setting": {"unit": {"N": 5000.0, "haz": 0.25}},
                "reductions": [
                    {
                        "id": "RED_SURV",
                        "oracle": "constant_hazard_survival",
                        "oracle_args": {"hazard": 0.2, "dt": 1.0},
                        "set": {"comp_a": 0, "base_scale": 1.0},
                        "unit": {"N": 5000.0, "haz": 0.2},
                        "horizon": {"start": 0.0, "stop": 1.0, "window": [0.0, 1.0],
                                    "windows": {"w0": [0.0, 1.0]}},
                        "compare": {"windows.w0.surv": {"abs": 1e-6}},
                    },
                ],
                "trials": [
                    {
                        "id": "TR1",
                        "dossier": "E1",
                        "component": "comp_a",
                        "arms": {"ctrl": {"comp_a": 0}, "trt": {"comp_a": 1}},
                        "setting": {"unit": {"N": 5000.0, "haz": 0.2}},
                        "window": [0.0, 1.0],
                        "estimand_type": "ratio",
                        "estimand_mapping": {"num": "trt", "den": "ctrl", "value": "per_capita.cases.all"},
                        "published": {"lower": lo_t, "upper": hi_t, "level": 0.95},
                    },
                ],
                "reference_tolerance": {"abs": 0.02, "rel": 0.04},
            },
        }

    def _cf_files(*, tag: str = "v1", bad_static: bool = False) -> dict[str, bytes]:
        if bad_static:
            eng_src = "import os\nclass Model:\n    pass\n"
        else:
            eng_src = (
                "import math\n"
                "from toolkit.engines.contract import EngineCard, Outcomes, ParamSpec\n"
                f"TAG = {tag!r}\n"
                "class Model:\n"
                "    def describe(self):\n"
                "        return EngineCard(\n"
                "            name='built:cf_mod', version='0.1',\n"
                "            mechanisms=['custom_coupled_hazard_scaling', 'exponential_survival'],\n"
                "            components={'comp_a': [0, 1]},\n"
                "            unit_attributes={'N': 'pop', 'haz': 'hazard'},\n"
                "            outputs={'per_capita': ['cases'], 'windows': ['surv'], 'cost_drivers': ['units_delivered']},\n"
                "            age_groups=['all'], valid_ranges={'N': [1000.0, 10000.0], 'haz': [0.1, 0.5]},\n"
                "        )\n"
                "    def parameters(self):\n"
                "        return [ParamSpec('base_scale', 1.0, 'fixed', units='ratio', lo=0.5, hi=1.5, source='dossier:E1')]\n"
                "    def evaluate(self, params, unit, package, horizon):\n"
                "        dt = max(float(horizon.stop) - float(horizon.start), 1e-6)\n"
                "        haz = float(unit.attributes['haz'])\n"
                "        bs = float(params.get('base_scale', 1.0))\n"
                "        ca = int(package.components.get('comp_a', 0))\n"
                "        h_eff = haz * bs * (1.0 - 0.4 * ca)\n"
                "        surv = math.exp(-h_eff * dt)\n"
                "        cases = 1.0 - surv\n"
                "        return Outcomes(per_capita={'cases': {'all': cases}},\n"
                "                        windows={'w0': {'surv': surv}},\n"
                "                        cost_drivers={'units_delivered': float(ca)})\n"
            )
        card = (
            "name: built:cf_mod\nversion: '0.1'\nkind: closed_form\n"
            "mechanisms:\n  - custom_coupled_hazard_scaling\n  - exponential_survival\n"
            "components:\n  comp_a: [0, 1]\n"
            "unit_attributes:\n  N: pop\n  haz: hazard\n"
            "outputs:\n  per_capita: [cases]\n  windows: [surv]\n  cost_drivers: [units_delivered]\n"
            "age_groups: [all]\n"
            "valid_ranges:\n  N: [1000.0, 10000.0]\n  haz: [0.1, 0.5]\n"
            "benchmarks: []\nassumptions:\n  - constant hazard\nlimitations:\n  - synthetic\n"
        )
        return {
            "engine.py": eng_src.encode("utf-8"),
            "card.yaml": card.encode("utf-8"),
            "ENGINE_NOTES.md": f"# cf_mod {tag}\n\nConstant-hazard survival model.\n".encode("utf-8"),
        }

    def _cf_spec(rd: str, *, include_trial_oob: bool = False) -> dict:
        with open(os.path.join(rd, "units.csv"), "w", encoding="utf-8") as fh:
            fh.write("id,region,grp,haz,pop\n0,r0,g0,0.20,2000\n1,r1,g1,0.35,4000\n2,r0,g0,0.15,3000\n3,r1,g1,0.40,5000\n")
        return {
            "schema_version": 1,
            "question": "Which package per unit?",
            "decision_maker": "programme manager",
            "decision_date": 2026,
            "engine": {"build": _cf_build(include_trial_oob=include_trial_oob)},
            "horizon": {"start": 0, "stop": 1},
            "decision_units": {
                "csv": "units.csv", "id": "id", "region": "region",
                "populations": {"all": {"column": "pop", "age_groups": ["all"]}},
                "engine_attributes": {"N": {"column": "pop"}, "haz": {"column": "haz"}},
            },
            "options": {
                "populations": {"total": "pop"},
                "components": {"comp_a": {}},
                "packages": {"generate": True},
                "engine_map": {"comp_a": {"comp_a": 1}},
                "prices": {"base_year": "2024", "index": {"2024": 1.0}},
                "cost_items": [{"id": "ca", "component": "comp_a", "value": 1.0, "price_year": "2024",
                                "price_rule": "as_published", "basis": "per_person", "includes": ["x"]}],
            },
            "objective": {"outcome": "cases", "horizon": [0, 1], "discounting": "none"},
            "constraints": {"envelope_usd": 9000},
            "comparator": {"package": "none", "text": "no package"},
            "outcomes_reported": ["cases"],
            "calibration": {"method": "none"},
            "uncertainty": {
                "sources": [{"kind": "costs", "items": {"ca": [0.8, 1.2]}}],
                "excluded": [{"kind": k, "reason": "test"} for k in ("engine_parameters", "calibration", "emulator")],
            },
            "analysis": {
                "evaluation": "direct",
                "stability": {"holdout_group": "grp", "covariates": ["haz"]},
                "sensitivity": {"parameters": ["base_scale"]},
                "decision_tests": [],
            },
            "deliverable": {
                "kind": "package_per_unit",
                "allocation_file": "allocation_optimized.csv",
                "columns": {"unit": "unit", "package": "package", "value": "cases_averted"},
                "headline": {"name": "total_cases_averted", "outcome": "cases"},
            },
        }

    class _FastBuiltBackend:
        """Delegates spec/sign-off/choice to real `_ToolkitApi`, using a tiny no-op analyze step so
        state-machine tests exercise real admission, sign-off, tamper, and gate paths in ~1s."""
        settings_paths = getattr(real_b, "settings_paths", AB.SETTINGS_PATHS)

        def pre_sign_problems(self, rd, derived=False):
            return real_b.pre_sign_problems(rd, derived=derived)

        def signoff_status(self, rd):
            return real_b.signoff_status(rd)

        def sign(self, rd, by, note="", **kw):
            return real_b.sign(rd, by, note, **kw)

        def signed_sha(self, rd):
            return real_b.signed_sha(rd)

        def choice_spec(self, rd):
            return real_b.choice_spec(rd)

        def eligible_engines(self, spec):
            return []

        def validate(self, spec_path):
            return []

        def pipeline_steps(self, spec_path, run_dir):
            import shlex
            code = (
                "import json, os; "
                "os.makedirs('analysis', exist_ok=True); "
                "open('analysis/analysis.json', 'w').write(json.dumps({'headline': {'point': 10.0}})); "
                "open('analysis/allocation_optimized.csv', 'w').write('unit,package,cases_averted\\n0,comp_a,10.0\\n')"
            )
            return [{
                "id": "analyze",
                "run": f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                "inputs": ["decision_spec.yaml", "engine_choice.json", "engine_build/admission.json"],
                "outputs": ["analysis/analysis.json", "analysis/allocation_optimized.csv"],
            }]

        def gate_findings(self, rd):
            return []

        def spec_violations(self, rd):
            return []

        def registry_violations(self, rd):
            return []

        def headline(self, rd):
            return {"point": 10.0}

        def render_tables(self, rd):
            return ""

    fast_b = _FastBuiltBackend()

    def _mk_run(name: str, *, signoff: str = "owner", include_trial_oob: bool = False,
                build_fn=None, critics=None, budget: float | None = None,
                engine_build_usd: float | None = None, max_rounds: int = 3,
                with_ref: bool = True):
        rd = os.path.join(td, name)
        os.makedirs(rd, exist_ok=True)
        if with_ref:
            ref_dir = os.path.join(rd, "engine_build", "reference")
            os.makedirs(ref_dir, exist_ok=True)
            open(os.path.join(ref_dir, "reference.py"), "wb").write(_cf_files(tag="ref")["engine.py"])
        sp = _cf_spec(rd, include_trial_oob=include_trial_oob)
        sc = {
            "spec": sp,
            "settings": {"uncertainty": {"draws": 4, "seed": 1}},
            "build_engine": build_fn if build_fn is not None else _cf_files(),
            "critics": critics or {},
        }
        st = init_run(rd, "Which package per unit?", max_rounds=max_rounds, signoff=signoff,
                      budget_usd=budget, derived_inputs=False, engine_unsandboxed=dev_un,
                      engine_build_usd=engine_build_usd)
        return rd, StubTasks(sc), st

    # BE28: owner lifecycle (pause awaiting_engine_signoff -> sign -> FINAL + hidden_reveal + owner disclaimer),
    # lead provisional auto-sign + provisional disclaimer, and post-sign & FINAL snapshot tamper -> artifact_tampered
    d28, T28, st28 = _mk_run("be28_owner", signoff="owner")
    s_sign = Machine(d28, T28, st28, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    fast_b.sign(d28, "owner", "approved spec")
    s_wait = Machine(d28, StubTasks(T28.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    ch_wait = json.load(open(os.path.join(d28, AB.ENGINE_CHOICE)))
    pending_ok = (s_wait == f"paused:{AWAITING_ENGINE_SIGNOFF}" and RF.exit_code_for(AWAITING_ENGINE_SIGNOFF) == 2
                  and "engine" not in ch_wait and (ch_wait.get("build") or {}).get("status") == "admitted"
                  and BSIG.status(d28) == "awaiting")
    BSIG.sign(d28, "owner", note="approved engine")
    s_done = Machine(d28, StubTasks(T28.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    ch_done = json.load(open(os.path.join(d28, AB.ENGINE_CHOICE)))
    rev_path = os.path.join(d28, "engine_build", "hidden_reveal.json")
    rep28 = open(os.path.join(d28, "report.md"), encoding="utf-8").read() if os.path.exists(os.path.join(d28, "report.md")) else ""
    lead_d, lead_T, lead_st = _mk_run("be28_lead", signoff="lead")
    s_lead = Machine(lead_d, lead_T, lead_st, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    rep_lead = open(os.path.join(lead_d, "report.md"), encoding="utf-8").read() if os.path.exists(os.path.join(lead_d, "report.md")) else ""
    so_lead = json.load(open(os.path.join(lead_d, BSIG.SIGNOFF_REL))) if os.path.exists(os.path.join(lead_d, BSIG.SIGNOFF_REL)) else {}
    # Tamper snapshot on a completed FINAL copy -> resuming FINAL detects tamper and withholds report
    tamp_d = os.path.join(td, "be28_tamper")
    shutil.copytree(lead_d, tamp_d)
    st_t = load_state(tamp_d)
    snap_t = st_t.engine_build["snapshot_sha256"]
    sdir_t = os.path.join(tamp_d, "engine_build", "snapshots", snap_t)
    os.chmod(sdir_t, 0o755)
    ep_t = os.path.join(sdir_t, "engine.py")
    os.chmod(ep_t, 0o644)
    open(ep_t, "ab").write(b"\n# tampered\n")
    s_tamp = Machine(tamp_d, StubTasks(lead_T.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    ok(s_sign == "paused:awaiting_signoff" and pending_ok
       and (s_done == "completed" or (dev_un and s_done == "run_failed"))
       and (ch_done.get("engine") or {}).get("ref") == BUILT_ADAPTER_REF
       and os.path.isfile(rev_path) and "Agent-built engine (owner-signed)" in rep28[:600]
       and (s_lead == "completed" or (dev_un and s_lead == "run_failed"))
       and so_lead.get("by") == "lead" and so_lead.get("provisional") is True
       and "Agent-built engine (lead-signed, provisional)" in rep_lead[:600]
       and s_tamp == "artifact_tampered" and not os.path.exists(os.path.join(tamp_d, "report.md")),
       f"BE28 owner sign-off lifecycle, lead provisional sign-off + disclaimer, hidden_reveal, and FINAL tamper detection "
       f"(pending={pending_ok}, done={s_done}, lead={s_lead}, tamp={s_tamp})")

    # BE29a: static/admission failure twice -> completed_engine_not_admitted (exit 2) + request_for_owner.md;
    # default require_reference=False skips A11 cleanly; explicit require_reference=True without ref fails closed
    d29a, T29a, st29a = _mk_run("be29_fail", signoff="lead", build_fn=_cf_files(bad_static=True))
    s29a = Machine(d29a, T29a, st29a, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    d29_ok, T29_ok, st29_ok = _mk_run("be29_noref_default", signoff="lead", with_ref=False)
    s29_ok = Machine(d29_ok, T29_ok, st29_ok, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    d29r, T29r, st29r = _mk_run("be29_noref_req", signoff="lead", with_ref=False)
    st29r.engine_build["require_reference"] = True
    save_state(d29r, st29r)
    s29r = Machine(d29r, T29r, st29r, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    # BE29b: A8 out-of-band without departure -> ENGINE_ADMIT -> SPEC -> paused:awaiting_spec_amendment
    d29b, T29b, st29b = _mk_run("be29_a8", signoff="owner", include_trial_oob=True)
    Machine(d29b, T29b, st29b, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    fast_b.sign(d29b, "owner", "approved spec")
    s29b = Machine(d29b, StubTasks(T29b.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    rq29b = json.load(open(os.path.join(d29b, SPEC_RETHINK_REQUEST))) if os.path.exists(os.path.join(d29b, SPEC_RETHINK_REQUEST)) else {}
    # BE29c: owner decline on try 1 rebuilds; decline on final try -> completed_engine_declined (exit 2)
    decl_n = [0]

    def _build_tagged(_rd, _pk, _eid):
        decl_n[0] += 1
        return _cf_files(tag=f"try{decl_n[0]}")

    d29c, T29c, st29c = _mk_run("be29_decline", signoff="owner", build_fn=_build_tagged)
    Machine(d29c, T29c, st29c, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    fast_b.sign(d29c, "owner", "approved spec")
    s29c1 = Machine(d29c, StubTasks(T29c.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    BSIG.decline(d29c, "owner", note="need extra detail on try 1")
    s29c2 = Machine(d29c, StubTasks(T29c.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    BSIG.decline(d29c, "owner", note="still unacceptable on try 2")
    s29c3 = Machine(d29c, StubTasks(T29c.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    ok(s29a == ENGINE_NOT_ADMITTED and RF.exit_code_for(s29a) == 2
       and (s29_ok == "completed" or (dev_un and s29_ok == "run_failed"))
       and s29r == ENGINE_NOT_ADMITTED
       and os.path.isfile(os.path.join(d29a, "engine_build", "request_for_owner.md"))
       and s29b == f"paused:{AWAITING_SPEC_AMENDMENT}" and rq29b.get("source") == "ENGINE_ADMIT"
       and s29c1 == f"paused:{AWAITING_ENGINE_SIGNOFF}" and s29c2 == f"paused:{AWAITING_ENGINE_SIGNOFF}"
       and decl_n[0] == 2 and s29c3 == ENGINE_DECLINED and RF.exit_code_for(s29c3) == 2,
       f"BE29 not-admitted (exit 2), optional A11 default + explicit require_reference fail-closed, A8 amendment, decline "
       f"({s29a}, noref_default={s29_ok}, noref_req={s29r}, {s29b}, {s29c1}->{s29c2}->{s29c3}, builds={decl_n[0]})")

    # BE30: post-analysis GATE finding with target_stage=ENGINE triggers 1 repair (new hidden seed) and refuses 2nd
    rep_n = [0]
    commits_30: list[str] = []

    def _build_30(_rd, _pk, _eid):
        rep_n[0] += 1
        return _cf_files(tag=f"rep{rep_n[0]}")

    eng_bl1 = {"id": "D-301", "severity": "HIGH", "target_stage": "ENGINE", "claim": "missing age effect",
               "done_when": [{"judgement": "engine represents age effect"}]}
    eng_bl2 = {"id": "D-302", "severity": "HIGH", "target_stage": "ENGINE", "claim": "second engine issue",
               "done_when": [{"judgement": "second engine issue addressed"}]}
    d30, T30, st30 = _mk_run("be30_repair", signoff="lead", build_fn=_build_30, max_rounds=2,
                             critics={1: {"critique-domain": [eng_bl1]}, 2: {"critique-domain": [eng_bl2]}})
    m30 = Machine(d30, T30, st30, backend=fast_b, log=lambda *a: None, cross_engine_check=False)
    for _ in range(60):
        if m30.st.state == "FINAL" or m30.st.paused:
            break
        m30.step()
        if m30.st.state == "ENGINE_REVIEW":
            c30 = (m30.st.engine_build or {}).get("hidden_commitment")
            if c30 and c30 not in commits_30:
                commits_30.append(c30)
    st30_end = load_state(d30)
    eb_visits = sum(1 for h in st30_end.history if h.get("to") == "ENGINE_BUILD")
    refused_30 = any(h.get("engine_repair_refused") for h in st30_end.history)
    ok(eb_visits == 2 and int((st30_end.engine_build or {}).get("repairs") or 0) == 1
       and len(commits_30) == 2 and refused_30,
       f"BE30 single target_stage=ENGINE repair gets new hidden seed; 2nd repair refused "
       f"(visits={eb_visits}, repairs={(st30_end.engine_build or {}).get('repairs')}, "
       f"commits={len(commits_30)}, refused={refused_30})")

    # BE31: committed hidden seed reused on resume; missing seed file after commitment fails closed;
    # _run_lock held in another thread refuses concurrent owner resume
    d31, T31, st31 = _mk_run("be31_lock", signoff="owner")
    st31.state = "ENGINE_ADMIT"
    st31.engine_build = {"id": "cf_mod", "kind": "closed_form", "status": "building",
                         "tries": 1, "repairs": 0, "spent_usd": 0.0, "snapshot_sha256": "a" * 64}
    save_state(d31, st31)
    m31 = Machine(d31, T31, None, backend=fast_b, log=lambda *a: None, cross_engine_check=False)
    sb1, cm1 = m31._ensure_hidden_seed("a" * 64)
    sb2, cm2 = m31._ensure_hidden_seed("a" * 64)
    os.remove(os.path.join(d31, "engine_build", ".hidden", "seed.json"))
    missing_seed_refused = False
    try:
        m31._ensure_hidden_seed("a" * 64)
    except RuntimeError:
        missing_seed_refused = True
    locked_ok = [False]
    ready_ev, release_ev = threading.Event(), threading.Event()

    def _hold():
        with _run_lock(d31):
            ready_ev.set()
            release_ev.wait(5.0)

    th = threading.Thread(target=_hold, daemon=True)
    th.start()
    ready_ev.wait(5.0)
    try:
        try:
            Machine(d31, StubTasks(T31.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False)
        except RunLocked:
            locked_ok[0] = True
    finally:
        release_ev.set()
        th.join(5.0)
    ok(sb1 == sb2 and cm1 == cm2 and missing_seed_refused and locked_ok[0],
       f"BE31 committed hidden seed reused on resume, missing seed fails closed, and _run_lock refuses concurrent owner resume "
       f"(same_seed={sb1 == sb2}, miss_ref={missing_seed_refused}, locked={locked_ok[0]})")

    # BE33: pre-dispatch compute reservation pauses before BADM.admit; interrupted build task charges lane budget;
    # engine_build_cap pauses & resumes via run_v2(..., engine_build_usd=40.0)
    d33, T33, st33 = _mk_run("be33_budget", signoff="lead", budget=20.0)
    m33_prep = Machine(d33, T33, st33, backend=fast_b, log=lambda *a: None, cross_engine_check=False)
    for _ in range(10):
        if m33_prep.st.state == "ENGINE_ADMIT" or m33_prep.st.state == "FINAL" or m33_prep.st.paused:
            break
        m33_prep.step()
    st33_ad = load_state(d33)
    st33_ad.spent_usd = 10.0       # remaining $10 < ENGINE_ADMIT_COMPUTE_CAP_USD ($15)
    save_state(d33, st33_ad)
    admit_calls = [0]
    orig_admit = BADM.admit

    def _spy_admit(*a, **kw):
        admit_calls[0] += 1
        return orig_admit(*a, **kw)

    BADM.admit = _spy_admit
    try:
        s33_adm = Machine(d33, StubTasks(T33.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    finally:
        BADM.admit = orig_admit
    # Build-cap pause (including interrupted build task charging engine_build.spent_usd) & resume via engine_build_usd=40
    d33b, T33b, st33b = _mk_run("be33_bcap", signoff="lead", budget=100.0, engine_build_usd=1.0)
    m33b_prep = Machine(d33b, T33b, st33b, backend=fast_b, log=lambda *a: None, cross_engine_check=False)
    for _ in range(10):
        if m33b_prep.st.state == "ENGINE_BUILD" or m33b_prep.st.state == "FINAL" or m33b_prep.st.paused:
            break
        m33b_prep.step()
    st33b = load_state(d33b)
    st33b.journal["ENGINE_BUILD#0:interrupted"] = {
        "task": "build_engine", "status": "started", "allowance_usd": 0.80, "build_budget": True, "readback_usd": 0.0,
    }
    save_state(d33b, st33b)
    s33_bcap = Machine(d33b, StubTasks(T33b.s), None, backend=fast_b, log=lambda *a: None, cross_engine_check=False).run()
    lane_charged = abs(float((load_state(d33b).engine_build or {}).get("spent_usd") or 0.0) - 0.80) < 1e-6
    # Raise build cap via run_v2 -> verify cap raised and build runs
    s33_res = run_v2("Which package per unit?", d33b, StubTasks(T33b.s), signoff="lead", budget_usd=100.0,
                     engine_build_usd=40.0, engine_unsandboxed=dev_un, backend=fast_b,
                     log=lambda *a: None, cross_engine_check=False)
    st33b_end = load_state(d33b)
    ok(s33_adm == f"paused:{AWAITING_BUDGET}" and admit_calls[0] == 0
       and lane_charged and s33_bcap == f"paused:{AWAITING_BUDGET}"
       and st33b_end.engine_build_cap_usd == 40.0
       and any(h.get("engine_build_cap_raised") for h in st33b_end.history)
       and not s33_res.startswith("paused:"),
       f"BE33 compute reservation pauses before BADM.admit (calls={admit_calls[0]}, {s33_adm}); "
       f"interrupted build charges lane ({lane_charged}), engine_build_cap pauses ({s33_bcap}) and resumes ({s33_res})")

    # BEG_GATE_WIRE: manifest size tampering is caught by Machine.harness_findings() via built_engine_gate
    d_gw = os.path.join(td, "be_gate_wire")
    shutil.copytree(lead_d, d_gw)
    snap_gw = (load_state(d_gw).engine_build or {}).get("snapshot_sha256")
    man_p = os.path.join(d_gw, "engine_build", "snapshots", f"{snap_gw}.json")
    man_doc = json.load(open(man_p))
    man_doc["files"][0]["size"] = int(man_doc["files"][0]["size"]) + 99
    with open(man_p, "w", encoding="utf-8") as _mf:
        json.dump(man_doc, _mf)
    hf_gw = Machine(d_gw, StubTasks(lead_T.s), load_state(d_gw), backend=fast_b, log=lambda *a: None, cross_engine_check=False).harness_findings()
    ok(any(f.get("kind") == "built_engine_snapshot_invalid" and "size mismatch" in str(f.get("claim")) for f in hf_gw),
       f"BEG_GATE_WIRE harness_findings runs built_engine_gate ({[f.get('kind') for f in hf_gw]})")




def _self_test() -> int:
    import tempfile
    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    with tempfile.TemporaryDirectory() as td:
        _self_test_built_engine(ok, td)

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    if sys.argv[1:2] == ["--self-test"]:
        from agents.engine_build_stage import _self_test as _run_self_test
        sys.exit(_run_self_test())
    sys.exit(2)
