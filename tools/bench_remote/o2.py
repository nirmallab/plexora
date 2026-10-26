"""The far side of the remote-access benchmark: a data node in a SLURM job.

Everything here runs over the system ssh, as Plexora's own connections do.
The harness's control commands (srun steps, fadvise, /proc reads) share one
multiplexed ssh master so each costs a round trip rather than a login; the
measured connections -- the node's own launch, its tunnel, the files-only
server -- never use that master, so their setup is timed as a user would
see it.

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

    def ssh(self, command: str, *, timeout: float = 120, check: bool = True) -> str:
        """Run `command` on the login node over the shared control master."""
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self.control_path}", "-o", "ControlPersist=30m",
                self.login, command]
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        if check and done.returncode != 0:
            raise RuntimeError(f"ssh {command[:80]!r} failed: {done.stderr.strip()[-400:]}")
        return _strip_banner(done.stdout)

    def on_node(self, command: str, *, timeout: float = 300) -> str:
        """Run `command` on the job's compute node (an overlapping step)."""
        step = (f"srun --jobid={self.job} --overlap -N1 -n1 -c1 bash -c "
                f"{shlex.quote(command)}")
        return self.ssh(step, timeout=timeout)

    def locate(self) -> dict:
        """The job's state and node; waits while it is pending."""
        while True:
            out = self.ssh(f"squeue -h -j {self.job} -o '%T %N %M %l'", check=False).split()
            if not out:
                raise RuntimeError(f"job {self.job} is not in the queue")
            if out[0] == "RUNNING":
                self.node = out[1]
                return {"state": out[0], "node": out[1], "elapsed": out[2], "limit": out[3]}
            time.sleep(15)

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
    launcher: subprocess.Popen | None = None
    tunnel: subprocess.Popen | None = None
    timings: dict = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.local_port}"

    def start(self, timeout: float = 600) -> dict:
        """Launch the node in the job and wait for its announce line; then the
        tunnel, and wait for /health through it."""
        s = self.site
        env = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in {
            "PLEXORA_DATA_PATH": s.data_root, "PLEXORA_TELEMETRY": "off", **self.env}.items())
        serve = (f"{s.python_env}/bin/plexora node serve --host 127.0.0.1 "
                 f"--port {self.node_port} --token {self.token} --node-id {self.node_id} "
                 f"--dynamic --manifest {s.data_root}/manifest.json "
                 f"--allow-origin {self.allow_origin}")
        remote = (f"srun --jobid={s.job} --overlap -N1 -n1 -c{s.cpus} "
                  f"env {env} {serve}")
        log = open(self.log_dir / f"node-{int(time.time())}.log", "w", encoding="utf-8")
        t0 = time.time()
        # -tt: ending this ssh hangs the remote step up, as a closed terminal would.
        self.launcher = subprocess.Popen(
            ["ssh", "-tt", "-o", "BatchMode=yes", s.login, remote],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.PIPE,
            text=True, bufsize=1, start_new_session=True)
        announced = None
        deadline = t0 + timeout
        while time.time() < deadline:
            line = self.launcher.stdout.readline()
            if not line:
                if self.launcher.poll() is not None:
                    raise RuntimeError("the node exited before announcing itself")
                continue
            match = ANNOUNCE.search(line)
            log.write(ANNOUNCE.sub("[plexora-node] (announce)", line))
            log.flush()
            if match:
                announced = time.time()
                break
        if announced is None:
            raise RuntimeError("the node did not announce itself in time")
        # Keep draining its output so the pipe never blocks it.
        import threading

        def drain():
            for line in self.launcher.stdout:
                log.write(line)
            log.close()
        threading.Thread(target=drain, daemon=True).start()
        self.tunnel = subprocess.Popen(
            ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=30", "-J", s.login,
             f"{s.login.split('@')[0]}@{s.node}",
             "-L", f"{self.local_port}:127.0.0.1:{self.node_port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        if not s.node:
            raise RuntimeError("the job's compute node is not known (Site.locate)")
        healthy = self.wait_health(deadline)
        self.timings = {"t_node_announce": announced - t0, "t_tunnel_health": healthy - announced,
                        "t_node_up": healthy - t0}
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
        `ssh -W` ProxyJump child (the outer ssh only talks to it locally)."""
        if not self.tunnel:
            return None
        out = subprocess.run(["pgrep", "-P", str(self.tunnel.pid)], capture_output=True,
                             text=True).stdout.split()
        return int(out[0]) if out else self.tunnel.pid

    def stop(self) -> None:
        for proc in (self.tunnel, self.launcher):
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        for proc in (self.tunnel, self.launcher):
            if proc:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        # Belt and braces: no node of ours may outlive its block.
        try:
            pattern = shlex.quote(_own_node(self.node_id))
            self.site.on_node(f"pkill -u $USER -f {pattern}; sleep 1; pgrep -u $USER -f {pattern} || true",
                              timeout=120)
        except Exception:
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
