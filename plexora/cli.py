"""Friendly command-line entry point for launching Plexora.

`plexora-server` remains the low-level sidecar command used by Jupyter proxy
integrations. This module backs the end-user `plexora` command: it starts the
same Waitress server, prints the URL, and opens a browser only when doing so
looks appropriate for the current environment.
"""

from __future__ import annotations

import argparse
import getpass
import os
import platform
import secrets
import socket
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import quote


HEADLESS_ENV_VARS = (
    "CI",
    "SLURM_JOB_ID",
    "PBS_JOBID",
    "LSB_JOBID",
    "SSH_CONNECTION",
    "SSH_CLIENT",
)

DEFAULT_PORT = 8000

#: Same default as connect.DEFAULT_REMOTE_COMMAND, and duplicated for the same
#: reason `_clean_base_url` is: this module has to parse arguments without the
#: plexora package being importable. tests/test_cli.py pins the two together.
DEFAULT_REMOTE_COMMAND = "plexora"

#: Set on the child of the one re-exec `python -m plexora --plugins` may need,
#: so a mistake in that logic loops zero times instead of forever.
REEXEC_ENV_VAR = "PLEXORA_CLI_REEXEC"


# A deliberate duplicate of plexora._url.clean_prefix, kept in sync by
# tests/test_url_helpers.py's parity test. This module must stay importable
# WITHOUT the plexora package: tests/test_cli.py loads it straight off disk via
# spec_from_file_location, and under a PyInstaller onefile build `plexora` is
# not on a path an importlib file loader could reach. Six lines is a cheaper
# price than either of those.
def _clean_base_url(base_url):
    if not base_url:
        return ""
    base_url = str(base_url).strip()
    if base_url == "/":
        return ""
    return "/" + base_url.strip("/")


def _public_host(host):
    return "127.0.0.1" if host in ("0.0.0.0", "::") else host


def browser_url(host, port, base_url="", datasource=None):
    path = _clean_base_url(base_url)
    if datasource:
        path = f"{path}/{quote(datasource.strip('/'), safe='')}"
    return f"http://{_public_host(host)}:{int(port)}{path or '/'}"


def should_open_browser(env=None, system=None, preference="auto"):
    """Whether the friendly CLI should open a browser.

    `preference` is one of:
    - "yes": explicit --browser
    - "no": explicit --no-browser
    - "auto": desktop when likely interactive, quiet on HPC/CI/SSH/headless
    """
    if preference == "yes":
        return True
    if preference == "no":
        return False

    env = os.environ if env is None else env
    system = platform.system() if system is None else system

    if any(env.get(name) for name in HEADLESS_ENV_VARS):
        return False
    if system in ("Windows", "Darwin"):
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def _wait_until_ready(url, timeout=30, token=None):
    """Poll until the server answers. `token` is not optional where one exists:
    a guarded server answers 403, urllib raises, and this would poll to the
    deadline and quietly never open the browser."""
    health_url = url.rstrip("/") + "/health"
    if token:
        health_url = f"{health_url}?token={quote(token)}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status < 500:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _open_browser_when_ready(open_url, health_url, *, token=None,
                             wait_fn=_wait_until_ready, open_fn=webbrowser.open):
    if wait_fn(health_url, token=token):
        open_fn(open_url)


def _schedule_browser_open(open_url, health_url, token=None):
    thread = threading.Thread(
        target=_open_browser_when_ready,
        args=(open_url, health_url),
        kwargs={"token": token},
        daemon=True,
    )
    thread.start()
    return thread


def version_string():
    """The installed version, or an honest admission that there isn't one.

    A source checkout that was never `pip install`ed has no distribution
    metadata, and `--version` blowing up with PackageNotFoundError would be a
    worse answer than saying so.
    """
    try:
        from importlib.metadata import version, PackageNotFoundError
    except ImportError:  # pragma: no cover - Python < 3.8
        return "unknown"
    try:
        return version("plexora")
    except PackageNotFoundError:
        return "unknown (source checkout)"


# -- port selection ------------------------------------------------------


def _probe_bind(host, port):
    """Bind exactly as Waitress will and report the port, or None if taken.

    SO_REUSEADDR is set off Windows because Waitress sets it too, and the probe
    has to ask the same question the real bind will: without it, a port left in
    TIME_WAIT by a Plexora that exited seconds ago reads as busy even though
    Waitress would take it happily. On Windows that option means something else
    entirely -- it lets a second socket steal a port that is actively bound --
    so setting it there would make every probe answer "free".
    """
    host = host or "127.0.0.1"
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return None
        return sock.getsockname()[1]


def _resolve_port(host, requested, *, explicit, log=print):
    """Which port to serve on, given what the user asked for.

    The interesting case is the one nobody asks for: a second `plexora` in
    another terminal. Waitress's own failure there is an OSError traceback from
    inside serve(), after the URL has already been printed -- so the user is
    told to open a page that belongs to the FIRST server. Moving to another
    port is both recoverable and what they meant, so an unrequested 8000 gives
    way; an explicitly requested port does not, because "--port 9000" is a
    statement about something else on the network expecting 9000.

    There is a small TOCTOU window between this probe and Waitress's bind. It
    is accepted: losing that race puts us back at today's behaviour, which is
    the OSError this function exists to make rare rather than impossible.
    """
    if requested == 0:
        chosen = _probe_bind(host, 0)
        if chosen is None:
            raise SystemExit(f"Could not obtain any free port on {host}.")
        return chosen

    if _probe_bind(host, requested) is not None:
        return requested

    if explicit:
        raise SystemExit(
            f"Port {requested} on {host} is already in use.\n"
            f"Another Plexora may already be running there -- try opening "
            f"http://{_public_host(host)}:{requested}/ first.\n"
            f"Otherwise pass a different --port, or --port 0 to pick a free one."
        )

    chosen = _probe_bind(host, 0)
    if chosen is None:
        raise SystemExit(f"Could not obtain any free port on {host}.")
    log(f"Port {requested} is in use; using {chosen} instead.")
    return chosen


# -- `python -m plexora` and the one thing it cannot do late -------------


def _plugins_argument(argv):
    """The value of --plugins in `argv`, or None if it isn't there.

    Hand-scanned rather than run through build_parser(), because this has to
    answer the question BEFORE argparse could reject an unrelated argv (a
    subcommand, a typo) -- and because a parse error at this point would be
    reported from the wrong place entirely.
    """
    for index, item in enumerate(argv):
        if item == "--plugins":
            return argv[index + 1] if index + 1 < len(argv) else None
        if item.startswith("--plugins="):
            return item.split("=", 1)[1]
    return None


