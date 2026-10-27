"""Warm dev VM for real-SDK scenario tests (scripts/sdk_scenarios.py).

    python dev_vm.py up                 # start (or keep) one small VM; idle-stops after 45 min
    python dev_vm.py run [scenario ...] # ship the dev tree, install the pinned SDK, run scenarios
    python dev_vm.py down               # stop it

`run` and `down` never create a cluster (attach guard)."""
import json, os, subprocess, sys, time
import coiled

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ab_coiled import read_env_keys  # noqa: E402

NAME, WS = "dev-vm-gcc", "idm-coiled-idmad-r2"
TREE = os.path.expanduser(os.environ.get("DEV_TREE", "~/modeling-agentic-dev"))


def _running():
    cs = coiled.list_clusters(workspace=WS)
    items = cs if isinstance(cs, list) else list(cs.values())
    return any(c.get("name") == NAME and str(c.get("current_state", {}).get("state", c.get("state", ""))).lower()
               in ("ready", "running", "pending", "starting", "scaling") for c in items)


def _attach():
    if not _running():
        raise SystemExit(f"{NAME} is not running; use `up` first")
    return coiled.Cluster(name=NAME, workspace=WS, shutdown_on_close=False)


def side(code, key, names):
    import io, os, re, subprocess, sys, tarfile
    work = "/tmp/devvm"
    subprocess.run(["rm", "-rf", work]); os.makedirs(work)
    tarfile.open(fileobj=io.BytesIO(code), mode="r:gz").extractall(work)
    pins = [l.split("#")[0].strip() for l in open(os.path.join(work, "requirements.txt"))
            if re.match(r"\s*claude-agent-sdk==", l)]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pins], check=True, timeout=600)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "OPENAI", "AZ_", "AZURE_"))}
    env["ANTHROPIC_API_KEY"] = key
    env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    r = subprocess.run([sys.executable, "scripts/sdk_scenarios.py", "run", *names], cwd=work, env=env,
                       capture_output=True, text=True, timeout=1800)
    return r.stdout[-20000:] + ("\nSTDERR:\n" + r.stderr[-3000:] if r.returncode else "")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "up":
        cl = coiled.Cluster(name=NAME, workspace=WS, n_workers=1, worker_cpu=4, worker_memory="16 GiB",
                            scheduler_cpu=2, spot_policy="on-demand", idle_timeout="45 minutes",
                            shutdown_on_close=False)
        cl.get_client().wait_for_workers(1, timeout=1500)
        print(f"{NAME} up")
    elif cmd == "run":
        code = subprocess.check_output(["git", "-C", TREE, "archive", "--format=tar.gz", "HEAD"])
        head = subprocess.check_output(["git", "-C", TREE, "rev-parse", "--short", "HEAD"], text=True).strip()
        key = read_env_keys(os.path.expanduser("~/modeling-agentic/.env"))["ANTHROPIC_API_KEY"]
        c = _attach().get_client()
        t0 = time.time()
        print(f"code @ {head}", flush=True)
        print(c.submit(side, code, key, sys.argv[2:], pure=False).result(timeout=2400))
        print(f"[{time.time() - t0:.0f}s]")
    elif cmd == "down":
        _attach().shutdown()
        print(f"{NAME} stopped")
    else:
        raise SystemExit(__doc__)
