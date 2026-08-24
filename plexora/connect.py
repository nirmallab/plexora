"""`plexora connect user@host` -- run Plexora over there, use it from here.

`plexora --remote` prints the ssh command a user must then paste into a second
terminal on their own machine, and it has to: a server cannot reach across the
network and create a tunnel on somebody's laptop. This module is the other end
of that -- it runs LOCALLY, so it can do both halves.

The whole thing shells out to the system `ssh`. No paramiko, no key handling,
no host-key policy of our own: whatever `~/.ssh/config`, an agent, a
ProxyJump, a hardware token or a site's kerberos setup already do for
`ssh user@host` is exactly what happens here, and a connection that works in a
terminal works here without being described twice.

Two shapes, because clusters have two:

- Direct -- the machine you ssh to is the machine that runs Plexora. One ssh
  process carrying both the command and the -L forward.
- Job (`--srun`) -- the machine you ssh to is a login node that is not allowed
  to run anything heavy, and the server belongs on a compute node the
  scheduler picks. Two ssh processes: one holding the job, one tunnelling to
  wherever the job landed. Which node that is cannot be known in advance, so
  the remote `plexora --remote` announces it on stdout and this module reads
  it back.

Standalone-loadable on purpose: stdlib only, nothing from `plexora` at module
level, same rule cli.py follows. tests/test_connect.py loads it straight off
disk, and there is nothing in here that needs the app.
"""

from __future__ import annotations

import atexit
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser


DEFAULT_REMOTE_COMMAND = "plexora"

#: Ports to ask for on the remote side. The ephemeral range: high enough to be
#: out of the way of real services, and we cannot probe the remote host for a
#: free one without a round trip that would be wrong by the time it returned.
#: A collision is handled by retrying rather than prevented.
REMOTE_PORT_RANGE = (49152, 65000)

DEFAULT_TIMEOUT = 60
#: Generous, because a queued job is not a failure. A user who asked for a GPU
#: partition at 09:00 on a Monday may genuinely wait this long, and giving up
#: on them would cancel the allocation they were waiting for.
DEFAULT_SRUN_TIMEOUT = 900

ANNOUNCE_RE = re.compile(r"\[plexora-remote\]\s+node=(\S+)\s+port=(\d+)")

#: What a shell says when the remote `plexora` is not on a non-interactive
#: PATH -- the single most likely way this fails, and the one with a specific
#: fix worth naming.
MISSING_COMMAND_MARKERS = (
    "command not found",
    "No such file or directory",
    "is not recognized as an internal or external command",
    "not found",
)

# Seams. Rebound by tests on the loaded module object so no real ssh, browser
# or network call happens in CI; production reads them exactly once each.
_popen = subprocess.Popen
_which = shutil.which
_open_browser = webbrowser.open
_urlopen = urllib.request.urlopen
_sleep = time.sleep
_now = time.monotonic


class ConnectError(RuntimeError):
    """Something went wrong that the user has to act on."""


class _Retriable(ConnectError):
    """...but trying again on a different port is worth one shot first."""


# -- pure command construction -------------------------------------------


def split_target(target):
    """`("user", "host")` for `user@host`; `(None, "host")` without a user.

    The user half matters beyond politeness: in `--srun` mode the second hop
    is built here rather than by ssh's own config lookup, and on Windows the
    local account name ("Ajit Nirmal", with a space) is almost never the
    cluster account name.
    """
    if "@" in target:
        user, _, host = target.partition("@")
        return (user or None), host
    return None, target


def remote_command_line(remote_command, port, *, bind_node=False, datasource=None,
                        data_dir=None, plugins=None):
    """The command string to hand the remote shell.

    `remote_command` is spliced in RAW, not shlex-split and rejoined: it is the
    escape hatch for environments where reaching Plexora is itself a shell
    expression -- `conda run -n imaging plexora`, `module load python && ...`,
    a bare absolute path with spaces already quoted by whoever typed it. The
    flags this function adds are quoted, because those come from argv and may
    contain a project name with spaces in it.
    """
    parts = ["--remote", "--no-browser", "--port", str(port)]
    if bind_node:
        parts.append("--bind-node")
    if data_dir:
        parts += ["--data-dir", data_dir]
    if plugins is not None:
        parts += ["--plugins", plugins]
    if datasource:
        parts.append(datasource)
    return " ".join([remote_command] + [shlex.quote(part) for part in parts])


