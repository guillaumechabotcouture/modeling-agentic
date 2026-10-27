"""Phase 30 at the attempt-7 scale, detached (VM cost only, no LLM).

    python autoscale_check7.py launch   # create pool, start the agent-side batch, adapt, EXIT
    python autoscale_check7.py watch    # reattach, sample workers, query the busy agent VM, collect, shut down

Checks what the attempt-7 launcher relies on: Coiled keeps autoscaling after
the launcher disconnects; the pool reaches ~24 VMs of the run's VM type (vCPU
quota); compute tasks never run on the agent VM; and client.run reaches the
agent VM while its single task thread is busy (status / brief / sign)."""
import json, os, subprocess, sys, time
import coiled
from distributed import fire_and_forget

TREE = os.path.expanduser(os.environ.get("CHECK_TREE", "~/modeling-agentic-lessons"))
NAME, WS = "autoscale-check7-gcc", "idm-coiled-idmad-r2"
STATE = os.path.expanduser("~/modeling-agentic-keep/autoscale_check7_state.json")
N_TASKS, TASK_S, MAX_COMPUTE = 2400, 20, 24


def agent_side(code, agent_addr, n_tasks, task_s):
    import io, json, os, subprocess, sys, tarfile, time
    work = "/tmp/ascheck7"; subprocess.run(["rm", "-rf", work]); os.makedirs(work)
    tarfile.open(fileobj=io.BytesIO(code), mode="r:gz").extractall(work)
    run = os.path.join(work, "runs", "ascheck7"); os.makedirs(os.path.join(run, "models"))
    open(os.path.join(run, "models", "burn.py"), "w").write(
        f"import socket, time\ndef f(p):\n    t = time.time()\n    while time.time() - t < {task_s}: pass\n"
        "    return {'host': socket.gethostname()}\n")
    inp = os.path.join(run, "models", "in.jsonl")
    with open(inp, "w") as fh:
        for k in range(n_tasks):
            fh.write(json.dumps({"k": k}) + "\n")
    sys.path.insert(0, os.path.join(work, "scripts")); import compute
    env = dict(os.environ)
    env.update(compute.export_worker_env(os.path.expanduser("~/.ma_tls_check7")))
    env.update(MA_COMPUTE_EXCLUDE_WORKERS=agent_addr)     # resource / target / caps come from the cluster environ
    open("/tmp/ascheck7.status", "w").write("running")
    t0 = time.time()
    r = subprocess.run([sys.executable, "scripts/compute.py", "map", run, "--fn", "models/burn.py::f",
                        "--inputs", inp, "--out", os.path.join(run, "models", "out.jsonl")],
                       cwd=work, env=env, capture_output=True, text=True, timeout=5400)
    rows = [json.loads(l) for l in open(os.path.join(run, "models", "out.jsonl"))] \
        if os.path.exists(os.path.join(run, "models", "out.jsonl")) else []
    import socket
    res = {"rc": r.returncode, "wall_s": round(time.time() - t0), "stderr": r.stderr[-800:],
           "n_ok": sum(x["ok"] for x in rows), "agent_host": socket.gethostname(),
           "hosts_used": sorted({(x.get("result") or {}).get("host") for x in rows if x.get("ok")}),
           "ledger": json.load(open(os.path.join(run, "models", "compute_ledger.json")))
           if os.path.exists(os.path.join(run, "models", "compute_ledger.json")) else None}
    json.dump(res, open("/tmp/ascheck7.result.json", "w"))
    open("/tmp/ascheck7.status", "w").write("done")
    time.sleep(240)          # stay busy a little longer: status calls must still reach this VM
    return res


def peek():
    import json, os
    st = open("/tmp/ascheck7.status").read() if os.path.exists("/tmp/ascheck7.status") else "missing"
    res = json.load(open("/tmp/ascheck7.result.json")) if os.path.exists("/tmp/ascheck7.result.json") else None
    return {"status": st, "result": res}


def launch():
    sys.path.insert(0, os.path.join(TREE, "scripts"))
    import compute
    code = subprocess.check_output(["git", "-C", TREE, "archive", "--format=tar.gz", "HEAD"])
    env = {"MA_COMPUTE_RESOURCE": "compute", "MA_COMPUTE_TARGET_WORKERS": str(MAX_COMPUTE),
           "MA_COMPUTE_MAX_CORE_HOURS": "1500", "MA_COMPUTE_CALL_CORE_HOURS": "400"}
    cl = coiled.Cluster(name=NAME, workspace=WS, n_workers=3, worker_cpu=8, worker_memory="64 GiB",
                        worker_disk_size=100, scheduler_cpu=2, spot_policy="on-demand", idle_timeout="45 minutes",
                        shutdown_on_close=False, worker_options=compute.WORKER_OPTIONS, environ=env)
    c = cl.get_client(); c.wait_for_workers(3, timeout=1500)
    agent = sorted(c.scheduler_info()["workers"])[0]
    compute.register_compute_tags(c, exclude=[agent])
    fut = c.submit(agent_side, code, agent, N_TASKS, TASK_S, workers=[agent], allow_other_workers=False,
                   pure=False, key="ascheck7-agent")
    fire_and_forget(fut)
    time.sleep(5)
    cl.adapt(minimum=3, maximum=1 + MAX_COMPUTE, **compute.ADAPT_OPTIONS)
    json.dump({"agent": agent, "t0": time.time()}, open(STATE, "w"))
    print(f"launched; agent={agent}; adapt 3..{1 + MAX_COMPUTE}; launcher exiting", flush=True)


def watch():
    st = json.load(open(STATE))
    cs = coiled.list_clusters(workspace=WS)
    items = cs if isinstance(cs, list) else list(cs.values())
    if not any(x.get("name") == NAME for x in items):
        raise SystemExit("cluster not running; refusing to create one")
    cl = coiled.Cluster(name=NAME, workspace=WS, shutdown_on_close=False)
    c = cl.get_client()
    counts, peeks, out = [], [], None
    try:
        t_end = time.time() + 3600
        while time.time() < t_end:
            n = len(c.scheduler_info()["workers"])
            counts.append((round(time.time() - st["t0"]), n))
            t = time.time()
            try:
                p = c.run(peek, workers=[st["agent"]])[st["agent"]]
                peeks.append((round(time.time() - t, 1), p["status"]))
            except Exception as e:  # noqa: BLE001
                p = {"status": f"error {type(e).__name__}"}
                peeks.append((round(time.time() - t, 1), p["status"]))
            if p.get("status") == "done":
                out = p["result"]
                break
            time.sleep(30)
        print("timeline (s since launch, workers):", counts)
        print("client.run on the busy agent VM (seconds, status):", peeks[:5], "...", peeks[-3:])
        print(json.dumps(out, indent=1)[:2500] if out else "no result")
        peak = max(n for _, n in counts) if counts else 0
        busy_ok = all(s in ("running", "done") for _, s in peeks) and max(t for t, _ in peeks) < 30
        ok = bool(out) and out["n_ok"] == N_TASKS and out["agent_host"] not in out["hosts_used"] and peak >= 20 \
            and busy_ok
        print(f"peak workers {peak} (agent + compute); compute hosts used {len(out['hosts_used']) if out else 0}")
        print("AUTOSCALE CHECK 7", "PASSED" if ok else "FAILED")
    finally:
        cl.shutdown()
        print("cluster shut down")


if __name__ == "__main__":
    {"launch": launch, "watch": watch}[sys.argv[1]]()
