"""The remote-access benchmark: a data node beside the file (arm N) against
streaming the file's bytes over SFTP (arm S), on one image, from this laptop.

    python campaign.py --config cfg.json setup        # bench primary, projects, survey
    python campaign.py --config cfg.json c0 --arm N --reps 3
    python campaign.py --config cfg.json blocks --blocks 0-5
    python campaign.py --config cfg.json probes        # RTT, bandwidth, calibration

`cfg.json` names the cluster (login, job, env, node data root, the image),
the files-only server (its saved name and target), the bench directory on
this machine and the results directory; see README.md. Nothing here writes
outside those two directories, the node's bench data root and the job.

A block is one paired run: both arms, in ABBA order across blocks, on one
waypoint set. Each arm starts cold (C1) and is then rerun warm (C2):

- N, C1: the image's pages dropped from the compute node's page cache
  (posix_fadvise, verified by fincore), a new node process (empty tile and
  chunk caches) and a new primary; the node's derived files (.ims_index,
  quantization) kept, as a returning user has them.
- S, C1: a new primary with the on-disk chunk cache restored to what the
  first-ever open left in it (the HDF5 metadata a returning user has cached),
  so no block finds another block's pixels there.
- C2: the same processes and the same Chrome, straight after C1.

Every row of runs.jsonl is one arm of one block: timings from the page,
bytes from Chrome (by origin), from nettop (the ssh process that carries
the arm's traffic) and from the far side (/proc io for N; SFTP counters for
S), split by pass.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE.parent / "bench3d"))
sys.path.insert(0, str(HERE))
import o2  # noqa: E402
from serve import start as serve_start, stop as serve_stop  # noqa: E402

PY = sys.executable


# -- small tools --------------------------------------------------------------

def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def http(method, url, payload=None, timeout=60, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as reply:
        body = reply.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip() else {}


def nettop(pid: int | None) -> dict | None:
    """Cumulative bytes in/out of one process's sockets (nettop, unprivileged)."""
    if not pid:
        return None
    out = subprocess.run(["nettop", "-P", "-L", "1", "-J", "bytes_in,bytes_out", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip().splitlines()
    for line in out[1:]:
        parts = line.split(",")
        if len(parts) >= 3 and parts[1].isdigit():
            return {"in": int(parts[1]), "out": int(parts[2]), "t": time.time()}
    return {"in": 0, "out": 0, "t": time.time()}


def delta(after: dict | None, before: dict | None) -> dict | None:
    if not after or not before:
        return None
    return {k: after[k] - before[k] for k in after if isinstance(after.get(k), (int, float))
            and isinstance(before.get(k), (int, float))}


def tcp_rtt(host: str, port: int = 22, n: int = 10) -> dict:
    times = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=10):
                times.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
        time.sleep(0.1)
    return summary(times)


def summary(values) -> dict:
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    return {"n": len(values), "median": round(statistics.median(values), 2),
            "min": round(ordered[0], 2), "max": round(ordered[-1], 2)}


def laptop_state() -> dict:
    """What else this machine is doing: load, and anything known to skew a
    benchmark (a test run, Spotlight indexing)."""
    busy = subprocess.run(["ps", "-Ao", "pcpu,command"], capture_output=True, text=True).stdout
    rows = [line.strip().split(None, 1) for line in busy.splitlines()[1:]]
    heavy = sorted(((float(c), cmd[:90]) for c, cmd in rows if c.replace(".", "").isdigit()
                    and float(c) > 20), reverse=True)[:6]
    return {"load": os.getloadavg(), "pytest": any("pytest" in cmd for _, cmd in rows),
            "heavy": heavy}


# -- the bench primary --------------------------------------------------------

class Primary:
    """A Plexora on this laptop, serving the bench data root, user's HOME kept."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.port = int(cfg["primary_port"])
        self.data = Path(cfg["bench_dir"]).expanduser() / "data"
        self.info = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> float:
        t0 = time.time()
        env = {"PLEXORA_TELEMETRY": "off", **self.cfg.get("primary_env", {})}
        self.info = serve_start(self.data, self.port, env, python=PY, root=ROOT, real_home=True)
        return time.time() - t0

    def stop(self) -> None:
        if self.info:
            serve_stop(self.info["pid"])
            self.info = None
        else:
            # A primary left from an earlier run of this script.
            out = subprocess.run(["lsof", "-ti", f"tcp:{self.port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True).stdout.split()
            for pid in out:
                serve_stop(int(pid))
        time.sleep(0.5)

    def connect_sftp(self, timeout=600) -> float:
        name = self.cfg["sftp_remote"]
        t0 = time.time()
        http("POST", f"{self.base}/settings/remotes/{name}/connect", {})
        while time.time() - t0 < timeout:
            state = http("GET", f"{self.base}/settings/remotes/{name}/status").get("state")
            if state == "connected":
                return time.time() - t0
            if state in ("failed", "error"):
                raise RuntimeError(f"sftp connect failed: {state}")
            time.sleep(0.25)
        raise RuntimeError("sftp connect timed out")

    def sftp_pid(self) -> int | None:
        if not self.info:
            return None
        out = subprocess.run(["pgrep", "-P", str(self.info["pid"]), "-f", "sftp"],
                             capture_output=True, text=True).stdout.split()
        return int(out[0]) if out else None

    def register_node(self, node: o2.Node) -> dict:
        return http("POST", f"{self.base}/settings/nodes", {
            "name": self.cfg["node_name"], "endpoint": node.endpoint, "token": node.token,
            "browser_endpoint": node.endpoint})

    def perf(self, dataset: str) -> dict | None:
        try:
            return http("GET", f"{self.base}/volume/{dataset}/perf", timeout=30)
        except Exception as error:  # noqa: BLE001
            return {"error": repr(error)}

    def import_image(self, paths, name, node=None, timeout=3600) -> dict:
        payload = {"paths": paths, "name": name}
        if node:
            payload["node"] = node
        t0 = time.time()
        answer = http("POST", f"{self.base}/import/sample", payload, timeout=timeout)
        answer["seconds"] = time.time() - t0
        return answer


# -- the tour -----------------------------------------------------------------

def run_tour(cfg, primary, dataset, params, out: Path, between) -> dict:
    """tour.mjs over `params["passes"]`, calling `between(pass)` after each
    pass but the last (and before the next one starts)."""
    params = {**params, "handshake": True}
    argv = ["node", str(HERE / "tour.mjs"), "--base", primary.base, "--dataset", dataset,
            "--out", str(out), "--params", json.dumps(params)]
    log = open(out.with_suffix(".log"), "w", encoding="utf-8")
    proc = subprocess.Popen(argv, cwd=str(HERE), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=log, text=True, bufsize=1)
    for line in proc.stdout:
        if line.startswith("PASS_DONE"):
            between(line.split()[1])
            proc.stdin.write("\n")
            proc.stdin.flush()
    proc.wait()
    log.close()
    try:
        return json.loads(out.read_text(encoding="utf-8"))
    except Exception as error:  # noqa: BLE001
        return {"error": f"no result: {error!r}"}


# -- the campaign --------------------------------------------------------------

class Campaign:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.out = Path(cfg["results_dir"]).expanduser()
        self.raw = self.out / "raw"
        self.raw.mkdir(parents=True, exist_ok=True)
        self.bench = Path(cfg["bench_dir"]).expanduser()
        self.primary = Primary(cfg)
        c = cfg["cluster"]
        self.site = o2.Site(login=c["login"], job=str(c["job"]), python_env=c["python_env"],
                            data_root=c["node_data_root"], file=c["file"], cpus=int(c.get("cpus", 10)))
        self.node = None
        self.snapshot = self.bench / "remote_cache.snapshot"
        if self.cfg["bench_dir"] in ("", "/") or "Application Support/plexora" in str(self.bench):
            raise SystemExit("refusing a bench directory that is the live data root")

    # -- bookkeeping
    def record(self, row: dict, name="runs.jsonl") -> None:
        with open(self.out / name, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    def commit(self) -> dict:
        local = git("rev-parse", "HEAD")
        remote = self.site.ssh(f"git -C {self.cfg['cluster']['checkout']} rev-parse HEAD").strip()
        return {"laptop": local, "laptop_dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
                "node": remote}

    # -- the node
    def new_node(self) -> o2.Node:
        c = self.cfg["cluster"]
        return o2.Node(site=self.site, token=c["token"], node_port=int(c["node_port"]),
                       local_port=int(c["tunnel_port"]), allow_origin=self.primary.base,
                       node_id=self.cfg["node_name"], env=c.get("node_env", {}),
                       log_dir=self.raw)

    def node_down(self):
        if self.node:
            self.node.stop()
            self.node = None

    def node_up(self) -> dict:
        self.node_down()
        if not self.site.node:
            self.site.locate()
        self.node = self.new_node()
        return self.node.start()

    # -- arms
    def prepare(self, arm: str, state: str) -> dict:
        """Bring `arm` to the start of a cold (C1) run; return what it cost."""
        prep = {"arm": arm, "state": state}
        self.primary.stop()
        if arm == "N":
            prep["fadvise"] = self.site.drop_file_cache()
            prep["node"] = self.node_up()
            prep["t_primary"] = self.primary.start()
            t0 = time.time()
            self.primary.register_node(self.node)
            prep["t_register"] = time.time() - t0
        else:
            self.node_down()
            cache = self.primary.data / ".remote_cache"
            if state == "C1" and self.snapshot.exists():
                shutil.rmtree(cache, ignore_errors=True)
                # APFS clone: instant, and the snapshot stays as it was.
                subprocess.run(["cp", "-cR", str(self.snapshot), str(cache)], check=True)
            prep["cache_bytes"] = _du(cache)
            prep["t_primary"] = self.primary.start()
            prep["t_connect"] = self.primary.connect_sftp()
        return prep

    def counters(self, arm: str, dataset: str) -> dict:
        out = {"t": time.time()}
        if arm == "N":
            out["wire"] = nettop(self.node.wire_pid()) if self.node else None
            try:
                out["node_io"] = self.site.proc_io(self.cfg["node_name"])
            except Exception as error:  # noqa: BLE001
                out["node_io"] = {"error": repr(error)}
        else:
            out["wire"] = nettop(self.primary.sftp_pid())
            perf = self.primary.perf(dataset) or {}
            out["sftp"] = perf.get("sftp")
            out["remote"] = {k: v for k, v in (perf.get("remote") or {}).items() if k != "disk"}
            out["remote_disk"] = (perf.get("remote") or {}).get("disk", {}).get("used_bytes")
            out["chunk_cache"] = perf.get("chunk_cache")
            out["tile_cache"] = perf.get("tile_cache")
            out["exact_window_queue"] = perf.get("exact_window_queue")
            out["plane_prefetch"] = perf.get("plane_prefetch")
        return out

    def run_arm(self, block: int, arm: str, wp: dict, params: dict, label="primary") -> dict:
        # The project the first-ever open (C0 rep 1) made.
        dataset = (self.cfg.get("block_projects") or {}).get(arm) or f"{self.cfg['projects'][arm]}-c0r1"
        row = {"label": label, "block": block, "arm": arm, "dataset": dataset,
               "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "waypoints": wp,
               "laptop_before": laptop_state()}
        try:
            row["prep"] = self.prepare(arm, "C1")
            row["rtt_ms"] = self.rtt(arm)
            marks = [("start", self.counters(arm, dataset))]

            def between(name):
                marks.append((name, self.counters(arm, dataset)))

            tour_params = {**params, "waypoints": wp}
            out = self.raw / f"{label}-b{block:02d}-{arm}-{int(time.time())}.json"
            result = run_tour(self.cfg, self.primary, dataset, tour_params, out, between)
            marks.append(("end", self.counters(arm, dataset)))
            row["tour_file"] = out.name
            row["error"] = result.get("error")
            row["renderer"] = result.get("renderer")
            row["passes"] = [_pass_row(p) for p in result.get("passes", [])]
            row["marks"] = marks
            row["laptop_after"] = laptop_state()
        except Exception as error:  # noqa: BLE001 -- a failed arm is a result
            row["error"] = repr(error)
        row["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.record(row)
        return row

    def rtt(self, arm: str) -> dict:
        c = self.cfg["cluster"]
        out = {}
        if arm == "N" and self.node:
            times = []
            for _ in range(10):
                t0 = time.perf_counter()
                try:
                    http("GET", f"{self.node.endpoint}/node/v1/health", timeout=10,
                         headers={"X-Plexora-Node-Token": self.node.token})
                    times.append((time.perf_counter() - t0) * 1000)
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.1)
            out["node_http"] = summary(times)
            out["login_tcp"] = tcp_rtt(c["login"].split("@")[-1])
        else:
            out["transfer_tcp"] = tcp_rtt(self.cfg["sftp_target"].split("@")[-1])
        return out

    # -- the plan
    def blocks(self, which, params):
        sets = json.loads(Path(self.cfg["waypoints"]).read_text(encoding="utf-8"))["sets"]
        first = first_arm(self.cfg.get("seed", 20261026))
        for block in which:
            order = arm_order(block, first)
            wp = sets[block % len(sets)]
            z0 = self.cfg["z0s"][(block // len(sets)) % len(self.cfg["z0s"])]
            for arm in order:
                print(f"block {block} arm {arm} z0 {z0}", flush=True)
                row = self.run_arm(block, arm, wp, {**params, "z0": z0})
                print(f"  -> {_brief(row)}", flush=True)
        self.node_down()


    # -- C0: the first-ever open
    def c0(self, arm: str, rep: int, params: dict) -> dict:
        """Import the image into a new project as a first-time user would and
        open it: nothing derived from the file exists anywhere this
        benchmark controls (the node's bench data root; this side's chunk
        cache). Rep 1 of S leaves the snapshot every S C1 starts from."""
        c = self.cfg["cluster"]
        name = f"{self.cfg['projects'][arm]}-c0r{rep}"
        row = {"label": "C0", "arm": arm, "rep": rep, "dataset": name,
               "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "laptop_before": laptop_state()}
        try:
            self.primary.stop()
            if arm == "N":
                self.node_down()
                root = c["node_data_root"]
                self.site.ssh(f"cd {root} && rm -rf .ims_index node-quantization node-derived "
                              f"node-manifests node-masks manifest.json")
                row["fadvise"] = self.site.drop_file_cache()
                row["node"] = self.node_up()
                row["t_primary"] = self.primary.start()
                self.primary.register_node(self.node)
                wire = lambda: nettop(self.node.wire_pid())  # noqa: E731
                before = {"wire": wire(), "node_io": self.site.proc_io(self.cfg["node_name"])}
                row["import"] = self.primary.import_image([c["file"]], name, node=self.cfg["node_name"])
            else:
                self.node_down()
                shutil.rmtree(self.primary.data / ".remote_cache", ignore_errors=True)
                row["t_primary"] = self.primary.start()
                row["t_connect"] = self.primary.connect_sftp()
                wire = lambda: nettop(self.primary.sftp_pid())  # noqa: E731
                before = {"wire": wire()}
                url = f"sftp://{self.cfg['sftp_target']}{c['file']}"
                row["import"] = self.primary.import_image([url], name)
                row["cache_after_import"] = _du(self.primary.data / ".remote_cache")
                if rep == 1:
                    shutil.rmtree(self.snapshot, ignore_errors=True)
                    subprocess.run(["cp", "-cR", str(self.primary.data / ".remote_cache"),
                                    str(self.snapshot)], check=True)
                    row["snapshot_bytes"] = _du(self.snapshot)
            row["after_import"] = {"wire": wire()}
            if arm == "N":
                row["after_import"]["node_io"] = self.site.proc_io(self.cfg["node_name"])
            row["before"] = before
            out = self.raw / f"C0-{arm}-r{rep}-{int(time.time())}.json"
            result = run_tour(self.cfg, self.primary, name,
                              {**params, "passes": ["C0"], "openOnly": True}, out, lambda _: None)
            row["after_open"] = {"wire": wire()}
            if arm == "N":
                row["after_open"]["node_io"] = self.site.proc_io(self.cfg["node_name"])
            else:
                row["after_open"]["sftp"] = (self.primary.perf(name) or {}).get("sftp")
            row["tour_file"] = out.name
            row["error"] = result.get("error")
            row["passes"] = [_pass_row(p) for p in result.get("passes", [])]
        except Exception as error:  # noqa: BLE001
            row["error"] = repr(error)
        row["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.record(row, "c0.jsonl")
        return row

    # -- probes: the link and the hosts
    def probes(self) -> dict:
        c = self.cfg["cluster"]
        out = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "tcp_login": tcp_rtt(c["login"].split("@")[-1], n=20),
               "tcp_transfer": tcp_rtt(self.cfg["sftp_target"].split("@")[-1], n=20)}
        # ssh session setup, as each arm's first connection pays it.
        for name, target in (("login", c["login"]), ("transfer", self.cfg["sftp_target"])):
            times = []
            for _ in range(3):
                t0 = time.time()
                subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=no",
                                "-o", "ControlPath=none", target, "true"],
                               capture_output=True, timeout=120)
                times.append(time.time() - t0)
            out[f"ssh_setup_{name}_s"] = summary(times)
        # Raw SFTP throughput with Plexora's own client: 256 MB read from the
        # middle of the image in one pipelined span.
        script = (
            "import sys,time,subprocess; sys.path.insert(0, %r)\n"
            "from plexora.server.utils import sftp\n"
            "p=subprocess.Popen(['ssh','-T','-x','-a','-o','BatchMode=yes','-s',%r,'sftp'],"
            "stdin=subprocess.PIPE,stdout=subprocess.PIPE)\n"
            "c=sftp.SFTPClient(p); c.handshake(60)\n"
            "off=300*1024**3; n=256*1024**2\n"
            "t=time.time(); b=c.read_range(%r, off, off+n); dt=time.time()-t\n"
            "print(len(b)/dt/1e6, dt, c.max_read)\n"
            "t=time.time(); [c.read_range(%r, off+i*4096, off+i*4096+4096) for i in range(20)]\n"
            "print((time.time()-t)/20*1000)\n"
        ) % (str(ROOT), self.cfg["sftp_target"], c["file"], c["file"])
        done = subprocess.run([PY, "-c", script], capture_output=True, text=True, timeout=900)
        lines = done.stdout.strip().splitlines()
        if len(lines) >= 2:
            mbs, secs, max_read = lines[0].split()
            out["sftp_stream"] = {"MBps": round(float(mbs), 1), "seconds": round(float(secs), 2),
                                  "bytes": 256 * 1024 ** 2, "max_read": int(max_read)}
            out["sftp_small_read_ms"] = round(float(lines[1]), 1)
        else:
            out["sftp_stream"] = {"error": done.stderr[-400:]}
        # The same 256 MB through the node's tunnel is not measurable without a
        # route that streams raw bytes; the tunnel's rate is read off the tours
        # (wire bytes over transfer time) instead.
        out["calibration"] = {"laptop": json.loads(subprocess.run(
            [PY, str(HERE / "calibrate.py"), "30"], capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(ROOT)}).stdout.strip().splitlines()[-1])}
        try:
            out["calibration"]["node"] = o2.calibrate_remote(self.site)
        except Exception as error:  # noqa: BLE001
            out["calibration"]["node"] = {"error": repr(error)}
        self.record(out, "probes.jsonl")
        return out


def first_arm(seed: int) -> str:
    """The arm that leads block 0: a coin flip, fixed by the campaign's seed."""
    return random.Random(seed).choice(["N", "S"])


def arm_order(block: int, first: str) -> list:
    """ABBA across blocks: blocks 0 and 3 of every four lead with `first`, so
    neither arm always runs on the link or the node the other just warmed."""
    other = "S" if first == "N" else "N"
    return [first, other] if block % 4 in (0, 3) else [other, first]


def _du(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def _pass_row(p: dict) -> dict:
    """A pass, kept whole but for the bulky per-request phase lists."""
    steps = []
    for s in p.get("steps", []):
        net = s.get("net") or {}
        for kind in net.values():
            if isinstance(kind, dict) and "phases" in kind:
                kind["phases"] = {name: summary(vals) for name, vals in kind["phases"].items()}
        steps.append(s)
    return {"pass": p.get("pass"), "passFrom": p.get("passFrom"), "passTo": p.get("passTo"),
            "info": p.get("info"), "F": p.get("F"), "steps": steps}


def _brief(row: dict) -> str:
    if row.get("error"):
        return f"ERROR {row['error'][:200]}"
    parts = []
    for p in row.get("passes", []):
        fulls = [s.get("full") for s in p["steps"]]
        done = sum(1 for f in fulls if f is not None)
        parts.append(f"{p['pass']}: {done}/{len(fulls)} full, open "
                     f"{round((p['steps'][0].get('full') or -1) / 1000, 1)} s")
    return "; ".join(parts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", required=True)
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("blocks")
    b.add_argument("--blocks", default="0-5")
    b.add_argument("--label", default="primary")
    z = sub.add_parser("c0")
    z.add_argument("--arm", choices=["N", "S"], required=True)
    z.add_argument("--reps", default="1")
    sub.add_parser("probes")
    args = parser.parse_args(argv)
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    camp = Campaign(cfg)
    if args.cmd == "blocks":
        lo, _, hi = args.blocks.partition("-")
        which = range(int(lo), int(hi or lo) + 1)
        camp.record({"event": "session", "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                     "commit": camp.commit(), "job": camp.site.locate(),
                     "node_facts": camp.site.facts(), "laptop": {
                         "platform": platform.platform(), "machine": platform.machine(),
                         "state": laptop_state()}, "params": cfg["tour"]},
                    name="sessions.jsonl")
        try:
            camp.blocks(which, cfg["tour"])
        finally:
            camp.node_down()
            camp.primary.stop()
    elif args.cmd == "c0":
        lo, _, hi = args.reps.partition("-")
        try:
            for rep in range(int(lo), int(hi or lo) + 1):
                print(f"C0 {args.arm} rep {rep}", flush=True)
                row = camp.c0(args.arm, rep, cfg["tour"])
                print(f"  -> import {row.get('import', {}).get('seconds')} s; {_brief(row)}", flush=True)
        finally:
            camp.node_down()
            camp.primary.stop()
    elif args.cmd == "probes":
        print(json.dumps(camp.probes(), indent=1))


if __name__ == "__main__":
    main()
