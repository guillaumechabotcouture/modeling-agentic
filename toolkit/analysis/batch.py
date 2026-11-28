"""Engine batch for the emulator path (design §2), run through scripts/compute.py.

Design: `analysis.emulator.points` design points. Each takes the engine attributes of one
real unit (units drawn without replacement, then with replacement), a Sobol value of the
calibrated attribute over `calibration.range`, Sobol values of every fitted engine
parameter over its engine-card range, and Sobol values of every prior parameter the
uncertainty draws vary (toolkit/spec/priors.py) over its declared range (v2 priors; a spec
whose engine and spec declare no prior gets the spike's design unchanged). A parameter that a
`parameter_value` decision test sets outside that range has its design box widened to span the
test's values (`design_ranges` in design.json); the draws keep the declared range. Engine parameter values the
signed spec declares (toolkit/spec/engine_parameters.py) are in every job's params; one held at its spec value that a
`parameter_value` test varies is a design input over the box from its value to the tested values (`spec_value_names`,
`spec_value_ranges`), so the test is evaluated inside the design. Every package runs at every point with the same
seed (common random numbers). A share `holdout_share` of points is held out to measure
emulator accuracy. The design is the spike's (`spike/analysis/batch.py`), unchanged, so
the stored spike batch is reusable.

Runs go to `scripts/compute.py` (`run_map`): dask on Coiled when the launcher provides a
scheduler, else a local process pool that is replaced every chunk (spike lesson 7);
results are kept per input, so a rerun completes a stopped batch.
For an engine whose packages share a starting state (`batch_shares_state`, e.g. the malaria
engine's burn-in), the compute tasks are design points: `run_point` runs one point's packages in
one `run_batch` call, and the per-job rows (with their own provenance) are written as before.
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
from toolkit.engines.contract import UnitSpec, Package  # noqa: E402
from toolkit.engines.horizon import make_horizon  # noqa: E402

PRODUCER = "toolkit/analysis/batch.py"
CAL_WINDOW = "cal"


def fitted_ranges(engine) -> dict:
    """{name: (lo, hi, value)} of the engine's fitted parameters, in engine order. Refused (ValueError "refit required")
    when the engine's fitted file was fitted against other band declarations of its pack (F6 S1-2): every analysis read
    of fitted values goes through here (core, the batch design, joint calibration)."""
    from toolkit.engines.benchmark_sources import load_evidence
    return load_evidence(engine).require().fitted


def prior_ranges(spec) -> dict:
    """{name: (lo, hi, value)} of the prior parameters the uncertainty draws vary (toolkit/spec/priors.py), in name
    order. They are emulator inputs: the design spans their declared ranges, so every draw is inside the design box."""
    from toolkit.spec import priors as PRI
    return {k: (p.lo, p.hi, p.value) for k, p in PRI.drawn(spec).items()}


def test_value_ranges(spec, names) -> dict:
    """{name: (lo, hi)} of the values the spec's parameter_value decision tests set for design parameters `names`.
    The design box spans them too, so each test is evaluated inside the design (the draws keep the declared ranges)."""
    from toolkit.spec import engine_parameters as PV
    out = {}
    for dt in (spec["analysis"].get("decision_tests") or []):
        for n, vs in PV.test_settings(dt, names=names).items():     # its own values and each companion's (`with`)
            if n in names:
                lo, hi = out.get(n, (min(vs), max(vs)))
                out[n] = (min(lo, *vs), max(hi, *vs))
    return out


def design_box(spec, eng) -> tuple:
    """The emulator design's parameter inputs and their box: (box {name: (lo, hi, value)}, fitted names, prior names,
    spec-value names, widened {name: [lo, hi]}). Fitted parameters, drawn priors (v2) and held spec values a decision
    test varies are inputs; the box spans each parameter_value test's values. The batch design and the decision-test
    planner (toolkit/analysis/decision_tests.py plan_test, before a run) read it here, so they cannot disagree."""
    from toolkit.spec import engine_parameters as PV
    fr = fitted_ranges(eng)
    names = sorted(fr)
    pr = prior_ranges(spec)                 # v2 priors: drawn parameters enter the design as inputs
    pnames = list(pr)
    fr = dict(fr, **pr)
    sbox = PV.tested_boxes(spec)            # held spec values a decision test varies: design inputs over their box
    snames = list(sbox)
    sval = PV.values(spec)
    fr.update({k: (b[0], b[1], sval[k]) for k, b in sbox.items()})
    # owner, 14 Nov (f6-posterior-ranges): a declared external posterior's hull is its columns' box;
    # listed in `ext` so the emulator's domain is it too
    from toolkit.engines import hooks as _hooks
    ne = _hooks.external_posterior_sample(spec, eng)
    fr, _ = _hooks.external_posterior_with_ranges(ne, fr)
    ext = {k: list(fr[k][:2]) for k in (ne or {}).get("ranges", {}) if k in fr}
    # owner, 15 Nov ("tests may go wider"): an explicit parameter_value test widens a posterior column's box too (the
    # union of the hull and its values); the draws stay inside the hull. The sign-off precondition refuses a test value
    # outside the column's hard bounds (toolkit/spec/preconditions.py net_effect_posterior)
    tv = test_value_ranges(spec, set(fr))
    for nm, (tlo, thi) in tv.items():
        lo_, hi_ = min(fr[nm][0], tlo), max(fr[nm][1], thi)
        if (lo_, hi_) != (fr[nm][0], fr[nm][1]):
            ext[nm] = [lo_, hi_]
            fr[nm] = (lo_, hi_, fr[nm][2])
    return fr, names, pnames, snames, ext


def calibrated_value(spec, z: float) -> float:
    """The calibrated attribute from its fitted value z: exp(z) on the log scale, z on the linear one. The scale is
    the spec's one resolution (toolkit/spec/schema.py calibration_scale: declared, else from the engine card, else
    linear)."""
    from toolkit.spec import schema as _schema
    return float(math.exp(z)) if _schema.resolved_scale(spec) == "log" else float(z)


def windows(spec) -> dict:
    """The engine's named windows: the calibration window (emulator inversion) and, only when the spec declares one,
    the burden validation's model window (F6 S2-5b), so other specs' job hashes stay."""
    from toolkit.analysis import burden as BV
    cal, mw = spec["calibration"], BV.model_window(spec)
    out = {CAL_WINDOW: list(cal["window"])} if cal.get("method") == "emulator_inversion" else {}
    return dict(out, **({BV.WINDOW: mw["window"]} if mw else {}))


def independent_features(spec) -> tuple[list, dict]:
    """(features, basis) of the signed spec's `analysis.emulator.independent_features: {features, basis, generator}`:
    declared unit features of a synthetic units table that its generator draws independently of the table's other
    columns. A declared fact about the data-generating process, never a statistical claim and never for measured
    data: the generator (a file beside the spec) is named and fingerprinted. A feature that is not a declared design feature, a
    missing basis or a missing generator is refused (ValueError)."""
    import hashlib
    from toolkit.analysis import features as FT
    dec = (spec["analysis"].get("emulator") or {}).get("independent_features")
    names = [FT.name(x) for x in FT.declared(spec.raw)]
    if not isinstance(dec, dict) or not isinstance(dec.get("features"), list) or not dec["features"]:
        raise ValueError("analysis.emulator.independent_features: {features: [...], basis, generator}")
    bad = [f for f in dec["features"] if f not in names]
    if bad:
        raise ValueError(f"analysis.emulator.independent_features: {bad} are not declared design features {names}")
    if not str(dec.get("basis") or "").strip():
        raise ValueError("analysis.emulator.independent_features.basis: how the generator draws them independently")
    gen = str(dec.get("generator") or "").strip()
    path = spec.resolve(gen) if gen else None
    if not path or not os.path.isfile(path):
        raise ValueError(f"analysis.emulator.independent_features.generator: {gen!r} is not a file beside the spec "
                         "(the synthetic table's generator)")
    with open(path, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    # A generator that draws columns independently can still realize a correlated table on a few units (a world-model
    # trial's 24 units gave rank correlation 0.82 between two separately drawn columns): the declaration holds only
    # when the realized table's declared features are also uncorrelated with every design feature (|Spearman| <=
    # INDEPENDENCE_RHO_MAX).
    rho = realized_rank_correlations(spec, list(dec["features"]))
    big = {k: v for k, v in rho.items() if abs(v) > INDEPENDENCE_RHO_MAX}
    if big:
        raise ValueError(f"analysis.emulator.independent_features: the realized units table correlates declared features "
                         f"{ {k: round(v, 3) for k, v in big.items()} } (|Spearman| > {INDEPENDENCE_RHO_MAX}): draw a "
                         "table whose declared columns are uncorrelated, or do not declare them independent")
    return list(dec["features"]), {"basis": str(dec["basis"]), "generator": gen, "generator_sha256": sha,
                                   "realized_max_abs_spearman": max((abs(v) for v in rho.values()), default=0.0)}


INDEPENDENCE_RHO_MAX = 0.3


def realized_rank_correlations(spec, features) -> dict:
    """{"a|b": Spearman rho} over the units table (as features.vector reads them) between each of `features` (the
    declared-independent ones) and every other declared design feature, each pair once: a feature drawn independently
    must be uncorrelated with every other column the design varies, not only with the other declared ones (Codex
    review of #225: pop declared alone passed with Spearman 1 against seed_share)."""
    from scipy.stats import spearmanr
    from toolkit.analysis import features as FT
    names = [FT.name(x) for x in FT.declared(spec.raw)]
    rows = [FT.vector(spec, u) for u in spec.units()]
    col = {f: [r[names.index(f)] for r in rows] for f in names}
    feats = [f for f in features if f in col]
    out = {}
    for i, a in enumerate(feats):
        for b in feats[i + 1:] + [x for x in names if x not in feats]:
            if len(set(col[a])) > 1 and len(set(col[b])) > 1:
                out[f"{a}|{b}"] = float(spearmanr(col[a], col[b])[0])
    return out


def cover_features(order: list, hold: set, cols: list) -> list:
    """The first len(order) design points' units, changed only as far as needed for the points to span every emulator
    feature's range over the units (1C-b 2b). The emulator's feature domain is the range of its design points
    (toolkit/analysis/model.py), so a unit beyond it is an extrapolation that stops the analysis. For each feature
    minimum or maximum no design point reaches, a unit at that value takes a design point whose replacement leaves
    every extreme already reached still reached (Codex r1 P1-1): the last such point that is not held out, a held-out
    one only when no unit at that value can take a training point (P1-2, r2 P1; ties: the first such unit). An order that already spans every feature is returned
    unchanged, so its jobs keep their hashes. `cols`: one list per feature, a value per unit. An extreme no swap can
    add (more extremes than points) stays uncovered, and the extrapolation stops the analysis as before."""
    order = list(order)
    ext = [(c, w) for c in cols for w in (min(c), max(c))]        # every (feature, extreme value)
    for c, want in ext:
        cnt = [sum(d[u] == w for u in order) for d, w in ext]     # design points at each extreme
        if any(c[u] == want for u in order):
            continue
        # every unit tied at the missing extreme is a candidate (Codex r2 P1): (training point?, point, -unit)
        free = [(k not in hold, k, -new) for new in range(len(c)) if c[new] == want for k in range(len(order))
                if all(n > 1 or d[new] == w or d[order[k]] != w for (d, w), n in zip(ext, cnt))]
        if free:
            _, k, new = max(free)
            order[k] = -new
    return order


def design(spec) -> tuple[dict, list[dict]]:
    """(design, jobs): the spike's design, from the spec."""
    from scipy.stats import qmc
    from toolkit.spec import engine_parameters as PV
    eng = spec.engine()
    units = spec.units()
    em = spec["analysis"].get("emulator") or {}
    n = int(em.get("points", 64))
    rng = np.random.default_rng(int(em.get("seed", 7)))
    fr, names, pnames, snames, ext = design_box(spec, eng)
    sbox = PV.tested_boxes(spec)
    sob = qmc.Sobol(d=1 + len(names) + len(pnames) + len(snames), scramble=True, seed=int(em.get("seed", 7))).random(n)
    lo, hi = spec["calibration"]["range"]
    order = list(rng.permutation(len(units)))
    while len(order) < n:
        order += list(rng.permutation(len(units)))
    hold = set(rng.choice(n, size=int(round(n * float(em.get("holdout_share", 0.1)))), replace=False).tolist())
    from toolkit.analysis import features as FT
    fcols = [list(c) for c in zip(*(FT.vector(spec, u) for u in units))]    # empty without declared features
    order = cover_features([int(k) for k in order[:n]], hold, fcols)
    pk = spec.packages()
    uid = spec.uid
    attr = spec["calibration"]["attribute"]
    points, jobs = [], []
    for i in range(n):
        row = units[order[i]]
        z = lo + (hi - lo) * sob[i, 0]
        theta = PV.params(spec, {nm: fr[nm][0] + (fr[nm][1] - fr[nm][0]) * sob[i, 1 + k]
                                 for k, nm in enumerate(names + pnames + snames)})
        attrs = spec.unit_attributes(row)
        attrs[attr] = calibrated_value(spec, z)
        seed = 1000 + i
        points.append({"point": i, "unit_id": row[uid], "z": z, "theta": theta, "seed": seed, "holdout": i in hold})
        for name, cs in pk.items():
            jobs.append({"id": f"{i}|{name}", "point": i, "package": name, "components": spec.engine_package(cs),
                         "unit": attrs, "params": theta, "seed": seed})
    h = spec["horizon"]
    d = {"points": points, "fitted_names": names, "packages": list(pk), "spec_engine": spec["engine"],
         "horizon": {"start": h["start"], "stop": h["stop"], "window": list(spec["objective"]["horizon"])},
         "cal_window": list(spec["calibration"]["window"])}
    if windows(spec).get("burden_validation"):          # F6 S2-5b: only when declared, so other designs stay
        d["burden_window"] = windows(spec)["burden_validation"]
    if spec["objective"].get("delivery") is not None:   # v0.6: only when given, so other specs' job hashes stay
        d["horizon"]["delivery"] = [float(x) for x in spec["objective"]["delivery"]]
    if pnames:
        d["prior_names"] = pnames
    if ext:
        d["design_ranges"] = ext          # parameters whose design box is not their declared range (decision-test
        #                                   values, a net-effect posterior's hull)
    if em.get("independent_features") is not None:     # only when declared, so other designs stay
        d["independent_features"], d["independent_features_basis"] = independent_features(spec)
    from toolkit.engines import hooks as _hooks
    ne = _hooks.external_posterior_sample(spec, eng)    # cached: the sample design_box read
    if ne is not None:                      # only with the field, so other designs stay
        d["posterior_ranges"] = _hooks.external_posterior_range_record(ne)
    if snames:                            # only with spec values a decision test varies, so other designs stay
        d["spec_value_names"], d["spec_value_ranges"] = snames, {k: list(sbox[k]) for k in snames}
    return d,[dict(j, engine=spec["engine"], horizon=d["horizon"], windows=windows(spec)) for j in jobs]


_E = {}


def job_sha(job: dict) -> str:
    """sha256 of a job's full inputs (engine reference and config, horizon, windows, unit, package, params, seed)."""
    import hashlib
    return hashlib.sha256(json.dumps({k: v for k, v in job.items() if k != "id"}, sort_keys=True,
                                     default=str).encode()).hexdigest()


def _engine(ref, *, run_dir: str | None = None):
    from toolkit.engines.built import BUILT_ADAPTER_REF
    from toolkit.spec.schema import load_engine
    key = json.dumps(ref, sort_keys=True) if run_dir is None else json.dumps({"ref": ref, "run_dir": run_dir}, sort_keys=True)
    if key not in _E:
        is_built = (ref == BUILT_ADAPTER_REF) or (isinstance(ref, dict) and ref.get("ref") == BUILT_ADAPTER_REF)
        _E[key] = load_engine(ref, allow_built=is_built, run_dir=run_dir)
    return _E[key]


def run_job(job: dict) -> dict:
    """One engine run (the compute.py function), with its provenance: the job's input hash and the engine's identity
    and code fingerprint (Codex P1-3). Engine instances are cached per process."""
    from toolkit.engines.identity import engine_identity
    eng = _engine(job["engine"])
    hz = job["horizon"]
    h = make_horizon(hz["start"], hz["stop"], hz["window"], job.get("windows"), hz.get("delivery"))
    o = eng.run(job["params"], UnitSpec(str(job["point"]), job["unit"]), Package(job["package"], job["components"]),
                h, int(job["seed"]))
    return {"result": {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers, "meta": o.meta},
            "provenance": {"job_sha": job_sha(job), "engine": engine_identity(eng), "producer": PRODUCER}}


def shares_state(engine) -> bool:
    """The engine declares that the packages of one unit, parameter set and seed share one starting state (computed
    once, e.g. a burn-in) and offers run_batch: one design point's jobs then run as one compute task."""
    return bool(getattr(engine, "batch_shares_state", False)) and callable(getattr(engine, "run_batch", None))


def point_groups(jobs: list[dict]) -> list[dict]:
    """Compute inputs for a shared-state engine: one per (design point, seed), holding that point's jobs in order."""
    by = {}
    for j in jobs:
        by.setdefault((j["point"], j["seed"]), []).append(j)
    return [{"jobs": g} for g in by.values()]


def run_point(group: dict) -> dict:
    """The compute.py function for a shared-state engine: one point's jobs in one run_batch call. Each row carries
    its own job's provenance, as run_job's does."""
    from toolkit.engines.identity import engine_identity
    jobs = group["jobs"]
    j0 = jobs[0]
    for j in jobs[1:]:
        if any(json.dumps(j.get(k), sort_keys=True) != json.dumps(j0.get(k), sort_keys=True)
               for k in ("engine", "horizon", "windows", "seed")):
            raise ValueError("run_point: the jobs of one task must share engine, horizon, windows and seed")
    eng = _engine(j0["engine"])
    hz = j0["horizon"]
    h = make_horizon(hz["start"], hz["stop"], hz["window"], j0.get("windows"), hz.get("delivery"))
    outs = eng.run_batch([j["params"] for j in jobs], [UnitSpec(str(j["point"]), j["unit"]) for j in jobs],
                         [Package(j["package"], j["components"]) for j in jobs], h, int(j0["seed"]))
    ident = engine_identity(eng)
    return {"rows": [{"result": {"per_capita": o.per_capita, "windows": o.windows, "cost_drivers": o.cost_drivers,
                                 "meta": o.meta},
                      "provenance": {"job_sha": job_sha(j), "engine": ident, "producer": PRODUCER}}
                     for j, o in zip(jobs, outs)]}


def run(run_dir: str, jobs: list[dict], engine, out_rel: str = "analysis/batch/results.jsonl", **kw) -> dict:
    """Run every job through scripts/compute.py; returns {job id: result} for rows whose provenance matches. For a
    shared-state engine the compute tasks are design points (`points.jsonl` beside `out_rel`), and the per-job rows
    are written to `out_rel` in the same format as run_job's."""
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import compute
    out = os.path.join(run_dir, out_rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    from toolkit.engines.identity import engine_identity
    key = {"engine": engine_identity(engine)}      # W0-A: the engine identity is a part of the rows' reuse key
    if not shares_state(engine):
        inputs = [{k: v for k, v in j.items() if k != "id"} for j in jobs]
        rows, summ = compute.run_map(run_dir, os.path.join(ROOT, "toolkit", "analysis", "batch.py") + "::run_job",
                                     inputs, out, purpose="v2 analysis: engine batch", key_extra=key, **kw)
    else:
        groups = point_groups([{k: v for k, v in j.items()} for j in jobs])
        pout = os.path.join(os.path.dirname(out), "points.jsonl")
        prow, summ = compute.run_map(run_dir, os.path.join(ROOT, "toolkit", "analysis", "batch.py") + "::run_point",
                                     groups, pout, purpose="v2 analysis: engine batch (one task per design point)",
                                     key_extra=key, **kw)
        byid = {}
        for g, r in zip(groups, prow):
            if r.get("ok"):
                for j, row in zip(g["jobs"], r["result"]["rows"]):
                    byid[j["id"]] = row
        rows = [{"ok": True, "result": byid[j["id"]]} if j["id"] in byid else {"ok": False} for j in jobs]
        from toolkit.analysis.artifacts import _safe_io
        _safe_io().publish(out, "".join(json.dumps({"i": n, "ok": True, "result": r["result"]}) + "\n"
                                        for n, (j, r) in enumerate(zip(jobs, rows)) if r["ok"]), run_dir)
        summ = dict(summ, tasks=len(groups), n=len(jobs), n_failed=sum(not r["ok"] for r in rows))
    res, problems = verify_rows([dict(r, id=j["id"]) for j, r in zip(jobs, rows) if r.get("ok")], jobs, engine)
    return {"results": res, "problems": problems,
            "summary": {k: summ.get(k) for k in ("backend", "workers", "n", "n_failed", "failed_by_kind", "wall_s",
                                                 "tasks")}, "path": out}


def verify_rows(rows: list[dict], jobs: list[dict], engine) -> tuple[dict, list[str]]:
    """Keep a row only when it carries harness provenance matching its job's full inputs and the current engine
    identity and code (Codex P1-3). Rows: {"id", "result": {"result", "provenance"}} (compute.py format)."""
    from toolkit.engines.identity import engine_identity
    ident = engine_identity(engine)
    want = {j["id"]: job_sha(j) for j in jobs}
    res, bad = {}, []
    for r in rows:
        jid, body = r.get("id"), r.get("result")
        pv = body.get("provenance") if isinstance(body, dict) else None
        if jid not in want:
            continue
        if not isinstance(pv, dict):
            bad.append(f"{jid}: no provenance")
        elif pv.get("job_sha") != want[jid]:
            bad.append(f"{jid}: inputs differ from the current job")
        elif pv.get("engine") != ident:
            bad.append(f"{jid}: computed by another engine identity or code")
        else:
            res[jid] = body["result"]
    return res, bad


def load_results(path: str, jobs: list[dict], engine) -> tuple[dict, list[str]]:
    """Stored results (compute.py rows, in job order) accepted only with matching provenance per row."""
    rows = []
    with open(path) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                if r.get("ok") and isinstance(r.get("i"), int) and r["i"] < len(jobs):
                    rows.append(dict(r, id=jobs[r["i"]]["id"]))
                elif r.get("ok") and "id" in r:
                    rows.append(r)
    return verify_rows(rows, jobs, engine)


class BatchRefused(ValueError):
    """A stored batch that is not used, with a stable code (the message starts with it)."""

    def __init__(self, code: str, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code


# --------------------------------------------------------------------------- self-test
def _self_test() -> int:
    """python3 toolkit/analysis/batch.py --self-test."""
    import copy
    import tempfile
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def case(label, fn):
        try:
            fn()
        except Exception as e:      # noqa: BLE001 -- a missing function is a failing case, not a crash of the test
            ok(False, f"{label} ({type(e).__name__}: {e})")

    from toolkit.spec import schema
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    g = globals()
    with tempfile.TemporaryDirectory() as d:
        spec = schema.load(schema.toy_spec(d, n=12))
        spec["analysis"]["emulator"]["points"] = 8
        eng = spec.engine()
        design0, jobs0 = design(spec)
        attr = spec["calibration"]["attribute"]
        # the VM's inputs: design point 3 regenerated with a last-bit difference in the calibrated attribute
        vm_jobs = copy.deepcopy(jobs0)
        for j in vm_jobs:
            if j["point"] == 3:
                j["unit"][attr] = float(np.nextafter(j["unit"][attr], np.inf))
        n3 = sum(j["point"] == 3 for j in vm_jobs)
        rows = [{"id": j["id"], "ok": True, "result": run_job(j)} for j in vm_jobs]

        def b0():
            # the observed failure, kept as evidence: regenerating the design at merge time loses the shipped rows
            res, probs = verify_rows(rows, jobs0, eng)
            ok(len(probs) == n3 and all("inputs differ" in p for p in probs),
               f"BA0 regenerating the design at merge time rejects the {n3} rows of the point with last-bit "
               f"differences (the re-run's 32 rows, one per package): {len(probs)}")
        case("BA0", b0)

        def b10():
            from toolkit.engines.benchmark import SharedStateToy
            ref = "toolkit.engines.benchmark:SharedStateToy"
            from toolkit import import_guard as _G  # an unregistered toy, registered for this self-test only
            _G.register_test_engine("toolkit.engines.benchmark:SharedStateToy")
            sj = [dict(j, engine=ref) for j in jobs0 if j["point"] in (0, 1)]
            groups = g["point_groups"](sj)
            SharedStateToy.calls.clear()
            rows = [r for gr in groups for r in g["run_point"](gr)["rows"]]
            flat = [j for gr in groups for j in gr["jobs"]]
            npk = len(design0["packages"])
            ok(len(groups) == 2 and SharedStateToy.calls == [npk, npk] and len(rows) == len(sj)
               and sorted(j["id"] for j in flat) == sorted(j["id"] for j in sj)
               and all(r["provenance"]["job_sha"] == job_sha(j) and r["result"] == run_job(j)["result"]
                       for r, j in zip(rows, flat)),
               f"BA10 for an engine whose packages share a starting state, one point's jobs run in one run_batch call "
               f"(batch sizes {SharedStateToy.calls}); each row keeps its job's provenance and equals run_job")
            ok(g["shares_state"](SharedStateToy()) and not g["shares_state"](eng),
               "BA11 other engines keep one compute task per job")
            rd = os.path.join(d, "shared_run")
            os.makedirs(rd)
            b = g["run"](rd, sj, SharedStateToy(), max_workers=1)
            res, probs = load_results(b["path"], sj, SharedStateToy())
            ok(not b["problems"] and len(b["results"]) == len(sj) and res == b["results"] and not probs
               and b["summary"]["tasks"] == 2 and b["summary"]["n"] == len(sj)
               and all(b["results"][j["id"]] == run_job(j)["result"] for j in sj),
               f"BA12 run() computes one task per point and writes per-job rows that load_results accepts: "
               f"{b['summary']}, {probs[:2]}")
        case("BA10", b10)

        def bd1():
            ok(all("delivery" not in j["horizon"] for j in jobs0) and "delivery" not in design0["horizon"],
               "BA-D1 a spec without objective.delivery writes no delivery key (v0.5 job hashes unchanged)")
            sd = schema.Spec(copy.deepcopy(spec.raw), d)
            sd.raw["objective"]["delivery"] = [0, 2]
            dd, jd = design(sd)
            ok(dd["horizon"].get("delivery") == [0.0, 2.0] and all(j["horizon"].get("delivery") == [0.0, 2.0] for j in jd)
               and job_sha(jd[0]) != job_sha(jobs0[0]),
               f"BA-D1b objective.delivery reaches the design and every job's horizon: {dd['horizon']}")
            from toolkit.engines.contract import ToyEngine
            seen = []

            class Spy(ToyEngine):
                def run(self, params, unit, package, horizon, seed):
                    seen.append(getattr(horizon, "delivery", None))
                    return super().run(params, unit, package, horizon, seed)
            key = json.dumps(jd[0]["engine"], sort_keys=True)
            old_e = g["_E"].get(key)
            g["_E"][key] = Spy()
            try:
                run_job(jd[0])
                run_job(jobs0[0])
            finally:
                if old_e is None:
                    g["_E"].pop(key, None)
                else:
                    g["_E"][key] = old_e
            ok(seen == [(0.0, 2.0), None], f"BA-D1c run_job hands the engine the delivery period (or none): {seen}")
        case("BA-D1", bd1)

    def bnp():
        """owner, 14 Nov (f6-posterior-ranges): a net-effect posterior's hull sets its columns' design box."""
        import hashlib
        import json
        import yaml
        from toolkit.engines import effect_fit as EF
        from toolkit.spec import schema
        with tempfile.TemporaryDirectory() as d:
            s0 = schema.load(schema.toy_spec(d))
            got0 = hashlib.sha256(json.dumps([design(s0), list(design_box(s0, s0.engine()))], sort_keys=True,
                                             default=str).encode()).hexdigest()
            # recorded on origin/v2 at d7b940d, before this change
            ok(got0 == "6827c1f3a493b5e268c18626a12c59cef49106e2b9cdd9f4b4ab8ca1f4ee90a7",
               f"BA-NP1 a spec without a net-effect posterior has the same design and design box as before ({got0[:12]})")
            u = [float(v) for v in np.random.default_rng(4).uniform(0, 1, 24)]
            rows = [{"eff_a": round(0.25 + 0.5 * x, 6), "eff_b": round(0.15 + 0.3 * (1 - x), 6)} for x in u]
            EF._fixture_placeholder(os.path.join(d, "nef.yaml"), s0.engine(), rows, {"eff_a": [0, 1], "eff_b": [0, 1]})
            hull = {k: (min(r[k] for r in rows), max(r[k] for r in rows)) for k in ("eff_a", "eff_b")}
            raw = yaml.safe_load(open(os.path.join(d, "decision_spec.yaml")))
            raw["uncertainty"]["net_effect_posterior"] = {
                "path": "nef.yaml", "sha256": hashlib.sha256(open(os.path.join(d, "nef.yaml"), "rb").read()).hexdigest()}
            yaml.safe_dump(raw, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
            s1 = schema.load(os.path.join(d, "decision_spec.yaml"))
            fr, names, _p, _s, ext = design_box(s1, s1.engine())
            dd, _jobs = design(s1)
            th = np.array([[p["theta"][k] for k in ("eff_a", "eff_b")] for p in dd["points"]])
            ok(all(tuple(fr[k][:2]) == hull[k] and ext[k] == list(hull[k]) for k in hull)
               and fitted_ranges(s1.engine())["eff_a"][:2] == (0.3, 0.7)
               and dd["posterior_ranges"]["ranges"] == {k: list(v) for k, v in hull.items()}
               and dd["posterior_ranges"]["sha256"] == raw["uncertainty"]["net_effect_posterior"]["sha256"]
               and dd["design_ranges"] == ext,
               f"BA-NP2 the posterior's hull is its columns' design box ({ {k: fr[k][:2] for k in hull} }; fitted "
               f"eff_a 0.3-0.7), recorded in the design with the sample's sha256")
            ok(all(hull[k][0] - 1e-12 <= th[:, j].min() and th[:, j].max() <= hull[k][1] + 1e-12
                   for j, k in enumerate(("eff_a", "eff_b")))
               and th[:, 0].min() < 0.3 and th[:, 0].max() > 0.7,
               f"BA-NP3 the design points cover the posterior's hull (eff_a {th[:, 0].min():.3f}-{th[:, 0].max():.3f}, "
               f"beyond the fitted 0.3-0.7) and stay inside it")
            # owner, 15 Nov ("tests may go wider"): an explicit parameter_value test on a posterior column widens its
            # design box to the union of the hull and the test's values (a DT9-like test at the prior's ends and its
            # middle); the draws stay inside the hull
            from toolkit.engines import hooks as _hooks
            raw["analysis"]["decision_tests"] = list(raw["analysis"].get("decision_tests") or []) + [
                {"id": "DTpa", "type": "parameter_value", "parameter": "eff_a", "values": [0.1, 0.5, 0.9], "text": "t"}]
            yaml.safe_dump(raw, open(os.path.join(d, "decision_spec.yaml"), "w"), sort_keys=False)
            s2 = schema.load(os.path.join(d, "decision_spec.yaml"))
            fr2, _n, _p, _s, ext2 = design_box(s2, s2.engine())
            ok(tuple(fr2["eff_a"][:2]) == (0.1, 0.9) and ext2["eff_a"] == [0.1, 0.9]
               and tuple(fr2["eff_b"][:2]) == hull["eff_b"]
               and _hooks.external_posterior_sample(s2, s2.engine())["ranges"] == hull,
               f"BA-NP4 a decision test outside the hull widens that column's box to the union ({fr2['eff_a'][:2]}); "
               f"the draw ranges stay the hull")
    case("BA-NP", bnp)

    def bxu():
        """1C-b 2b (M_UNIT_HULL): with fewer design points than units, a unit whose emulator feature lies outside the
        design points' range used to stop the analysis as an extrapolation. The design now includes those units; a
        design that already covers every feature's range is unchanged (so its stored batch is reused)."""
        import yaml
        with tempfile.TemporaryDirectory() as d:
            p = schema.toy_spec(d, n=40)
            raw = yaml.safe_load(open(p))
            raw["analysis"]["emulator"]["points"] = 8

            def build(features, cov):
                lines = open(os.path.join(d, "units.csv")).read().splitlines()
                at = lines[0].split(",").index("cov")
                rows = [ln.split(",") for ln in lines[1:]]
                for r in rows:
                    r[at] = str(cov.get(r[0], 0.0))
                open(os.path.join(d, "units.csv"), "w").write("\n".join([lines[0]] + [",".join(r) for r in rows]) + "\n")
                raw["analysis"]["emulator"]["features"] = features
                yaml.safe_dump(raw, open(p, "w"), sort_keys=False)
                return design(schema.load(p))
            d0, j0 = build([], {})
            used = [str(pt["unit_id"]) for pt in d0["points"]]
            out = [str(i) for i in range(40) if str(i) not in used][:2]
            cov = {out[0]: -9.0, out[1]: 9.0}
            d1, j1 = build(["cov"], cov)
            u1 = [str(pt["unit_id"]) for pt in d1["points"]]
            moved = [k for k, (a, b) in enumerate(zip(used, u1)) if a != b]
            ok(set(out) <= set(u1) and len(moved) == 2 and not any(d1["points"][k]["holdout"] for k in moved)
               and all({**a, "unit_id": 0} == {**b, "unit_id": 0} for a, b in zip(d0["points"], d1["points"])),
               f"BA-XU1 the units at a feature's minimum and maximum are design points ({out} in {u1}); only "
               f"{len(moved)} points change unit, none of them held out, and no z, parameter or seed moves")
            same = sum(job_sha(a) == job_sha(b) for a, b in zip(j0, j1))
            ok(same >= len(j0) - 2 * len(d0["packages"]),
               f"BA-XU2 every job of an unchanged design point keeps its hash ({same} of {len(j0)})")
            d2, j2 = build(["cov"], {used[0]: -9.0, used[-1]: 9.0})
            ok([pt["unit_id"] for pt in d2["points"]] == [pt["unit_id"] for pt in d0["points"]]
               and [job_sha(j) for j in j2] == [job_sha(j) for j in j0],
               "BA-XU3 a design whose points already span every feature's range is unchanged: features alone still "
               "move no design point and no job hash")
    case("BA-XU", bxu)

    def bxu2():
        """Codex r1 P1-1, P1-2: a swap is judged by the coverage it leaves, not by which unit first carries an extreme;
        a held-out point is taken only when no training point can be."""
        ind = lambda u: [1.0 if k == u else 0.0 for k in range(9)]  # noqa: E731 -- one indicator feature per unit
        o1 = cover_features(list(range(8)), set(), [ind(u) for u in range(1, 9)])
        ok(sorted(o1) == list(range(1, 9)),
           f"BA-XU4 the one design point that carries no extreme of its own gives way (unit 0 for unit 8): {o1}")
        o2 = cover_features(list(range(8)), {7}, [ind(u) for u in (1, 2, 3, 4, 5, 6, 8)])
        ok(o2.index(8) != 7 and 7 in o2 and 0 not in o2,
           f"BA-XU5 a training point is replaced before a held-out one when both keep every range covered: {o2}")
        o3 = cover_features([0, 1, 2], {2}, [[0.0, 0.0, 5.0, 9.0]])       # units 0 and 1 tie at the minimum
        ok(sorted(o3) == [0, 2, 3] or sorted(o3) == [1, 2, 3],
           f"BA-XU6 a point tied at a covered extreme can give way while the other carrier stays: {o3}")
        # Codex r2 P1: every unit tied at the missing extreme is a candidate, so a training point is found when one is
        o4 = cover_features([0, 1], {1}, [[0.0, 0.0, 1.0, 1.0], [1.0, 0.0, 0.0, 1.0]])
        ok(o4 == [3, 1], f"BA-XU7 of the units tied at a missing extreme, one that can take a training point is used: {o4}")
    case("BA-XU2", bxu2)

    def ba_sc1():
        # eval baseline: a log-named attribute on [-8, 2] with no declared scale was read on the log scale (exp(z) > 0),
        # so the likelihood fit ran into the bound and was refused. Undeclared now follows the engine card (a range
        # going below 0: linear), and the same toy panel calibrates with no parameter at a bound.
        import yaml
        from toolkit.analysis import run as R
        from toolkit.analysis.calibration import CalibrationRefused, fit_likelihood
        from toolkit.analysis.model import DirectModel
        from toolkit.spec.schema import load
        with tempfile.TemporaryDirectory() as d:
            R._direct_toy(d)
            sp = os.path.join(d, "decision_spec.yaml")
            raw = yaml.safe_load(open(sp))
            raw["calibration"].pop("scale", None)
            yaml.safe_dump(raw, open(sp, "w"))
            spec = load(sp)
            m = DirectModel(spec, log=lambda *a: None)
            m.fit()
            try:
                fit = fit_likelihood(spec, m, identifiability=False)
                dg = fit["diagnostics"]
                good = fit["cov"] is not None and not dg["at_bound"] and dg.get("converged") is True
                why = {"at_bound": dg["at_bound"], "converged": dg.get("converged")}
            except CalibrationRefused as e:
                good, why = False, str(e)[:200]
            ok(spec.calibration_scale() == "linear" and calibrated_value(spec, -1.0) == -1.0 and good,
               f"BA-SC1 an undeclared scale for an attribute whose card range goes below 0 is linear, and the toy panel "
               f"calibrates ({spec.calibration_scale()}, {why})")
    case("BA-SC1", ba_sc1)
    if fails:
        print(f"FAILED: {len(fails)} self-test case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
    sys.exit(2)