def bootstrap_program(plugins):
    """A `python -c` program that pins PLEXORA_PLUGINS before any import.

    The env write has to happen in a program that runs BEFORE `plexora` is in
    sys.modules, and there is no entry point that qualifies: `plexora` is
    generated as `from plexora.cli import main`, which imports the package to
    reach the submodule, and `-m plexora` imports it to find `__main__`. Both
    therefore register Blueprints before main() has seen a single argument. A
    `-c` string is the one shape that can get in front of that.

    The value is embedded as a literal rather than passed in the child's
    environment because the environment cannot carry it: `--plugins ""` is a
    deliberate core-only build, and on Windows setting a variable to "" DELETES
    it, so the child would read "unset" -- which means activate everything, the
    exact opposite. `repr` of a str is always a valid, fully escaped Python
    literal, so a plugin list cannot break out of it.
    """
    return (
        "import os, sys; "
        f"os.environ['PLEXORA_PLUGINS'] = {str(plugins)!r}; "
        "from plexora.cli import main; "
        "raise SystemExit(main())"
    )


def _relaunch(command):  # pragma: no cover - exercised through injection
    """Replace this process with `command`, or the nearest thing available.

    os.execv is the honest call on POSIX: same pid, same terminal, no orphan.
    Windows has no real exec -- the CRT emulation starts a NEW process and
    exits this one, at which point cmd.exe decides the command finished and
    prints a fresh prompt while a server is still running and still writing to
    that console. So Windows waits on a child and passes its exit code up
    instead; one extra process is a better outcome than a terminal that lies.
    """
    if os.name == "nt":
        import subprocess

        raise SystemExit(subprocess.run(command).returncode)
    os.execv(command[0], command)


