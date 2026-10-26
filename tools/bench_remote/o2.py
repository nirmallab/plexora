"""The far side of the remote-access benchmark: a data node in a SLURM job.

Everything here runs over the system ssh, as Plexora's own connections do.
Every command runs in ONE long-lived shell on the job's compute node, opened
through a compute-node ssh master that hops via the login master; the
tunnel is a forward added to that master. On O2 every new session on the
login node (even a multiplexed one) runs Duo, so the run opens none: the
node is started from that shell (inside the job's cgroup, which the ssh
session is adopted into) rather than with srun from the login node.

Nothing here touches anything but the user's own processes and files:
the page cache is dropped for the benchmark's file only (`posix_fadvise`
DONTNEED, verified with `fincore`), never system-wide.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import signal
import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ANNOUNCE = re.compile(r"\[plexora-node\]\s+host=(\S+)\s+port=(\d+)\s+node_id=(\S+)\s+token=(\S+)")


@dataclass
class Site:
    """Where the node runs. Every path is on the cluster."""

    login: str                      # user@login-host
    job: str                        # the SLURM job the node runs in
    python_env: str                 # conda env holding the plexora checkout
    data_root: str                  # the node's PLEXORA_DATA_PATH (bench only)
    file: str                       # the image
    cpus: int = 10
    control_path: str = "~/.ssh/plx-bench-%r@%h:%p"
    node: str = ""                  # compute host, filled in by `locate`
    tunnel_ports: tuple = ()

    def master_options(self) -> list:
        return ["-o", "BatchMode=yes", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self.control_path}", "-o", "ControlPersist=12h"]

    def master_pid(self) -> int | None:
        """The master ssh: every byte to and from the login node crosses it."""
        done = subprocess.run(["ssh", *self.master_options(), "-O", "check", self.login],
                              capture_output=True, text=True)
        import re as _re
        match = _re.search(r"pid=(\d+)", done.stderr + done.stdout)
        return int(match.group(1)) if match else None

    def compute_master(self) -> list:
        """ssh options for the session's one connection INTO the compute node,
        hopping through the login master. Opened on first use and kept for
        12 h: a login on the compute node is a Duo push, so it happens once."""
        # The hop names the login master's socket literally: inside a
        # ProxyCommand, ssh expands %h to the COMPUTE host, and a templated
        # ControlPath there opened a second login (one more Duo push) per hop.
        login_socket = str(Path(self.control_path.replace("%r@%h:%p", self.login + ":22")).expanduser())
        hop = " ".join(["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=no",
                        "-o", f"ControlPath={login_socket}", "-W", "%h:%p", self.login])
        compute_socket = str(Path(f"~/.ssh/plx-bench-compute-{self.node}").expanduser())
        return ["-o", "BatchMode=yes", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={compute_socket}", "-o", "ControlPersist=12h",
                "-o", f"ProxyCommand={hop}"]

    def ensure_tunnel(self, local_port: int, node_port: int) -> float:
        """The forward to the node's loopback port, added to the compute
        master (`ssh -O forward`), so a node restart opens nothing new.
        Seconds spent (0 when it was already up)."""
        if self.tunnel_ports == (local_port, node_port):
            return 0.0
        self.close_tunnel()
        target = f"{self.login.split('@')[0]}@{self.node}"
        t0 = time.time()
        check = subprocess.run(["ssh", *self.compute_master(), "-O", "check", target],
                               capture_output=True, text=True)
        if check.returncode != 0:
            # The one login on the compute node this session makes.
            subprocess.run(["ssh", *self.compute_master(), "-fN", target], check=True, timeout=180)
        done = subprocess.run(["ssh", *self.compute_master(), "-O", "forward",
                               "-L", f"{local_port}:127.0.0.1:{node_port}", target],
                              capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise RuntimeError(f"could not add the forward: {done.stderr.strip()}")
        self.tunnel_ports = (local_port, node_port)
        return time.time() - t0

    def close_tunnel(self) -> None:
        """Take the forward off the compute master (the master stays up)."""
        if self.tunnel_ports and self.node:
            local_port, node_port = self.tunnel_ports
            subprocess.run(["ssh", *self.compute_master(), "-O", "cancel",
                            "-L", f"{local_port}:127.0.0.1:{node_port}",
                            f"{self.login.split('@')[0]}@{self.node}"],
                           capture_output=True, text=True, timeout=60)
        self.tunnel_ports = ()

    def compute_master_pid(self) -> int | None:
        if not self.node:
            return None
        done = subprocess.run(["ssh", *self.compute_master(), "-O", "check",
                               f"{self.login.split('@')[0]}@{self.node}"],
                              capture_output=True, text=True)
        import re as _re
        match = _re.search(r"pid=(\d+)", done.stderr + done.stdout)
        return int(match.group(1)) if match else None

    def shell(self) -> "RemoteShell":
        """The run's one session on the compute node (inside the job's
        cgroup), opened on first use through the compute master. Every
        command goes down it: a session on the login node is a Duo push."""
        if getattr(self, "_shell", None) is None or not self._shell.alive:
            target = f"{self.login.split('@')[0]}@{self.node}"
            self._shell = RemoteShell(["ssh", *self.compute_master(), target,
                                       "bash --noprofile --norc -s"])
        return self._shell

    def ssh(self, command: str, *, timeout: float = 300, check: bool = True) -> str:
        """`command` on the cluster: the compute node's shell sees the same
        filesystems and the same SLURM as the login node."""
        return self.shell().run(command, timeout=timeout, check=check)

    def on_node(self, command: str, *, timeout: float = 300) -> str:
        """`command` on the job's compute node."""
        return self.shell().run(command, timeout=timeout)

    def close(self) -> None:
        self.close_tunnel()
        if getattr(self, "_shell", None) is not None:
            self._shell.close()
            self._shell = None

    def locate(self) -> dict:
        """The job's state; its node is fixed in the configuration."""
        out = self.ssh(f"squeue -h -j {self.job} -o '%T %N %M %l'", check=False).split()
        if not out or out[0] != "RUNNING":
            raise RuntimeError(f"job {self.job} is not running: {out}")
        if out[1].split(".")[0] != self.node.split(".")[0]:
            raise RuntimeError(f"job {self.job} is on {out[1]}, not {self.node}")
        return {"state": out[0], "node": out[1], "elapsed": out[2], "limit": out[3]}

    def facts(self) -> dict:
        """The compute node: CPU model, cores, memory, load, kernel."""
        out = self.on_node(
            "lscpu | grep -E 'Model name|^CPU\\(s\\):'; nproc; free -g | sed -n 2p; "
            "cat /proc/loadavg; uname -r; cat /sys/fs/cgroup$(cat /proc/self/cgroup | "
            "cut -d: -f3)/cpu.max 2>/dev/null || true")
        return {"raw": out.strip().splitlines()}

    def drop_file_cache(self) -> dict:
        """Drop the image's pages from the compute node's page cache; returns
        the cached bytes before and after (fincore)."""
        py = f"{self.python_env}/bin/python"
        command = (
            f"fincore --bytes --noheadings {shlex.quote(self.file)}; "
            f"{py} -c \"import os;fd=os.open({self.file!r},os.O_RDONLY);"
            f"os.posix_fadvise(fd,0,0,os.POSIX_FADV_DONTNEED);os.close(fd)\"; "
            f"fincore --bytes --noheadings {shlex.quote(self.file)}")
        lines = [l.split() for l in self.on_node(command).strip().splitlines() if l.strip()]
        before = int(lines[0][0]) if lines else None
        after = int(lines[-1][0]) if lines else None
        return {"cached_before": before, "cached_after": after}

    def proc_io(self, node_id: str = "bench-o2") -> dict:
        """/proc/<pid>/io and CPU time of the node's server process."""
        command = (
            f"for p in $(pgrep -u $USER -f {shlex.quote(_own_node(node_id))}); do "
            "[ \"$(cat /proc/$p/comm)\" = plexora ] || continue; echo PID $p; cat /proc/$p/io; "
            "awk '{print \"utime\", $14; print \"stime\", $15}' /proc/$p/stat; "
            "grep VmRSS /proc/$p/status; done")
        out = self.on_node(command)
        procs, current = {}, None
        for line in out.splitlines():
            parts = line.replace(":", "").split()
            if len(parts) >= 2 and parts[0] == "PID":
                current = procs.setdefault(parts[1], {})
            elif current is not None and len(parts) >= 2 and parts[1].isdigit():
                current[parts[0]] = int(parts[1])
        return procs


