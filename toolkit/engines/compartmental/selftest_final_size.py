#!/usr/bin/env python3
"""Self-tests of the compartmental engine's direct final-size solver (v0.3), split from engine.py.

    python3 toolkit/engines/compartmental/selftest_final_size.py --self-test

A final_size run without forcing, without a component acting during the epidemic and with deterministic importation
solves the multigroup final-size relation directly (engine.solve_final_size) instead of stepping the sim. These
cases check that path against the v0.2 stepping path (the generation grid, run here through the engine's own
_simulate), against the pack's independent oracles (benchmarks/oracles.py, imported by this test only, never by the
engine), on the edge cases (R below 1, unreachable groups, a group behind an immune one, no susceptibles, near the
threshold), and that batched equals run. Design: docs/design/direct-final-size-solver.md (PR #346). Nothing here names a
disease.
"""
from __future__ import annotations

import ast
import os
from decimal import Decimal as D, getcontext
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from toolkit.engines.compartmental import engine as E  # noqa: E402
from toolkit.engines.compartmental.benchmarks import oracles as O  # noqa: E402
from toolkit.engines.contract import Horizon, Package, UnitSpec  # noqa: E402

H = Horizon(0, 200000, (0, 200000), {"all": [0, 200000]})
C3 = [[0.3, 0.2, 0.5], [0.6, 0.4, 1.0], [0.15, 0.1, 0.25]]                       # rank one: a = (1, 2, 0.5)
N3 = [3.0e5, 2.0e5, 1.0e5]
C5 = [[3.0, 1.0, 0.5, 0.3, 0.1], [0.8, 4.0, 1.2, 0.4, 0.2], [0.4, 1.0, 2.5, 0.9, 0.3], [0.2, 0.5, 1.1, 1.8, 0.6],
      [0.1, 0.3, 0.4, 0.7, 1.2]]
N5 = [2.0e5, 3.0e5, 2.5e5, 1.5e5, 1.0e5]
G5 = ["g1", "g2", "g3", "g4", "g5"]


def single(**kw):
    return E.CompartmentalEngine(**dict(dict(groups=["all"], generation_interval=[0.2, 0.3, 0.3, 0.2], tol=1e-13,
                                             final_size=True), **kw))


def multi(groups, **kw):
    cfg = dict(groups=groups, structure="age_groups_contact_matrix", transmission="q",
               generation_interval={"seir": {"latent_days": 1.5, "infectious_days": 3.0}}, tol=1e-13, final_size=True)
    cfg.update(kw)
    return E.CompartmentalEngine(**cfg)


def stepped(eng, params, unit, pkgs, horizon=H):
    """The v0.2 path for the same configuration: one generation per Starsim step, stopped by the engine's rule."""
    pkgs = pkgs if isinstance(pkgs, list) else [pkgs]
    ctx = eng._context([params] * len(pkgs), unit, pkgs)
    ctx["w"], ctx["generations"] = np.ones(1), True
    state, _ = eng._simulate(ctx, horizon, 1)
    return eng._outcomes(ctx, state, horizon, 1)


def vec(o, groups):
    return np.asarray([o.per_capita["infections"][g] for g in groups])


