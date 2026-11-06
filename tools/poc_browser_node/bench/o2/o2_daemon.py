"""ONE ssh login to O2 for a whole benchmark run, held by a small daemon.

    python bench/o2/o2_daemon.py start --state DIR [--login o2.hms.harvard.edu]
    python bench/o2/o2_daemon.py call --state DIR OP [JSON]

On O2 every new ssh session on a login node runs Duo -- a multiplexed one
too -- and so does a login on a compute node. A run that submits a job, waits
for it, starts a node, restarts it between arms and cancels it would ask for a
push at each step, and a driver that crashed halfway would ask again on the
rerun. `start` makes the run's only login and keeps it:

- one ssh process (from WSL, where the benchmark key needs no passphrase) with
  one session on the login node: a long-lived bash fed on stdin, down which
  every command goes -- sbatch, squeue, srun --overlap into the job, scancel;
- a control socket (ControlMaster) on that same connection, on which the
  tunnel is added with `-O forward` once the job's host is known: a forward is
  a channel, not a session, and runs nothing on the far side.

The node runs as a step of a job that only holds the allocation, so it can be
stopped and started per arm (cold) without a login. It listens on the compute
node's interfaces and the login node forwards to it (Plexora's own `bind_node`
layout), so no ssh into the compute node is needed; it answers nothing without
the token. The token is made here, written to a mode-600 file on O2 through
the session's stdin and to `DIR/node.token` here, and never appears on a
command line or in this daemon's output.

The daemon listens on loopback for its driver; every call carries the key in
`DIR/daemon.json` (keep DIR in a folder only this account can read). When the
session ends -- `close`, the ssh dying, or the daemon killed -- the remote
shell's trap cancels the job. The job's own time limit is the last stop.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

JOB_NAME = "plx-poc-timing"
CONTROL = "~/.ssh/cm/plx-poc-timing"
REMOTE_DIR = "$HOME/plexora-poc"

HOLD_SBATCH = """#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --partition={partition}
#SBATCH --time={time}
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={mem}
#SBATCH --output=/dev/null
# Holds the allocation. The node runs in it as a step (srun --overlap from the
# login node, down the run's one ssh session), so it can be restarted per arm.
sleep {seconds}
"""

NODE_RUN = """#!/bin/bash
# One node of the run, as a step of the holding job. The image's pages are
# dropped from this compute node's page cache first (posix_fadvise on the file
# only; fincore before and after into cache.txt), then the node starts on all
# interfaces (the login node forwards to it) with the token from a mode-600
# file in its environment.
set -eu
umask 077
HERE="$HOME/plexora-poc"
PORT="$1"; shift
export PLEXORA_NODE_TOKEN="$(cat "$HERE/node.token")"
export PLEXORA_DATA_PATH={data_root}
export PLEXORA_TELEMETRY=off
mkdir -p "$PLEXORA_DATA_PATH"
IMAGE={image}
before=$(fincore --bytes --noheadings "$IMAGE" | awk '{{print $1}}')
{python} -c "import os,sys;fd=os.open(sys.argv[1],os.O_RDONLY);os.posix_fadvise(fd,0,0,os.POSIX_FADV_DONTNEED);os.close(fd)" "$IMAGE"
after=$(fincore --bytes --noheadings "$IMAGE" | awk '{{print $1}}')
echo "$(hostname -s) $before $after" > "$HERE/cache.txt"
ORIGINS=()
for o in "$@"; do ORIGINS+=(--allow-origin "$o"); done
exec {plexora} node serve --host 0.0.0.0 --port "$PORT" --node-id poc-o2 \\
    --serve "image:img=$IMAGE" --dynamic --manifest "$PLEXORA_DATA_PATH/manifest.json" "${{ORIGINS[@]}}"
