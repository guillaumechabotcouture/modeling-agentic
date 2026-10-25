"""Coiled backend for scripts/replay.py (architecture v2).

Runs replay cases (fixture x variant x seed) in parallel on IDM Coiled,
robust to the laptop disconnecting:

  * one named cluster, shutdown_on_close=False, nthreads=1 per worker so a
    worker runs one case at a time (extra cases queue on their worker);
  * code (working tree, tracked + untracked) and each fixture are tarred and
    *scattered* to the pinned worker before the task is fired, and the
    launcher only exits once the scheduler holds every task key (a short
    grace period lost a 108 MB submission in the Phase 21 measles resume);
  * each case runs `replay.py case ...` on the VM — the same case logic as the
    local backend — and persists result.tar.gz + result.json to worker disk;
    the task never raises;
  * `collect` pulls results in sha256-verified chunks into the local batch
    dir in the local layout, so replay_compare.py works unchanged;
  * attach is guarded: coiled.Cluster(name=...) silently CREATES a fresh,
    empty cluster when none is running, which once replaced a cluster whose
    worker disks held results.

VMs run as the non-root user `cloudenv`; everything lives under /tmp/rc/.
Secrets travel as Redacted (repr '<redacted>') so Dask "Compute Failed" logs
cannot print them.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import time

WORKSPACE = "idm-coiled-idmad-r2"
REMOTE_BASE = "/tmp/rc"
CHUNK = 8 * 2**20
RUNNING_STATES = ("running", "ready", "pending", "starting", "queued", "provisioning")
ASSIGN_FILE = "coiled_assignments.json"
CODEX_VERSION = "0.163.0"          # same CLI as the laptop; gpt-6.1-sol via ~/.codex/config.toml
# Coiled containers can't create network namespaces, so Codex's default Linux
# sandbox (bwrap) fails every command ("bwrap: loopback: Failed RTM_NEWADDR:
# Operation not permitted") and the critic silently reviews nothing. Landlock
# needs no namespaces; the verification smoke below fails loudly if it doesn't work.
CODEX_CONFIG = ('model = "gpt-6.1-sol"\nmodel_reasoning_effort = "xhigh"\n'
                '[features]\nuse_legacy_landlock = true\n')


class Redacted:
    """Carries a secret without ever printing it."""
    def __init__(self, value):
        self._v = value

    def get(self):
        return self._v

    def __repr__(self):
        return "<redacted>"
    __str__ = __repr__


def needs_codex(variant: dict) -> bool:
    """Only the E5 cross-model arm runs Codex; only its cases receive the OpenAI key."""
    return (variant or {}).get("agent") == "crossmodel"


def _download(url: str) -> bytes:
    import urllib.request
    return urllib.request.urlopen(url, timeout=120).read()


CODEX_PACKAGE_KEEP = ("bin/", "codex-path/", "codex-resources/bwrap", "codex-package.json")


def setup_codex(env: dict, openai_key: "Redacted", home: str | None = None,
                download=_download, run=subprocess.run, smoke: bool = False) -> dict:
    """Install the Codex CLI on this VM and log it in with the OpenAI key.

    Uses the official `codex-package` tarball (bin/codex + bin/codex-code-mode-host,
    codex-path/rg, codex-resources/bwrap), extracting only those members and
    keeping the package layout. The bare `codex-*-linux-musl` binary is not
    enough: without codex-code-mode-host every tool call fails, and the critic
    then "reviews" nothing (observed 2026-10-22: verdict PASS, 0 files read).
    Config: ~/.codex/config.toml; login: `codex login --with-api-key` with the
    key on stdin. Mutates env["PATH"]. The returned record never contains the
    key. Never raises.
    """
    home = home or os.path.expanduser("~")
    info = {"version": CODEX_VERSION, "installed": False, "login_rc": None}
    try:
        arch = "x86_64" if os.uname().machine in ("x86_64", "amd64") else "aarch64"
        url = (f"https://github.com/openai/codex/releases/download/rust-v{CODEX_VERSION}/"
               f"codex-package-{arch}-unknown-linux-musl.tar.gz")
        pkg = os.path.join(home, ".local", "codex")
        os.makedirs(pkg, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(download(url)), mode="r:gz") as t:
            members = [m for m in t.getmembers()
                       if m.name.lstrip("./").startswith(CODEX_PACKAGE_KEEP) and ".." not in m.name]
            t.extractall(pkg, members=members)
        exe = os.path.join(pkg, "bin", "codex")
        for p_ in (exe, os.path.join(pkg, "bin", "codex-code-mode-host"),
                   os.path.join(pkg, "codex-path", "rg"), os.path.join(pkg, "codex-resources", "bwrap")):
            if os.path.exists(p_):
                os.chmod(p_, 0o755)
        info["helpers"] = sorted(n for n in ("bin/codex-code-mode-host", "codex-path/rg", "codex-resources/bwrap")
                                 if os.path.exists(os.path.join(pkg, n)))
        if not os.path.exists(exe):
            raise FileNotFoundError("bin/codex missing from codex-package")
        info["installed"] = True
        env["PATH"] = os.pathsep.join([os.path.join(pkg, "bin"), os.path.join(pkg, "codex-path"),
                                       env.get("PATH", "")])
        os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
        with open(os.path.join(home, ".codex", "config.toml"), "w") as f:
            f.write(CODEX_CONFIG)
        r = run([exe, "login", "--with-api-key"], input=openai_key.get(), text=True,
                capture_output=True, env=env, timeout=120)
        info["login_rc"] = r.returncode
        out = (r.stdout or "") + (r.stderr or "")
        info["login_msg"] = out.replace(openai_key.get() or "\0", "<redacted>").strip()[:160]
        v = run([exe, "--version"], text=True, capture_output=True, env=env, timeout=60)
        info["cli"] = (v.stdout or "").strip()[:60]
        if smoke and r.returncode == 0:
            # Must exercise a real tool call: "reply OK" passed while tools were broken.
            probe = os.path.join(home, ".codex", "smoke_dir")
            os.makedirs(probe, exist_ok=True)
            for n_ in ("a.txt", "b.txt", "c.txt"):
                open(os.path.join(probe, n_), "w").write(n_)
            # Same sandbox mode the critic uses on the VM (bwrap can't run in the container).
            sm = run([exe, "exec", "--skip-git-repo-check", "-s", "danger-full-access", "-C", probe,
                      "Use a shell command to count the files in the current directory. "
                      "Reply with only the number."],
                     text=True, capture_output=True, env=env, timeout=300)
            info["smoke_rc"] = sm.returncode
            info["smoke_out"] = (sm.stdout or "").strip()[-40:]
            info["smoke_tools_ok"] = sm.returncode == 0 and (sm.stdout or "").strip().endswith("3")
    except Exception as e:  # noqa: BLE001 - persist, never raise
        info["error"] = f"{type(e).__name__}: {e}"[:300]
    return info


# --------------------------------------------------------------------------- packaging (local)

def case_key(variant_id: str, fixture_id: str, seed: int) -> str:
    return f"{variant_id}__{fixture_id}__s{seed}"


def tar_bytes(entries: list[tuple[str, str]], compresslevel: int = 1) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=compresslevel) as t:
        for src, arc in entries:
            t.add(src, arcname=arc)
    return buf.getvalue()


def code_tarball(repo: str) -> tuple[bytes, dict]:
    """Ship the working tree exactly (tracked + untracked, minus ignored), so
    uncommitted edits under test are what runs on the VM."""
    files = subprocess.check_output(["git", "-C", repo, "ls-files", "-co", "--exclude-standard"],
                                    text=True).split("\n")
    entries = [(os.path.join(repo, f), f) for f in files if f and os.path.isfile(os.path.join(repo, f))]
    head = subprocess.check_output(["git", "-C", repo, "rev-parse", "--short", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "-C", repo, "status", "--porcelain"], text=True).strip())
    blob = tar_bytes(entries)
    return blob, {"head": head, "dirty": dirty, "files": len(entries),
                  "sha256": hashlib.sha256(blob).hexdigest()[:16]}


def fixture_tarball(fixture_dir: str) -> bytes:
    fid = os.path.basename(os.path.normpath(fixture_dir))
    return tar_bytes([(fixture_dir, fid)])


def variants_bundle(variants: list[dict]) -> bytes:
    """Variant YAMLs with prompt_file copied alongside and made relative."""
    import tempfile
    import yaml
    with tempfile.TemporaryDirectory() as td:
        for v in variants:
            vd = dict(v)
            if vd.get("prompt_file"):
                name = f"{vd['id']}.prompt.md"
                with open(vd["prompt_file"]) as src, open(os.path.join(td, name), "w") as dst:
                    dst.write(src.read())
                vd["prompt_file"] = name
            with open(os.path.join(td, f"{vd['id']}.yaml"), "w") as f:
                yaml.safe_dump(vd, f, sort_keys=False)
        return tar_bytes([(os.path.join(td, n), n) for n in sorted(os.listdir(td))])


def write_batch_json(batch_dir: str, cases: list) -> None:
    """batch.json = {"preflight": <launch preflight record or None>, "cases": [...]}."""
    pf = None
    try:
        pf = json.load(open(os.path.join(batch_dir, "preflight.json")))
    except (OSError, ValueError):
        pass
    json.dump({"preflight": pf, "cases": cases}, open(os.path.join(batch_dir, "batch.json"), "w"),
              indent=1, default=str)


def plan_cases(fixture_dirs, variants, seeds):
    return [{"key": case_key(v["id"], os.path.basename(os.path.normpath(f)), s),
             "fixture_dir": f, "fixture_id": os.path.basename(os.path.normpath(f)),
             "variant_id": v["id"], "seed": s, "budget_usd": float(v["budget_usd"]),
             "needs_codex": needs_codex(v), "codex_smoke": bool(v.get("codex_smoke")),
             "compute": bool(v.get("compute"))}
            for f in fixture_dirs for v in variants for s in range(seeds)]


def check_total_budget(cases, max_total_usd):
    total = sum(c["budget_usd"] for c in cases)
    if max_total_usd is not None and total > max_total_usd:
        raise SystemExit(f"refusing to launch: {len(cases)} cases x budgets = ${total:.2f} "
                         f"exceeds --max-total-usd ${max_total_usd:.2f}")
    return total


def assign_workers(cases, workers):
    """Fixture-affine assignment: every case sharing a fixture runs on the same
    worker, so the fixture is uploaded once (round-robin spread E5's 5 arms of
    each variant over 5 workers -> 5 uploads each, 1.9 GB). Fixture groups go
    to the least-loaded worker (by case count), largest groups first."""
    ws = sorted(workers)
    groups = {}
    for c in cases:
        groups.setdefault(c["fixture_id"], []).append(c)
    load = {w: 0 for w in ws}
    assign = {}
    for fid, grp in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        w = min(ws, key=lambda x: (load[x], x))
        for c in grp:
            assign[c["key"]] = w
        load[w] += len(grp)
    return assign


# --------------------------------------------------------------------------- remote (runs on the VM)

def _unchanged_input(path, fixture_inputs, relpath):
    src = os.path.join(fixture_inputs, relpath)
    try:
        if os.stat(path).st_nlink > 1:            # hard-linked large fixture data
            return True
        a, b = os.stat(path), os.stat(src)
        return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)
    except OSError:
        return False


def _pip_freeze():
    try:
        from importlib.metadata import distributions
        return sorted(f"{d.metadata['Name']}=={d.version}" for d in distributions() if d.metadata["Name"])
    except Exception as e:  # noqa: BLE001
        return [f"<unavailable: {e}>"]


def tls_key_secrets(tls_dir: str) -> list:
    """The exported TLS private key as redactable strings: the whole PEM and
    each base64 line, so a key printed in pieces is still caught."""
    try:
        pem = open(os.path.join(tls_dir, "key.pem")).read()
    except OSError:
        return []
    lines = [l.strip() for l in pem.splitlines() if l.strip() and not l.startswith("-----")]
    return [pem.strip()] + [l for l in lines if len(l) >= 12]


def redact_secrets(paths: list, secrets: list) -> list:
    """Scan files about to leave the VM for secret strings; redact in place.
    Returns the relative names of files that contained one (never the secret)."""
    hits = []
    secrets = [x for x in secrets if x and len(x) >= 12]
    for p in paths:
        try:
            if not os.path.isfile(p) or os.path.getsize(p) > 200 * 2**20:
                continue
            data = open(p, "rb").read()
        except OSError:
            continue
        new = data
        for sec in secrets:
            new = new.replace(sec.encode(), b"<redacted>")
        if new != data:
            open(p, "wb").write(new)
            hits.append(os.path.basename(p))
    return hits


def remote_case(case: dict, code: bytes, fixture: bytes, variants: bytes,
                anthropic_key: "Redacted", dry_agent: bool = False, base: str = REMOTE_BASE,
                openai_key: "Redacted | None" = None) -> dict:
    """Run one replay case on this machine. Never raises: failures are
    persisted to result.json so `collect`/`status` report them."""
    import shutil
    import traceback
    root = os.path.join(base, case["key"])
    out_meta = os.path.join(root, "result.json")
    # Idempotent re-runs: a previous attempt left large inputs read-only (0444),
    # which made re-extraction fail with PermissionError.
    def _force_writable(func, path, *_):
        os.chmod(path, 0o755 if os.path.isdir(path) else 0o644)
        func(path)
    if os.path.exists(root):
        for dp, dns, fns in os.walk(root):
            for n_ in dns + fns:
                try:
                    os.chmod(os.path.join(dp, n_), 0o755 if n_ in dns else 0o644)
                except OSError:
                    pass
        shutil.rmtree(root, onexc=_force_writable)
    os.makedirs(root, exist_ok=True)
    open(os.path.join(root, "started"), "w").write(str(time.time()))
    t0 = time.time()
    meta = {"key": case["key"], "status": "error", "started": t0}
    try:
        repo = os.path.join(root, "repo")
        shutil.rmtree(repo, ignore_errors=True)
        for blob, dest in ((code, repo), (fixture, os.path.join(root, "fixtures")),
                           (variants, os.path.join(root, "variants"))):
            os.makedirs(dest, exist_ok=True)
            tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz").extractall(dest)
        fixture_dir = os.path.join(root, "fixtures", case["fixture_id"])
        batch = os.path.join(root, "batch")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "OPENAI", "AZ_", "AZURE_"))}
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
        if anthropic_key is not None and anthropic_key.get():
            env["ANTHROPIC_API_KEY"] = anthropic_key.get()
        if case.get("needs_codex") and openai_key is not None and openai_key.get() and not dry_agent:
            # OPENAI_* stays stripped from env: codex authenticates from ~/.codex/auth.json.
            meta["codex"] = setup_codex(env, openai_key, smoke=bool(case.get("codex_smoke")))
            env["CROSS_MODEL_SANDBOX"] = "danger-full-access"   # disposable VM; integrity enforced by replay
        if case.get("compute"):
            # Phase 24: the case's agent reaches the compute workers through this
            # worker's scheduler connection; credentials stay outside the packaged workspace.
            import importlib.util
            spec_ = importlib.util.spec_from_file_location("ma_compute", os.path.join(repo, "scripts", "compute.py"))
            mod = importlib.util.module_from_spec(spec_)
            spec_.loader.exec_module(mod)
            env.update(mod.export_worker_env(os.path.join(root, ".tls")))
            env["MA_COMPUTE_EXCLUDE_WORKERS"] = ",".join(case.get("exclude_workers") or [])
            meta["compute"] = {"scheduler": bool(env.get("MA_COMPUTE_SCHEDULER")),
                               "tls": bool(env.get("MA_COMPUTE_TLS_DIR"))}
        cmd = [sys.executable, os.path.join(repo, "scripts", "replay.py"), "case",
               "--fixture", fixture_dir, "--variant", os.path.join(root, "variants", f"{case['variant_id']}.yaml"),
               "--seed", str(case["seed"]), "--out", batch] + (["--dry-agent"] if dry_agent else [])
        with open(os.path.join(root, "case.log"), "w") as log:
            proc = subprocess.run(cmd, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT)
        ws = os.path.join(batch, case["variant_id"], f"{case['fixture_id']}-s{case['seed']}")
        cj = os.path.join(ws, "case.json")
        case_json = json.load(open(cj)) if os.path.exists(cj) else None
        # Package outputs only: skip fixture inputs that were linked or copied unchanged.
        entries, skipped = [], 0
        inputs = os.path.join(fixture_dir, "inputs")
        for dp, _, files in os.walk(ws):
            for fn in files:
                p = os.path.join(dp, fn)
                rel = os.path.relpath(p, ws)
                if rel.startswith("run" + os.sep) and _unchanged_input(p, inputs, rel[len("run") + 1:]):
                    skipped += 1
                    continue
                entries.append((p, os.path.join(case["variant_id"], f"{case['fixture_id']}-s{case['seed']}", rel)))
        entries.append((os.path.join(root, "case.log"), os.path.join(
            case["variant_id"], f"{case['fixture_id']}-s{case['seed']}", "cloud_case.log")))
        sid = ((case_json or {}).get("result") or {}).get("session_id")
        if sid:
            import glob
            for p in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}*")):
                entries.append((p, os.path.join(case["variant_id"], f"{case['fixture_id']}-s{case['seed']}",
                                                "claude_session", os.path.basename(p))))
        secrets = [k.get() for k in (anthropic_key, openai_key) if k is not None and k.get()]
        secrets += tls_key_secrets(os.path.join(root, ".tls"))
        meta["secret_hits"] = redact_secrets([p for p, _ in entries], secrets)
        # Re-read after redaction: the copy loaded above predates it and is
        # embedded in result.json, which collect prefers.
        case_json = json.load(open(cj)) if os.path.exists(cj) else None
        blob = tar_bytes(entries)
        open(os.path.join(root, "result.tar.gz"), "wb").write(blob)
        meta.update(status=(case_json or {}).get("status", "no_case_json"), exit_code=proc.returncode,
                    case=case_json, skipped_inputs=skipped, files=len(entries),
                    size=len(blob), sha256=hashlib.sha256(blob).hexdigest())
    except BaseException as e:  # noqa: BLE001 - persist, never raise
        meta.update(error=f"{type(e).__name__}: {e}", traceback=traceback.format_exc()[-3000:])
        if not os.path.exists(os.path.join(root, "result.tar.gz")):
            open(os.path.join(root, "result.tar.gz"), "wb").write(b"")
    meta.update(duration_s=round(time.time() - t0, 1), pip_freeze=_pip_freeze(),
                hostname=os.uname().nodename)
    text = json.dumps(meta, indent=1, default=str)
    for sec in [k.get() for k in (anthropic_key, openai_key) if k is not None and k.get()] + \
            tls_key_secrets(os.path.join(root, ".tls")):
        if len(sec) >= 12:                       # same rule as redact_secrets
            text = text.replace(sec, "<redacted>")   # error/traceback strings too
    open(out_meta, "w").write(text)
    shutil.rmtree(os.path.join(root, ".tls"), ignore_errors=True)
    meta = json.loads(text)
    # Phase 26: the case stores its own result (tarball, then metadata) so
    # collection never depends on the operator's laptop or the VM surviving.
    store = case.get("store") or {}
    if store.get("account"):
        try:
            import importlib.util
            sp = importlib.util.spec_from_file_location(
                "ma_results_store", os.path.join(root, "repo", "scripts", "results_store.py"))
            rs = importlib.util.module_from_spec(sp)
            sp.loader.exec_module(rs)
            base = f"outputs/{store['prefix']}/{case['key']}"
            rs.upload(os.path.join(root, "result.tar.gz"), f"{base}/result.tar.gz", store["account"])
            rs.upload(out_meta, f"{base}/result.json", store["account"])
            open(os.path.join(root, "stored"), "w").write(base)
        except Exception as e:  # noqa: BLE001 — the local copy still exists; collect reports it
            open(os.path.join(root, "store_error"), "w").write(f"{type(e).__name__}: {e}"[:500])
    return {k: v for k, v in meta.items() if k not in ("pip_freeze", "case", "traceback")}


def remote_meta(key: str, base: str = REMOTE_BASE) -> dict:
    root = os.path.join(base, key)
    if os.path.exists(os.path.join(root, "result.json")):
        m = json.load(open(os.path.join(root, "result.json")))
        m["state"] = "done"
        return m
    if os.path.exists(os.path.join(root, "started")):
        log = os.path.join(root, "case.log")
        return {"state": "running", "log_bytes": os.path.getsize(log) if os.path.exists(log) else 0}
    return {"state": "queued"}


def remote_chunk(key: str, offset: int, length: int, base: str = REMOTE_BASE) -> bytes:
    with open(os.path.join(base, key, "result.tar.gz"), "rb") as f:
        f.seek(offset)
        return f.read(length)


# --------------------------------------------------------------------------- cluster (local)

def _state(c):
    s = c.get("current_state")
    return s.get("state") if isinstance(s, dict) else s


def running_cluster(name, workspace=WORKSPACE):
    import coiled
    cs = coiled.list_clusters(workspace=workspace)
    items = cs if isinstance(cs, list) else list(cs.values())
    live = [c for c in items if c.get("name") == name and _state(c) in RUNNING_STATES]
    return live[0] if live else None


def attach(name, workspace=WORKSPACE):
    """Attach-only: refuse rather than create a fresh, empty cluster."""
    if not running_cluster(name, workspace):
        raise SystemExit(f"cluster {name!r} is not running; refusing to create a new one")
    import coiled
    return coiled.Cluster(name=name, workspace=workspace, shutdown_on_close=False)


def create_or_attach(name, n_workers, workspace=WORKSPACE):
    import coiled
    if running_cluster(name, workspace):
        return coiled.Cluster(name=name, workspace=workspace, shutdown_on_close=False)
    return coiled.Cluster(name=name, workspace=workspace, n_workers=n_workers, worker_cpu=8,
                          worker_memory="32 GiB", worker_disk_size=100, scheduler_cpu=2,
                          idle_timeout="2 hours", spot_policy="on-demand",
                          worker_options={"nthreads": 1}, shutdown_on_close=False)


def launch(cases, variants, repo, batch_dir, cluster_name, workers, anthropic_key, dry_agent=False,
           workspace=WORKSPACE, log=print, openai_key=None, compute_workers=0):
    """compute_workers: extra workers that run no case, only compute-service
    batches from cases whose variant sets `compute: true` (Phase 24)."""
    from distributed import fire_and_forget
    store = {"account": os.environ.get("MA_STORAGE_ACCOUNT"),
             "prefix": f"replay/{os.path.basename(os.path.normpath(batch_dir))}"}
    if not store["account"] and not dry_agent:
        raise SystemExit("MA_STORAGE_ACCOUNT is not set: cases must store their own results "
                         "(Phase 26; E8 was lost to laptop-only collection)")
    n = max(1, min(workers or len(cases), len(cases)))
    if any(c.get("compute") for c in cases) and not compute_workers:
        raise SystemExit("a variant sets compute: true but --compute-workers is 0")
    code, code_info = code_tarball(repo)
    vbundle = variants_bundle(variants)
    log(f"code {code_info} ({len(code) >> 20} MB); variants {len(vbundle) >> 10} KB; {len(cases)} cases on {n} workers")
    cluster = create_or_attach(cluster_name, n + compute_workers, workspace)
    client = cluster.get_client()
    if len(client.scheduler_info()["workers"]) < n + compute_workers:
        cluster.scale(n + compute_workers)       # an attached cluster may be smaller
    client.wait_for_workers(n + compute_workers, timeout=1800)
    client.upload_file(os.path.abspath(__file__))
    case_workers = sorted(client.scheduler_info()["workers"])[:n]
    assign = assign_workers(cases, case_workers)
    scattered = {}
    for w in sorted(set(assign.values())):
        code_f, vb_f = client.scatter([code, vbundle], workers=[w], broadcast=False)
        scattered[w] = {"code": code_f, "variants": vb_f, "fixtures": {}}
    record = {"cluster": cluster_name, "workspace": workspace, "code": code_info,
              "compute_workers": compute_workers, "store": store,
              "dry_agent": dry_agent, "launched": time.strftime("%F %T"), "cases": {}}
    for c in cases:
        w = assign[c["key"]]
        fx = scattered[w]["fixtures"]
        if c["fixture_id"] not in fx:
            blob = fixture_tarball(c["fixture_dir"])
            log(f"  scatter {c['fixture_id']} ({len(blob) >> 20} MB) -> {w}")
            fx[c["fixture_id"]] = client.scatter(blob, workers=[w], broadcast=False)
        spec = {k: c[k] for k in ("key", "fixture_id", "variant_id", "seed")}
        spec.update(needs_codex=bool(c.get("needs_codex")), codex_smoke=bool(c.get("codex_smoke")),
                    compute=bool(c.get("compute")), exclude_workers=case_workers, store=store)
        okey = Redacted(openai_key if c.get("needs_codex") else None)
        fut = client.submit(remote_case, spec, scattered[w]["code"], fx[c["fixture_id"]],
                            scattered[w]["variants"], Redacted(anthropic_key), dry_agent, REMOTE_BASE, okey,
                            workers=[w], allow_other_workers=False, pure=False, key=f"replay-{c['key']}")
        fire_and_forget(fut)
        record["cases"][c["key"]] = {"worker": w, **spec, "budget_usd": c["budget_usd"]}
    # Persist the assignment first, so `collect` works even if confirmation fails.
    os.makedirs(batch_dir, exist_ok=True)
    json.dump(record, open(os.path.join(batch_dir, ASSIGN_FILE), "w"), indent=1)
    # Exit only once every case is accounted for: still held by the scheduler,
    # or already started/finished on its worker (a finished fire-and-forget
    # task is released from the scheduler, so "held" alone never converges).
    pending = set(record["cases"])
    for _ in range(120):
        held = set(client.run_on_scheduler(
            lambda dask_scheduler, ks=tuple(f"replay-{k}" for k in pending):
                [k[len("replay-"):] for k in ks if k in dask_scheduler.tasks]))
        for k in list(pending - held):
            w = record["cases"][k]["worker"]
            st = client.run(remote_meta, k, workers=[w]).get(w, {}).get("state")
            if st in ("running", "done"):
                pending.discard(k)
        pending -= held
        if not pending:
            break
        time.sleep(5)
    else:
        raise SystemExit(f"cases not accounted for after 10 minutes: {sorted(pending)}")
    log(f"all {len(record['cases'])} cases registered or running; safe to disconnect")
    return record


def _download(client, worker, key, size, sha, dest):
    have = os.path.getsize(dest) if os.path.exists(dest) else 0
    with open(dest, "ab") as out:
        while have < size:
            for attempt in range(6):
                try:
                    # client.run executes on the worker's event loop, not its single task
                    # thread (which may be busy running another case) — a submitted task
                    # would queue behind it and time out.
                    b = client.run(remote_chunk, key, have, CHUNK, workers=[worker]).get(worker)
                    break
                except Exception:
                    if attempt == 5:
                        raise
                    time.sleep(10 * (attempt + 1))
            out.write(b); out.flush(); have += len(b)
    h = hashlib.sha256()
    with open(dest, "rb") as f:
        for b in iter(lambda: f.read(2**20), b""):
            h.update(b)
    if h.hexdigest() != sha:
        os.remove(dest)
        raise RuntimeError(f"{key}: checksum mismatch; partial download removed")


def install_result(batch_dir, case_rec, meta, tar_path):
    """Extract into the local batch layout and point case.json at local paths."""
    if os.path.getsize(tar_path):
        tarfile.open(tar_path, mode="r:gz").extractall(batch_dir)
    ws = os.path.join(batch_dir, case_rec["variant_id"], f"{case_rec['fixture_id']}-s{case_rec['seed']}")
    os.makedirs(ws, exist_ok=True)
    cj = os.path.join(ws, "case.json")
    case = meta.get("case") or (json.load(open(cj)) if os.path.exists(cj) else
                                {"fixture": case_rec["fixture_id"], "variant": case_rec["variant_id"],
                                 "seed": case_rec["seed"], "status": meta.get("status", "error"),
                                 "result": {"error": meta.get("error")}, "telemetry": {}, "scores": {}, "flat": {}})
    case["workspace"] = ws
    case["cloud"] = {k: meta.get(k) for k in ("codex", "compute", "secret_hits", "hostname", "duration_s", "exit_code", "error", "skipped_inputs",
                                              "files", "size", "sha256", "pip_freeze")}
    case["cloud"]["worker"] = case_rec["worker"]
    json.dump(case, open(cj, "w"), indent=1, default=str)
    return case


def collect_from_store(batch_dir, rec, done_dir, log=print, service=None):
    """Download each uncollected case's stored result (Phase 26). Returns the
    case list when every case is collected, else None (missing ones listed)."""
    store = rec.get("store") or {}
    if not store.get("account"):
        log("cluster is gone and this batch has no storage record; results on its VMs are lost")
        return None
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
    import results_store as rs
    missing = []
    for k, cr in rec["cases"].items():
        if os.path.exists(os.path.join(done_dir, k)):
            continue
        base = f"outputs/{store['prefix']}/{k}"
        meta_p, tar_p = os.path.join(done_dir, f"{k}.json.part"), os.path.join(done_dir, f"{k}.tar.gz.part")
        try:
            rs.download(f"{base}/result.json", meta_p, store["account"], service=service)
            got = rs.download(f"{base}/result.tar.gz", tar_p, store["account"], service=service)
        except Exception as e:  # noqa: BLE001 — not stored (yet)
            missing.append(f"{k} ({type(e).__name__})")
            continue
        meta = json.load(open(meta_p))
        if meta.get("sha256") and got["sha256"] != meta["sha256"] and got["bytes"]:
            missing.append(f"{k} (sha mismatch)")
            continue
        case = install_result(batch_dir, cr, meta, tar_p)
        for f in (meta_p, tar_p):
            os.remove(f)
        open(os.path.join(done_dir, k), "w").write(meta.get("sha256") or "")
        log(f"  {k}: collected from storage ({case.get('status')})")
    if missing:
        log(f"  not in storage yet: {', '.join(missing)}")
        return None
    cases = []
    for k, cr in rec["cases"].items():
        cases.append(json.load(open(os.path.join(batch_dir, cr["variant_id"],
                                                 f"{cr['fixture_id']}-s{cr['seed']}", "case.json"))))
    write_batch_json(batch_dir, cases)
    log(f"all {len(cases)} cases collected from storage -> {batch_dir}/batch.json")
    return cases


def collect(batch_dir, cluster_name=None, watch=False, interval_s=60, log=print):
    rec = json.load(open(os.path.join(batch_dir, ASSIGN_FILE)))
    name = cluster_name or rec["cluster"]
    done_dir = os.path.join(batch_dir, ".collected")
    os.makedirs(done_dir, exist_ok=True)
    while True:
        if not running_cluster(name, rec.get("workspace", WORKSPACE)):
            # Cluster gone (idled out): fetch what the cases stored themselves.
            cases = collect_from_store(batch_dir, rec, done_dir, log)
            if cases is not None or not watch:
                return cases
            time.sleep(interval_s)
            continue
        cluster = attach(name, rec.get("workspace", WORKSPACE))
        client = cluster.get_client()
        pending = [k for k in rec["cases"] if not os.path.exists(os.path.join(done_dir, k))]
        for k in pending:
            cr = rec["cases"][k]
            m = client.run(remote_meta, k, workers=[cr["worker"]]).get(cr["worker"])
            if m["state"] != "done":
                log(f"  {k}: {m['state']}")
                continue
            tmp = os.path.join(done_dir, f"{k}.tar.gz.part")
            if m.get("size"):
                _download(client, cr["worker"], k, m["size"], m["sha256"], tmp)
            else:
                open(tmp, "wb").close()
            case = install_result(batch_dir, cr, m, tmp)
            os.remove(tmp)
            open(os.path.join(done_dir, k), "w").write(m.get("sha256") or "")
            log(f"  {k}: collected ({case.get('status')}, {m.get('size', 0) >> 10} KB, sha256 verified)")
        remaining = [k for k in rec["cases"] if not os.path.exists(os.path.join(done_dir, k))]
        if not remaining:
            cases = []
            for k, cr in rec["cases"].items():
                cj = os.path.join(batch_dir, cr["variant_id"], f"{cr['fixture_id']}-s{cr['seed']}", "case.json")
                cases.append(json.load(open(cj)))
            write_batch_json(batch_dir, cases)
            log(f"all {len(cases)} cases collected -> {batch_dir}/batch.json")
            if watch:
                cluster.shutdown()
                log(f"cluster {name!r} shut down")
            return cases
        if not watch:
            return None
        time.sleep(interval_s)


def status(batch_dir, cluster_name=None, log=print):
    rec = json.load(open(os.path.join(batch_dir, ASSIGN_FILE)))
    client = attach(cluster_name or rec["cluster"], rec.get("workspace", WORKSPACE)).get_client()
    out = {}
    for k, cr in rec["cases"].items():
        m = client.run(remote_meta, k, workers=[cr["worker"]]).get(cr["worker"])
        out[k] = m.get("state")
        log(f"  {k}: {m.get('state')} {m.get('status', '')} {m.get('log_bytes', '')}")
    return out


def shutdown(cluster_name, workspace=WORKSPACE, log=print):
    attach(cluster_name, workspace).shutdown()
    log(f"cluster {cluster_name!r} shut down")