def maybe_reexec_for_plugins(argv=None, *, modules=None, environ=None, relaunch=_relaunch):
    """Re-launch once so that `--plugins` means anything at all.

    Blueprint registration happens inside the first `import plexora`, and every
    entry point has already done that import by the time main() runs -- the
    console script because it reaches `main` through `plexora.cli`, and
    `python -m plexora` because `-m` imports the package to find `__main__`.
    So writing PLEXORA_PLUGINS in main() is always too late: it lands after the
    decision it is supposed to make. `--plugins` silently did nothing from
    either command. Everything else the CLI sets (the data path, the base URL,
    the host and port) is resolved on demand or overridden on app.config
    afterwards, so this flag is the whole of the problem.

    The cure is one re-exec into a `-c` program that sets the variable first
    (`bootstrap_program`). It costs a second interpreter startup, which is why
    it is conditional: nothing happens unless `--plugins` was actually passed
    AND the environment does not already say the same thing.

    Returns False -- meaning "carry on in this process" -- when the package is
    genuinely not imported yet, for an argv without --plugins, for an
    environment that already agrees, and for the child of a re-exec that has
    already happened.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    modules = sys.modules if modules is None else modules
    environ = os.environ if environ is None else environ

    if environ.get(REEXEC_ENV_VAR):
        return False
    if "plexora" not in modules:
        return False
    requested = _plugins_argument(argv)
    if requested is None:
        return False
    if environ.get("PLEXORA_PLUGINS") == requested:
        return False

    environ[REEXEC_ENV_VAR] = "1"
    relaunch([sys.executable, "-c", bootstrap_program(requested), *argv])
    return True


# -- remote / SSH ---------------------------------------------------------


def remote_host(fqdn=None, hostname=None):
    """The name a user on their laptop should ssh to.

    getfqdn() is preferred because a bare hostname off a cluster node is rarely
    resolvable from outside, but it is not trusted blindly: on a machine with
    an unhelpful /etc/hosts it returns "localhost", which would print a tunnel
    command that connects to the user's own laptop.
    """
    fqdn = socket.getfqdn() if fqdn is None else fqdn
    if fqdn and "." in fqdn and not fqdn.lower().startswith("localhost"):
        return fqdn
    return hostname or socket.gethostname()


def scheduler_topology(env=None, hostname=None):
    """`(scheduler, node, login_host)` for the batch job we are inside.

    All three are None off a cluster. `node` is where Plexora is actually
    running and `login_host` is the only machine on the way to it that accepts
    connections from the outside world -- which is why a single -L forward is
    the wrong instruction to print here, and knowing this is what lets the CLI
    print the right one instead of a paragraph of caveats.

    `login_host` may still come back None: the variable that carries it is a
    convention, not a guarantee, and some sites submit from a host that isn't
    the one you ssh into. Callers print a placeholder rather than guessing.
    """
    env = os.environ if env is None else env
    hostname = hostname or socket.gethostname()

    if env.get("SLURM_JOB_ID"):
        return ("slurm", env.get("SLURMD_NODENAME") or hostname,
                env.get("SLURM_SUBMIT_HOST") or None)
    if env.get("PBS_JOBID"):
        return ("pbs", hostname, env.get("PBS_O_HOST") or None)
    if env.get("LSB_JOBID"):
        hosts = (env.get("LSB_HOSTS") or "").split()
        return ("lsf", hosts[0] if hosts else hostname,
                env.get("LSB_SUB_HOST") or None)
    return (None, None, None)


#: Emitted on its own line so `plexora connect --srun` can learn which compute
#: node the scheduler actually granted. Nothing else can tell it: the job is
#: submitted from the login node and the allocation is not known until the job
#: starts.
ANNOUNCE_PREFIX = "[plexora-remote]"


def remote_instructions(user, host, port, base_url="", *, login_host=None,
                        node=None, bind_node=False):
    """The lines `plexora --remote` prints, as a pure function.

    `node` is the switch between the two shapes: set means we are on a compute
    node behind a login node and the user needs two hops, unset means the host
    they ssh to is the host Plexora is on.
    """
    prefix = _clean_base_url(base_url)
    local_url = f"http://localhost:{port}{prefix or '/'}"
    login = login_host or "<login-host>"
    lines = [f"{ANNOUNCE_PREFIX} node={node or host} port={port}", ""]

    if node and bind_node:
        lines += [
            f"Plexora is running on compute node {node}, bound to 0.0.0.0:{port}.",
            "From your own machine, run:",
            f"  ssh -N -L {port}:{node}:{port} {user}@{login}",
            f"then open  {local_url}",
            "",
            "Note: --bind-node makes this port reachable from anywhere on the",
            "cluster's internal network for as long as Plexora runs.",
        ]
    elif node:
        lines += [
            f"Plexora is running on compute node {node}, bound to 127.0.0.1:{port}.",
            "From your own machine, run:",
            f"  ssh -N -J {user}@{login} {user}@{node} -L {port}:127.0.0.1:{port}",
            f"then open  {local_url}",
            "",
            "If your cluster refuses ssh into a compute node, restart Plexora",
            "with --bind-node for a login-node forward instead.",
        ]
    else:
        lines += [
            f"Plexora is running on {host}, bound to 127.0.0.1:{port}.",
            "From your own machine, run:",
            f"  ssh -N -L {port}:127.0.0.1:{port} {user}@{host}",
            f"then open  {local_url}",
        ]

    if node and login_host is None:
        lines += ["", f"Replace {login} with the cluster login node you ssh into "
                      "(or restart with --login-host)."]
    return lines


#: What Open OnDemand calls the proxy door that STRIPS the prefix before
#: forwarding -- the one a root-serving app like this needs. Its sibling
#: `/node/` passes the path through untouched, which is right for Jupyter (it
#: mounts under a matching base_url) and a guaranteed 404 for Plexora.
OOD_MOUNT = "/rnode"


def ood_mount(node, port):
    return f"{OOD_MOUNT}/{node}/{int(port)}"


def ood_instructions(node, port, token, base_url, datasource=None,
                     portal="<your-OnDemand-host>"):
    """The lines `plexora --ood` prints, as a pure function.

    The portal's own address is a placeholder and cannot be anything else: a
    compute node knows the cluster's name for itself, but nothing on it records
    which public hostname the OnDemand web front end is served under, and
    guessing one would print a link that fails in a way nobody could debug. The
    user has it in their address bar.
    """
    prefix = _clean_base_url(base_url) or ood_mount(node, port)
    path = f"{prefix}/{quote(datasource.strip('/'), safe='')}" if datasource else f"{prefix}/"
    return [
        f"Plexora is running on {node}, bound to 0.0.0.0:{port} so Open OnDemand "
        f"can proxy it.",
        "Open this in the browser your OnDemand session is already in:",
        f"  https://{portal}{path}?token={token}",
        "",
        f"Replace {portal} with the host the OnDemand portal itself is open at.",
        "",
        "While it runs, the port is reachable from the cluster's internal "
        "network. The token in that URL is what keeps other accounts out, so "
        "treat the link as a password.",
    ]


# -- working out where we are ---------------------------------------------
#
# `plexora.view()` in a notebook has always asked plexora/notebook_env.py which
# kind of environment it is in and produced a URL that works there. The bare
# `plexora` command never did, so the same user in the same JupyterHub had to
# know to type `--base-url /user/me/` -- and the failure mode for not knowing
# was a printed localhost URL that is simply a lie, pointing at the laptop
# rather than the machine the server is on. Everything below wires that same
# ladder into the plain command. It only ever fills in flags the user did not
# type: every one of them still wins.


#: Same list as notebook_env.REMOTE_ENV_VARS, duplicated for the same reason
#: `_clean_base_url` is -- this module has to load without the plexora package.
#: tests/test_cli.py pins the two copies together.
REMOTE_ENV_VARS = (
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "SSH_TTY",
    "SLURM_JOB_ID",
    "PBS_JOBID",
    "LSB_JOBID",
)

#: Same spelling as notebook_env.PORT_PLACEHOLDER, and duplicated for the same
#: reason. `resolve_display` is asked its question before a port exists, so the
#: mount it returns carries this until one does.
PORT_PLACEHOLDER = "{port}"

#: Flags that are themselves an answer to "how will this be reached?". Any one
#: of them means the user has already decided and detection would be
#: second-guessing them.
DETECTION_OVERRIDES = (
    "--ood",
    "--remote",
    "-r",
    "--bind-node",
    "--base-url",
    "--host",
    "--login-host",
)

DETECTION_LABELS = {
    "ood": "an Open OnDemand session",
    "proxy": "a Jupyter server that can proxy this port",
    "colab": "Google Colab",
    "origin": "a configured public origin",
    "remote": "a machine reached over SSH",
}


def looks_remote(env=None):
    env = os.environ if env is None else env
    return any(env.get(name) for name in REMOTE_ENV_VARS)


def should_detect(argv, env=None, *, disabled=False):
    """Whether a bare `plexora` should work out its own environment.

    Only when nothing else has already said. The environment variable counts
    as much as the flags do: the Docker image sets `PLEXORA_HOST=0.0.0.0` and
    means it, and a container that started proxying itself under a Jupyter
    prefix because one happened to be discoverable inside it would be a
    genuinely mystifying regression.
    """
    if disabled:
        return False
    env = os.environ if env is None else env
    if env.get("PLEXORA_HOST"):
        return False
    return not any(
        str(item).split("=", 1)[0] in DETECTION_OVERRIDES for item in argv or ()
    )


def detect_environment(resolver=None, echo=print):
    """A `notebook_env.Resolved`, or None if this machine cannot say.

    Lazy and defensive on purpose. This runs on the startup path of the plain
    `plexora` command, which has to keep working on a machine with no Jupyter,
    no network, and a half-installed environment -- so every failure in here
    means "we learned nothing" and never a traceback in front of somebody who
    only wanted a local viewer.
    """
    if resolver is None:
        try:
            from plexora.notebook_env import resolve_display as resolver
        except Exception:
            return None
    try:
        return resolver(echo=echo)
    except Exception:
        return None


def apply_detection(args, resolved, *, remote_env=False):
    """Set the flags the user did not type. Returns the route's name, or None.

    Only the decisions that have to be made BEFORE a port exists are made
    here, because one of them is the bind address and the port probe needs
    that. The proxy mount names its own port, so it is finished off by
    `detected_base_url` once `_resolve_port` has answered.
    """
    kind = getattr(resolved, "kind", None)
    if kind == "ood":
        args.ood = True
        return "ood"
    if kind in ("proxy", "explicit"):
        return "proxy"
    if kind == "origin":
        args.base_url = str(resolved.display)
        return "origin"
    if kind == "colab":
        return "colab"
    # "direct" on a machine the user ssh'd into. The URL is not wrong, it is
    # just unreachable from where they are sitting, which is what --remote
    # exists to explain.
    if remote_env:
        args.remote = True
        return "remote"
    return None


def detected_base_url(resolved, port):
    """The proxy mount with the port that was actually taken written into it."""
    return str(resolved.server_base).replace(PORT_PLACEHOLDER, str(int(port)))


def jupyter_prefix_from_mount(mount):
    """The notebook's own base_url, back out of `<prefix>proxy/<port>`.

    rpartition rather than a split: a hub prefix may legitimately contain the
    word `proxy` (a named server called that is allowed), and only the last
    occurrence is the one we appended.
    """
    head, separator, _tail = str(mount).rpartition("proxy/")
    return head if separator else None


def ood_node_from_mount(mount):
    """The node name out of `/rnode/<host>/<port>`.

    Preferred over the scheduler's answer because it is the spelling the
    portal itself routes: OOD put that host into the notebook's prefix, and a
    node whose `$SLURMD_NODENAME` differs from it (short name versus FQDN, a
    site with two naming schemes) would produce a URL the portal cannot map.
    """
    parts = str(mount or "").strip("/").split("/")
    if len(parts) >= 2 and parts[0] == OOD_MOUNT.strip("/"):
        return parts[1]
    return None


def hub_instructions(mount, datasource=None, origin=None):
    """The lines a Jupyter-proxied `plexora` prints, as a pure function.

    The path is the whole answer and the origin deliberately is not. A hub
    sets no variable naming the address a browser reaches it at
    (JUPYTERHUB_API_URL is the internal one, routinely a different host), so
    printing a guess would send the user to a URL that fails for a reason
    nothing on this machine could explain. They have the right origin in their
    address bar already.
    """
    base = str(mount).rstrip("/")
    path = f"{base}/{quote(datasource.strip('/'), safe='')}" if datasource else f"{base}/"
    lines = [
        "This kernel is not on the machine holding your screen, so Plexora is "
        "reached through your Jupyter server rather than at localhost.",
        "",
        "Open this in the browser tab your notebook is already in:",
    ]
    if origin:
        lines.append(f"  {str(origin).rstrip('/')}{path}")
    else:
        lines += [
            f"  {path}",
            "",
            "That is a path, not a whole address: put it after the host your "
            "notebook is already open at, e.g.",
            f"  https://jupyter.your-institution.edu{path}",
        ]
    lines += [
        "",
        "This route needs jupyter-server-proxy installed in the environment "
        "running the JUPYTER SERVER -- not necessarily this kernel's. A 404 is "
        "what its absence looks like.",
    ]
    return lines


def colab_instructions():
    """Why the CLI cannot finish the job in Colab, and what does.

    Colab's proxy hands back a whole `…googleusercontent.com` origin, and the
    only way to learn it is `google.colab.kernel.proxyPort()`, which runs
    Javascript in the notebook FRONTEND and waits for an answer. A shell has no
    frontend to ask, so this is one place where the CLI genuinely cannot
    configure itself and says so instead of printing a URL that cannot work.
    """
    return [
        "This looks like Google Colab, where a server on 127.0.0.1 is not "
        "reachable from the notebook and only the frontend knows the address "
        "that is.",
        "",
        "Run this in a cell instead:",
        "  import plexora",
        "  plexora.view()",
    ]


#: Words that mean "do this instead of starting a server". Recognised only as
#: the FIRST argument, which is how anyone types them -- so a project may still
#: be called "config" as long as it is opened from the picker rather than as
#: `plexora config`.
SUBCOMMANDS = ("where", "config", "connect", "node")


def split_command(argv):
    """`(subcommand, remaining_argv)`, or `(None, argv)` for a plain launch."""
    if argv and argv[0] in SUBCOMMANDS:
        return argv[0], list(argv[1:])
    return None, list(argv)


def build_parser(command=None):
    """The parser for ONE invocation shape.

    Subcommands and the bare `plexora [datasource]` form cannot share a parser.
    An argparse subparsers action is itself a positional, so with an optional
    positional beside it argparse gives the subparsers action first refusal on
    the only argument -- `plexora tonsil` dies with "invalid choice: 'tonsil'"
    -- and when a subcommand IS matched, the trailing `datasource` action then
    runs with nothing left to consume and quietly resets it to None, throwing
    away what `plexora connect host tonsil` just parsed. Splitting on argv[0]
    in `split_command` costs three lines and has neither failure.
    """
    if command == "where":
        parser = argparse.ArgumentParser(
            prog="plexora where",
            description="Print the resolved data directory and how it was chosen.",
        )
        parser.add_argument(
            "--data-dir-only",
            action="store_true",
            help="Print only the data directory, for scripting.",
        )
        return parser

    if command == "config":
        parser = argparse.ArgumentParser(
            prog="plexora config",
            description="Show or change where Plexora keeps its data.",
        )
        config_subs = parser.add_subparsers(dest="config_command")
        config_subs.add_parser("show", help="Print the current settings file.")
        config_set = config_subs.add_parser("set", help="Change a setting.")
        config_set.add_argument(
            "key", choices=("data-dir", "shared-dirs", "mask-output"))
        config_set.add_argument(
            "value",
            help="A path for data-dir; a comma-separated list for shared-dirs "
                 "(pass an empty string to clear); `beside` or `project` for "
                 "mask-output, which is where a converted segmentation mask is "
                 "written -- next to the mask it came from (the default, so a "
                 "second project and a data node reuse one conversion), or "
                 "under the project's own directory.",
        )
        return parser

    if command == "connect":
        return _build_connect_parser()

    if command == "node":
        return _build_node_parser()

    parser = argparse.ArgumentParser(
        prog="plexora",
        description="Start the Plexora local image viewer server.",
        epilog="Other commands: " + ", ".join(f"plexora {name}" for name in SUBCOMMANDS)
               + ".  Run one with --help for its own options.",
    )
    parser.add_argument(
        "datasource",
        nargs="?",
        help="Optional datasource/project name to open directly.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"plexora {version_string()}",
    )
    # Honours the same override the Docker image and run.py already use, so
    # every entry point answers to one variable.
    parser.add_argument("--host", default=os.environ.get("PLEXORA_HOST", "127.0.0.1"))
    # default=None rather than 8000 so main() can tell "the user wants 8000"
    # from "the user did not say", which is the difference between failing on a
    # busy port and quietly moving off it.
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"Port to serve on (default {DEFAULT_PORT}, or the next free one "
             f"if that is taken). Pass 0 to always pick a free port.",
    )
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--plugins",
        default=None,
        help=(
            "Comma-separated plugins to activate. Omit for all installed; pass "
            "an empty string for a core-only build."
        ),
    )
    parser.add_argument(
        "-r",
        "--remote",
        action="store_true",
        help="Serving on a machine you reached over SSH: print the exact "
             "tunnel command to run from your own computer.",
    )
    parser.add_argument(
        "--login-host",
        default=None,
        help="With --remote on a cluster, the login node to tunnel through "
             "when the scheduler does not say.",
    )
    parser.add_argument(
        "--bind-node",
        action="store_true",
        help="With --remote on a cluster, bind all interfaces so the login "
             "node can forward to this one. For sites that refuse ssh into a "
             "compute node; the port becomes visible cluster-internally.",
    )
    parser.add_argument(
        "--ood",
        action="store_true",
        help="Serving from inside an Open OnDemand session: bind all "
             "interfaces, mount under the portal's /rnode/ proxy and print a "
             "token-protected URL to open through the portal.",
    )
    parser.add_argument(
        "--also-serve",
        action="append",
        default=[],
        metavar="KIND:ID=PATH",
        help="Also run a data node beside this viewer, serving one resource, "
             "e.g. --also-serve table:cells=/scratch/cells.h5ad. Repeat for "
             "each. Started as a second process and stopped with this one; "
             "`plexora connect --also-serve` sets it up from the other end and "
             "registers it for you.",
    )
    parser.add_argument(
        "--node-port", type=int, default=8642,
        help="Port for the data node started by --also-serve.",
    )
    parser.add_argument(
        "--node-allow-origin",
        default=None,
        metavar="ORIGIN",
        help="A viewer origin the data node will accept browser requests "
             "from, so tiles can be fetched directly rather than relayed.",
    )
    parser.add_argument(
        "--no-detect",
        action="store_true",
        help="Do not work out the environment. Plexora otherwise recognises "
             "JupyterHub, Open OnDemand, Colab and an SSH session on its own "
             "and configures the URL to match; pass this to serve plain "
             "localhost regardless.",
    )
    browser_group = parser.add_mutually_exclusive_group()
    browser_group.add_argument(
        "--browser",
        action="store_true",
        help="Open the Plexora URL in a browser even if the environment looks headless.",
    )
    browser_group.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser; only print the URL and serve.",
    )
    return parser


def _build_node_parser():
    """Define `plexora node`, without importing the server that runs it.

    Same split as `connect` below and for the same reason: build_parser() runs
    in a standalone-loaded cli.py, and the node app pulls in Flask, tifffile
    and the adapters.
    """
    node = argparse.ArgumentParser(
        prog="plexora node",
        description="Serve data files to a Plexora viewer running elsewhere. "
                    "A node has no viewer and no project registry of its own -- "
                    "it hands out bytes from the files it is pointed at.",
    )
    subs = node.add_subparsers(dest="node_command")
    serve = subs.add_parser(
        "serve",
        help="Serve one or more files to a Plexora viewer.",
        description="Serve one or more files to a Plexora viewer. Every "
                    "--serve names one resource as kind:id=path, where kind is "
                    "image, segmentation or table, and id is the name a project "
                    "will point at.",
    )
    serve.add_argument(
        "--serve",
        action="append",
        default=[],
        metavar="KIND:ID=PATH",
        help="A resource to serve, e.g. --serve table:cells=/data/cells.h5ad. "
             "Repeat for each one.",
    )
    serve.add_argument("--host", default=os.environ.get("PLEXORA_NODE_HOST", "127.0.0.1"),
                       help="Address to bind. 127.0.0.1 by default; pass "
                            "0.0.0.0 to accept connections from other machines.")
    serve.add_argument("--port", type=int, default=8642)
    serve.add_argument(
        "--token",
        default=os.environ.get("PLEXORA_NODE_TOKEN"),
        help="Shared secret the viewer authenticates with. One is generated "
             "and printed if you do not supply one.",
    )
    serve.add_argument("--node-id", default=None,
                       help="Stable name for this node, used in cache keys. "
                            "Generated per launch if unset.")
    serve.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help="A viewer origin allowed to read this node from a browser, e.g. "
             "http://127.0.0.1:8000. Repeat for each. Needed only when the "
             "browser talks to this node directly rather than through the "
             "viewer.",
    )
    serve.add_argument("--plugins", default=None,
                       help="Comma-separated plugin names whose file-side work "
                            "this node should be able to run. Defaults to every "
                            "installed plugin.")
    serve.add_argument(
        "--dynamic",
        action="store_true",
        help="Let the viewer add and remove resources while this node runs, "
             "instead of only serving what --serve names. Needed for picking "
             "files from the viewer's data forms; whoever holds the token can "
             "then read any file this account can, so use it on your own "
             "machine rather than a shared one.",
    )
    serve.add_argument(
        "--manifest",
        default=None,
        metavar="PATH",
        help="A file recording what this node ends up serving, re-read at "
             "startup so a project reopened in a later session finds its files "
             "again. Holds paths on this machine and nothing about any project.",
    )

    node_connect = subs.add_parser(
        "connect",
        help="Start a data node on another machine and register it here.",
        description="Run `plexora node serve` on a remote host over SSH, "
                    "forward it to this machine, and register it -- so a "
                    "project opened in the Plexora running HERE can read an "
                    "image that never leaves the cluster. Run this on your own "
                    "computer.",
    )
    node_connect.add_argument("target", help="[user@]host to ssh into.")
    node_connect.add_argument(
        "--serve",
        action="append",
        default=[],
        metavar="KIND:ID=PATH",
        help="A resource on the REMOTE host to serve, e.g. "
             "--serve image:tonsil=/scratch/tonsil.ome.tif. Repeat for each.",
    )
    node_connect.add_argument(
        "--name", default=None,
        help="What to call this node here. Defaults to the host's name.",
    )
    node_connect.add_argument(
        "--remote-command", default=None,
        help="How to invoke Plexora on the remote host. An environment "
             "prefix is enough, e.g. \"/home/you/miniconda3/envs/plexora\".",
    )
    node_connect.add_argument("-J", "--jump", default=None,
                              help="ssh -J jump host to reach the target through.")
    node_connect.add_argument("--ssh-opt", action="append", default=[],
                              metavar="KEY=VALUE", help="Extra ssh -o option.")
    node_connect.add_argument("--port", type=int, default=None,
                              help="Local port for the tunnel (default: free).")
    node_connect.add_argument("--remote-port", type=int, default=None,
                              help="Port to use on the remote host.")
    node_connect.add_argument(
        "--timeout", type=float, default=None,
        help="Seconds to wait for the node to answer. Raise it when a "
             "segmentation mask has to be converted first.",
    )
    node_connect.add_argument("--plugins", default=None,
                              help="Plugins the node should be able to run.")

    prepare = subs.add_parser(
        "prepare",
        help="Convert a label mask into something a node can serve.",
        description="Convert a label mask into a tiled, pyramidal label image. "
                    "Masks out of a segmentation pipeline are usually one "
                    "full-resolution plane, which no tile route can serve at a "
                    "zoomed-out level. This is the same conversion an import "
                    "runs, on a machine that has no viewer. `node serve` does "
                    "it for you when it has to -- run this first to get the "
                    "wait over with, to convert somewhere other than beside "
                    "the mask, or to choose outlines.",
    )
    prepare.add_argument("source", help="The mask to convert.")
    prepare.add_argument("output", nargs="?", default=None,
                         help="Where to write it. Defaults to beside the "
                              "original, under the name `node serve` looks for "
                              "-- so preparing ahead of time needs no path "
                              "here and none in the --serve that follows. Name "
                              "one when the mask's own directory is read-only.")
    prepare.add_argument(
        "--outlines",
        action="store_true",
        help="Bake cell boundaries into the file instead of serving filled "
             "labels. Rarely wanted -- the viewer derives outlines at tile-load "
             "time from a filled mask, which is faster and smaller.",
    )
    return node


def _run_node_connect(args):
    """Bring a remote data node here, and write it into this machine's registry."""
    from plexora.connect import connect_node

    def register(name, endpoint, token):
        from plexora import nodes as node_api

        node_api.register_node(name, endpoint, token=token)

    return connect_node(
        args.target,
        args.serve,
        name=args.name,
        remote_command=args.remote_command or DEFAULT_REMOTE_COMMAND,
        jump=args.jump,
        ssh_opts=args.ssh_opt,
        local_port=args.port,
        remote_port=args.remote_port,
        timeout=args.timeout,
        plugins=args.plugins,
        register=register,
    )