def srun_command_line(srun_args, launch_line):
    """Wrap a remote launch in `srun`, so the server lands on a compute node."""
    return " ".join(["srun", (srun_args or "").strip(), launch_line]).strip()


def _ssh_options(ssh_opts):
    out = []
    for opt in ssh_opts or ():
        out += ["-o", opt]
    return out


def direct_ssh_argv(target, local_port, remote_port, launch_line, *, jump=None,
                    ssh_opts=()):
    """One process: the forward and the command that fills it.

    `-t` asks for a pty so that when this ssh dies -- the user hits Ctrl+C, the
    laptop's lid closes, the network drops -- the remote Plexora gets a SIGHUP
    rather than being left running and holding a port forever.
    """
    argv = ["ssh", "-t", *_ssh_options(ssh_opts)]
    if jump:
        argv += ["-J", jump]
    argv += ["-L", f"{local_port}:127.0.0.1:{remote_port}", target, launch_line]
    return argv


def job_ssh_argv(target, launch_line, *, jump=None, ssh_opts=()):
    """The process that holds the SLURM job open. No forward -- see below."""
    argv = ["ssh", "-t", *_ssh_options(ssh_opts)]
    if jump:
        argv += ["-J", jump]
    argv += [target, launch_line]
    return argv


def tunnel_ssh_argv(target, local_port, node, node_port, *, user=None,
                    bind_node=False, ssh_opts=()):
    """The process that carries traffic to wherever the job landed.

    It cannot be folded into `job_ssh_argv`: an `-L` forward is set up when the
    connection opens, and at that moment the job has not been allocated, so
    there is no node to name yet.

    Two forms. By default the forward is made by a second ssh that goes
    *through* the login node into the compute node (`-J`), leaving Plexora
    bound to that node's loopback where nothing else on the cluster can reach
    it. Sites that refuse ssh into compute nodes get `--bind-node` instead:
    Plexora listens on all interfaces and the login node forwards to it over
    the internal network -- fewer moving parts, in exchange for the port being
    visible to everyone else on the cluster while it runs.
    """
    argv = ["ssh", "-N", *_ssh_options(ssh_opts)]
    if bind_node:
        argv += ["-L", f"{local_port}:{node}:{node_port}", target]
        return argv
    node_target = f"{user}@{node}" if user else node
    argv += ["-J", target, node_target, "-L", f"{local_port}:127.0.0.1:{node_port}"]
    return argv


def parse_announce(line):
    """`(node, port)` from the remote's announce line, or None."""
    match = ANNOUNCE_RE.search(line)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def looks_like_missing_command(lines):
    return any(marker in line for line in lines for marker in MISSING_COMMAND_MARKERS)


# -- ports ----------------------------------------------------------------


def _local_port_is_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
        return True


def _free_local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pick_ports(local_port=None, remote_port=None, *, randint=None, is_free=None,
               free_port=None):
    """`(local, remote)`, matching numbers when that is possible.

    Same number on both ends is worth a little effort: every message the user
    sees mentions a port, and having one number instead of two removes the
    question of which end a given one refers to.
    """
    import random

    randint = random.randint if randint is None else randint
    is_free = _local_port_is_free if is_free is None else is_free
    free_port = _free_local_port if free_port is None else free_port

    remote = remote_port or randint(*REMOTE_PORT_RANGE)
    if local_port:
        return local_port, remote
    if is_free(remote):
        return remote, remote
    return free_port(), remote


# -- running processes ----------------------------------------------------


