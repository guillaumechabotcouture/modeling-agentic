#!/usr/bin/env python3
"""Try tool backend (`mcp__engine__try` & `mcp__engine__run`, docs/design/agent-built-engines.md §3.3).

    python3 toolkit/engines/built/try_tool.py --self-test

Gives the build task a safe, bounded way to run its working copy in the minimal-mount sandbox
without a shell:
- `engine_try(run_dir, checks="all")` snapshots `models/ext/engine_<id>/` into a scratch snapshot
  under `engine_build/try/snapshots/`, runs S1-S4 and ONLY the **public** cases of the named checks
  via `CandidateEngine`, writes `engine_build/try/<n>.json`, and returns a summary capped at 8 KB.
- `engine_run_one(run_dir, ...)` runs one sandboxed evaluation on inputs validated against the
  signed request's domains and returns capped `Outcomes` + balance summary.
- Both share a single persistent 30-call ledger (`engine_build/try/ledger.json`) under the run
  lock, so the cap survives crashes and restarts. Neither ever runs hidden cases. Nothing here
  names a disease.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from collections.abc import Mapping

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
for _p in (ROOT, os.path.join(ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib import safe_io as SIO  # noqa: E402
from toolkit.engines.contract import Horizon, Package, UnitSpec  # noqa: E402
from toolkit.engines.built import admission as BADM  # noqa: E402
from toolkit.engines.built import candidate as BCAND  # noqa: E402
from toolkit.engines.built import requested as BREQ  # noqa: E402
from toolkit.engines.built import snapshot as BSNAP  # noqa: E402

LEDGER_REL = "engine_build/try/ledger.json"


class TryCapExceeded(RuntimeError):
    """Raised when a build try reaches the harness call limit (default 30 calls per try)."""


def _load_ledger(run_dir: str) -> dict:
    try:
        raw, _ = SIO.read_nofollow(run_dir, LEDGER_REL)
        doc = json.loads(raw.decode("utf-8"))
        if isinstance(doc, dict) and isinstance(doc.get("calls"), list):
            return doc
    except (FileNotFoundError, SIO.LinkRefused, OSError, UnicodeDecodeError, ValueError):
        pass
    return {"count": 0, "calls": []}


def _consume_call(run_dir: str, kind: str, max_calls: int) -> tuple[int, dict]:
    ledger = _load_ledger(run_dir)
    count = int(ledger.get("count", len(ledger.get("calls") or [])))
    if count >= max_calls:
        raise TryCapExceeded(f"try tool call cap reached ({count}/{max_calls} calls used)")
    n = count + 1
    ledger["count"] = n
    ledger.setdefault("calls", []).append({
        "n": n,
        "tool": kind,
        "at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    })
    SIO.write_nofollow(run_dir, LEDGER_REL, json.dumps(ledger, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
    return n, ledger


def _load_spec_and_build(run_dir: str) -> tuple[dict, dict]:
    import yaml
    raw, _ = SIO.read_nofollow(run_dir, "decision_spec.yaml")
    spec_raw = yaml.safe_load(raw.decode("utf-8")) or {}
    eng = spec_raw.get("engine") if isinstance(spec_raw, Mapping) else None
    if not (isinstance(eng, Mapping) and isinstance(eng.get("build"), Mapping)):
        raise ValueError("decision_spec.yaml does not declare engine.build")
    return spec_raw, dict(eng["build"])


def _cap_summary(obj: dict, max_bytes: int) -> dict:
    raw = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    if len(raw) <= max_bytes:
        return obj
    trimmed = {
        "call_n": obj.get("call_n"),
        "ok": obj.get("ok"),
        "snapshot_sha256": obj.get("snapshot_sha256"),
        "static_problems": (obj.get("static_problems") or [])[:6],
        "checks": {
            cid: {
                "status": c.get("status"),
                "passed": c.get("passed"),
                "total": c.get("total"),
                "problems": (c.get("problems") or [])[:3],
            }
            for cid, c in (obj.get("checks") or {}).items()
        },
        "truncated": True,
        "full_record": obj.get("full_record"),
    }
    return trimmed


def engine_try(
    run_dir: str,
    checks: str | list[str] = "all",
    *,
    work_dir: str | None = None,
    config: Mapping | None = None,
    unsandboxed_ok: bool = False,
) -> dict:
    """`mcp__engine__try`: snapshot working copy into `engine_build/try/snapshots/`, run S1-S4
    and ONLY public cases of requested checks, write `engine_build/try/<n>.json`, return <= 8 KB summary."""
    th = BADM.load_thresholds()
    max_calls = int(th["try_max_calls"])
    max_bytes = int(th["try_summary_max_bytes"])
    spec_raw, build_decl = _load_spec_and_build(run_dir)
    n, _ = _consume_call(run_dir, "try", max_calls)
    bid = str(build_decl["id"])
    wdir = work_dir or os.path.join(run_dir, "models", "ext", f"engine_{bid}")
    snaps_dir = os.path.join(run_dir, "engine_build", "try", "snapshots")
    rec_rel = f"engine_build/try/{n}.json"
    limits = {"wall_s": int(th["try_wall_s"]), "memory_bytes": int(th["try_memory_bytes"])}

    try:
        files = BSNAP.read_working_copy(wdir)
    except BSNAP.SnapshotError as e:
        out = {"call_n": n, "ok": False, "static_problems": [str(e)], "checks": {}, "full_record": rec_rel}
        SIO.write_nofollow(run_dir, rec_rel, json.dumps(out, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
        return _cap_summary(out, max_bytes)

    static_probs = BSNAP.check_static(files, request=build_decl)
    if static_probs:
        out = {"call_n": n, "ok": False, "static_problems": static_probs, "checks": {}, "full_record": rec_rel}
        SIO.write_nofollow(run_dir, rec_rel, json.dumps(out, indent=1, sort_keys=True).encode("utf-8"), atomic=True)
        return _cap_summary(out, max_bytes)

    man = BSNAP.create_snapshot(wdir, snaps_dir, run_static=True, request=build_decl)
    snap_sha = man["snapshot_sha256"]
    verified = BSNAP.read_verified_snapshot(snaps_dir, snap_sha)
    cand = BCAND.CandidateEngine(
        verified, build_decl, config=config, run_dir=run_dir,
        unsandboxed_ok=unsandboxed_ok, limits=limits, id_salt="public_try",
    )
    selected = None if checks == "all" else [str(c).upper() for c in checks]
    pub = BADM.run_public_checks(cand, build_decl, spec_raw, checks=selected)
    out = {
        "call_n": n,
        "ok": bool(pub["ok"]),
        "snapshot_sha256": snap_sha,
        "static_problems": [],
        "checks": pub["checks"],
        "full_record": rec_rel,
    }
    SIO.write_nofollow(run_dir, rec_rel, json.dumps(out, indent=1, sort_keys=True, default=str).encode("utf-8"),
                       atomic=True)
    return _cap_summary(out, max_bytes)


def engine_run_one(
    run_dir: str,
    *,
    unit: Mapping,
    package: Mapping,
    params: Mapping | None = None,
    horizon: Mapping,
    seed: int = 1,
    work_dir: str | None = None,
    config: Mapping | None = None,
    unsandboxed_ok: bool = False,
) -> dict:
    """`mcp__engine__run`: validate inputs against the signed request domains, consume one call from
    the shared 30-call ledger, execute one sandboxed run, and return capped Outcomes + balance summary."""
    th = BADM.load_thresholds()
    max_calls = int(th["try_max_calls"])
    max_bytes = int(th["try_summary_max_bytes"])
    _spec_raw, build_decl = _load_spec_and_build(run_dir)
    req = build_decl["request"]

    # Validate unit attributes, package components, params, horizon against request domains
    errs: list[str] = []
    attr_specs = req.get("unit_attributes") or {}
    for k, v in (unit or {}).items():
        if k not in attr_specs:
            errs.append(f"unit.{k}: unknown unit attribute")
        else:
            errs += BREQ._check_attr_val(v, attr_specs[k], f"unit.{k}")
    comp_specs = req.get("components") or {}
    for k, v in (package or {}).items():
        if k not in comp_specs:
            errs.append(f"package.{k}: unknown component")
        elif not BREQ._val_in(v, comp_specs[k]["values"]):
            errs.append(f"package.{k}: {v!r} not in {comp_specs[k]['values']}")
    p_by_name = {p["name"]: p for p in (req.get("parameters") or [])}
    for k, v in (params or {}).items():
        if k not in p_by_name:
            errs.append(f"params.{k}: unknown parameter")
        else:
            errs += BREQ._check_param_val(v, p_by_name[k], f"params.{k}")
    errs += BREQ._check_horizon_map(horizon, "horizon")
    if errs:
        raise ValueError("mcp__engine__run invalid inputs: " + "; ".join(errs[:6]))

    n, _ = _consume_call(run_dir, "run", max_calls)
    bid = str(build_decl["id"])
    wdir = work_dir or os.path.join(run_dir, "models", "ext", f"engine_{bid}")
    snaps_dir = os.path.join(run_dir, "engine_build", "try", "snapshots")
    limits = {"wall_s": int(th["try_wall_s"]), "memory_bytes": int(th["try_memory_bytes"])}
    man = BSNAP.create_snapshot(wdir, snaps_dir, run_static=True, request=build_decl)
    verified = BSNAP.read_verified_snapshot(snaps_dir, man["snapshot_sha256"])
    cand = BCAND.CandidateEngine(
        verified, build_decl, config=config, run_dir=run_dir,
        unsandboxed_ok=unsandboxed_ok, limits=limits, id_salt="public_run",
    )
    hor = Horizon(
        float(horizon["start"]),
        float(horizon["stop"]),
        tuple(horizon.get("window") or (horizon["start"], horizon["stop"])),
        {str(k): list(v) for k, v in (horizon.get("windows") or {}).items()},
    )
    out = cand.run(dict(params or {}), UnitSpec("try_u", dict(unit)), Package("try_p", dict(package)), hor, int(seed))
    bal = (out.meta or {}).get("balance")
    bal_summary = None
    if isinstance(bal, dict):
        bal_summary = {
            "n_steps": bal.get("n_steps"),
            "initial_total": bal.get("initial_total"),
            "final_total": bal.get("final_total"),
            "max_rel_drift": bal.get("max_rel_drift"),
            "min_state": bal.get("min_state"),
        }
    res = {
        "call_n": n,
        "per_capita": out.per_capita,
        "windows": out.windows,
        "cost_drivers": out.cost_drivers,
        "age_shares": (out.meta or {}).get("age_shares"),
        "balance": bal_summary,
    }
    return _cap_summary(res, max_bytes)


def _self_test() -> int:
    import tempfile
    import yaml
    from toolkit.engines.built import fixtures as BFIX
    from toolkit.engines.built import worker as BWORK

    fails: list[str] = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    dev_un = BWORK.engine_sandbox_kind() is None
    with tempfile.TemporaryDirectory() as d:
        b = BFIX.two_group_sir_build()
        sp = BFIX.two_group_sir_spec(b)
        open(os.path.join(d, "decision_spec.yaml"), "w").write(yaml.safe_dump(sp, sort_keys=False))
        wdir = os.path.join(d, "models", "ext", f"engine_{b['id']}")
        os.makedirs(wdir, exist_ok=True)
        for fn, data in BFIX.two_group_sir_files().items():
            open(os.path.join(wdir, fn), "wb").write(data)
        r1 = engine_try(d, checks=["A1", "A4"], unsandboxed_ok=dev_un)
        r2 = engine_run_one(
            d,
            unit={"N": 10000.0, "s0": 0.9, "age_pop": [5000.0, 5000.0]},
            package={"comp_a": 1},
            params={"r0": 2.0},
            horizon={"start": 0.0, "stop": 60.0, "window": [0.0, 60.0], "windows": {"w0": [0.0, 60.0]}},
            unsandboxed_ok=dev_un,
        )
        led = _load_ledger(d)
        ok(r1["ok"] and r1["call_n"] == 1 and r2["call_n"] == 2 and led["count"] == 2
           and os.path.isfile(os.path.join(d, "engine_build", "try", "1.json"))
           and len(json.dumps(r1).encode("utf-8")) <= 8192
           and "cases" in r2["per_capita"] and r2["balance"] is not None,
           f"TRY1 engine_try & engine_run_one run public checks, persist ledger/results, and cap summary ({led['count']})")
        # Cap enforcement: simulate 30 calls already used -> 31st refused
        led["count"] = 30
        SIO.write_nofollow(d, LEDGER_REL, json.dumps(led).encode("utf-8"), atomic=True)
        try:
            engine_try(d, checks=["A1"], unsandboxed_ok=dev_un)
            capped = False
        except TryCapExceeded:
            capped = True
        ok(capped, "TRY2 shared 30-call ledger refuses the 31st call")

    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