def _self_test() -> int:
    getcontext().prec = 50
    fails = []

    def ok(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            fails.append(msg)

    def safe(fn):
        try:
            return fn(), None
        except Exception as e:  # noqa: BLE001 -- a test that raises is a failing test
            return None, f"{type(e).__name__}: {e}"

    # FD1: which path runs, recorded in the meta
    es = single()
    u1 = UnitSpec("u", {"N": [1e6], "s0": [1.0], "seed": 1e-9})
    paths = {}
    for name, eng in (("direct", es), ("forced", single(forcing="cosine")), ("branching", single(importation="branching")),
                      ("late", single(components={"v": {"kind": "campaign", "targets": ["all"], "reach": 0.5,
                                                        "day": 30}}))):
        p = dict({"r0": 2.0}, **({"forcing_amplitude": 0.1, "forcing_phase": 0.0} if name == "forced" else {}),
                 **({"efficacy:v": 1.0} if name == "late" else {}))
        o, err = safe(lambda: eng.run(p, u1, Package("0", {}), H, 1))
        paths[name] = o.meta.get("final_size_path") if o else err
    plain = E.CompartmentalEngine(groups=["all"], generation_interval=[0.2, 0.3, 0.3, 0.2], tol=1e-13)
    ok(paths == {"direct": "direct_solver", "forced": "simulation", "branching": "simulation", "late": "simulation"}
       and "final_size_path" not in plain.run({"r0": 2.0}, u1, Package("0", {}), H, 1).meta,
       f"FD1 the solver runs only on final_size, no forcing, no component during the epidemic, deterministic "
       f"importation; meta final_size_path says which ({paths})")

    # FD2: the direct path builds no Starsim sim
    def no_sim(*a, **k):
        raise AssertionError("a sim was built")
    es._sim = no_sim
    o, err = safe(lambda: es.run({"r0": 2.0}, u1, Package("0", {}), H, 1))
    del es._sim
    ok(err is None, f"FD2 the direct path builds no sim: {err}")

    # FD3: FS1 (Lambert W closed form) and FS2 (threshold), and the stepping path, single group
    worst_o, worst_s = 0.0, 0.0
    for r0 in (0.5, 1.01, 1.5, 2.0, 4.0, 12.0, 18.0):
        o = es.run({"r0": r0}, u1, Package("0", {}), H, 1)
        sp = stepped(es, {"r0": r0}, u1, Package("0", {}))[0]
        z = o.windows["all"]["attack_rate"]
        if r0 > 1:
            worst_o = max(worst_o, abs(z - O.oracle_final_size_lambert(r0, seed=1e-9)))
        else:
            ok(z <= O.oracle_seed_bound(r0, 1e-9) + 1e-15, f"FD3a R0 = {r0}: within the seed-size bound ({z:.3e})")
        worst_s = max(worst_s, abs(z - sp.windows["all"]["attack_rate"]))
    ok(worst_o < 1e-9 and worst_s < 1e-9, f"FD3 single group: solver vs Lambert W {worst_o:.1e}, vs stepping "
       f"{worst_s:.1e} (R0 0.5-18)")

    # FD4: FS3 rank-one mixing; the multigroup oracle; the stepping path (3 groups)
    em = multi(["a", "b", "c"])
    ur = UnitSpec("r", {"N": N3, "s0": [1.0, 1.0, 1.0], "seed": 1e-9, "intro": "a", "contact": C3})
    o = vec(em.run({"q": 1.5}, ur, Package("0", {}), H, 1), "abc")
    r1 = np.asarray(O.oracle_final_size_rank_one(1.5, [1.0, 2.0, 0.5], [0.3, 0.2, 0.5], N3, [1.0] * 3,
                                                 [2e-9, 0.0, 0.0]))
    mg = np.asarray(O.oracle_final_size_multigroup(1.5, C3, N3, [1.0] * 3, [2e-9, 0.0, 0.0]))
    sp = vec(stepped(em, {"q": 1.5}, ur, Package("0", {}))[0], "abc")
    ok(np.max(np.abs(o - r1)) < 1e-9 and np.max(np.abs(o - mg)) < 1e-12 and np.max(np.abs(o - sp)) < 1e-9,
       f"FD4 rank-one mixing: vs rank-one {np.max(np.abs(o - r1)):.1e}, vs multigroup oracle "
       f"{np.max(np.abs(o - mg)):.1e}, vs stepping {np.max(np.abs(o - sp)):.1e}")

    # FD5: the multigroup ODE checks (FS6, AS1: 5 x 5 asymmetric, partial susceptibility) and the stepping path
    e5 = multi(G5)
    s5 = [1.0, 0.9, 0.7, 0.8, 1.0]
    u5 = UnitSpec("a", {"N": N5, "s0": s5, "seed": 1e-6, "intro": "g2", "contact": C5})
    o = vec(e5.run({"q": 0.6}, u5, Package("0", {}), H, 1), G5)
    seed5 = [0.0, 1e-6 * sum(N5) / N5[1], 0.0, 0.0, 0.0]
    ode = np.asarray(O.oracle_ode_final_size(0.6, C5, N5, s5, seed5, gamma=0.25))
    mg = np.asarray(O.oracle_final_size_multigroup(0.6, C5, N5, s5, seed5))
    sp = vec(stepped(e5, {"q": 0.6}, u5, Package("0", {}))[0], G5)
    ok(np.max(np.abs(o - ode)) < 1e-6 and np.max(np.abs(o - mg)) < 1e-12 and np.max(np.abs(o - sp)) < 1e-9,
       f"FD5 asymmetric 5 x 5: vs ODE {np.max(np.abs(o - ode)):.1e}, vs multigroup oracle "
       f"{np.max(np.abs(o - mg)):.1e}, vs stepping {np.max(np.abs(o - sp)):.1e}")
    # near the threshold (the pack's AS7 setting, Reff about 1.05), and with the oracle at criticality's edge
    for q in (0.22, 0.2105):
        o = vec(e5.run({"q": q}, u5, Package("0", {}), H, 1), G5)
        sp = vec(stepped(e5, {"q": q}, u5, Package("0", {}))[0], G5)
        mg = np.asarray(O.oracle_final_size_multigroup(q, C5, N5, s5, seed5))
        ok(np.max(np.abs(o - sp)) < 1e-9 and np.max(np.abs(o - mg)) < 1e-10,
           f"FD5b near the threshold (q {q}): vs stepping {np.max(np.abs(o - sp)):.1e}, vs multigroup oracle "
           f"{np.max(np.abs(o - mg)):.1e}")

    # FD6: unreachable groups. Block-diagonal: a supercritical block without a seed stays at zero. A chain a -> b -> c
    # with b fully immune: c is not reached, although the contact graph connects it (Codex review of the plan).
    ud = UnitSpec("d", {"N": N3, "s0": [1.0, 1.0, 1.0], "seed": 1e-9, "intro": "a",
                        "contact": [[2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 2.0]]})
    od = em.run({"q": 1.0}, ud, Package("0", {}), H, 1)
    chain = [[1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [0.0, 1.0, 3.0]]                  # infection a -> b -> c only
    uc = UnitSpec("c", {"N": N3, "s0": [1.0, 0.0, 1.0], "seed": 1e-9, "intro": "a", "contact": chain})
    oc = em.run({"q": 1.0}, uc, Package("0", {}), H, 1)
    uo = UnitSpec("o", {"N": N3, "s0": [1.0, 1.0, 1.0], "seed": 1e-9, "intro": "a", "contact": chain})
    # two critical groups in a row amplify rounding about 100-fold (FD14): the open chain is checked at tol 1e-11
    oo = multi(["a", "b", "c"], tol=1e-11).run({"q": 1.0}, uo, Package("0", {}), H, 1)
    reach = E.infection_reach(np.asarray([chain]), np.asarray([[1.0, 0.0, 1.0]]), np.asarray([[3e-9, 0.0, 0.0]]))
    ok(od.per_capita["infections"]["b"] == 0.0 and od.per_capita["infections"]["c"] == 0.0
       and oc.per_capita["infections"]["b"] == 0.0 and oc.per_capita["infections"]["c"] == 0.0
       and oo.per_capita["infections"]["c"] > 0.5 and reach.tolist() == [[True, False, False]],
       f"FD6 unreached groups keep z = 0: block-diagonal {od.per_capita['infections']}, a chain behind an immune group "
       f"{oc.per_capita['infections']} (open chain: c {oo.per_capita['infections']['c']:.3f})")

    # FD7: no susceptibles, no seed, and a seed larger than the susceptibles (clipped as at the sim's first step)
    for name, attrs in (("no susceptibles", {"s0": [0.0]}), ("no seed", {"seed": 0.0}),
                        ("seed above the susceptibles", {"s0": [1e-10], "seed": 1e-9})):
        u = UnitSpec("z", dict(u1.attributes, **attrs))
        o, err = safe(lambda: es.run({"r0": 3.0}, u, Package("0", {}), H, 1))
        sp = stepped(es, {"r0": 3.0}, u, Package("0", {}))[0]
        ok(err is None and abs(o.windows["all"]["attack_rate"] - sp.windows["all"]["attack_rate"]) < 1e-15
           and o.meta["seed_clipped"] == sp.meta["seed_clipped"] and o.windows["all"]["converged"] == 1.0,
           f"FD7 {name}: solver equals stepping ({err or o.windows['all']['attack_rate']})")

    # FD8: a 1e-12 seed in a growing epidemic: the first step from e moves z by about 1e-12 while the root is about 0.8,
    # so only a closed bracket may stop the solver (Codex review of the plan)
    u12 = UnitSpec("t", dict(u1.attributes, seed=1e-12))
    z = es.run({"r0": 2.0}, u12, Package("0", {}), H, 1).windows["all"]["attack_rate"]
    ok(abs(z - O.oracle_final_size_lambert(2.0, seed=1e-12)) < 1e-9, f"FD8 a 1e-12 seed still reaches the root ({z:.6f})")

    # FD9: the bracket bounds the error: against a 50-digit bisection (decimal) at R0 = 1, where s - a exp(-x)
    # cancellation alone costs about 2e-12
    lo, hi, eD = D("1e-12"), D("0.5"), D("1e-9")
    for _ in range(200):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if mid - (1 - (1 - eD) * (-mid).exp()) < 0 else (lo, mid)
    z = es.run({"r0": 1.0}, u1, Package("0", {}), H, 1).windows["all"]["attack_rate"]
    ok(abs(z - float(lo)) <= 1e-13, f"FD9 at R0 = 1 the error is within tol 1e-13 ({abs(z - float(lo)):.1e})")

    # FD10: batched equals run, member by member, exactly, with dose packages and per-member iteration counts
    ev = multi(G5, transmission="r0", tol=1e-10,
               components={f"d{i}": {"kind": "doses", "group": g, "block_size": 5e4, "max_blocks": 4}
                           for i, g in enumerate(G5)})
    pv = {"r0": 1.5, **{f"efficacy:d{i}": 0.6 for i in range(5)}}
    uv = UnitSpec("v", {"N": N5, "s0": [1.0] * 5, "seed": 1e-9, "intro": "g2", "contact": C5})
    rng = np.random.default_rng(5)
    pk = [Package(f"p{i}", {f"d{j}": int(x) for j, x in enumerate(rng.integers(0, 5, 5))}) for i in range(24)]
    ob = ev.run_batch([pv] * len(pk), uv, pk, H, 1)
    same = all(vec(b, G5).tolist() == vec(ev.run(pv, uv, k, H, 1), G5).tolist()
               and b.meta["steps"] == ev.run(pv, uv, k, H, 1).meta["steps"] for b, k in zip(ob, pk))
    sp = stepped(ev, pv, uv, pk)
    worst = max(float(np.max(np.abs(vec(b, G5) - vec(s, G5)))) for b, s in zip(ob, sp))
    dose = max(abs(b.cost_drivers[c] - s.cost_drivers[c]) for b, s in zip(ob, sp) for c in b.cost_drivers)
    ok(same and worst < 1e-8 and dose == 0.0 and len({b.meta["steps"] for b in ob}) > 1,
       f"FD10 run_batch equals run member by member (outcomes and steps); vs stepping {worst:.1e}, doses identical")

    # FD11: fail closed when the bracket does not close within the cap
    _, err = safe(lambda: single(step_cap=1).run({"r0": 2.0}, u1, Package("0", {}), H, 1))
    ok(err is not None and "converged: false" in err, f"FD11 the iteration cap fails closed: {err}")

    # FD12: the engine never imports the pack's oracles (outside the engine fingerprint, and an independent check)
    tree = ast.parse(open(E.__file__).read())
    names = [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names] + \
            [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    ok(not any("oracles" in x for x in names), f"FD12 engine.py imports no oracles module ({[x for x in names if 'oracles' in x]})")

    # Codex r1 of #351 (tests first): FD14-FD17
    # FD14 (P1-1): a chain of critical groups amplifies the rounding of T; a bracket certified by rounding alone
    # reported an error 55 x tol. The solver either meets tol against a 50-digit solution or fails closed.
    ch = [[0.0, 0.0, 0.0, 0.0], [1e-6, 1.0, 0.0, 0.0], [0.0, 1.0, 1.0, 0.0], [0.0, 0.0, 1.0, 1.0]]
    e4 = multi(["a", "b", "c", "d"], generation_interval=[1.0], step_cap=20000)   # it crawls to the cap
    u4 = UnitSpec("k", {"N": [1e6] * 4, "s0": [1.0] * 4, "seed": 1e-9, "intro": "a", "contact": ch})
    o4, err = safe(lambda: e4.run({"q": 1.0}, u4, Package("0", {}), H, 1))
    ref = [D(4) * D("1e-9")]
    for k in range(1, 4):
        lo, hi = D(0), D(1)
        for _ in range(220):
            mid = (lo + hi) / 2
            x = D(str(ch[k][k - 1])) * ref[k - 1] + mid
            lo, hi = (mid, hi) if mid - (1 - (-x).exp()) < 0 and mid > 0 else (lo, mid)
            if mid == 0:
                lo = mid
        ref.append(hi)
    gap = None if o4 is None else max(abs(o4.per_capita["infections"][g] - float(r)) for g, r in zip("abcd", ref))
    ok((err is not None and "converged: false" in err) or (err is None and gap <= 1e-13),
       f"FD14 (r1 P1-1) an ill-conditioned chain meets tol or fails closed ({err or f'error {gap:.1e}'})")
    # FD15 (P1-2): a bracket already narrower than tol is still evaluated: q = 0 infects only the seed
    e15 = single(transmission="q", tol=0.01)
    o15 = e15.run({"q": 0.0}, UnitSpec("q", {"N": [1e6], "s0": [0.01], "seed": 0.001}), Package("0", {}), H, 1)
    w = o15.windows["all"]
    ok(abs(w["attack_rate"] - 0.001) < 1e-15 and abs(w["attack_rate"] + w["s_end"] - w["s_start"]) < 1e-15,
       f"FD15 (r1 P1-2) q = 0: only the seed is infected and the population balances ({w['attack_rate']}, "
       f"{w['s_end']}, {w['s_start']})")
    # FD16 (P2-3): bounds advancing along a 64-group chain are progress, not a floating-point stall
    K64 = np.diag([40.0] * 63, -1)[None]
    e64 = np.zeros((1, 64))
    e64[0, 0] = 1e-9
    z64, err = safe(lambda: E.solve_final_size(K64, np.ones((1, 64)), e64, 1e-13, 500))
    ok(err is None and z64[0][0, -1] > 0.99, f"FD16 (r1 P2-3) a long chain converges: {err or z64[1]}")
    # FD17 (P2-4): the direct path's steps are solver iterations, not generations
    ok(es.run({"r0": 2.0}, u1, Package("0", {}), H, 1).meta["time_axis"] == "solver_iterations",
       "FD17 (r1 P2-4) a direct run's time axis is solver_iterations")

    ok(o15.meta["steps"] >= 1, f"FD15b (r2 P3-6) the map was evaluated at least once ({o15.meta['steps']} iterations)")

    # Codex r2 of #351 (tests first): FD18-FD22
    def solve(K, s, e, tol, cap=500):
        return safe(lambda: E.solve_final_size(np.asarray(K), np.asarray(s), np.asarray(e), tol, cap))
    # FD18 (P1-1): a force that underflows to zero still carries an allowance: a 4-chain whose last share is
    # 1 - exp(-1) must not be certified at zero
    r18, err = solve(np.diag([1e-150, 1e175, 1e175], -1)[None], np.ones((1, 4)), [[1e-200, 0, 0, 0]], 1e-12)
    ok((err is not None and "converged: false" in err) or (err is None and abs(r18[0][0, 3] - (1 - np.exp(-1))) <= 1e-12),
       f"FD18 (r2 P1-1) underflow does not certify zero infections ({err or r18[0][0, 3]})")
    # FD19 (P1-2): float32 inputs are promoted; the bound stays at or below the root of the stored inputs
    r19, err = solve(np.asarray([[[2.0]]], np.float32), np.asarray([[1.0]], np.float32),
                     np.asarray([[0.001]], np.float32), 1e-13)
    e32 = D(float(np.float32(0.001)))
    lo19, hi19 = D(0), D(1)
    for _ in range(200):
        mid = (lo19 + hi19) / 2
        lo19, hi19 = (mid, hi19) if mid - (1 - (1 - e32) * (-2 * mid).exp()) < 0 else (lo19, mid)
    ok(err is None and float(lo19) - 1e-13 <= r19[0][0, 0] <= float(hi19) + 1e-16,
       f"FD19 (r2 P1-2) float32 inputs: within tol below the root ({err or r19[0][0, 0]} vs {float(lo19)})")
    # FD20 (P2-3): slow but certified progress closes within the cap; it is not declared a floating-point limit
    r20, err = solve([[[1.0, 0.0], [1e-4, 1.0]]], [[1.0, 1.0]], [[1.21e-11, 0.0]], 1e-13, cap=1500)
    ok(err is None, f"FD20 (r2 P2-3) a slowly closing bracket is not aborted: {err or r20[1]}")
    # FD21 (P2-4): a row sum that overflows saturates the map instead of poisoning the bracket with NaN
    r21, err = solve([[[0, 0, 0], [1, 0, 0], [1e308, 1e308, 0]]], np.ones((1, 3)), [[1e-9, 0, 0]], 1e-9)
    ok(err is None and np.all(np.isfinite(r21[0])) and abs(r21[0][0, 2] - 1.0) <= 1e-9,
       f"FD21 (r2 P2-4) an overflowing force saturates: {err or r21[0]}")
    # FD22 (P2-5): a structurally zero row is known exactly (z = e), at any tol
    o22, err = safe(lambda: single(transmission="q", tol=5e-18).run(
        {"q": 0.0}, UnitSpec("q", {"N": [1e6], "s0": [1.0], "seed": 0.01}), Package("0", {}), H, 1))
    ok(err is None and o22.windows["all"]["attack_rate"] == 0.01,
       f"FD22 (r2 P2-5) zero transmission gives the seed exactly: {err or o22.windows['all']['attack_rate']}")

    # FD13: the card says what the solver's steps are, and the version moved
    card = es.describe()
    ok(E.VERSION == "0.3" and any("solved directly" in m for m in card.mechanisms),
       f"FD13 engine version {E.VERSION}; the mechanism line names the direct solver")
    if fails:
        print(f"FAILED: {len(fails)} case(s)")
        return 1
    print("OK: all self-test cases passed.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        sys.exit(_self_test())
    print(__doc__)