class _Watched:
    """A spawned ssh, echoed line by line, watched for the announce line.

    The output is drained on a thread whatever else happens. Not for tidiness:
    ssh writes the remote's stdout into a pipe with a finite buffer, and a
    Plexora that logs a few kilobytes -- which it does on startup -- would
    block forever on a write nobody was reading, which presents as the job
    hanging at exactly the moment it was about to work.
    """

    def __init__(self, argv, label, *, echo=print):
        self.argv = argv
        self.label = label
        self.lines = []
        self.announce = None
        self.saw_announce = threading.Event()
        self.process = _popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._echo = echo
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        stream = self.process.stdout
        if stream is not None:
            for raw in stream:
                # -t gives us a pty, and a pty gives us \r\n.
                line = raw.rstrip("\r\n")
                self.lines.append(line)
                found = parse_announce(line)
                if found and self.announce is None:
                    self.announce = found
                    self.saw_announce.set()
                self._echo(f"  [{self.label}] {line}")
        # Wake anyone waiting for an announce that is never coming.
        self.saw_announce.set()

    @property
    def alive(self):
        return self.process.poll() is None

    def drain(self, timeout=2):
        """Let the reader thread catch up before anyone reads `lines`.

        Whoever noticed the process had exited got there by polling, and the
        thread is still working through what ssh wrote on its way out. Without
        this pause the diagnosis runs against a buffer that does not yet
        contain "plexora: command not found" -- the one line worth reading.
        """
        self._thread.join(timeout=timeout)

    def tail(self, count=6):
        return self.lines[-count:]

    def stop(self):
        if self.process.poll() is not None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass


_ACTIVE = []


def _shut_down_active():
    # Reversed so the tunnel goes before the job that it points at: tearing
    # down the job first would leave the tunnel briefly pointing at a node that
    # no longer has anything listening, which is a worse thing to log.
    for watched in reversed(_ACTIVE):
        watched.stop()
    _ACTIVE.clear()


atexit.register(_shut_down_active)


def _wait_for_health(url, deadline, watchers):
    """Poll through the tunnel until Plexora answers, or something dies."""
    while _now() < deadline:
        for watched in watchers:
            if not watched.alive:
                raise _Retriable(
                    f"the {watched.label} ssh connection exited with code "
                    f"{watched.process.returncode} before Plexora answered"
                )
        try:
            with _urlopen(url, timeout=2) as response:
                if response.status < 500:
                    return True
        except Exception:
            _sleep(0.5)
    return False


def _wait_for_announce(watched, deadline, *, echo=print):
    """Block until the remote says where it is, or gives up trying."""
    announced = False
    while _now() < deadline:
        if watched.saw_announce.wait(timeout=5):
            break
        if not watched.alive:
            break
        if not announced:
            echo("  waiting for the scheduler to allocate a node...")
            announced = True
    # Only when we are about to give up: the line may still have been in the
    # reader's buffer. On the success path the process is alive and streaming,
    # so joining its thread would just cost a second for nothing.
    if watched.announce is None:
        watched.drain(timeout=1)
    if watched.announce:
        return watched.announce
    if not watched.alive:
        raise _Retriable(
            f"the job ssh connection exited with code "
            f"{watched.process.returncode} before Plexora started"
        )
    raise ConnectError(
        "Timed out waiting for the remote Plexora to report which node it is "
        "on.\nRaise --timeout if the queue is long, or fall back to running "
        "`plexora --remote` inside your own srun session."
    )


# -- orchestration --------------------------------------------------------


def _no_ssh_message():
    if sys.platform == "win32":
        return (
            "No `ssh` command found.\n"
            "Windows ships OpenSSH as an optional feature: enable it under "
            "Settings > System > Optional features > Add a feature > "
            "'OpenSSH Client', or install Git for Windows, then reopen this "
            "terminal."
        )
    return (
        "No `ssh` command found. Install an OpenSSH client and try again."
    )


def _missing_command_hint(remote_command, watched):
    return ConnectError(
        f"The remote host could not run {remote_command!r}:\n"
        + "\n".join(f"    {line}" for line in watched.tail())
        + "\n\nA non-interactive ssh session often has a shorter PATH than a "
          "login shell. Name the command explicitly, e.g.\n"
          "    --remote-command \"conda run -n myenv plexora\"\n"
          "    --remote-command /home/you/miniconda3/envs/myenv/bin/plexora\n"
          "Or run `plexora --remote` on the host yourself and use the tunnel "
          "command it prints."
    )