@dataclass
class Node:
    """One `plexora node serve` inside the job, and the tunnel to it."""

    site: Site
    token: str
    node_port: int
    local_port: int
    allow_origin: str
    node_id: str = "bench-o2"
    env: dict = field(default_factory=dict)
    log_dir: Path = Path(".")
    log_path: str = ""
    tunnel: subprocess.Popen | None = None
    timings: dict = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def start(self, timeout: float = 600) -> dict:
        """Start the node on the compute node (in the job's cgroup, through
        the run's shell) and wait for its announce line; then the forward,
        and wait for /health through it."""
        s = self.site
        env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in {
            "PLEXORA_DATA_PATH": s.data_root, "PLEXORA_TELEMETRY": "off", **self.env}.items())
        self.log_path = f"{s.data_root}/node-{int(time.time())}.log"
        serve = (f"{s.python_env}/bin/plexora node serve --host 127.0.0.1 "
                 f"--port {self.node_port} --token {self.token} --node-id {self.node_id} "
                 f"--dynamic --manifest {s.data_root}/manifest.json "
                 f"--allow-origin {self.allow_origin}")
        t0 = time.time()
        s.on_node(f"nohup env {env} {serve} > {self.log_path} 2>&1 < /dev/null & echo started")
        announced = None
        deadline = t0 + timeout
        while time.time() < deadline:
            out = s.on_node(f"grep -c '\\[plexora-node\\]' {self.log_path} 2>/dev/null || true")
            if out.strip().splitlines() and out.strip().splitlines()[-1].strip() not in ("", "0"):
                announced = time.time()
                break
            time.sleep(0.5)
        if announced is None:
            tail = s.on_node(f"tail -5 {self.log_path}")
            raise RuntimeError(f"the node did not announce itself in time: {tail}")
        opened = s.ensure_tunnel(self.local_port, self.node_port)
        healthy = self.wait_health(deadline)
        self.timings = {"t_node_announce": announced - t0, "t_tunnel_open": opened,
                        "t_tunnel_health": healthy - announced, "t_node_up": healthy - t0}
        return self.timings

    def wait_health(self, deadline: float) -> float:
        while time.time() < deadline:
            try:
                request = urllib.request.Request(f"{self.endpoint}/node/v1/health",
                                                 headers={"X-Plexora-Node-Token": self.token})
                with urllib.request.urlopen(request, timeout=5) as reply:
                    if reply.status == 200:
                        return time.time()
            except Exception:
                pass
            time.sleep(0.25)
        raise RuntimeError("the node did not answer through the tunnel")

    def wire_pid(self) -> int | None:
        """The process that carries the tunnel's bytes over the network: the
        login master (the tunnel's hop is a channel on it)."""
        return self.site.master_pid()

    def stop(self) -> None:
        """End this benchmark's node and nothing else of the user's."""
        pattern = shlex.quote(_own_node(self.node_id))
        try:
            self.site.on_node(f"pkill -u $USER -f {pattern}; sleep 1; "
                              f"pkill -9 -u $USER -f {pattern}; true", timeout=120)
        except Exception:  # noqa: BLE001
            pass