def _run_node(args):
    from plexora.server.node.app import NodeStartupError, prepare_mask, serve_node

    command = getattr(args, "node_command", None)
    if command == "prepare":
        try:
            prepare_mask(args.source, args.output, outline=args.outlines)
        except NodeStartupError as exc:
            raise SystemExit(str(exc))
        return 0
    if command == "connect":
        if not args.serve:
            raise SystemExit(
                "Name at least one resource to serve, e.g.\n"
                "  plexora node connect me@hpc --serve image:tonsil=/scratch/t.ome.tif")
        return _run_node_connect(args)
    if command != "serve":
        # argparse cannot make a subcommand required without also making the
        # error unreadable, so this says the one thing that is actionable.
        print("Usage: plexora node serve --serve kind:id=path [...]")
        print("       plexora node connect <host> --serve kind:id=path [...]")
        print("       plexora node prepare <mask> [<output>]")
        return 2
    plugins = None
    if args.plugins is not None:
        plugins = [name.strip() for name in args.plugins.split(",") if name.strip()]
    try:
        serve_node(
            args.serve,
            token=args.token,
            host=args.host,
            port=args.port,
            node_id=args.node_id,
            allow_origins=args.allow_origin,
            plugins=plugins,
            dynamic=args.dynamic,
            manifest=args.manifest,
        )
    except NodeStartupError as exc:
        raise SystemExit(str(exc))
    return 0


