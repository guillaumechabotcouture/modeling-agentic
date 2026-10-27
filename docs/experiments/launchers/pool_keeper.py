"""Keep a Coiled cluster's compute pool autoscaling (Phase 30).

The dask Adaptive loop runs in the process that calls adapt(); a launcher that
fires the run and exits leaves the pool at its starting size (autoscale check 7,
2026-10-26). This process attaches (never creates) and holds the loop, and
re-attaches if the connection drops. If it stops (laptop asleep), the pool
stays at its minimum: slower batches, never lost results.

    python pool_keeper.py <cluster-name> <min-workers> <max-workers> [hours]
"""
import os, sys, time
import coiled

WS = "idm-coiled-idmad-r2"


def running(name):
    cs = coiled.list_clusters(workspace=WS)
    items = cs if isinstance(cs, list) else list(cs.values())
    return any(c.get("name") == name and str((c.get("current_state") or {}).get("state")).lower()
               in ("ready", "running", "pending", "starting", "scaling") for c in items)


def _alive(name):
    try:
        return running(name)
    except Exception:  # noqa: BLE001 — treat an API hiccup as still running
        return True


def main(name, lo, hi, hours=48.0):
    sys.path.insert(0, os.path.expanduser("~/modeling-agentic-lessons/scripts"))
    import compute
    t_end = time.time() + hours * 3600
    while time.time() < t_end:
        try:   # a transient Coiled API error must not end the keeper (Codex review)
            alive = running(name)
        except Exception as e:  # noqa: BLE001
            print(f"{time.strftime('%T')} status check failed ({type(e).__name__}); retrying", flush=True)
            time.sleep(60)
            continue
        if not alive:
            print(f"{time.strftime('%T')} {name} is not running; keeper exiting", flush=True)
            return
        try:
            cl = coiled.Cluster(name=name, workspace=WS, shutdown_on_close=False)
            cl.adapt(minimum=lo, maximum=hi, **compute.ADAPT_OPTIONS)
            c = cl.get_client()
            print(f"{time.strftime('%T')} holding adapt {lo}..{hi} on {name}", flush=True)
            while time.time() < t_end and _alive(name):
                print(f"{time.strftime('%T')} workers={len(c.scheduler_info()['workers'])}", flush=True)
                time.sleep(120)
        except Exception as e:  # noqa: BLE001 — reconnect after a dropped connection
            print(f"{time.strftime('%T')} keeper error {type(e).__name__}: {str(e)[:200]}; retrying", flush=True)
            time.sleep(60)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4]) if len(sys.argv) > 4 else 48.0)