class RemoteShell:
    """One long-lived remote bash, fed commands on stdin; each command's
    output ends at a marker line carrying its exit status."""

    def __init__(self, argv: list):
        import queue
        import threading

        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     start_new_session=True)
        self.lines: "queue.Queue[str | None]" = queue.Queue()
        self.lock = threading.Lock()

        def pump():
            for line in self.proc.stdout:
                self.lines.put(line)
            self.lines.put(None)
        threading.Thread(target=pump, daemon=True).start()
        self.run("echo ready", timeout=180)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def run(self, command: str, *, timeout: float = 300, check: bool = False) -> str:
        import queue
        import uuid

        marker = f"__plx_done_{uuid.uuid4().hex}__"
        with self.lock:
            self.proc.stdin.write(f"{{ {command}\n}} 2>&1; echo {marker} $?\n")
            self.proc.stdin.flush()
            out, deadline = [], time.time() + timeout
            while True:
                try:
                    line = self.lines.get(timeout=max(0.1, deadline - time.time()))
                except queue.Empty:
                    raise TimeoutError(f"remote command timed out: {command[:80]}")
                if line is None:
                    raise RuntimeError("the remote shell ended")
                if line.startswith(marker):
                    status = int(line.split()[1])
                    break
                out.append(line)
        text = _strip_banner("".join(out))
        if check and status != 0:
            raise RuntimeError(f"{command[:80]!r} failed ({status}): {text[-400:]}")
        return text

    def close(self) -> None:
        try:
            self.proc.stdin.write("exit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass


def _own_node(node_id: str) -> str:
    """A pgrep pattern for this benchmark's node and nothing else -- never
    the user's own sessions, which may share the compute node. The bracket
    keeps the pattern from matching the shell that runs pgrep itself."""
    return f"[n]ode serve .*--node-id {node_id}( |$)"


def _strip_banner(text: str) -> str:
    """The login node's banner lines are noise in every command's output."""
    noise = ("Problems logging in?", "Use your lower case HMS ID", "If locked out, see:",
             "https://it.hms.harvard.edu/")
    return "\n".join(line for line in text.splitlines()
                     if not any(line.startswith(n) for n in noise))


def calibrate_remote(site: Site, reps: int = 30) -> dict:
    """The tile encoder and one chunk read+decode on the compute node, timed
    the same way as `probes.calibrate` times them on the laptop."""
    script = Path(__file__).with_name("calibrate.py").read_text(encoding="utf-8")
    command = (f"cd {shlex.quote(site.data_root)} && {site.python_env}/bin/python - "
               f"{reps} {shlex.quote(site.file)} <<'PYEOF'\n{script}\nPYEOF")
    out = site.on_node(command, timeout=900)
    return json.loads(out.strip().splitlines()[-1])