def _build_connect_parser():
    """Define `plexora connect`, without importing the module that runs it.

    The flags live here and the logic lives in plexora/connect.py, imported
    lazily by _run_connect. That split is not tidiness: build_parser() runs in
    a standalone-loaded cli.py where `import plexora.connect` would fail, and
    argparse definitions are pure data anyway.
    """
    connect = argparse.ArgumentParser(
        prog="plexora connect",
        description="Start Plexora on a remote host, tunnel to it, and open a "
                    "browser here. Run this on your OWN computer, not on the "
                    "remote host.",
    )
    connect.add_argument(
        "target",
        help="[user@]host to ssh into, or the name of a server saved with "
             "--save (or in Settings > Remote servers).",
    )
    connect.add_argument(
        "datasource",
        nargs="?",
        help="Optional datasource/project name to open directly.",
    )
    connect.add_argument(
        "--remote-command",
        # None rather than "plexora" so a saved profile's own value can be
        # told apart from the default and is not silently overwritten by it.
        default=None,
        help="How to invoke Plexora on the remote host (default "
             f"\"{DEFAULT_REMOTE_COMMAND}\"). Use this when it is not on a "
             "non-interactive PATH. An environment prefix is enough -- "
             "\"/home/you/miniconda3/envs/plexora\" -- or give a full "
             "command, e.g. \"conda run --no-capture-output -n imaging plexora\".",
    )
    connect.add_argument(
        "--save",
        default=None,
        metavar="NAME",
        help="Remember this connection under NAME, so next time "
             "`plexora connect NAME` is the whole command. Saved servers are "
             "shared with Settings > Remote servers. No password is stored.",
    )
    connect.add_argument(
        "--srun",
        default=None,
        metavar="ARGS",
        help="Treat the target as a SLURM login node and run Plexora inside a "
             "job, e.g. --srun \"-p interactive -t 4:00:00 --mem 16G\".",
    )
    connect.add_argument(
        "--bind-node",
        action="store_true",
        help="With --srun, forward from the login node instead of ssh-ing into "
             "the compute node. For sites that refuse the latter.",
    )
    connect.add_argument(
        "--forward",
        action="append",
        default=[],
        metavar="[LOCAL:]REMOTE",
        help="Forward another port from the remote host, e.g. --forward 8642 "
             "for a `plexora node serve` running beside the viewer. Repeat for "
             "each. Only needed when your BROWSER has to reach the node "
             "directly; the viewer reaches it over there without any of this.",
    )
    connect.add_argument(
        "-J",
        "--jump",
        default=None,
        help="ssh -J jump host to reach the target through.",
    )
    connect.add_argument(
        "--ssh-opt",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Extra ssh -o option; repeatable. Keys, ports and usernames are "
             "usually better placed in ~/.ssh/config.",
    )
    connect.add_argument(
        "--port", type=int, default=None,
        help="Local port to serve the tunnel on (default: a free one).",
    )
    connect.add_argument(
        "--remote-port", type=int, default=None,
        help="Port to use on the remote host (default: a free-looking high one).",
    )
    connect.add_argument(
        "--timeout", type=float, default=None,
        help="Seconds to wait for Plexora to answer (default 60, or 900 with "
             "--srun, where the job may sit in a queue).",
    )
    connect.add_argument("--data-dir", default=None,
                         help="Data directory to use ON THE REMOTE HOST.")
    connect.add_argument("--plugins", default=None,
                         help="Plugins to activate on the remote host.")
    connect.add_argument(
        "--no-browser", action="store_true",
        help="Set up the tunnel and print the URL, but do not open a browser.",
    )
    # -- data spread across two machines ---------------------------------
    connect.add_argument(
        "--also-serve",
        action="append",
        default=[],
        metavar="KIND:ID=PATH",
        help="A file on the REMOTE host to serve as a data node beside the "
             "viewer, e.g. --also-serve table:cells=/scratch/cells.h5ad. "
             "Started, forwarded and registered automatically. Repeat for each.",
    )
    connect.add_argument(
        "--local-serve",
        action="append",
        default=[],
        metavar="KIND:ID=PATH",
        help="A file on THIS machine to serve to the remote viewer, e.g. "
             "--local-serve table:cells=~/study/cells.h5ad. For the layout "
             "where the images are on the cluster and the cell table never "
             "left your laptop. Repeat for each.",
    )
    connect.add_argument(
        "--no-local-node",
        action="store_true",
        help="Do not run a data node on this computer. One is started for "
             "every connection by default, with nothing to serve until you "
             "pick something: it is what lets the viewer's data forms offer "
             "files from HERE, which they otherwise cannot reach at all.",
    )
    connect.add_argument(
        "--node-name",
        default=None,
        help="What to call the data nodes this connection registers. Defaults "
             "to the saved server's name, or the remote host's.",
    )
    connect.add_argument(
        "--node-port", type=int, default=None,
        help="Port for the remote data node (default: a free-looking high one).",
    )
    return connect