def _attempt(target, *, datasource, remote_command, srun, bind_node, jump,
             ssh_opts, local_port, remote_port, timeout, data_dir, plugins,
             browser, echo):
    user, _host = split_target(target)
    local, remote = pick_ports(local_port, remote_port)
    launch = remote_command_line(
        remote_command, remote,
        bind_node=bind_node, datasource=datasource,
        data_dir=data_dir, plugins=plugins,
    )
    deadline = _now() + timeout
    watchers = []

    try:
        if srun is None:
            argv = direct_ssh_argv(target, local, remote, launch,
                                   jump=jump, ssh_opts=ssh_opts)
            echo(f"$ {' '.join(argv)}")
            primary = _Watched(argv, "ssh", echo=echo)
            watchers.append(primary)
            _ACTIVE.append(primary)
        else:
            argv = job_ssh_argv(target, srun_command_line(srun, launch),
                                jump=jump, ssh_opts=ssh_opts)
            echo(f"$ {' '.join(argv)}")
            primary = _Watched(argv, "job", echo=echo)
            watchers.append(primary)
            _ACTIVE.append(primary)

            node, node_port = _wait_for_announce(primary, deadline, echo=echo)
            echo(f"  Plexora is on {node}:{node_port}; opening the tunnel.")
            tunnel_argv = tunnel_ssh_argv(
                target, local, node, node_port,
                user=user, bind_node=bind_node, ssh_opts=ssh_opts,
            )
            echo(f"$ {' '.join(tunnel_argv)}")
            tunnel = _Watched(tunnel_argv, "tunnel", echo=echo)
            watchers.append(tunnel)
            _ACTIVE.append(tunnel)

        url = f"http://127.0.0.1:{local}/"
        if not _wait_for_health(url, deadline, watchers):
            raise ConnectError(
                f"Plexora did not answer on {url} within {timeout:g}s.\n"
                "Raise --timeout, or check that the remote host can run "
                "`plexora --remote` on its own."
            )

        open_url = url if not datasource else url + datasource
        echo("")
        echo(f"Plexora is available at {open_url}")
        echo("Leave this command running; press Ctrl+C to disconnect"
             + (" and end the job." if srun is not None else "."))
        if browser:
            _open_browser(open_url)

        primary.process.wait()
        return 0
    except _Retriable as exc:
        if not watchers:
            raise
        watchers[0].drain()
        if looks_like_missing_command(watchers[0].lines):
            raise _missing_command_hint(remote_command, watchers[0]) from exc
        raise
    finally:
        for watched in reversed(watchers):
            watched.stop()
            if watched in _ACTIVE:
                _ACTIVE.remove(watched)


def connect(target, datasource=None, *, remote_command=DEFAULT_REMOTE_COMMAND,
            srun=None, bind_node=False, jump=None, ssh_opts=(),
            local_port=None, remote_port=None, timeout=None, data_dir=None,
            plugins=None, browser=True, attempts=3, echo=print):
    """Run Plexora on `target`, tunnel to it, open it here. Returns an exit code.

    Best-effort by design. Every failure below ends with the printed
    instructions from `plexora --remote` as the fallback, because that path has
    one moving part (the user's own ssh) where this one has several.
    """
    if _which("ssh") is None:
        raise SystemExit(_no_ssh_message())

    if timeout is None:
        timeout = DEFAULT_SRUN_TIMEOUT if srun is not None else DEFAULT_TIMEOUT

    # A pinned port is an instruction, not a preference: retrying would just
    # try the same thing again, and the user asked for that number for a reason.
    if remote_port is not None:
        attempts = 1

    last = None
    for attempt in range(1, attempts + 1):
        try:
            return _attempt(
                target,
                datasource=datasource, remote_command=remote_command, srun=srun,
                bind_node=bind_node, jump=jump, ssh_opts=ssh_opts,
                local_port=local_port, remote_port=remote_port, timeout=timeout,
                data_dir=data_dir, plugins=plugins, browser=browser, echo=echo,
            )
        except KeyboardInterrupt:
            echo("\nDisconnecting.")
            return 0
        except _Retriable as exc:
            last = exc
            if attempt < attempts:
                echo(f"  {exc}; retrying on a different port "
                     f"({attempt + 1}/{attempts}).")
        except ConnectError as exc:
            raise SystemExit(str(exc)) from exc

    raise SystemExit(
        f"Could not start Plexora on {target} after {attempts} attempts: {last}\n"
        "Fall back to running `plexora --remote` on the host and pasting the "
        "ssh command it prints."
    )
