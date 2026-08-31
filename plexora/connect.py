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
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
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

#: How often the health wait says it is still waiting. Long enough not to bury
#: the log, short enough that the first note arrives while somebody is still
#: watching rather than after they have concluded it is hung.
HEALTH_NOTE_SECONDS = 15
#: Where the failing health poll's backoff stops. High enough that pending ssh
#: channels cannot stack to ssh's cap within a TCP connect timeout, low enough
#: that a slow remote start is still noticed within seconds of finishing.
HEALTH_POLL_MAX_DELAY = 6.0
#: How long to wait for a node on THIS machine that has nothing to prepare.
#: See `_register_local_node` -- the session's own deadline is the right budget
#: for a node converting a mask, and much too long for one that failed to start.
EMPTY_NODE_TIMEOUT = 20.0

#: How long to give a data node on another machine before calling it dead.
#: Much longer than DEFAULT_TIMEOUT, and the reason is the filesystem rather
#: than the network: the first `import plexora` on a cluster pulls numpy,
#: polars, zarr, tifffile and anndata off a shared mount whose cache is cold,
#: and on a busy GPFS or NFS home directory that alone runs past a minute. The
#: viewer's 60s budget is measured against a machine that has usually run
#: Plexora before; a node started on demand, in the middle of a form, usually
#: has not.
NODE_START_TIMEOUT = 240

#: How long to give `pip install` on the far side. Its own budget rather than a
#: slice of the connection's, because the two are unrelated waits: a fresh
#: install pulls numpy, zarr, tifffile and anndata onto a cold shared home
#: directory, and one that has to build a wheel there runs into minutes. Timing
#: it out against DEFAULT_TIMEOUT would abandon a working install at the moment
#: it was about to finish, and leave the environment half-upgraded.
INSTALL_TIMEOUT = 900

ANNOUNCE_RE = re.compile(r"\[plexora-remote\]\s+node=(\S+)\s+port=(\d+)")

#: The line `plexora node serve` prints before it binds. Carries the token,
#: which is why it is only ever read off a pipe inside the ssh channel -- see
#: server/node/app.py, where it is emitted, for why that beats the alternative
#: of passing the token in the remote command line.
NODE_ANNOUNCE_RE = re.compile(
    r"\[plexora-node\]\s+host=(?P<host>\S+)\s+port=(?P<port>\d+)"
    r"\s+node_id=(?P<node_id>\S+)\s+token=(?P<token>\S+)"
)

#: Which machine the node is actually ON, read separately so that a node from
#: an older Plexora -- which does not send it -- still parses. Only the
#: scheduler path needs it, and that path says so plainly when it is missing
#: rather than tunnelling to a host it guessed.
NODE_HOSTNAME_RE = re.compile(r"\bhostname=(?P<hostname>\S+)")

#: What a shell says when the remote `plexora` is not on a non-interactive
#: PATH -- the single most likely way this fails, and the one with a specific
#: fix worth naming.
MISSING_COMMAND_MARKERS = (
    "command not found",
    "No such file or directory",
    "is not recognized as an internal or external command",
    "not found",
)

#: What argparse says on the far side when the Plexora installed there is older
#: than the one that built the command line. Worth its own message because the
#: raw output is a usage dump -- which reads as "you typed something wrong",
#: when nobody typed anything and the fix is on the other machine entirely.
OLD_REMOTE_RE = re.compile(r"unrecognized arguments:\s*(--[\w-]+)")

#: srun complaining, as opposed to srun narrating. `srun: job 91 queued and
#: waiting for resources` is the normal case and must not match; only the two
#: prefixes it uses when it has given up do.
SRUN_ERROR_RE = re.compile(r"\bsrun:\s*(?:error|fatal)\b", re.IGNORECASE)

#: What the scheduler's refusals mean for the job arguments. A scheduler that
#: will not start a job says exactly why, and then the login node's banner
#: pushes it out of the tail: on HMS O2 what reached the user was a paragraph
#: about lower-case usernames and a password-reset URL, with `please specify
#: partition with -p` buried in the middle of it. Matched against srun's own
#: lines only -- see `scheduler_lines` -- so no site's motd can trip one.
#:
#: Order is load-bearing at the top: a site with no default partition refuses
#: twice, once for the missing `-p` and then again because the empty name it
#: submitted is not a partition either. The first is the one with the fix.
SCHEDULER_REFUSALS = (
    ("please specify partition",
     "This site has no default partition, so every job has to name one. Put "
     "`-p <partition>` in the job's scheduler arguments -- the “Additional "
     "scheduler arguments” box on the saved server, or `--srun` from the "
     "command line. `sinfo -s` on the login node lists what your site offers."),
    ("invalid partition name",
     "That partition does not exist here, or this account may not use it. "
     "`sinfo -s` on the login node lists the ones that do; correct `-p` in "
     "the job's scheduler arguments."),
    ("requested node configuration is not available",
     "No machine in that partition can give the job what it asked for. Lower "
     "the cores or the memory, or name a partition with bigger nodes."),
    ("requested partition configuration not available",
     "That partition cannot serve this job as asked -- usually the walltime, "
     "the cores or the memory are past what it allows. Ask for less, or name "
     "a partition that allows more."),
    ("invalid account or account/partition",
     "The scheduler did not accept that account for this partition. "
     "`sacctmgr show associations user=$USER` lists the pairs you may use; "
     "`-A <account>` names one."),
    ("job violates accounting/qos policy",
     "The scheduler's own limits refused the job -- most often the walltime "
     "for that partition, sometimes how many jobs you already have. Ask for "
     "less time, or name a partition that allows more."),
    ("requested time limit is invalid",
     "That partition will not allow a job this long. Lower the walltime, or "
     "name a partition that allows more."),
)