def _browser_preference(args):
    if args.browser:
        return "yes"
    if args.no_browser:
        return "no"
    return "auto"


def _run_where(args):
    from plexora import paths

    if args.data_dir_only:
        print(paths.data_root())
        return 0
    for line in paths.describe():
        print(line)
    return 0


def _run_config(args):
    from plexora import paths

    if args.config_command == "set":
        settings = paths.read_settings()
        if args.key == "data-dir":
            settings["data_dir"] = str(Path(args.value).expanduser().resolve())
        elif args.key == "mask-output":
            choice = str(args.value).strip().lower()
            if choice not in paths.MASK_OUTPUT_CHOICES:
                # Refused before writing, so a typo cannot leave a settings file
                # holding a value that silently reads back as the default.
                print(f"mask-output is one of: "
                      f"{', '.join(paths.MASK_OUTPUT_CHOICES)}")
                return 2
            settings["mask_output"] = choice
        else:
            entries = [part.strip() for part in args.value.split(",")]
            settings["shared_dirs"] = [
                str(Path(part).expanduser().resolve()) for part in entries if part
            ]
        paths.write_settings(settings)
        # The resolution is cached per process, so a `set` immediately followed
        # by a read in the same process must not answer from before the write.
        paths.reset()
        print(f"Wrote {paths.settings_path()}")

    settings = paths.read_settings()
    if not settings:
        print(f"No settings recorded ({paths.settings_path()} does not exist).")
    else:
        print(f"{paths.settings_path()}:")
        for key, value in sorted(settings.items()):
            print(f"  {key} = {value}")
    return 0