"""

#: Lines the login node prints that are not any command's output.
BANNER = ("Problems logging in?", "Use your lower case HMS ID", "If locked out, see:",
          "https://it.hms.harvard.edu/")


class Shell:
    """The run's one ssh process: a remote bash fed on stdin (bytes, LF only),
    each command's output ending at a marker line carrying its status."""

    def __init__(self, login: str, log):
        self.login = login
        self.log = log
        argv = ["wsl.exe", "-d", "Ubuntu", "-e", "ssh", "-M", "-o", f"ControlPath={CONTROL}",
                "-o", "ControlPersist=no", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=6", "-o", "ExitOnForwardFailure=yes",
                login, "bash --noprofile --norc -s"]
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.lines: "queue.Queue[str | None]" = queue.Queue()
        self.errors: list[str] = []
        self.lock = threading.Lock()

        def pump(stream, sink):
            for raw in iter(stream.readline, b""):
                sink(raw.decode("utf-8", "replace").rstrip("\r\n"))
            if stream is self.proc.stdout:
                self.lines.put(None)
        threading.Thread(target=pump, args=(self.proc.stdout, self.lines.put), daemon=True).start()
        threading.Thread(target=pump, args=(self.proc.stderr, self.errors.append), daemon=True).start()

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def run(self, command: str, timeout: float = 300, check: bool = True, label: str | None = None) -> str:
        marker = f"__plx_{secrets.token_hex(8)}__"
        with self.lock:
            if not self.alive:
                raise RuntimeError("the ssh session has ended")
            # The marker on a line of its own even after output with no final
            # newline (curl -w): a newline first, dropped again below.
            self.proc.stdin.write(f"{{ {command}\n}} 2>&1; s=$?; echo; echo {marker} $s\n".encode())
            self.proc.stdin.flush()
            out, end = [], time.time() + timeout
            while True:
                try:
                    line = self.lines.get(timeout=max(0.1, end - time.time()))
                except queue.Empty:
                    raise TimeoutError(f"no answer in {timeout:.0f} s: {label or command[:60]}") from None
                if line is None:
                    raise RuntimeError("the ssh session ended: " + " | ".join(self.errors[-3:]))
                if line.startswith(marker):
                    status = int(line.split()[1])
                    break
                if not any(line.startswith(b) for b in BANNER):
                    out.append(line)
        if out and out[-1] == "":
            out.pop()
        text = "\n".join(out)
        if check and status != 0:
            raise RuntimeError(f"failed ({status}): {label or command[:80]!r}: {text[-600:]}")
        return text

    def forward(self, local: str, target: str) -> None:
        done = subprocess.run(["wsl.exe", "-d", "Ubuntu", "-e", "ssh", "-o", f"ControlPath={CONTROL}",
                               "-O", "forward", "-L", f"{local}:{target}", self.login],
                              capture_output=True, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(f"forward {local} -> {target}: {done.stderr.decode(errors='replace')[-400:]}")

    def close(self) -> None:
        try:
            with self.lock:
                self.proc.stdin.write(b"exit\n")
                self.proc.stdin.flush()
                self.proc.stdin.close()
            self.proc.wait(timeout=60)
        except Exception:  # noqa: BLE001
            self.proc.kill()


class Run:
    """The cluster side of one run: the holding job, the node in it, the tunnel."""

    def __init__(self, state: Path, login: str, log):
        self.state = state
        self.log = log
        self.token = secrets.token_hex(32)
        token_file = state / "node.token"
        token_file.write_text(self.token, encoding="ascii")
        self.shell = Shell(login, log)
        self.job = None
        self.host = None
        self.cfg: dict = {}
        self.node_pid = None
        self.tunnel = None
        self.address = None
        self.started = time.time()

    # -- the login
    def connect(self) -> dict:
        t0 = time.time()
        self.shell.run("echo ready", timeout=300)
        # From here on, however the session ends, the job goes with it.
        self.shell.run(f"trap 'scancel --name={JOB_NAME} >/dev/null 2>&1' EXIT HUP TERM")
        self.shell.run(f"umask 077; mkdir -p {REMOTE_DIR}; chmod 700 {REMOTE_DIR}")
        # The token through stdin into a mode-600 file: printf is a builtin, so
        # it is on no process's command line.
        self.shell.run(f"printf '%s' '{self.token}' > {REMOTE_DIR}/node.token; chmod 600 {REMOTE_DIR}/node.token",
                       label="write the token")
        return {"login_s": round(time.time() - t0, 1), "host": self.shell.run("hostname -s").strip()}

    # -- the job and the node's environment
    def setup(self, cfg: dict) -> dict:
        self.cfg = cfg
        out = {}
        hold = HOLD_SBATCH.format(job_name=JOB_NAME, partition=cfg.get("partition", "short"),
                                  time=cfg.get("time", "2:00:00"), cpus=cfg.get("cpus", 8),
                                  mem=cfg.get("mem", "32G"), seconds=cfg.get("hold_seconds", 7200))
        self._write("hold.sbatch", hold)
        if self.job:
            # A second setup in the same session replaces the first job.
            self.node_stop()
            self.shell.run(f"scancel {self.job}", check=False)
        t0 = time.time()
        self.job = self.shell.run(f"cd {REMOTE_DIR} && sbatch --parsable hold.sbatch").strip().split(";")[0]
        out["job"] = self.job
        self.log(f"submitted job {self.job}")
        if cfg.get("python") and cfg.get("plexora"):
            python, plexora = cfg["python"], cfg["plexora"]
        else:
            # The cluster env's own interpreter and entry point, by activating it.
            found = self.shell.run(f"( source {cfg['conda']}/etc/profile.d/conda.sh && conda activate {cfg['conda_env']} "
                                   "&& command -v python && command -v plexora )").split()
            python, plexora = found[-2], found[-1]
        if cfg.get("pip"):
            # While the job queues: a private venv over the cluster env with
            # the release the run asks for (one pip install, no new login).
            # If it fails, the cluster env's own Plexora serves instead.
            t1 = time.time()
            pip = self.shell.run(
                f"( source {cfg['conda']}/etc/profile.d/conda.sh && conda activate {cfg['conda_env']} && "
                f"cd {REMOTE_DIR} && (test -x venv/bin/python || python -m venv --system-site-packages venv) && "
                f"venv/bin/python -m pip install -q --disable-pip-version-check {cfg['pip']} > pip.log 2>&1 ); "
                f"r=$?; tail -3 {REMOTE_DIR}/pip.log; test $r = 0", timeout=1800, check=False)
            version = self.shell.run(f"{REMOTE_DIR}/venv/bin/python -c "
                                     "'import importlib.metadata as m;print(m.version(\"plexora\"))'", check=False).strip()
            out.update(pip_s=round(time.time() - t1, 1), pip_tail=pip[-400:], venv_version=version)
            if version and version in cfg["pip"]:
                python, plexora = f"{REMOTE_DIR}/venv/bin/python", f"{REMOTE_DIR}/venv/bin/plexora"
        out["plexora"] = plexora
        out["version"] = self.shell.run(f"{python} -c 'import importlib.metadata as m;print(m.version(\"plexora\"))'",
                                        check=False).strip()
        self._write("node_run.sh", NODE_RUN.format(data_root=cfg["data_root"], image=cfg["image"],
                                                   python=python, plexora=plexora))
        state = self.shell.run(
            f"for i in $(seq 1 {int(cfg.get('queue_wait_s', 1800)) // 5}); do "
            f"s=$(squeue -h -j {self.job} -o '%T %N'); case \"$s\" in RUNNING*) echo \"$s\"; break;; "
            "FAILED*|CANCELLED*|TIMEOUT*|COMPLETED*) echo \"GONE $s\"; break;; "
            "'') [ $i -gt 6 ] && { echo GONE; break; };; esac; sleep 5; done",
            timeout=int(cfg.get("queue_wait_s", 1800)) + 60).strip().splitlines()
        if not state or not state[-1].startswith("RUNNING"):
            raise RuntimeError(f"job {self.job} did not start: {state[-1:] }")
        self.host = state[-1].split()[1]
        out.update(host=self.host, queued_s=round(time.time() - t0, 1))
        self.log(f"job {self.job} running on {self.host}")
        self.cfg["node_port"] = int(cfg.get("node_port", 0)) or 30000 + secrets.randbelow(20000)
        self.address = self.shell.run(f"getent ahostsv4 {self.host} | awk 'NR==1{{print $1}}'").strip() or self.host
        out.update(node_port=self.cfg["node_port"], compute=self.address)
        return out

    def tunnel_up(self, mode: str = "direct") -> dict:
        """The forward from this machine to the node, added to the run's one
        connection. `direct`: the login node connects to the compute node's
        port (Plexora's bind_node layout). `hop`: an ssh from the login node
        into the compute node carries it instead, for a cluster whose compute
        ports the login node cannot reach -- an ssh inside the cluster, which
        may or may not ask for Duo there; used only when `direct` cannot work."""
        local = f"127.0.0.1:{int(self.cfg.get('tunnel_port', 8765))}"
        port = self.cfg["node_port"]
        if mode == "direct":
            target = f"{self.address}:{port}"
        else:
            hop_port = 30000 + secrets.randbelow(20000)
            self.shell.run(
                f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ExitOnForwardFailure=yes -N "
                f"-L 127.0.0.1:{hop_port}:127.0.0.1:{port} {self.host} < /dev/null > {REMOTE_DIR}/hop.log 2>&1 & "
                "echo $! > " + REMOTE_DIR + "/hop.pid; sleep 5; cat " + REMOTE_DIR + "/hop.log", check=False)
            target = f"127.0.0.1:{hop_port}"
        self.shell.forward(local, target)
        self.tunnel = local
        out = {"mode": mode, "tunnel": local}
        if self.node_pid:
            out["health_s"] = self._health(time.time(), 30)
        return out

    def _health(self, t0: float, wait_s: float) -> float:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                request = urllib.request.Request(f"http://{self.tunnel}/node/v1/health",
                                                 headers={"X-Plexora-Node-Token": self.token})
                with urllib.request.urlopen(request, timeout=5) as reply:
                    if reply.status == 200:
                        return round(time.time() - t0, 2)
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        raise RuntimeError("the node does not answer through the tunnel")

    def _write(self, name: str, text: str) -> None:
        self.shell.run(f"cat > {REMOTE_DIR}/{name} <<'PLXEOF'\n{text}PLXEOF\nchmod 700 {REMOTE_DIR}/{name}")

    # -- the node
    def node_stop(self) -> dict:
        t0 = time.time()
        if self.node_pid:
            self.shell.run(
                f"srun --jobid={self.job} --overlap -N1 -n1 -c1 --mem=100M "
                "pkill -u $USER -f '[n]ode serve .*--node-id poc-o2( |$)' < /dev/null >/dev/null 2>&1; "
                f"wait {self.node_pid} 2>/dev/null; true", timeout=180, check=False)
            self.node_pid = None
        return {"stop_s": round(time.time() - t0, 2)}

    def node_start(self, origins: list) -> dict:
        out = self.node_stop()
        t0 = time.time()
        quoted = " ".join(f"'{o}'" for o in origins)
        self.node_pid = self.shell.run(
            f"cd {REMOTE_DIR} && : > node.log && "
            f"srun --jobid={self.job} --overlap -N1 -n1 -c{self.cfg.get('cpus', 8)} --mem={self.cfg.get('step_mem', '24G')} "
            # stdin from /dev/null: srun forwards its stdin to the task, and
            # this shell's stdin is the run's command stream.
            f"bash {REMOTE_DIR}/node_run.sh {self.cfg['node_port']} {quoted} < /dev/null > node.log 2>&1 & echo $!").strip()
        # The announce line carries the token: count it, never print it.
        waited = self.shell.run(
            "for i in $(seq 1 600); do "
            f"[ \"$(grep -c '\\[plexora-node\\]' {REMOTE_DIR}/node.log)\" != 0 ] && {{ echo up; break; }}; "
            f"kill -0 {self.node_pid} 2>/dev/null || {{ echo gone; break; }}; sleep 0.3; done", timeout=240).strip()
        if waited != "up":
            tail = self.shell.run(f"tail -8 {REMOTE_DIR}/node.log | sed 's/token=[^ ]*/token=<redacted>/g'",
                                  check=False)
            raise RuntimeError(f"the node did not come up ({waited}): {tail}")
        # Can the login node reach it? (Any HTTP status means yes; 000 no.)
        out["login_reach"] = self.shell.run(
            f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' http://{self.address}:{self.cfg['node_port']}/node/v1/health",
            check=False).strip()
        out["announce_s"] = round(time.time() - t0, 2)
        cache = self.shell.run(f"cat {REMOTE_DIR}/cache.txt", check=False).split()
        if len(cache) == 3:
            out["page_cache"] = {"host": cache[0], "before": int(cache[1]), "after": int(cache[2])}
        if self.tunnel:
            try:
                out["health_s"] = self._health(t0, 120)
            except RuntimeError as error:
                raise RuntimeError(f"{error} (from the login node: HTTP {out.get('login_reach')})") from None
        return out

    def status(self) -> dict:
        return {"alive": self.shell.alive, "job": self.job, "host": self.host, "tunnel": self.tunnel,
                "node_running": bool(self.node_pid), "uptime_s": round(time.time() - self.started),
                "endpoint": f"http://{self.tunnel}" if self.tunnel else None,
                "token_file": str(self.state / "node.token")}

    def close(self) -> dict:
        out = {}
        try:
            out.update(self.node_stop())
            if self.job:
                self.shell.run(f"[ -f {REMOTE_DIR}/hop.pid ] && kill $(cat {REMOTE_DIR}/hop.pid) 2>/dev/null; "
                               f"scancel {self.job}; rm -f {REMOTE_DIR}/node.token {REMOTE_DIR}/node.log {REMOTE_DIR}/hop.pid",
                               check=False)
                out["cancelled"] = self.job
        except Exception as error:  # noqa: BLE001
            out["error"] = repr(error)
        self.shell.close()
        try:
            (self.state / "node.token").unlink()
        except OSError:
            pass
        return out


def serve(args) -> None:
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(16)

    def log(text):
        print(time.strftime("%H:%M:%S"), text, flush=True)

    run = Run(state, args.login, log)
    log("connecting: one ssh login (one Duo push)")
    try:
        log(f"connected: {json.dumps(run.connect())}")
    except Exception as error:  # noqa: BLE001
        log(f"not connected: {error}")
        run.shell.close()
        sys.exit(1)
    server: ThreadingHTTPServer | None = None
    ops = {
        "status": lambda body: run.status(),
        "setup": lambda body: run.setup(body),
        "node_start": lambda body: run.node_start(body.get("origins") or []),
        "node_stop": lambda body: run.node_stop(),
        "tunnel": lambda body: run.tunnel_up(body.get("mode", "direct")),
        "run": lambda body: {"out": run.shell.run(body["cmd"], timeout=float(body.get("timeout", 300)),
                                                  check=False)},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_POST(self):
            if self.headers.get("X-Key") != key:
                self.send_error(403)
                return
            op = self.path.strip("/")
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            try:
                if op == "close":
                    result = run.close()
                    threading.Thread(target=server.shutdown, daemon=True).start()
                else:
                    result = ops[op](body)
                code = 200
            except Exception as error:  # noqa: BLE001
                result, code = {"error": f"{type(error).__name__}: {error}"}, 500
            log(f"{op}: {json.dumps(result)[:300]}")
            data = json.dumps(result).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    (state / "daemon.json").write_text(json.dumps({"port": server.server_address[1], "key": key,
                                                   "pid": os.getpid()}), encoding="utf-8")
    log(f"listening on 127.0.0.1:{server.server_address[1]}")

    def watch():
        while run.shell.alive:
            time.sleep(2)
        log("the ssh session ended: " + " | ".join(run.shell.errors[-3:]))
        server.shutdown()
    threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        if run.shell.alive:
            log(f"close: {json.dumps(run.close())}")
        (state / "daemon.json").unlink(missing_ok=True)


def call(state: Path, op: str, body: dict | None = None, timeout: float = 3600) -> dict:
    info = json.loads((Path(state) / "daemon.json").read_text(encoding="utf-8"))
    request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/{op}", method="POST",
                                     data=json.dumps(body or {}).encode(),
                                     headers={"X-Key": info["key"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return json.loads(reply.read())
    except urllib.error.HTTPError as error:
        return json.loads(error.read() or b"{}") or {"error": str(error)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start")
    start.add_argument("--state", required=True)
    start.add_argument("--login", default="o2.hms.harvard.edu")
    start.add_argument("--port", type=int, default=0)
    c = sub.add_parser("call")
    c.add_argument("--state", required=True)
    c.add_argument("op")
    c.add_argument("body", nargs="?", default="{}")
    args = parser.parse_args(argv)
    if args.cmd == "start":
        serve(args)
    else:
        print(json.dumps(call(Path(args.state), args.op, json.loads(args.body)), indent=1))


if __name__ == "__main__":
    main()