#: When srun refused for a reason not in the table above. Still worth saying
#: over a raw tail, because it puts the scheduler's own words at the top and
#: names the one setting that can answer them.
SCHEDULER_GENERIC = (
    "The scheduler refused the job rather than queueing it, so this is about "
    "what the job asked for and not about the connection. Whatever it names "
    "above belongs in the job's scheduler arguments -- the “Additional "
    "scheduler arguments” box on the saved server, or `--srun` from the "
    "command line."
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
    """Something went wrong that the user has to act on.

    `diagnosed` marks the ones whose message was built by a step that knew
    exactly what had failed and had the remote output in front of it. Callers
    that otherwise guess at a cause by looking for substrings in the ssh
    output -- `RemoteSession._diagnose` is the one that matters -- must not
    second-guess those.

    It exists because guessing from substrings is unreliable in a way that is
    invisible until it is not: the mount step began printing apt's own log on
    failure, that log contained `gpg: not found`, and a connection that had
    failed to install Cloud Storage FUSE started telling people their PATH was
    wrong and to go and edit a setting that was correct.
    """

    def __init__(self, *args, diagnosed=False):
        super().__init__(*args)
        self.diagnosed = bool(diagnosed)


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


#: What is inside a conda/venv prefix. Appended when the user names the
#: environment rather than the program in it, which is the answer they
#: actually have: `conda env list` prints prefixes, and the path they typed
#: when they created it was a prefix too.
ENV_PREFIX_BIN = "bin/plexora"


def _is_env_prefix(path):
    """True if `path` names an environment rather than the executable in it.

    Syntactic, because the remote filesystem is a round trip away and the
    answer is needed to build the command that would make the round trip. Two
    things disqualify a path: already ending in `bin/plexora`, and a dot in
    the last component, which is how a wrapper script (`run-plexora.sh`) says
    it is a program. An environment directory has neither.
    """
    tail = path.rstrip("/")
    if tail.endswith("/" + ENV_PREFIX_BIN):
        return False
    return "." not in tail.rsplit("/", 1)[-1]


def normalize_remote_command(remote_command):
    """What the user typed, resolved to something a remote shell can run.

    Three shapes arrive in "Plexora command or environment":

    * **A shell expression** -- `conda run -n imaging plexora`,
      `module load python && plexora`. Whitespace is the tell, and these are
      returned untouched. Only a shell can run them, so nothing may be put in
      front of them, and whoever wrote one has already said exactly what they
      meant.
    * **A POSIX path** -- either an environment prefix, which gets
      `bin/plexora` appended, or the executable itself, which does not. Both
      then get `env PYTHONUNBUFFERED=1` in front; see below.
    * **Anything else** -- a bare name on PATH, a Windows path. Returned as
      typed, because `env` is not a program on Windows and a bare name gives
      no evidence either way.

    The unbuffering is a compatibility shim, not the fix. `plexora --remote`
    flushes its announce line itself (cli.py), but the two sides of a
    connection are separately installed and drift: a laptop upgraded today
    still has to talk to the cluster install from last year, where a block
    -buffered announce never leaves the compute node and the connection
    times out having done nothing visible. `env` is used rather than a shell
    assignment because in `--srun` mode there is no shell -- srun execs the
    command itself, and `env` is a program it can exec.
    """
    command = (remote_command or "").strip()
    if not command:
        return DEFAULT_REMOTE_COMMAND
    if len(command.split()) > 1:
        return command
    if not command.startswith(("/", "~")):
        return command
    if _is_env_prefix(command):
        command = command.rstrip("/") + "/" + ENV_PREFIX_BIN
    return "env PYTHONUNBUFFERED=1 " + command


#: What is being installed, and how. `--upgrade` because the option is worded
#: as "install or update": somebody who turns it on for a machine that already
#: has Plexora is asking for the current one, and a plain install would report
#: "already satisfied" and leave last year's copy in place -- which is the
#: exact failure the option exists to end.
#: `--progress-bar off` because the install rides the launch's ssh, which asks
#: for a pty (`-t`) -- and pip with a pty redraws its download bar with
#: carriage returns, which a line-based log renders as one line growing to
#: kilobytes. The per-package Collecting/Installing lines still come through,
#: and they are the progress worth showing.
PIP_ARGUMENTS = ("install", "--progress-bar", "off", "--upgrade", "plexora")

#: What conda does to a subprocess's output unless told not to: buffers it and
#: prints the lot when the child exits. Correct for a script whose output is a
#: result; wrong for a six-minute pip, which then says nothing at all until it
#: has finished. The whole point of putting the install in the connection flow
#: is that somebody can watch it, so this is spliced in when it is missing.
CONDA_STREAMING_FLAG = "--no-capture-output"


def _pip_beside(program):
    """`pip` wherever this `plexora` is, or the one on PATH.

    A path answers the question by itself -- `/envs/imaging/bin/plexora` has
    its pip one name along, and using it is what "inside that environment"
    means without a shell to activate anything in. Two shapes do not: a bare
    name, where PATH is the only answer there is, and a wrapper script, whose
    directory says nothing about where any Python lives (`_is_env_prefix`
    already treats a dot in the last component as the mark of one).
    """
    head, sep, tail = program.rpartition("/")
    if not sep or "." in tail:
        return "pip"
    return head + sep + "pip"


def install_command_line(remote_command):
    """How to install or update Plexora where `remote_command` would run it.

    One rule, applied to every shape the launch command comes in: **the
    environment is whatever gets you to the program, and the program is the
    last word.** Swap that word for the pip beside it and the same expression
    installs into the same place it would have launched from.

        plexora                          -> pip install --upgrade plexora
        /home/you/envs/imaging           -> /home/you/envs/imaging/bin/pip …
        /home/you/envs/imaging/bin/plexora -> /home/you/envs/imaging/bin/pip …
        conda run -n imaging plexora     -> conda run -n imaging pip …
        module load python && plexora    -> module load python && pip …

    Which is why there is no separate "conda environment" box anywhere: the
    field that says how to reach Plexora over there has always been the field
    that names the environment, and a second one would be two answers to one
    question -- with the launch and the install free to disagree about which
    environment they meant. `conda run -n imaging pip install` IS activating
    that environment and installing inside it, and it is the form that works
    over ssh, where `conda activate` needs a shell whose rc file has been
    sourced and a non-interactive login has not sourced one.
    """
    command = (remote_command or "").strip() or DEFAULT_REMOTE_COMMAND
    parts = command.split()
    if len(parts) > 1:
        head, program = parts[:-1], parts[-1]
        if head[:2] == ["conda", "run"] and CONDA_STREAMING_FLAG not in head:
            head = head[:2] + [CONDA_STREAMING_FLAG] + head[2:]
        return " ".join(head + [_pip_beside(program), *PIP_ARGUMENTS])
    if command.startswith(("/", "~")) and _is_env_prefix(command):
        command = command.rstrip("/") + "/" + ENV_PREFIX_BIN
    return " ".join([_pip_beside(command), *PIP_ARGUMENTS])


def environment_label(remote_command):
    """What to call the environment this command runs Plexora in, or None.

    For a step in a progress list to read "Updating Plexora in imaging" rather
    than just "Updating Plexora" -- which is the difference between somebody
    being able to check, before it runs, that it is about to touch the
    environment they meant. A display name and nothing more: the basename of a
    prefix, because that is what people call an environment, and two prefixes
    ending in the same name is a label collision rather than a wrong install.
    """
    command = (remote_command or "").strip()
    if not command or command == DEFAULT_REMOTE_COMMAND:
        return None
    parts = command.split()
    for flag in ("-n", "--name", "-p", "--prefix"):
        if flag in parts and parts.index(flag) + 1 < len(parts):
            return parts[parts.index(flag) + 1].rstrip("/").rsplit("/", 1)[-1]
    if len(parts) > 1:
        # A shell expression with no environment flag in it -- `module load
        # python && plexora`. It certainly does something to the environment;
        # nothing here can put a name to it, and inventing one would be worse
        # than the honest silence of a step that says only "Updating Plexora".
        return None
    if not command.startswith(("/", "~")):
        return None
    prefix = command.rstrip("/")
    if not _is_env_prefix(prefix):
        head, sep, _ = prefix.rpartition("/" + ENV_PREFIX_BIN)
        if not sep:
            return None
        prefix = head
    return prefix.rsplit("/", 1)[-1] or None


#: What pip says it did, in the two shapes it says it in. A fresh install or an
#: upgrade ends with "Successfully installed plexora-1.2.3"; a machine that was
#: already current says "Requirement already satisfied: plexora in /path
#: (1.2.3)" and never prints the first line at all. Reading only the first
#: would report no version for the commonest outcome of a connection somebody
#: reconnects every morning.
_INSTALLED_RE = re.compile(r"\bplexora-(\d[^\s]*)")
_SATISFIED_RE = re.compile(
    r"Requirement already satisfied:\s*plexora\b[^(]*\(([^)]+)\)",
    re.IGNORECASE)


def parse_installed_version(lines):
    """Which Plexora the far side ended up with, from pip's own output.

    None when pip did not say -- an unusual outcome, a `--quiet` in somebody's
    pip.conf, a wheel built from a local path. The caller reports what it
    knows and stays quiet about the rest: a version invented here would be
    read as the answer to "did the upgrade actually land", which is the one
    question this is for.
    """
    for line in reversed(list(lines or ())):
        found = _INSTALLED_RE.search(line) if "Successfully installed" in line \
            else _SATISFIED_RE.search(line)
        if found:
            return found.group(1).strip()
    return None


#: Printed by the remote shell between the install and the launch. The two
#: run in ONE ssh -- `pip … && echo … && srun …` -- so the only way to know
#: pip finished (rather than printed its last routine line) is to say so
#: ourselves. `&&` is also the failure story: a pip that exits non-zero
#: short-circuits the chain, the marker never appears, the ssh ends, and
#: nothing was launched from the half-upgraded environment.
INSTALL_DONE_MARK = "PLEXORA_INSTALL_DONE"


def parse_install_done(line):
    """Matcher for the marker line. True on the marker, None otherwise."""
    return True if line.strip() == INSTALL_DONE_MARK else None


def install_prefixed(install_line, launch_line):
    """One remote command: install, say so, then launch.

    One command because it is one login. The install used to be its own ssh,
    which was one extra authentication -- and at a site that confirms every
    login on somebody's phone, "connect" buzzed it twice. Chaining also puts
    the install literally immediately before the run, in the same shell, so
    nothing can start from the copy the upgrade was replacing.

    Under a scheduler the chain still runs the install on the LOGIN node --
    it precedes the `srun` in the same command -- which is where it belongs:
    the environment is on a shared filesystem, and the allocation should not
    be spent watching pip download wheels.
    """
    return f"{install_line} && echo {INSTALL_DONE_MARK} && {launch_line}"


#: Printed by the remote shell once the data directory is mounted and readable,
#: for the same reason INSTALL_DONE_MARK exists: the mount, the install and the
#: launch are one ssh, and a marker we print ourselves is the only way to tell
#: "the mount finished" from "gcsfuse printed a routine line".
MOUNT_DONE_MARK = "PLEXORA_MOUNT_DONE"

#: Printed instead of failing when the bucket mounted but cannot be written to.
#: A read-only bucket is an ordinary thing to be given -- somebody else's
#: published atlas -- and images open from one perfectly well; what breaks is
#: saving a figure into it, which is worth a warning and is not worth refusing
#: a connection over. (`plexora.gcloud` emits this string; the two are pinned
#: to each other by a test, because this module may not import that one.)
MOUNT_READONLY_MARK = "PLEXORA_MOUNT_READONLY"

#: How long to give the mount step. Its own budget, like the install's and for
#: the same reason: a first connection installs gcsfuse from apt and builds a
#: Python environment on a machine that booted a minute ago, and timing that
#: out against the connection's 60s would abandon a VM at the moment it was
#: about to work -- having already paid for it.
MOUNT_TIMEOUT = 900


def parse_mount_done(line):
    return True if line.strip() == MOUNT_DONE_MARK else None


def parse_mount_readonly(line):
    return True if line.strip() == MOUNT_READONLY_MARK else None


def mount_prefixed(mount_line, launch_line):
    """One remote command: mount the data, say so, then launch.

    Outside the install, not inside it: when a connection does both, the
    environment Plexora is installed into lives on the machine the mount step
    also prepares, and the pip that upgrades it must run after that machine is
    ready rather than beside it. `&&` again carries the failure story -- a
    mount that fails launches nothing, which is the whole point of checking
    the data is there before starting a viewer that would open onto an empty
    directory.
    """
    return f"{mount_line} && echo {MOUNT_DONE_MARK} && {launch_line}"


def remote_command_line(remote_command, port, *, bind_node=False, datasource=None,
                        data_dir=None, plugins=None, also_serve=(),
                        node_port=None, node_allow_origin=None):
    """The command string to hand the remote shell.

    `remote_command` is spliced in RAW, not shlex-split and rejoined: it is the
    escape hatch for environments where reaching Plexora is itself a shell
    expression -- `conda run -n imaging plexora`, `module load python && ...`,
    a bare absolute path with spaces already quoted by whoever typed it. The
    flags this function adds are quoted, because those come from argv and may
    contain a project name with spaces in it.

    It is passed through `normalize_remote_command` first, which resolves the
    one shape that is not runnable as typed -- an environment prefix -- and
    leaves every other shape alone.
    """
    parts = ["--remote", "--no-browser", "--port", str(port)]
    if bind_node:
        parts.append("--bind-node")
    if data_dir:
        parts += ["--data-dir", data_dir]
    if plugins is not None:
        parts += ["--plugins", plugins]
    # A port on the command line is fine and a token never is: everything here
    # is visible in `ps` to every other account on a shared login node. The
    # node generates its own token and announces it back down the ssh pipe.
    for entry in also_serve or ():
        parts += ["--also-serve", entry]
    if also_serve and node_port:
        parts += ["--node-port", str(int(node_port))]
    if also_serve and node_allow_origin:
        parts += ["--node-allow-origin", node_allow_origin]
    if datasource:
        parts.append(datasource)
    return " ".join([normalize_remote_command(remote_command)]
                    + [shlex.quote(part) for part in parts])


def node_command_line(remote_command, port, serve, *, allow_origin=None,
                      plugins=None, dynamic=False, node_id=None, manifest=None,
                      host="127.0.0.1"):
    """The command string for a host that runs a data node and no viewer.

    The second layout: the viewer stays on the laptop, where the browser and
    the small files are, and only the pixels come over the wire.

    `dynamic` is what makes that layout usable without knowing the paths first.
    Without it every file has to be named here, on this command line, before
    the user has opened a form -- which is the thing the Local/Remote switch
    exists to stop asking of them. With it the node starts empty and the viewer
    hands it a path when somebody picks one. `node_id` and `manifest` are its
    memory: same id, same manifest, same resource ids next session, so a
    project reopened tomorrow finds the same files without being repointed.
    """
    parts = ["node", "serve", "--port", str(int(port)), "--host", str(host)]
    for entry in serve or ():
        parts += ["--serve", entry]
    if dynamic:
        parts.append("--dynamic")
    if node_id:
        parts += ["--node-id", str(node_id)]
    if manifest:
        parts += ["--manifest", str(manifest)]
    if allow_origin:
        parts += ["--allow-origin", allow_origin]
    if plugins is not None:
        parts += ["--plugins", plugins]
    return " ".join([normalize_remote_command(remote_command)]
                    + [shlex.quote(part) for part in parts])


def srun_command_line(srun_args, launch_line):
    """Wrap a remote launch in `srun`, so the server lands on a compute node."""
    return " ".join(["srun", (srun_args or "").strip(), launch_line]).strip()


#: Noticing that a tunnel has died. Without these, a laptop that sleeps, a VPN
#: that drops or a compute node whose job ends leaves ssh sitting on a TCP
#: connection nothing will ever answer: the process stays alive, the forward
#: stays open, and every request through it hangs instead of failing. Three
#: missed thirty-second probes end it, which turns a silent hang into an exit
#: the watcher sees and the page can report.
KEEPALIVE_OPTIONS = ("ServerAliveInterval=30", "ServerAliveCountMax=3")


def _ssh_options(ssh_opts):
    """`-o` flags for one ssh, keepalive first and the caller's able to win.

    A caller who set `ServerAliveInterval` themselves has said something about
    this host -- a site whose firewall dislikes frequent probes, say -- so the
    default drops out rather than being passed twice. (ssh honours the FIRST of
    a repeated option, so appending a second one would silently do nothing;
    dropping ours is the only way the user's value takes effect.)
    """
    given = tuple(ssh_opts or ())
    named = {str(opt).split("=", 1)[0].strip().lower() for opt in given}
    out = []
    for opt in KEEPALIVE_OPTIONS:
        if opt.split("=", 1)[0].lower() not in named:
            out += ["-o", opt]
    for opt in given:
        out += ["-o", opt]
    return out


def parse_forward(argument):
    """`(local, remote)` from a `--forward local:remote` (or bare `port`).

    Exists so a data node running beside the viewer on the remote host is
    reachable too. `plexora node serve` binds a second port over there, and
    without a second `-L` the browser can talk to the viewer and nothing else
    -- which is precisely the arrangement "image on the cluster, table on the
    laptop" produces in reverse.
    """
    text = str(argument).strip()
    if ":" not in text:
        port = int(text)
        return port, port
    local, _, remote = text.partition(":")
    return int(local), int(remote)


def extra_forwards(forwards):
    """`-L` flags for every extra port, each bound to the remote's loopback.

    Loopback on the far side, always: a node is bound to 127.0.0.1 by default
    for the same reason the viewer is, and forwarding to the interface it is
    NOT listening on would fail in a way that reads as the node being down.
    """
    argv = []
    for argument in forwards or ():
        local, remote = parse_forward(argument)
        argv += ["-L", f"{local}:127.0.0.1:{remote}"]
    return argv


def reverse_forwards(pairs):
    """`-R` flags: a port on the REMOTE host that reaches back to this one.

    The mirror of `extra_forwards`, and the piece that makes the third data
    layout possible at all -- images on the cluster, cell table on the laptop,
    viewer next to the images. In that arrangement the primary is the machine
    that has to reach the node, and it cannot: a compute node cannot open a
    connection to a laptop behind NAT and a captive-portal wifi. A reverse
    forward is the one direction that does work, because the laptop already
    has an outbound ssh session to lend.

    Each pair is `(remote_port, local_port)` rather than a "a:b" string, so
    that which end is which cannot be read the wrong way round -- the -L
    spelling means the opposite thing in the same shape.
    """
    argv = []
    for remote_port, local_port in pairs or ():
        argv += ["-R", f"{int(remote_port)}:127.0.0.1:{int(local_port)}"]
    return argv


def direct_ssh_argv(target, local_port, remote_port, launch_line, *, jump=None,
                    ssh_opts=(), forwards=(), reverse=()):
    """One process: the forward and the command that fills it.

    `-t` asks for a pty so that when this ssh dies -- the user hits Ctrl+C, the
    laptop's lid closes, the network drops -- the remote Plexora gets a SIGHUP
    rather than being left running and holding a port forever.
    """
    argv = ["ssh", "-t", *_ssh_options(ssh_opts)]
    if jump:
        argv += ["-J", jump]
    argv += ["-L", f"{local_port}:127.0.0.1:{remote_port}"]
    argv += extra_forwards(forwards)
    argv += reverse_forwards(reverse)
    argv += [target, launch_line]
    return argv


def _gcloud_head(gcloud):
    """`gcloud compute ssh <vm> …` up to the point the command is added.

    The shape of a Google Cloud connection, and the reason there is no second
    tunnel process for it. A Plexora VM refuses every inbound connection that
    is not the Identity-Aware Proxy, so the way in is IAP -- and
    `--tunnel-through-iap`
    carries an ordinary ssh over it, forwards and all. What would otherwise be
    two processes (`start-iap-tunnel` holding a local port, plain ssh through
    it) is one, and it is one that already knows the OS Login user name, has
    registered this machine's key, and trusts the host key.
    """
    return ["gcloud", "compute", "ssh", str(gcloud.get("vm") or ""),
            "--project", str(gcloud.get("project") or ""),
            "--zone", str(gcloud.get("zone") or ""),
            "--tunnel-through-iap", "--quiet"]


def gcloud_ssh_argv(gcloud, local_port, remote_port, launch_line, *,
                    ssh_opts=(), forwards=(), reverse=()):
    """`direct_ssh_argv`'s shape, carried by gcloud instead of by ssh.

    Everything after `--` is handed to the underlying ssh verbatim, so the
    flags are the same flags -- `-t` for the SIGHUP that stops a Plexora being
    left running, the keepalives that turn a dead tunnel into an exit somebody
    can see, and every `-L`/`-R` this session needs. That is what makes this a
    drop-in: the watcher, the matchers, the askpass relay and the teardown
    downstream of it cannot tell which of the two builders produced their argv.
    """
    argv = _gcloud_head(gcloud)
    argv += ["--command", launch_line, "--", "-t", *_ssh_options(ssh_opts)]
    argv += ["-L", f"{local_port}:127.0.0.1:{remote_port}"]
    argv += extra_forwards(forwards)
    argv += reverse_forwards(reverse)
    return argv


def gcloud_node_ssh_argv(gcloud, local_port, remote_port, launch_line, *,
                         ssh_opts=()):
    """The same again, for a data node: one forward and nothing else.

    A node session's ssh is `direct_ssh_argv` with no extra forwards and no
    reverse ones -- the viewer stays on this machine, so there is nothing on
    the far side to reach back to. This mirrors that exactly, for the same
    reason the viewer's builder mirrors its own: the difference between the
    two transports must stop at the argv.
    """
    argv = _gcloud_head(gcloud)
    argv += ["--command", launch_line, "--", "-t", *_ssh_options(ssh_opts)]
    argv += ["-L", f"{local_port}:127.0.0.1:{remote_port}"]
    return argv


def job_ssh_argv(target, launch_line, *, jump=None, ssh_opts=()):
    """The process that holds the SLURM job open. No forward -- see below."""
    argv = ["ssh", "-t", *_ssh_options(ssh_opts)]
    if jump:
        argv += ["-J", jump]
    argv += [target, launch_line]
    return argv


def tunnel_ssh_argv(target, local_port, node, node_port, *, user=None,
                    bind_node=False, ssh_opts=(), forwards=(), reverse=()):
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
        argv += ["-L", f"{local_port}:{node}:{node_port}"]
        # Bound to the COMPUTE node's address here, not to loopback: in this
        # form the forward is made from the login node, whose own loopback is a
        # different machine's.
        for argument in forwards or ():
            local, remote = parse_forward(argument)
            argv += ["-L", f"{local}:{node}:{remote}"]
        # A reverse forward opened here lands on the LOGIN node, which in this
        # form is the machine the compute node reaches us through -- the same
        # asymmetry as the -L above, for the same reason.
        argv += reverse_forwards(reverse)
        argv += [target]
        return argv
    node_target = f"{user}@{node}" if user else node
    argv += ["-J", target, node_target, "-L", f"{local_port}:127.0.0.1:{node_port}"]
    argv += extra_forwards(forwards)
    argv += reverse_forwards(reverse)
    return argv


def parse_announce(line):
    """`(node, port)` from the remote's announce line, or None."""
    match = ANNOUNCE_RE.search(line)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def parse_node_announce(line):
    """`{host, port, node_id, token, hostname}` from a node's announce line.

    None when the line is not one. `hostname` is None against a Plexora old
    enough not to send it -- see NODE_HOSTNAME_RE.
    """
    match = NODE_ANNOUNCE_RE.search(line)
    if not match:
        return None
    found = match.groupdict()
    found["port"] = int(found["port"])
    named = NODE_HOSTNAME_RE.search(line)
    found["hostname"] = named.group("hostname") if named else None
    return found


def looks_like_missing_command(lines):
    return any(marker in line for line in lines for marker in MISSING_COMMAND_MARKERS)


def unsupported_remote_flag(lines):
    """The flag an older remote Plexora rejected, or None.

    Checked before `looks_like_missing_command`, because argparse's refusal
    contains "usage:" and a list of subcommands and is easy to mistake for a
    shell that could not find the program at all. The two have opposite fixes:
    one is a PATH, the other is a version on another machine.
    """
    for line in lines:
        found = OLD_REMOTE_RE.search(str(line))
        if found:
            return found.group(1)
    return None


def scheduler_lines(lines):
    """Just the scheduler's own complaints, out of the login node's noise.

    A cluster greets every connection with a banner, so the last six lines of
    the output are the banner and not the reason. Filtering to srun's own
    error lines is what puts the reason back at the top -- and it is a filter
    rather than a search over everything, so a motd that happens to contain
    the word "partition" cannot be read as the scheduler having said it.
    """
    return [str(line).strip() for line in lines
            if SRUN_ERROR_RE.search(str(line))]


def scheduler_refusal(lines):
    """Why the scheduler would not start the job, with the fix, or None.

    None when srun said nothing -- the job can fail for every reason a plain
    ssh does, and those have their own messages. This one is only for the case
    where the scheduler itself answered, which is never worth retrying: the
    same job line asked the same question and will get the same answer.
    """
    said = scheduler_lines(lines)
    if not said:
        return None
    joined = " ".join(said).lower()
    advice = SCHEDULER_GENERIC
    for marker, text in SCHEDULER_REFUSALS:
        if marker in joined:
            advice = text
            break
    return ("The scheduler would not start the job.\n"
            + "\n".join(f"    {line}" for line in said)
            + f"\n\n{advice}")


def _slug(text):
    """`text` reduced to what is safe in a node id, a filename and a URL."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-")
    return cleaned or "local"


def _local_manifest_path(node_id):
    """Where the node on this machine records what it is serving.

    Inside the user's own Plexora data root, beside nodes.json and
    remotes.json, because it describes this computer's filesystem in some
    detail and belongs with the other private registries.

    Imported defensively: this module is standalone-loadable by design (see the
    module docstring, and tests/test_connect.py, which loads it straight off
    disk). A manifest is a convenience -- a connection that cannot work out
    where to keep one should still connect.
    """
    try:
        from plexora import paths

        return os.path.join(str(paths.data_root()), "node-manifests",
                            f"{node_id}.json")
    except Exception:
        return None


def missing_local_paths(entries):
    """Which of these `kind:id=path` entries name nothing on this machine.

    A deliberately loose reading of the entry: anything that does not parse is
    left alone, because the node itself gives a better message about the shape
    of a bad entry than a guess made here could. This is only looking for the
    one failure worth catching early -- a path that is not there.

    The unquoting mirrors `node.resources.unquote_path`; connect.py stays
    stdlib-only and cannot import it. `tests/test_connect.py` holds the two in
    parity.
    """
    missing = []
    for entry in entries or ():
        _, separator, path = str(entry).partition("=")
        if not separator:
            continue
        path = path.strip()
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
            path = path[1:-1].strip()
        if path and not os.path.exists(os.path.expanduser(path)):
            missing.append(path)
    return missing


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


def pick_remote_port(randint=None):
    """A high port to ask the remote host for, sight unseen.

    Same bargain as `pick_ports`: the remote side cannot be probed without a
    round trip that would be stale by the time it returned, so a collision is
    retried rather than prevented.
    """
    import random

    return (random.randint if randint is None else randint)(*REMOTE_PORT_RANGE)


# -- running processes ----------------------------------------------------


class _Watched:
    """A spawned ssh, echoed line by line, watched for the announce line.

    The output is drained on a thread whatever else happens. Not for tidiness:
    ssh writes the remote's stdout into a pipe with a finite buffer, and a
    Plexora that logs a few kilobytes -- which it does on startup -- would
    block forever on a write nobody was reading, which presents as the job
    hanging at exactly the moment it was about to work.
    """

    #: What to look for in the output, by name. One entry was enough while the
    #: only thing worth waiting for was "which compute node did the scheduler
    #: give me"; a viewer that starts a data node beside itself announces twice,
    #: on the same pipe, in an order neither end controls.
    DEFAULT_MATCHERS = {"announce": parse_announce}

    def __init__(self, argv, label, *, echo=print, env=None, detach=False,
                 matchers=None):
        self.argv = argv
        self.label = label
        self.lines = []
        self.matchers = dict(self.DEFAULT_MATCHERS if matchers is None else matchers)
        self.found = {}
        self.events = {name: threading.Event() for name in self.matchers}
        self.process = _popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            # `env` carries the SSH_ASKPASS handshake when the app drives this
            # instead of a terminal; `detach` takes the controlling tty away so
            # ssh cannot fall back to prompting on a console the user is not
            # looking at. Both are inert for `plexora connect`, which wants
            # exactly the terminal behaviour it has always had.
            env=env,
            start_new_session=detach,
        )
        self._echo = echo
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    @property
    def announce(self):
        return self.found.get("announce")

    @property
    def saw_announce(self):
        return self.events["announce"]

    def _pump(self):
        stream = self.process.stdout
        if stream is not None:
            for raw in stream:
                # -t gives us a pty, and a pty gives us \r\n.
                line = raw.rstrip("\r\n")
                self.lines.append(line)
                for name, matcher in self.matchers.items():
                    if name in self.found:
                        continue
                    value = matcher(line)
                    if value:
                        self.found[name] = value
                        self.events[name].set()
                self._echo(f"  [{self.label}] {line}")
        # Wake anyone waiting for an announce that is never coming.
        for event in self.events.values():
            event.set()

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


#: The header a data node checks. Duplicated from server/node/api.py rather
#: than imported, because this module is standalone-loadable by design (see the
#: module docstring); tests/test_connect.py pins the two together.
#:
#: Every route on a node is guarded, `/health` included -- so a health poll
#: that sends nothing gets a 403, forever. That is not a failure any amount of
#: waiting fixes, and it looked exactly like a node that was slow to start.
NODE_TOKEN_HEADER = "X-Plexora-Node-Token"


def _wait_for_health(url, deadline, watchers, *, echo=None, headers=None,
                     any_answer=False):
    """Poll through the tunnel until Plexora answers, or something dies.

    Says so every `HEALTH_NOTE_SECONDS`. A forward into a compute node that the
    site's firewall drops does not fail -- it hangs, with nothing on any pipe --
    so silence here is the normal appearance of the commonest misconfiguration,
    and a caller with no output at all cannot tell it from a slow start.

    Failed polls back off. Every probe abandoned by its own short timeout
    leaves an ssh channel pending on the far side for the length of a TCP
    connect timeout (~2 minutes), so a fast poll against a forward whose far
    end is unreachable stacks pending channels until ssh's cap, after which
    every new probe is refused locally in milliseconds -- including the one
    that would have succeeded once the path recovered. Seen live against a
    cluster login node whose route to the compute node was being dropped.

    `any_answer` decides what counts as alive, and the two callers need
    opposite answers:

    - A VIEWER started over there may have been started with its own auth
      token, which this side has no way to know -- the announce line carries a
      node and a port and nothing else. Such a viewer answers 403, which is
      still proof that Plexora is up and listening on the far end of the
      tunnel, and is the whole question this poll is asking. So: any HTTP
      status below 500 is life, including the ones urllib raises on.
    - A NODE poll sends the token it was given, so a 403 means the token is
      wrong. That never fixes itself and must stay loud, so it keeps the
      strict reading where only a non-error response counts.
    """
    # Built once. A guarded endpoint needs the credential on every probe, and
    # a Request carries it in a header rather than in the URL, which keeps it
    # out of the far side's access log.
    probe = urllib.request.Request(url, headers=headers) if headers else url
    started = _now()
    noted = 0
    delay = 0.5
    while _now() < deadline:
        for watched in watchers:
            if not watched.alive:
                raise _Retriable(
                    # `label`, not the word "ssh": a local data node is watched
                    # here too, and it is not an ssh.
                    f"the {watched.label} process exited with code "
                    f"{watched.process.returncode} before Plexora answered"
                )
        try:
            with _urlopen(probe, timeout=5) as response:
                if response.status < 500:
                    return True
        except urllib.error.HTTPError as exc:
            # urlopen raises on 4xx, so the `< 500` above never sees one. A
            # refusal is an answer, and for a viewer it is the answer we came
            # for -- but only when the caller asked for that reading.
            if any_answer and exc.code < 500:
                return True
            _sleep(delay)
            delay = min(delay + 0.5, HEALTH_POLL_MAX_DELAY)
        except Exception:
            _sleep(delay)
            delay = min(delay + 0.5, HEALTH_POLL_MAX_DELAY)
        waited = _now() - started
        if echo is not None and waited >= (noted + 1) * HEALTH_NOTE_SECONDS:
            noted += 1
            echo(f"  still waiting for Plexora to answer on {url} "
                 f"({waited:.0f}s)...")
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
    # Before the retry: a scheduler that refused the job is not a flaky
    # connection, and _Retriable would spend another login and another queue
    # wait to be told the same thing -- with the reason itself replaced by
    # "exited with code 1", which names nothing at all.
    refusal = scheduler_refusal(watched.lines)
    if refusal:
        raise ConnectError(refusal, diagnosed=True)
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


def register_node_through(base_url, name, endpoint, token, *,
                          browser_endpoint=None, managed_by=None, role=None,
                          timeout=20):
    """Register a data node with a Plexora, over its own settings route.

    Used instead of writing the far side's nodes.json directly, and instead of
    passing the token in the remote command line. The route already exists, it
    already verifies the node answers before recording it, and re-registering
    the same name updates it -- which is exactly what a reconnection needs,
    since both the port and the token are new every session.

    Registering through the TUNNEL rather than to a public address is what
    makes this safe on a cluster: the request never leaves the ssh channel.
    """
    import json

    payload = {"name": name, "endpoint": endpoint, "token": token}
    if browser_endpoint:
        payload["browser_endpoint"] = browser_endpoint
    if managed_by:
        payload["managed_by"] = managed_by
    if role:
        payload["role"] = role
    request = urllib.request.Request(
        f"{str(base_url).rstrip('/')}/settings/nodes",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    answer = json.loads(body) if body.strip() else {}
    if answer.get("error"):
        raise ConnectError(f"The remote Plexora refused the data node: "
                           f"{answer['error']}")
    return answer


def _wait_for_node(watched, deadline, *, echo=print):
    """Block until a node announces itself on `watched`'s output.

    Separate from `_wait_for_announce` because the failure means something
    different: no compute node means no viewer at all, while no data node
    means a viewer that works with one layer missing. This one is allowed to
    give up and say so without taking the connection down with it.
    """
    while _now() < deadline:
        if watched.events["node"].wait(timeout=2):
            break
        if not watched.alive:
            break
    if "node" not in watched.found:
        watched.drain(timeout=1)
    return watched.found.get("node")


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


def _no_gcloud_message():
    """A Google Cloud profile on a machine with no gcloud.

    Its own message rather than the generic ssh one, because the missing
    program is different and so is the fix -- and because a profile saved on a
    laptop that had the CLI can be opened on one that does not, which is
    exactly when this needs to say which of the two machines is the problem.
    """
    return (
        "This connection is carried by the Google Cloud CLI, and `gcloud` is "
        "not installed on this machine.\n"
        "Install it from https://cloud.google.com/sdk, then sign in with "
        "`gcloud auth login`."
    )


def _old_remote_hint(target, flag, remote_command, watched):
    """Plexora is over there, and it is too old to be asked this.

    Named as its own failure because the symptom points the wrong way: what
    comes back is an argparse usage dump, which looks like a typo in something
    the user wrote -- and they wrote none of it. Nothing here can be adjusted
    to make it work, so the message does not offer an option; it says which
    machine is behind and what to run on it.
    """
    return ConnectError(
        f"The Plexora installed on {target} is too old for this: it does not "
        f"understand `{flag}`.\n"
        + "\n".join(f"    {line}" for line in watched.tail())
        + f"\n\nUpgrade Plexora there, in the environment "
          f"{remote_command!r} runs from:\n"
          f"    pip install --upgrade plexora\n"
          f"Then try again. Nothing needs changing on this computer, and "
          f"nothing about the saved connection is wrong."
    )


def _missing_command_hint(remote_command, watched):
    return ConnectError(
        f"The remote host could not run {remote_command!r}:\n"
        + "\n".join(f"    {line}" for line in watched.tail())
        + "\n\nA non-interactive ssh session often has a shorter PATH than a "
          "login shell. Name the environment Plexora is installed in -- the "
          "prefix `conda env list` prints is enough:\n"
          "    --remote-command /home/you/miniconda3/envs/myenv\n"
          "    --remote-command \"conda run --no-capture-output -n myenv plexora\"\n"
          "Or run `plexora --remote` on the host yourself and use the tunnel "
          "command it prints."
    )


class _BareInstall:
    """What `_begin_install`/`_await_install` read, for a caller with no session.

    `connect_node` drives one ssh by hand -- it predates `NodeSession` and is
    what `plexora node connect` still runs -- so there is no object there to
    read `target` and `remote_command` off. This is that object and nothing
    more; its `_phase` is a no-op because a terminal has the echoed lines and
    no progress list to update.
    """

    def __init__(self, target, remote_command, jump, ssh_opts, echo):
        self.target = target
        self.remote_command = remote_command
        self.jump = jump
        self.ssh_opts = ssh_opts
        self.echo = echo
        self.install_log = []
        self.installed_version = None

    def _phase(self, name):
        pass

    def _spawn(self, argv, label, matchers=None):
        self.echo(f"$ {' '.join(argv)}")
        watched = _Watched(argv, label, echo=self.echo, matchers=matchers)
        self.watchers.append(watched)
        _ACTIVE.append(watched)
        return watched


def _begin_install(session):
    """Announce the install and hand back the pip line the launch is chained to."""
    line = install_command_line(session.remote_command)
    environment = environment_label(session.remote_command)
    session._phase("installing")
    session.echo(f"  Installing Plexora on {session.target}"
                 + (f" in {environment}" if environment else "")
                 + " before launching it; this can take a few minutes.")
    return line


def _await_install(session, watched, line):
    """Block until the chained install prints its marker, or explain why not.

    `watched` is the connection's own primary ssh -- install and launch are
    one process now -- so unlike the old separate-ssh install there is no
    watcher to clean up afterwards: a marker means the same process is now
    busy launching, and a death before the marker means the connection failed
    and the watcher's exit is the correct thing for `alive` to report.
    """
    event = watched.events["installed"]
    deadline = _now() + INSTALL_TIMEOUT
    # `found`, not the event alone: the pump sets every event when the stream
    # ends, precisely so waiters do not hang on an announce that is never
    # coming -- so a set event proves the process finished talking, and only
    # `found` proves the marker was in what it said.
    while not watched.found.get("installed"):
        event.wait(0.2)
        if watched.found.get("installed"):
            break
        if not watched.alive:
            watched.drain()
            session.install_log = list(watched.lines)
            code = watched.process.poll()
            raise ConnectError(_install_failure(session, line, watched, code))
        if _now() > deadline:
            raise ConnectError(
                f"Installing Plexora on {session.target} did not finish within "
                f"{INSTALL_TIMEOUT:g}s.\n"
                + "\n".join(f"    {text}" for text in watched.tail())
                + "\n\nThe environment may be half-upgraded. Run it by hand "
                  "over there to see where it stopped:\n"
                  f"    {line}\n"
                  "Or turn “Install or update Plexora” off and connect to the "
                  "copy that is already installed."
            )
    session.install_log = list(watched.lines)
    version = parse_installed_version(watched.lines)
    session.installed_version = version
    session.echo(f"  Plexora {version} is installed on {session.target}."
                 if version else
                 f"  Plexora is installed on {session.target}.")
    return version


def _install_failure(session, line, watched, code):
    """Why the install exited non-zero, with the fix when there is a known one.

    Two of them are worth naming, because both read as "Plexora is broken"
    and neither is. A shell that cannot find `pip` is the same PATH problem
    that makes `plexora` unfindable, one step earlier and with the same
    answer -- name the environment. A read-only or unwritable site install is
    the other, and its fix is not a Plexora setting at all.
    """
    tail = "\n".join(f"    {text}" for text in watched.tail(12))
    lines = watched.lines
    if looks_like_missing_command(lines):
        advice = (
            "There is no `pip` where that command would run. Name the "
            "environment Plexora should be installed into, in “Plexora "
            "command or environment” -- the prefix `conda env list` prints "
            "is enough:\n"
            "    /home/you/miniconda3/envs/myenv\n"
            "    conda run -n myenv plexora"
        )
    elif any("Permission denied" in text or "Read-only file system" in text
             or "Could not install packages due to an OSError" in text
             for text in lines):
        advice = (
            "pip could not write to that environment. It is probably a "
            "site-managed install you do not own -- make an environment of "
            "your own over there and name it in “Plexora command or "
            "environment”."
        )
    else:
        advice = (
            "Run it by hand over there to see the whole of it:\n"
            f"    {line}\n"
            "Or turn “Install or update Plexora” off and connect to the copy "
            "that is already installed."
        )
    return (f"Installing Plexora on {session.target} failed "
            f"(pip exited {code}).\n{tail}\n\n{advice}")


def _begin_mount(session):
    """Announce the mount and hand back the line the launch is chained to."""
    session._phase("mounting_data")
    session.echo("  Mounting your data on the remote machine and checking it "
                 "can be read. A first connection also sets Plexora up over "
                 "there, which takes a few minutes.")
    return session.mount_command


def _await_mount(session, watched, line):
    """Block until the chained mount prints its marker, or explain why not.

    The same shape as `_await_install`, and deliberately so -- both are an
    expensive prerequisite chained ahead of the launch in one ssh, and both
    are told apart from a hung login only by a marker the remote shell prints.
    The one difference is the read-only marker, which is a warning: the mount
    worked, and a bucket somebody can only read is a bucket Plexora can still
    open images from.
    """
    event = watched.events["mounted"]
    deadline = _now() + MOUNT_TIMEOUT
    while not watched.found.get("mounted"):
        event.wait(0.2)
        if watched.found.get("mounted"):
            break
        if not watched.alive:
            watched.drain()
            code = watched.process.poll()
            # Diagnosed: this step has the remote output in front of it and
            # has already worked out what happened. The mount chain prints
            # apt's log when an install fails, and that log is full of
            # phrases -- "not found", "No such file or directory" -- that a
            # substring guess upstream would read as a PATH problem.
            raise ConnectError(_mount_failure(session, line, watched, code),
                               diagnosed=True)
        if _now() > deadline:
            raise ConnectError(
                f"Preparing the data on {session.target} did not finish "
                f"within {MOUNT_TIMEOUT:g}s.\n"
                + "\n".join(f"    {text}" for text in watched.tail()),
                diagnosed=True,
            )
    if watched.found.get("readonly"):
        session.mount_readonly = True
        session.echo("  That bucket is read-only for this account. Images "
                     "will open; saving a figure into it will not work.")
    session.echo("  The data is mounted and readable.")
    return True


def _mount_failure(session, line, watched, code):
    """Why the mount exited non-zero, with the fix when there is a known one.

    Two are worth naming. A bucket the VM's service account cannot see is by
    far the commonest, reads as "Plexora is broken", and has a one-line fix
    that is not a Plexora setting. A fuse device that is not there is the
    other, and means the image lost a race with its own startup script.
    """
    tail = "\n".join(f"    {text}" for text in watched.tail(12))
    lines = watched.lines
    joined = "\n".join(lines).lower()
    if ("403" in joined or "permission" in joined or "forbidden" in joined
            or "does not have storage" in joined):
        advice = (
            "The VM's service account cannot read that bucket. Grant it "
            "access once, from a machine with gcloud:\n"
            "    gcloud storage buckets add-iam-policy-binding gs://BUCKET \\\n"
            "        --member=serviceAccount:PROJECT_NUMBER"
            "-compute@developer.gserviceaccount.com \\\n"
            "        --role=roles/storage.objectUser"
        )
    elif "fuse" in joined and ("no such" in joined or "not found" in joined):
        advice = (
            "Cloud Storage FUSE is not installed on that VM yet. It is "
            "installed at first boot, so a VM created seconds ago may simply "
            "need another minute -- try connecting again."
        )
    else:
        advice = ("Run it by hand over there to see the whole of it:\n"
                  f"    {line}")
    return (f"Preparing the data on {session.target} failed "
            f"(exited {code}).\n{tail}\n\n{advice}")


class Session:
    """One remote Plexora and the ssh processes holding it up.

    Establishing a connection and waiting on it are two different acts, and
    this class exists to keep them apart. `plexora connect` does both back to
    back and blocks in a terminal until Ctrl+C. The in-app "Connect to remote
    server" cannot: it runs inside a Flask request that has to return a
    response in the next second or two, keep the ssh processes alive
    afterwards, and let a later request ask how it went or tear it down. Same
    establishment, two lifetimes.

    Everything about the connection that a caller might want to show a user --
    the local port, the compute node the scheduler chose, the log each ssh has
    written so far -- is an attribute here rather than a local variable in a
    function that has not returned yet.
    """

    def __init__(self, target, *, datasource=None,
                 remote_command=DEFAULT_REMOTE_COMMAND, srun=None,
                 bind_node=False, jump=None, ssh_opts=(), local_port=None,
                 remote_port=None, timeout=None, data_dir=None, plugins=None,
                 echo=print, forwards=(), env=None, detach=False,
                 on_phase=None, also_serve=(), local_serve=(), node_name=None,
                 node_port=None, allow_origin=None, local_node=True,
                 node_manifest=None, install=False, gcloud=None,
                 mount_command=None):
        self.target = target
        self.datasource = datasource
        self.remote_command = remote_command
        #: `{vm, project, zone}` when this connection is carried by
        #: `gcloud compute ssh` instead of by plain ssh, None otherwise. A
        #: transport detail and nothing more: everything downstream of the argv
        #: -- the watcher, the matchers, the askpass relay, the teardown -- is
        #: the same either way, which is the point.
        self.gcloud = dict(gcloud) if gcloud else None
        #: A shell line to run on the far side before anything is launched,
        #: chained into the same ssh. Set for a connection whose data has to be
        #: mounted first; None for every other kind.
        self.mount_command = mount_command or None
        #: Set when the mount succeeded but the data cannot be written to. A
        #: note on a working connection, never a failure.
        self.mount_readonly = False
        #: Whether to `pip install --upgrade plexora` on the far side before
        #: launching anything. Off by default and stored as an opt-in: this is
        #: the one thing a connection does that WRITES to somebody else's
        #: machine, and nobody should discover that by having it happen.
        self.install = bool(install)
        self.srun = srun
        self.bind_node = bind_node
        self.jump = jump
        self.ssh_opts = ssh_opts
        self.requested_local_port = local_port
        self.requested_remote_port = remote_port
        self.timeout = default_timeout(timeout, srun)
        self.data_dir = data_dir
        self.plugins = plugins
        self.forwards = forwards
        self.echo = echo
        self.env = env
        self.detach = detach
        #: Called with a phase name as establishment moves through it, for a
        #: caller that has to show somebody what is being waited for. The echo
        #: lines say the same things, but they say them when ssh gets round to
        #: printing -- and the longest wait here, a queued job, is announced
        #: five seconds after it starts. A UI reading that would spend those
        #: five seconds claiming to be doing something else.
        self.on_phase = on_phase

        #: Resources to serve FROM the remote host, beside the viewer.
        self.also_serve = tuple(also_serve or ())
        #: Resources to serve from THIS machine, reached by the remote viewer
        #: through a reverse forward.
        self.local_serve = tuple(local_serve or ())
        #: Whether to run a node on this machine at all, even with nothing to
        #: serve yet. On by default, and that default is what makes the data
        #: forms' Local option mean anything: the user picks a file on their
        #: own computer long after the session started, so nothing could have
        #: named it up front. `--no-local-node` turns it off for somebody who
        #: wants the tunnel and nothing else.
        self.local_node = bool(local_node) or bool(self.local_serve)
        self.node_name = node_name
        self.requested_node_port = node_port
        self.allow_origin = allow_origin
        #: Where the laptop node records what it ends up serving, so the next
        #: session serves it again under the same ids. None means "work it out"
        #: -- see `_local_manifest_path`.
        self.node_manifest = node_manifest

        self.watchers = []
        self.primary = None
        self.local_port = None
        self.remote_port = None
        #: The compute node the scheduler picked, in `--srun` mode. None until
        #: the job announces itself, and None forever in direct mode.
        self.node = None
        self.node_port = None
        #: `{name, endpoint, browser_endpoint}` per data node this session set
        #: up and registered. Reported rather than returned, because a node
        #: that could not be registered is a degraded connection, not a failed
        #: one -- the viewer still opens.
        self.data_nodes = []
        self.node_errors = []
        #: What the install said, and which version it left behind. A copy of
        #: the primary's lines up to the marker -- the install rides the same
        #: ssh as the launch (see `install_prefixed`).
        self.install_log = []
        self.installed_version = None

    # -- what a caller shows the user -------------------------------------

    @property
    def url(self):
        if self.local_port is None:
            return None
        return f"http://127.0.0.1:{self.local_port}/"

    @property
    def open_url(self):
        """The URL to actually open -- the datasource included when there is one."""
        url = self.url
        if url is None:
            return None
        return url + self.datasource if self.datasource else url

    @property
    def alive(self):
        return bool(self.watchers) and all(w.alive for w in self.watchers)

    def log(self, count=40):
        """Every ssh's recent output, labelled, oldest first.

        Install output needs no special handling any more: the install rides
        the primary ssh itself, so its lines are in that watcher's tail like
        everything else the connection printed.
        """
        lines = []
        for watched in self.watchers:
            lines += [f"[{watched.label}] {line}" for line in watched.tail(count)]
        return lines

    # -- establishing ------------------------------------------------------

    def _spawn(self, argv, label, matchers=None, env=None):
        self.echo(f"$ {' '.join(argv)}")
        watched = _Watched(argv, label, echo=self.echo,
                           env=self.env if env is None else env,
                           detach=self.detach, matchers=matchers)
        self.watchers.append(watched)
        _ACTIVE.append(watched)
        return watched

    def _silence_hint(self):
        """Why a tunnel that opened without complaint carries nothing.

        With a scheduler there are two ways to build the last hop, and a
        cluster can silently drop either one: some refuse ssh into a compute
        node (breaking the default), others drop forward connections made from
        a login node to a compute node (breaking --bind-node -- observed live
        on a cluster that allowed the very same connection from a shell, so no
        probe run by hand can rule it out). Whichever mode just failed
        silently, the other one is the first thing worth trying, and the
        message says so. ssh's own `channel open failed` line is quoted when
        there is one: it is the only direct evidence this timeout ever has.
        """
        switch = None
        if self.srun is not None:
            switch = (
                'turning OFF "Forward from the login node" (--bind-node), '
                "so the tunnel goes into the compute node itself"
                if self.bind_node else
                'turning ON "Forward from the login node" (--bind-node), '
                "for a cluster that refuses ssh into a compute node"
            )
        for watched in self.watchers:
            for line in watched.lines:
                if "open failed" in line and "connect failed" in line:
                    hint = (f"The tunnel is open, but its far end could not "
                            f"reach Plexora:\n  {line.strip()}\n")
                    if switch:
                        hint += f"Try {switch}."
                    else:
                        hint += ("Check that the remote host can run "
                                 "`plexora --remote` on its own.")
                    return hint
        if switch:
            return (
                f"Nothing came back through the tunnel at all. Try {switch}.\n"
                "Otherwise: raise --timeout, or check that the remote host "
                "can run `plexora --remote` on its own."
            )
        return ("Raise --timeout, or check that the remote host can run "
                "`plexora --remote` on its own.")

    def _phase(self, name):
        if self.on_phase is not None:
            try:
                self.on_phase(name)
            except Exception:
                # A caller's progress display must never be able to fail a
                # connection that is otherwise working.
                pass

    def establish(self):
        """Spawn, wait for Plexora to answer through the tunnel, return self.

        Leaves the processes running on success -- the caller owns them from
        here and must call `stop()`. Stops them itself on every failure, so a
        raise never leaks an ssh.
        """
        if self.gcloud and _which("gcloud") is None:
            raise ConnectError(_no_gcloud_message())
        user, _host = split_target(self.target)
        self.local_port, self.remote_port = pick_ports(
            self.requested_local_port, self.requested_remote_port)
        deadline = _now() + self.timeout

        # Ports for the data nodes are chosen HERE, before anything is spawned,
        # because an -L or -R forward is fixed when the ssh connection opens
        # and the far side has not run a line of code by then. A collision is
        # handled the way every other port collision here is: by retrying the
        # whole attempt on a different number.
        forwards = list(self.forwards or ())
        reverse = []
        remote_node_port = local_node_port = None
        local_node_serve_port = remote_reverse_port = None
        if self.also_serve:
            remote_node_port = self.requested_node_port or pick_remote_port()
            local_node_port = _free_local_port()
            forwards.append(f"{local_node_port}:{remote_node_port}")
        if self.local_node:
            local_node_serve_port = _free_local_port()
            remote_reverse_port = pick_remote_port()
            reverse.append((remote_reverse_port, local_node_serve_port))

        launch = remote_command_line(
            self.remote_command, self.remote_port,
            bind_node=self.bind_node, datasource=self.datasource,
            data_dir=self.data_dir, plugins=self.plugins,
            also_serve=self.also_serve, node_port=remote_node_port,
            # The browser's origin, which this end knows and the far end cannot
            # guess. Without it the browser's probe of a node fails CORS and
            # every tile falls back to being proxied through the viewer -- for
            # a node on THIS machine that means laptop -> cluster -> back down
            # the reverse tunnel -> laptop -> cluster -> browser, per tile.
            node_allow_origin=self.allow_origin or self._browser_origin(),
        )
        # The viewer's own ssh has to watch for two announcements at once when
        # it is also starting a node: which compute node the scheduler gave us,
        # and where the data node landed. They arrive on one pipe in an order
        # neither end controls.
        matchers = dict(_Watched.DEFAULT_MATCHERS)
        if self.also_serve:
            matchers["node"] = parse_node_announce

        try:
            # The install rides the SAME ssh as the launch, chained ahead of
            # it -- one login instead of two, and at a Duo site one buzz of
            # the phone instead of two. `&&` is what keeps the promise the
            # separate-ssh version made: a failed install launches nothing.
            install_line = _begin_install(self) if self.install else None
            if install_line:
                matchers["installed"] = parse_install_done
            # Announced BEFORE the install, because it runs before it: the
            # environment pip would write to lives on the machine the mount
            # step prepares.
            mount_line = _begin_mount(self) if self.mount_command else None
            if mount_line:
                matchers["mounted"] = parse_mount_done
                matchers["readonly"] = parse_mount_readonly

            local_node = None
            if self.local_node:
                local_node = self._start_local_node(local_node_serve_port)

            if self.srun is None:
                line = (install_prefixed(install_line, launch)
                        if install_line else launch)
                # Outermost, so the order on the far side is mount, then
                # install, then launch -- see `mount_prefixed`.
                if mount_line:
                    line = mount_prefixed(mount_line, line)
                self.primary = self._spawn(
                    gcloud_ssh_argv(self.gcloud, self.local_port,
                                    self.remote_port, line,
                                    ssh_opts=self.ssh_opts,
                                    forwards=forwards, reverse=reverse)
                    if self.gcloud else
                    direct_ssh_argv(self.target, self.local_port,
                                    self.remote_port, line, jump=self.jump,
                                    ssh_opts=self.ssh_opts,
                                    forwards=forwards, reverse=reverse),
                    "ssh", matchers,
                )
                if mount_line:
                    _await_mount(self, self.primary, mount_line)
                    # Neither prerequisite spends the connection's own budget:
                    # both are minutes long by nature and the timeout is about
                    # how long Plexora may take to answer once it is started.
                    deadline = _now() + self.timeout
                if install_line:
                    _await_install(self, self.primary, install_line)
                    # The install spent none of the connection's own budget.
                    deadline = _now() + self.timeout
                self._phase("starting")
            else:
                remote = srun_command_line(self.srun, launch)
                if install_line:
                    remote = install_prefixed(install_line, remote)
                self.primary = self._spawn(
                    job_ssh_argv(self.target, remote,
                                 jump=self.jump, ssh_opts=self.ssh_opts),
                    "job", matchers,
                )
                if install_line:
                    _await_install(self, self.primary, install_line)
                    deadline = _now() + self.timeout
                self._phase("waiting_for_job")
                self.node, self.node_port = _wait_for_announce(
                    self.primary, deadline, echo=self.echo)
                self.echo(f"  Plexora is on {self.node}:{self.node_port}; "
                          f"opening the tunnel.")
                self._phase("tunneling")
                self._spawn(
                    tunnel_ssh_argv(self.target, self.local_port, self.node,
                                    self.node_port, user=user,
                                    bind_node=self.bind_node,
                                    ssh_opts=self.ssh_opts,
                                    forwards=forwards, reverse=reverse),
                    "tunnel",
                )

            self._phase("waiting_for_app")
            if not _wait_for_health(self.url, deadline, self.watchers,
                                    echo=self.echo, any_answer=True):
                raise ConnectError(
                    f"Plexora did not answer on {self.url} within "
                    f"{self.timeout:g}s.\n" + self._silence_hint()
                )

            # Only now, with a viewer that answers: registering a node means
            # POSTing to that viewer, so there is nothing to POST to until here.
            if self.also_serve:
                self._register_remote_node(deadline, remote_node_port,
                                           local_node_port)
            if self.local_node:
                self._register_local_node(deadline, local_node,
                                          remote_reverse_port,
                                          local_node_serve_port)
        except _Retriable as exc:
            self.stop()
            if self.watchers:
                self.watchers[0].drain()
                if looks_like_missing_command(self.watchers[0].lines):
                    raise _missing_command_hint(
                        self.remote_command, self.watchers[0]) from exc
            raise
        except BaseException:
            self.stop()
            raise
        return self

    # -- data nodes --------------------------------------------------------

    def _node_label(self, suffix):
        base = self.node_name or f"{split_target(self.target)[1]}"
        return f"{base}-{suffix}" if suffix else base

    def _browser_origin(self):
        """Where the browser will be, as an Origin header spells it.

        Known here and nowhere else: the far side is handed a port number and
        never learns which loopback address a browser reached it from.
        """
        if self.local_port is None:
            return None
        return f"http://127.0.0.1:{self.local_port}"

    def _local_node_id(self):
        """A stable identity for the node on this machine.

        Stable across sessions on purpose, and per saved connection rather than
        per launch: it is what names this node's manifest, so the files shared
        last time come back under the same resource ids -- which is the whole
        of "reopen the project and it just works" on the local side.
        """
        base = _slug(self.node_name or split_target(self.target)[1] or "local")
        return f"connect-{base}-local"

    def _start_local_node(self, port):
        """Serve files from THIS machine to the viewer on the far side.

        Started for every connection, with or without anything to serve yet.
        That is the point: the user picks a file on their own computer from a
        form in a browser, minutes into the session, and nothing could have
        named it on this command line. `--dynamic` is what lets the viewer hand
        it one then.

        The token is read back off the node's own announce rather than passed
        in on the command line, exactly as for the remote one. On a laptop the
        argv exposure is a smaller problem than it is on a login node, but
        having one rule here means there is no second path to get wrong.
        """
        missing = missing_local_paths(self.local_serve)
        if missing:
            # Checked here, before any ssh, because the node is started first
            # and its death is not noticed until registration -- which is after
            # the scheduler has queued and allocated a job. A typo in a path on
            # THIS machine should not cost a wait in the queue to discover.
            raise ConnectError(
                "There is nothing at:\n  "
                + "\n  ".join(missing)
                + "\nCheck the files to share from this computer. Paths are "
                  "written plain -- no quotes, even when they contain spaces."
            )
        node_id = self._local_node_id()
        argv = [sys.executable, "-m", "plexora", "node", "serve",
                "--port", str(int(port)), "--host", "127.0.0.1",
                "--dynamic", "--node-id", node_id]
        # The browser reaches this node directly -- it is on the same machine,
        # which makes it the fastest path of the three layouts and the one that
        # most needs the CORS header to actually be there.
        origin = self._browser_origin()
        if origin:
            argv += ["--allow-origin", origin]
        manifest = self.node_manifest or _local_manifest_path(node_id)
        if manifest:
            argv += ["--manifest", str(manifest)]
        for entry in self.local_serve:
            argv += ["--serve", entry]
        # Unbuffered, because this stdout is a pipe and the announce -- and any
        # traceback -- must arrive when printed, not when a buffer happens to
        # fill. The ssh-launched nodes get the same treatment from the `env
        # PYTHONUNBUFFERED=1` in their remote command line.
        env = dict(self.env if self.env is not None else os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        return self._spawn(argv, "node", {"node": parse_node_announce}, env=env)

    def _register(self, name, endpoint, token, browser_endpoint, role=None):
        """Record one node with the remote viewer, or note why not.

        Never fatal. A viewer with an unregistered node is a project with one
        layer missing and a message saying so, which is a far better outcome
        than refusing to open the images that ARE reachable.
        """
        try:
            register_node_through(
                self.url, name, endpoint, token,
                browser_endpoint=browser_endpoint,
                role=role,
                managed_by=f"connect:{self.node_name}" if self.node_name else "connect",
            )
        except Exception as exc:
            self.node_errors.append(f"{name}: {exc}")
            self.echo(f"  Could not register the data node {name!r}: {exc}")
            return None
        self.data_nodes.append({"name": name, "endpoint": endpoint,
                                "browser_endpoint": browser_endpoint})
        self.echo(f"  Data node {name!r} registered with the remote Plexora.")
        return name

    def _register_remote_node(self, deadline, remote_port, local_port):
        """The node that runs beside the viewer, on the remote host.

        `endpoint` is the far side's own loopback -- the viewer and the node
        are on one machine, so that is the short path and the fast one.
        `browser_endpoint` is this end of the tunnel, because the browser is
        here and cannot resolve the other address at all.
        """
        # Said out loud because this wait can be long (a first-time mask is
        # pyramidized before the announce) and it happens AFTER the viewer is
        # already answering -- silence here reads as the connection hanging.
        self.echo("  waiting for the remote data node to say it is ready...")
        announced = _wait_for_node(self.primary, deadline, echo=self.echo)
        if not announced:
            self.node_errors.append(
                "the remote data node did not report that it had started")
            self.echo("  The remote data node did not start; the viewer will "
                      "open without it.")
            return
        self._register(
            self._node_label("node"),
            f"http://127.0.0.1:{remote_port}",
            announced["token"],
            f"http://127.0.0.1:{local_port}",
        )

    def _register_local_node(self, deadline, watched, remote_port, local_port):
        """The node on THIS machine, reached through the reverse forward.

        `endpoint` is the reverse forward's far end -- from the remote viewer's
        point of view, a port on its own loopback that comes back down the ssh
        connection. `browser_endpoint` is the node's real local address, which
        the browser can reach directly because the browser is on this machine
        too, which makes it the fastest path in any of the three layouts.
        """
        if not self.local_serve:
            # An empty node binds as fast as Python can import; the long
            # deadline exists for the case where a raw mask has to be
            # pyramidized before the announce. Paying it for a node that simply
            # failed to start would make every connection that hits a local
            # install problem look like a hang -- and this node is now started
            # for every connection, so that would be everybody.
            deadline = min(deadline, _now() + EMPTY_NODE_TIMEOUT)
        self.echo("  waiting for the data node on this machine to say it is "
                  "ready...")
        announced = watched and _wait_for_node(watched, deadline, echo=self.echo)
        if not announced:
            self.node_errors.append("the local data node did not start")
            self.echo("  The local data node did not start; the viewer will "
                      "open without it.")
            return
        self._register(
            self._node_label("local"),
            f"http://127.0.0.1:{remote_port}",
            announced["token"],
            f"http://127.0.0.1:{local_port}",
            # What makes the viewer's data forms offer a Local option at all.
            # "There is a node here" is not enough to know that: a node beside
            # the viewer on the cluster is also a node. This one is the user's
            # own computer, which is what "Local" means to them.
            role="client",
        )

    def wait(self):
        """Block until the process holding the remote Plexora up exits."""
        if self.primary is not None:
            self.primary.process.wait()

    def stop(self):
        # Reversed so the tunnel goes before the job it points at. `lines` is
        # deliberately left intact: a caller diagnosing a failure reads the log
        # after this has run.
        for watched in reversed(self.watchers):
            watched.stop()
            if watched in _ACTIVE:
                _ACTIVE.remove(watched)


class NodeSession:
    """A data node on another machine, and the one ssh holding it up.

    The mirror image of `Session`. There, Plexora runs over there and the
    browser reaches it through a tunnel; here, Plexora runs *here* -- on the
    user's own computer, where the browser and the project database already are
    -- and the only thing on the far side is the pile of pixels nobody wants to
    copy. One ssh, one forward, one node.

    This is what the Remote half of a data field's Local/Remote switch resolves
    to when Plexora is running locally, which is the ordinary case. The user
    picks a saved connection in a form, this opens, and from that moment the
    far side's filesystem can be browsed and its files named -- **without the
    paths having been declared anywhere first.** That is what `--dynamic` buys,
    and it is the difference between choosing where data lives when you add it
    and having to decide before Plexora starts.

    **The saved profile decides how the host is reached, including `srun`.** A
    profile that says "run Plexora inside a job" gets a data node inside a job,
    on the compute node the scheduler picks, reached by the same two-process
    arrangement `Session` uses. That is not a preference this class is entitled
    to second-guess: serving tiles is sustained read I/O, and a site that keeps
    Plexora off its login nodes means it for this too. The cost is honest and
    is paid where it is visible -- pressing Connect can wait in the queue.

    Split into `establish()` and `wait()` for the same reason `Session` is: a
    request handler has to answer in the next second while the ssh it just
    started keeps running all afternoon.
    """

    def __init__(self, target, *, serve=(), remote_command=DEFAULT_REMOTE_COMMAND,
                 srun=None, bind_node=False,
                 jump=None, ssh_opts=(), local_port=None, remote_port=None,
                 timeout=None, plugins=None, allow_origin=None, node_name=None,
                 node_id=None, manifest=None, dynamic=True, echo=print, env=None,
                 detach=False, on_phase=None, register=None, install=False,
                 gcloud=None, mount_command=None):
        self.target = target
        self.serve = tuple(serve or ())
        self.remote_command = remote_command
        #: The same two as `Session`'s, and here for the same reason `srun` is:
        #: how a machine is reached, and what has to be ready on it before
        #: anything runs, are facts about the machine rather than about which
        #: half of the connection is being opened.
        self.gcloud = dict(gcloud) if gcloud else None
        self.mount_command = mount_command or None
        self.mount_readonly = False
        #: Same opt-in as `Session.install`, and it belongs here for the same
        #: reason `srun` does: it is a fact about how that machine is reached,
        #: not about one feature of it. A profile that says "keep Plexora
        #: current over there" means it whichever half of the connection is
        #: being opened -- and a node runs the same code the viewer would.
        self.install = bool(install)
        #: Straight off the saved profile. None means "this host runs Plexora
        #: directly"; anything else -- the empty string included -- means the
        #: node belongs in a job. See `as_node_kwargs` for why this is not a
        #: decision taken here.
        self.srun = srun
        self.bind_node = bind_node
        self.jump = jump
        self.ssh_opts = ssh_opts
        self.requested_local_port = local_port
        self.requested_remote_port = remote_port
        # A queued job is not a slow start, and the two want very different
        # budgets: `default_timeout` already knows that ratio, so the scheduler
        # case borrows it rather than inventing a second number.
        self.timeout = (default_timeout(timeout, srun) if srun is not None
                        else (NODE_START_TIMEOUT if timeout is None else timeout))
        self.plugins = plugins
        #: The origin the BROWSER will send. The browser is on this machine and
        #: reaches the node through the same forward this process does, so the
        #: node must echo this exact origin back or every direct tile fetch
        #: fails CORS and silently falls back to proxying through Plexora.
        self.allow_origin = allow_origin
        self.node_name = node_name or split_target(target)[1]
        self.dynamic = dynamic
        self.echo = echo
        self.env = env
        self.detach = detach
        self.on_phase = on_phase
        #: Injected rather than imported, so this module stays loadable without
        #: the plexora package -- see the module docstring.
        self.register = register

        #: Stable per saved connection, because it names the manifest on the
        #: far side. Same id next session means the same resource ids, which is
        #: the whole of "reopen the project and it just works".
        self.node_id = node_id or f"connect-{_slug(self.node_name)}-data"
        #: A path on the REMOTE machine, or None to let the node pick its own
        #: default there. None is the usual answer: this end has no business
        #: guessing what the other end's data root is called.
        self.manifest = manifest

        self.watchers = []
        self.primary = None
        self.local_port = None
        self.remote_port = None
        self.token = None
        #: The compute node the scheduler picked, once it says so. None in
        #: direct mode, where the host reached is the host named.
        self.node = None
        self.registered = None
        self.node_errors = []
        self.install_log = []
        self.installed_version = None

    # -- what a caller shows the user -------------------------------------

    @property
    def endpoint(self):
        if self.local_port is None:
            return None
        return f"http://127.0.0.1:{self.local_port}"

    #: `Session` calls this `url` and the two are polled by the same code.
    url = endpoint

    @property
    def open_url(self):
        """Nothing to open. The viewer is already where the user is."""
        return None

    @property
    def alive(self):
        return bool(self.watchers) and all(w.alive for w in self.watchers)

    def log(self, count=40):
        lines = []
        for watched in self.watchers:
            lines += [f"[{watched.label}] {line}" for line in watched.tail(count)]
        return lines

    # -- establishing ------------------------------------------------------

    def _phase(self, name):
        if self.on_phase is not None:
            try:
                self.on_phase(name)
            except Exception:
                pass

    def _spawn(self, argv, label, matchers=None):
        self.echo(f"$ {' '.join(argv)}")
        watched = _Watched(argv, label, echo=self.echo, env=self.env,
                           detach=self.detach, matchers=matchers)
        self.watchers.append(watched)
        _ACTIVE.append(watched)
        return watched

    def establish(self):
        if _which("ssh") is None:
            raise ConnectError(_no_ssh_message())
        if self.gcloud and _which("gcloud") is None:
            raise ConnectError(_no_gcloud_message())
        self.local_port, self.remote_port = pick_ports(
            self.requested_local_port, self.requested_remote_port)

        # Under a scheduler the node binds where the tunnel can reach it, and
        # which address that is depends on how the last hop is built -- exactly
        # as it does for the viewer. Loopback on the compute node when the
        # tunnel goes INTO it; all interfaces when the login node forwards.
        bind = "0.0.0.0" if (self.srun is not None and self.bind_node) \
            else "127.0.0.1"
        launch = node_command_line(
            self.remote_command, self.remote_port, self.serve,
            allow_origin=self.allow_origin, plugins=self.plugins,
            dynamic=self.dynamic, node_id=self.node_id, manifest=self.manifest,
            host=bind)
        matchers = {"node": parse_node_announce}

        # The install is chained ahead of the launch in the SAME ssh -- one
        # login, one Duo prompt -- and it has its own budget: `self.timeout`
        # is how long a node may take to answer, and an install that ate half
        # of it would leave a node that was starting normally being called
        # dead. The deadline is therefore taken after the marker.
        install_line = _begin_install(self) if self.install else None
        if install_line:
            matchers["installed"] = parse_install_done
        mount_line = _begin_mount(self) if self.mount_command else None
        if mount_line:
            matchers["mounted"] = parse_mount_done
            matchers["readonly"] = parse_mount_readonly

        if self.srun is None:
            if not install_line and not mount_line:
                self._phase("tunneling")
            line = (install_prefixed(install_line, launch)
                    if install_line else launch)
            if mount_line:
                line = mount_prefixed(mount_line, line)
            self.primary = self._spawn(
                gcloud_node_ssh_argv(self.gcloud, self.local_port,
                                     self.remote_port, line,
                                     ssh_opts=self.ssh_opts)
                if self.gcloud else
                direct_ssh_argv(self.target, self.local_port, self.remote_port,
                                line, jump=self.jump, ssh_opts=self.ssh_opts),
                "node", matchers)
            if mount_line:
                _await_mount(self, self.primary, mount_line)
            if install_line:
                _await_install(self, self.primary, install_line)
            if install_line or mount_line:
                self._phase("tunneling")
        else:
            # No forward on this one: at the moment it opens, the job has not
            # been allocated and there is no host to point a forward at. The
            # announce is what names it, and the tunnel follows.
            remote = srun_command_line(self.srun, launch)
            if install_line:
                remote = install_prefixed(install_line, remote)
            self.primary = self._spawn(
                job_ssh_argv(self.target, remote,
                             jump=self.jump, ssh_opts=self.ssh_opts),
                "job", matchers)
            if install_line:
                _await_install(self, self.primary, install_line)
            self._phase("waiting_for_job")
            self.echo("  asking the scheduler for a node to serve the data "
                      "from; this can wait in the queue.")
        deadline = _now() + self.timeout

        announced = _wait_for_node(self.primary, deadline, echo=self.echo)
        if not announced:
            # Order matters: argparse's refusal contains "usage:" and a list of
            # subcommands, and reads enough like a shell's "not found" to be
            # caught by the wrong branch and given the wrong fix.
            stale = unsupported_remote_flag(self.primary.lines)
            if stale:
                raise _old_remote_hint(self.target, stale, self.remote_command,
                                       self.primary)
            # Also before the missing-command guess, and for the same reason:
            # a job that was never allocated never ran `plexora` at all, so
            # the PATH it would send somebody off to fix is not the problem.
            refusal = scheduler_refusal(self.primary.lines)
            if refusal:
                raise ConnectError(refusal, diagnosed=True)
            if not self.primary.alive and looks_like_missing_command(self.primary.lines):
                raise _missing_command_hint(self.remote_command, self.primary)
            raise ConnectError(
                f"The data node on {self.target} did not start.\n"
                + "\n".join(f"    {line}" for line in self.primary.tail())
            )
        self.token = announced["token"]

        if self.srun is not None:
            self.node = announced.get("hostname")
            if not self.node:
                raise ConnectError(
                    f"The data node started in a job on {self.target}, but did "
                    f"not say which machine it landed on -- so there is nothing "
                    f"to open a tunnel to.\n\nThat field was added to the "
                    f"announce line; the Plexora over there is older than it. "
                    f"Upgrade it, or switch this saved server off "
                    f"“run Plexora inside a job”."
                )
            self.echo(f"  The data node is on {self.node}:{self.remote_port}; "
                      f"opening the tunnel.")
            self._phase("tunneling")
            self._spawn(
                tunnel_ssh_argv(self.target, self.local_port, self.node,
                                self.remote_port,
                                user=split_target(self.target)[0],
                                bind_node=self.bind_node,
                                ssh_opts=self.ssh_opts),
                "tunnel")

        # Health rather than the announce alone: the announce is printed BEFORE
        # the server binds, and a node restoring a manifest full of masks reads
        # them first.
        self._phase("waiting_for_app")
        self.echo("  node announced; waiting for it to answer through the "
                  "tunnel...")
        try:
            # Every watcher, not just the first: under a scheduler the tunnel
            # is a second process, and it is the one that dies when a site
            # refuses ssh into a compute node.
            answered = _wait_for_health(
                f"{self.endpoint}/node/v1/health", deadline, self.watchers,
                echo=self.echo,
                # Every node route is guarded, `/health` included. Without this
                # the poll gets a 403 it cannot tell from a closed port, and
                # waits out the whole deadline against a node that is up.
                headers={NODE_TOKEN_HEADER: self.token})
        except _Retriable:
            # "the node process exited with code N" and nothing else. True, and
            # useless on its own -- the reason it exited is on its own pipe,
            # which that message does not carry. One failure, one account of
            # it, built in the one place that has the output.
            raise self._silent_node() from None
        if not answered:
            raise self._silent_node()

        self._register()
        return self

    def _silent_node(self):
        """Why a node that announced itself never answered.

        The announce is printed **before** the server binds (see
        server/node/app.py's serve_node), so "it announced" is not "it is
        listening" -- which makes this failure look contradictory unless the
        message says so.

        Its own output is the whole of the evidence and used to be thrown away
        here. A node that died after announcing has the reason on that pipe: an
        import error, a permissions problem, or `Address already in use` --
        which is a real possibility, because the remote port is picked at
        random out of the ephemeral range and never probed. On a busy login
        node with a hundred other users, that collides.
        """
        tail = "\n".join(f"    [{watched.label}] {line}"
                         for watched in self.watchers
                         for line in watched.tail(8))
        evidence = f"\n\nWhat it printed:\n{tail}" if tail.strip() else ""

        if self.node is not None:
            # Two processes, and the failure that matters here is the second
            # one: the forward into a compute node. A site can refuse that
            # while allowing everything up to it, and it fails silently -- so
            # the advice is the OTHER way of building the last hop, which is
            # the first thing worth trying.
            switch = (
                f"Some clusters drop forwards made from a login node into a "
                f"compute node. Turn OFF “Forward from the login node” for "
                f"this saved server and the tunnel goes into {self.node} "
                f"directly."
                if self.bind_node else
                f"Some clusters refuse ssh into a compute node. Turn ON "
                f"“Forward from the login node” for this saved server and the "
                f"tunnel is made from {self.target} instead."
            )
            return ConnectError(
                f"The data node is running on {self.node}, but nothing came "
                f"back through the tunnel to it within {self.timeout:g}s."
                + evidence
                + f"\n\n{switch}"
            )

        if not self.primary.alive:
            return ConnectError(
                f"The data node on {self.target} started and then stopped, "
                f"before it began answering on port {self.remote_port}."
                + evidence
                + f"\n\nIf that says the address is already in use, the port "
                  f"was taken by somebody else on that machine -- try again "
                  f"and a different one is picked."
            )
        return ConnectError(
            f"The data node did not answer on port {self.local_port} within "
            f"{self.timeout:g}s. It said it had started, but a node prints "
            f"that line before it binds, so it may still be loading."
            + evidence
            + f"\n\nTwo things this usually is. Either loading Plexora over "
              f"there is genuinely slower than {self.timeout:g}s -- a first "
              f"start off a shared filesystem can be -- in which case running "
              f"it once by hand warms the cache:\n"
              f"    ssh {self.target} '{self.remote_command} node serve "
              f"--dynamic --port {self.remote_port}'\n"
              f"Or that host does not allow the port forward this needs "
              f"(`AllowTcpForwarding`), which fails exactly like this: "
              f"silently, with nothing on any pipe."
        )

    def _register(self):
        """Record the node with the Plexora on THIS machine.

        Never fatal on its own terms -- but unlike the viewer layout there is
        nothing else this session is for, so a failure is reported and the
        caller decides. `browser_endpoint` is the same address as `endpoint`:
        the browser is on this machine too, which makes the direct path the
        only path and the fastest one.
        """
        if self.register is None:
            return None
        try:
            self.registered = self.register(
                self.node_name,
                self.endpoint,
                self.token,
                browser_endpoint=self.endpoint,
                managed_by=f"connect:{self.node_name}",
            )
        except Exception as exc:
            self.node_errors.append(f"{self.node_name}: {exc}")
            raise ConnectError(
                f"The data node on {self.target} started, but could not be "
                f"recorded here: {exc}") from exc
        self.echo(f"  Data node {self.node_name!r} is registered at "
                  f"{self.endpoint}")
        return self.registered

    def wait(self):
        if self.primary is not None:
            self.primary.process.wait()

    def stop(self):
        for watched in reversed(self.watchers):
            watched.stop()
            if watched in _ACTIVE:
                _ACTIVE.remove(watched)


def default_timeout(timeout, srun):
    """Seconds to wait, given that a queued job is not a failure."""
    if timeout is not None:
        return timeout
    return DEFAULT_SRUN_TIMEOUT if srun is not None else DEFAULT_TIMEOUT


def _attempt(target, *, datasource, remote_command, srun, bind_node, jump,
             ssh_opts, local_port, remote_port, timeout, data_dir, plugins,
             browser, echo, forwards=(), also_serve=(), local_serve=(),
             node_name=None, node_port=None, local_node=True, install=False):
    session = Session(
        target, datasource=datasource, remote_command=remote_command, srun=srun,
        bind_node=bind_node, jump=jump, ssh_opts=ssh_opts, local_port=local_port,
        remote_port=remote_port, timeout=timeout, data_dir=data_dir,
        plugins=plugins, echo=echo, forwards=forwards, also_serve=also_serve,
        local_serve=local_serve, node_name=node_name, node_port=node_port,
        local_node=local_node, install=install,
    )
    try:
        session.establish()
        echo("")
        for entry in session.data_nodes:
            echo(f"Data node {entry['name']!r} is serving to it.")
        echo(f"Plexora is available at {session.open_url}")
        echo("Leave this command running; press Ctrl+C to disconnect"
             + (" and end the job." if srun is not None else "."))
        if browser:
            _open_browser(session.open_url)

        session.wait()
        return 0
    finally:
        session.stop()


def connect_node(target, serve, *, name=None,
                 remote_command=DEFAULT_REMOTE_COMMAND, jump=None, ssh_opts=(),
                 local_port=None, remote_port=None, timeout=None, plugins=None,
                 allow_origin=None, echo=print, register=None, browser=False,
                 install=False):
    """Start a data node on `target`, forward it here, and register it.

    The layout where the viewer stays on this machine: the browser is here, the
    cell table and the project database are here, and the only thing on the far
    side is the pile of pixels nobody wants to copy. One ssh, one forward, and
    a registration into this machine's own nodes.json -- so the node is a
    permanent-looking address that happens to be a tunnel.

    `register` is injected rather than imported so this module stays loadable
    without the plexora package (see the module docstring). cli.py supplies the
    real one.
    """
    if _which("ssh") is None:
        raise SystemExit(_no_ssh_message())
    # The same budget the in-app node session gets, and for the same reason:
    # what is being waited for is an import off a shared filesystem, not a
    # network round trip.
    timeout = NODE_START_TIMEOUT if timeout is None else timeout
    local, remote = pick_ports(local_port, remote_port)
    name = name or f"{split_target(target)[1]}-node"

    launch = node_command_line(remote_command, remote, serve,
                               allow_origin=allow_origin, plugins=plugins)
    matchers = {"node": parse_node_announce}
    shim = install_line = None
    if install:
        # A stand-in with the attributes `_begin_install`/`_await_install`
        # read. This function drives its ssh by hand rather than through a
        # session object, and giving it one just to install would be a bigger
        # change than the shim it replaces.
        shim = _BareInstall(target, remote_command, jump, ssh_opts, echo)
        install_line = _begin_install(shim)
        launch = install_prefixed(install_line, launch)
        matchers["installed"] = parse_install_done
    argv = direct_ssh_argv(target, local, remote, launch, jump=jump,
                           ssh_opts=ssh_opts)
    echo(f"$ {' '.join(argv)}")
    watched = _Watched(argv, "node", echo=echo, matchers=matchers)
    if install_line:
        _await_install(shim, watched, install_line)
    _ACTIVE.append(watched)
    deadline = _now() + timeout

    try:
        announced = _wait_for_node(watched, deadline, echo=echo)
        if not announced:
            stale = unsupported_remote_flag(watched.lines)
            if stale:
                raise _old_remote_hint(target, stale, remote_command, watched)
            refusal = scheduler_refusal(watched.lines)
            if refusal:
                raise ConnectError(refusal, diagnosed=True)
            if not watched.alive and looks_like_missing_command(watched.lines):
                raise _missing_command_hint(remote_command, watched)
            raise ConnectError(
                f"The data node on {target} did not start.\n"
                + "\n".join(f"    {line}" for line in watched.tail())
            )

        # Health rather than the announce alone: the announce is printed BEFORE
        # the server binds, and a node asked to serve a raw label mask converts
        # it first, which takes minutes on a large one.
        echo("  Node announced; waiting for it to finish preparing its data...")
        if not _wait_for_health(f"http://127.0.0.1:{local}/node/v1/health",
                                deadline, [watched],
                                headers={NODE_TOKEN_HEADER: announced["token"]}):
            raise ConnectError(
                f"The data node did not answer on port {local} within "
                f"{timeout:g}s. A mask that has to be converted first can take "
                f"longer than this -- raise --timeout, or run "
                f"`plexora node prepare` on it once over there.")

        endpoint = f"http://127.0.0.1:{local}"
        if register is not None:
            register(name, endpoint, announced["token"])
        echo("")
        echo(f"Data node {name!r} is registered at {endpoint}")
        echo("Point a project's image or table at it from that project's Edit "
             "page.")
        echo("Leave this command running; press Ctrl+C to disconnect.")
        watched.process.wait()
        return 0
    finally:
        watched.stop()
        if watched in _ACTIVE:
            _ACTIVE.remove(watched)


def connect(target, datasource=None, *, remote_command=DEFAULT_REMOTE_COMMAND,
            srun=None, bind_node=False, jump=None, ssh_opts=(),
            local_port=None, remote_port=None, timeout=None, data_dir=None,
            plugins=None, browser=True, attempts=3, echo=print, forwards=(),
            also_serve=(), local_serve=(), node_name=None, node_port=None,
            local_node=True, install=False):
    """Run Plexora on `target`, tunnel to it, open it here. Returns an exit code.

    Best-effort by design. Every failure below ends with the printed
    instructions from `plexora --remote` as the fallback, because that path has
    one moving part (the user's own ssh) where this one has several.
    """
    if _which("ssh") is None:
        raise SystemExit(_no_ssh_message())

    timeout = default_timeout(timeout, srun)

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
                forwards=forwards, also_serve=also_serve,
                local_serve=local_serve, node_name=node_name,
                node_port=node_port, local_node=local_node, install=install,
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