def connect_kwargs(args, profile=None):
    """What `connect()` should run with, given the flags and a saved profile.

    A typed flag always beats the saved value. The case that decides it is the
    ordinary one: a server described once, then opened at a different project
    every day -- so `plexora connect hpc other-study` has to mean the saved
    connection with a different datasource, not a refusal to combine them.
    """
    forwards = list(args.forward or ())
    ssh_opts = list(args.ssh_opt or ())
    if profile is not None:
        forwards = forwards or list(profile.forwards)
        ssh_opts = ssh_opts or list(profile.ssh_opts)

    def saved(name, default=None):
        return getattr(profile, name, default) if profile is not None else default

    return {
        "datasource": args.datasource or saved("datasource"),
        "remote_command": (args.remote_command or saved("remote_command")
                           or DEFAULT_REMOTE_COMMAND),
        # `srun` is three-valued: None is "no scheduler", "" is "srun with the
        # site's defaults". `or` would collapse those two into one.
        "srun": args.srun if args.srun is not None else saved("srun"),
        "bind_node": bool(args.bind_node or saved("bind_node", False)),
        "jump": args.jump or saved("jump"),
        "ssh_opts": ssh_opts,
        "local_port": args.port,
        "remote_port": args.remote_port,
        "timeout": args.timeout,
        "data_dir": args.data_dir or saved("data_dir"),
        "plugins": args.plugins if args.plugins is not None else saved("plugins"),
        "browser": not args.no_browser,
        "forwards": forwards,
        "also_serve": list(args.also_serve or ()) or list(saved("serve", ())),
        "local_serve": (list(args.local_serve or ())
                        or list(saved("local_serve", ()))),
        # The saved server's own name when it has one: the nodes this session
        # registers are named from it, and so is the manifest the node on this
        # machine keeps -- which is what lets a project reopened next week find
        # its local files again.
        "node_name": (args.node_name or saved("node_name")
                      or (profile.name if profile is not None else None)),
        "node_port": args.node_port,
        "local_node": not args.no_local_node,
    }


def _saved_remote(name):
    """A saved profile of that name, or None. Never raises.

    An unrecognised name is simply a hostname -- the older spelling, still the
    only one most people use -- so "not found" is an ordinary answer rather
    than an error. A remotes.json that cannot be read is the same answer: a
    connection typed out in full must not depend on a registry file.
    """
    try:
        from plexora.server.models import remotes

        return remotes.find(str(name))
    except Exception:
        return None


def _save_remote(name, target, kwargs):
    """Record this connection under `name`, before trying it.

    Before, not after: a first attempt that fails on the remote command is
    exactly when having the rest of it saved is worth most, because the fix is
    to change one field and press connect again.
    """
    from plexora.server.models.remotes import Remote, save

    save(Remote(
        name=name,
        target=target,
        remote_command=kwargs["remote_command"],
        datasource=kwargs["datasource"],
        data_dir=kwargs["data_dir"],
        plugins=kwargs["plugins"],
        srun=kwargs["srun"],
        bind_node=kwargs["bind_node"],
        jump=kwargs["jump"],
        ssh_opts=tuple(kwargs["ssh_opts"]),
        forwards=tuple(kwargs["forwards"]),
        serve=tuple(kwargs["also_serve"]),
        local_serve=tuple(kwargs["local_serve"]),
        node_name=kwargs["node_name"],
    ))
    print(f"Saved as {name!r}. Next time: plexora connect {name}")


def _run_connect(args):
    from plexora.connect import connect

    profile = _saved_remote(args.target)
    target = profile.target if profile is not None else args.target
    kwargs = connect_kwargs(args, profile)
    if profile is not None:
        print(f"Using saved server {profile.name!r} ({target}).")
    if args.save:
        _save_remote(args.save, target, kwargs)

    code = connect(target, **kwargs)
    if not args.save and profile is None:
        print(f"\nTip: add  --save NAME  to this command and next time "
              f"`plexora connect NAME` is all you need.")
    return code


def node_serve_argv(serve, port, allow_origin=None, python=None):
    """The command that starts the data node running beside this viewer.

    `python -m plexora` rather than the `plexora` console script: this process
    knows which interpreter it is, and a PATH that could not find `plexora`
    non-interactively is exactly the environment this is most likely to be
    started in.

    No `--token` on it. The node generates one and prints it, which keeps it
    out of `ps` on a shared login node -- the single reason the announce line
    exists at all.
    """
    argv = [python or sys.executable, "-m", "plexora", "node", "serve",
            "--host", "127.0.0.1", "--port", str(int(port))]
    for entry in serve or ():
        argv += ["--serve", entry]
    if allow_origin:
        argv += ["--allow-origin", allow_origin]
    return argv


def _start_side_node(serve, port, allow_origin=None):
    """Run a data node as a child of this viewer, relaying what it says.

    Relaying matters more than it looks: this process's stdout is the far end
    of an ssh pipe when `plexora connect --also-serve` started us, and the
    node's announce line is how that end learns the node's port and token.
    Swallowing the child's output would leave the connection with a node it
    could not register.
    """
    import subprocess

    argv = node_serve_argv(serve, port, allow_origin)
    print(f"Starting a data node: {' '.join(argv)}")
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )

    def pump():
        for line in process.stdout:
            print(line.rstrip("\r\n"), flush=True)

    threading.Thread(target=pump, daemon=True).start()
    return process


def _stop_side_node(process):
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _verify_detected_proxy(mount, port):  # pragma: no cover - needs jupyter_server
    """Warn if the notebook server will not actually proxy this port.

    Best effort and silent about everything it cannot prove -- see
    `notebook_env.verify_proxy_route`, which is where the judgement lives.
    """
    prefix = jupyter_prefix_from_mount(mount)
    if not prefix:
        return
    try:
        from plexora.notebook_env import verify_proxy_route

        verify_proxy_route(prefix, port)
    except Exception:
        pass


def _print_remote_instructions(args, port):
    """Work out this machine's place in the world and say how to reach it."""
    _scheduler, node, detected_login = scheduler_topology()
    for line in remote_instructions(
        getpass.getuser(),
        remote_host(),
        port,
        args.base_url or "",
        login_host=args.login_host or detected_login,
        node=node,
        bind_node=args.bind_node,
    ):
        # flush, every line: under --srun the task's stdout is a pipe rather
        # than the pty an ordinary `ssh -t` would give it, so Python block
        # -buffers it. The announce line is a few hundred bytes and the server
        # loop below never writes another, so an unflushed one sits in the
        # buffer for the life of the job -- and the waiting side, which has no
        # other way to learn the node and port, times out having been told
        # nothing.
        print(line, flush=True)
    print("", flush=True)


def main(argv=None):
    command, rest = split_command(list(sys.argv[1:] if argv is None else argv))

    # Only for the serve invocation. `plexora connect --plugins X` also carries
    # the flag, but that one is an instruction for the REMOTE host and has
    # nothing to say about which Blueprints this process registered.
    if command is None and maybe_reexec_for_plugins(rest):
        return 0  # not reached: _relaunch replaces or supersedes this process

    args = build_parser(command).parse_args(rest)

    # Handled before anything sets PLEXORA_DATA_PATH, so `where` reports the
    # rule that a plain `plexora` would actually follow rather than one this
    # invocation just installed.
    if command == "where":
        return _run_where(args)
    if command == "config":
        return _run_config(args)
    if command == "connect":
        return _run_connect(args)
    if command == "node":
        return _run_node(args)

    # Before anything reads a flag: fill in the ones the user did not type
    # from what this machine can be seen to be. Gated so that it only ever
    # happens when nothing else has already answered the question.
    detected = None
    detected_kind = None
    if should_detect(rest, disabled=args.no_detect):
        detected = detect_environment()
        if detected is not None:
            detected_kind = apply_detection(args, detected,
                                            remote_env=looks_remote())
    if detected_kind:
        print(f"Detected {DETECTION_LABELS[detected_kind]}; configuring the "
              f"URL to match (--no-detect turns this off).")

    # They answer different questions -- --remote prints an SSH tunnel to a
    # port only you can reach, --ood publishes one the portal reaches for you
    # -- and combining them would print two contradictory sets of directions.
    if args.ood and args.remote:
        raise SystemExit(
            "--ood and --remote cannot be combined: --ood serves through the "
            "OnDemand portal, --remote through an SSH tunnel. Pick one."
        )

    # --bind-node is the whole point of the flag: the login node cannot forward
    # to a port that only listens on the compute node's loopback. --ood needs
    # the same thing for the same reason -- the portal's web host connects to
    # this node over the network.
    host = "0.0.0.0" if (args.remote and args.bind_node) or args.ood else args.host
    if args.remote and not args.bind_node and _public_host(host) != "127.0.0.1":
        print(f"Warning: --host {host} with --remote; the printed tunnel still "
              f"targets 127.0.0.1. Use --bind-node if you meant to expose the "
              f"port on this machine's network.")
    if args.ood and args.host not in ("127.0.0.1", "0.0.0.0", "::"):
        print(f"Warning: --ood serves on 0.0.0.0; ignoring --host {args.host}.")

    port = _resolve_port(host, DEFAULT_PORT if args.port is None else args.port,
                         explicit=args.port is not None)

    # The jupyter-server-proxy mount names the port, so it can only be written
    # now. Asking the notebook server whether it will really proxy that port
    # costs one loopback request and turns a silent 404 into a sentence.
    if detected_kind == "proxy":
        args.base_url = detected_base_url(detected, port)
        _verify_detected_proxy(args.base_url, port)

    # Composed only now, because the mount path has to name the port that was
    # actually taken -- which is not necessarily the one that was asked for.
    # An explicit --base-url wins: it is the escape hatch for a site whose
    # portal spells this door differently.
    ood_node = ood_token = None
    if args.ood:
        _scheduler, ood_node, _login = scheduler_topology()
        if detected_kind == "ood":
            ood_node = ood_node_from_mount(detected.server_base) or ood_node
        ood_node = ood_node or socket.gethostname()
        if args.base_url is None:
            args.base_url = ood_mount(ood_node, port)
        ood_token = secrets.token_urlsafe(16)
        os.environ["PLEXORA_AUTH_TOKEN"] = ood_token

    # Set only for an explicit --data-dir. There is deliberately no default
    # written here any more: plexora.paths resolves on demand, so the CLI no
    # longer has to guess the answer before the app imports -- which is what
    # made this the only entry point that got the right directory.
    if args.data_dir:
        os.environ["PLEXORA_DATA_PATH"] = str(Path(args.data_dir).expanduser())
    if args.base_url is not None:
        os.environ["PLEXORA_BASE_URL"] = args.base_url
    if args.plugins is not None:
        os.environ["PLEXORA_PLUGINS"] = args.plugins

    from waitress import serve
    from plexora import app, paths, _clean_base_url as app_clean_base_url
    from plexora._resources import worker_threads

    if args.base_url is not None:
        app.config["PLEXORA_BASE_URL"] = app_clean_base_url(args.base_url)
    # Overridden here as well as in the environment for the same reason the
    # base URL is: create_app() ran during `import plexora`, which for the
    # console script happens before main() gets to see a single argument.
    if ood_token:
        app.config["PLEXORA_AUTH_TOKEN"] = ood_token

    # Printed before the URL, because a first-time user reading this is about
    # to import data and the one thing they will want later is where it went.
    notice = paths.first_run_notice()
    if notice:
        print(notice)

    health_url = browser_url(host, port, args.base_url)
    url = browser_url(host, port, args.base_url, args.datasource)
    if args.ood:
        # A browser on THIS node (X11, VNC) wants the bare address: the mount
        # path exists only on the portal's side of the proxy, and the guard
        # exempts nothing, so the token has to ride along.
        health_url = browser_url(host, port)
        url = f"{browser_url(host, port, '', args.datasource)}?token={ood_token}"

    if args.remote:
        _print_remote_instructions(args, port)
    if args.ood:
        # Not the browser_url line: that address is this node's own loopback
        # plus the mount path, which the app does not serve there -- the whole
        # point of /rnode/ is that the portal strips the prefix on the way in.
        for line in ood_instructions(ood_node, port, ood_token, args.base_url,
                                     args.datasource):
            print(line)
        print("")
        print(f"Serving Plexora on {ood_node}:{port}")
    elif detected_kind == "proxy":
        # Same reasoning as --ood: the URL that works is a path on the hub's
        # origin, and this machine's loopback address is not part of it.
        for line in hub_instructions(args.base_url, args.datasource):
            print(line)
        print("")
        print(f"Serving Plexora on {_public_host(host)}:{port}")
    else:
        if detected_kind == "colab":
            for line in colab_instructions():
                print(line)
            print("")
        print(f"Serving Plexora at {url}")

    # --remote and --ood both mean nobody is sitting at this machine, so a
    # browser here would open on the wrong desktop -- but an explicit --browser
    # still wins, for the case where "remote" is a workstation with a screen.
    # A detected proxy route is the same situation with a different URL: what
    # works there is a path on the hub, which webbrowser cannot open.
    preference = _browser_preference(args)
    if (args.remote or args.ood or detected_kind in ("proxy", "colab")) \
            and preference == "auto":
        preference = "no"
    if should_open_browser(preference=preference):
        print("Opening browser...")
        _schedule_browser_open(url, health_url, ood_token)
    elif preference == "auto":
        print("Browser auto-open skipped: headless environment detected.")

    # Started after the URL is printed and before the server blocks, so its
    # announce line is already on its way back down the ssh pipe by the time
    # anything is waiting for it. Stopped with this process: a node left
    # running would hold a port and a file handle on a login node forever.
    side_node = None
    if args.also_serve:
        side_node = _start_side_node(args.also_serve, args.node_port,
                                     args.node_allow_origin)

    try:
        serve(
            app,
            host=host,
            port=port,
            max_request_body_size=1073741824000000,
            max_request_header_size=85899345920000,
            # See server_cli for why this is wider than the core count.
            threads=worker_threads(),
        )
    finally:
        _stop_side_node(side_node)


if __name__ == "__main__":
    main()
