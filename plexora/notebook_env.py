"""Working out what kind of notebook this is, and therefore what URL works.

`PlexoraViewer` starts a server on 127.0.0.1 and shows it in an iframe. That
is correct exactly when the browser rendering the notebook is on the same
machine as the kernel -- local Jupyter, and (because it forwards ports for you)
VS Code Remote. Everywhere else it produces a URL pointing at the user's own
laptop, where nothing is listening, and the cell renders a blank box with no
error anywhere.

"Everywhere else" is most of the interesting places: JupyterHub, Open OnDemand,
Colab, a plain `jupyter lab` on a workstation the user ssh'd into. Each needs a
different URL, and each can be recognised from the environment, so the viewer
asks here instead of making the user pass `proxy=True` after finding out the
hard way that they needed to.

Two ideas do most of the work:

**A hosted URL is a PATH, not a host.** Under jupyter-server-proxy the viewer
is reachable at `<notebook prefix>proxy/<port>` on the notebook's OWN origin --
which is the origin holding the user's auth cookie, and the only one that will
be allowed to load. So the display base stays path-only and an iframe resolves
it. Writing in a hostname would be both wrong and unauthenticated.

**Colab is the exception that proves it.** `proxyPort()` returns a whole
separate `https://…googleusercontent.com` origin, which is why `join_display`
accepts a full origin at all and why `clean_prefix` refuses one.

Imports only `plexora._url`, and everything that touches an optional
third-party module does so behind `_module_available` inside a function -- this
is consulted on the import path of a notebook helper, and a hard dependency on
`jupyter_server` would make `import plexora` fail on a machine that has no
Jupyter at all.
"""

from __future__ import annotations

import importlib.util
import os
import sys

from plexora._url import is_full_origin, prefix_with_slash


def _module_available(name):
    """Whether `name` could be imported, without importing it. Never raises.

    `find_spec` alone is not enough, and its failure modes are exactly the ones
    that turn up in notebooks: it raises ValueError for a module already in
    sys.modules whose `__spec__` is None (dynamically created modules, some
    frozen ones, anything a hub's startup code injected), and
    ModuleNotFoundError when an intermediate package is missing. Every caller
    here is on the display path of a notebook cell, where an exception would
    replace the viewer with a traceback about introspection.
    """
    if name in sys.modules:
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


#: Variables that mean "the kernel is not on the machine holding the screen".
#: The scheduler ones are not padding: Open OnDemand runs Jupyter inside a
#: batch job and sets no hub variable of its own, so its job variables are the
#: only evidence available that a direct localhost URL cannot work.
REMOTE_ENV_VARS = (
    "SSH_CONNECTION",
    "SSH_CLIENT",
    "SSH_TTY",
    "SLURM_JOB_ID",
    "PBS_JOBID",
    "LSB_JOBID",
)

HUB_ENV_VARS = ("JUPYTERHUB_SERVICE_PREFIX", "JUPYTERHUB_USER", "JUPYTERHUB_API_URL")

#: Stands in for a port that has not been chosen yet, so `resolve_display` can
#: be asked its question BEFORE a server exists -- which is what lets the
#: sidecar cache be keyed on the answer. Same spelling as the placeholder
#: `plexora/proxy.py` hands jupyter-server-proxy, for one convention rather
#: than two.
PORT_PLACEHOLDER = "{port}"

#: Returned as the display base when only the Colab FRONTEND can answer, and
#: only once a real port exists. Every other case resolves to a string that a
#: port can simply be substituted into; Colab needs a round trip, so it gets a
#: sentinel and the caller makes that call after starting the server.
COLAB = "colab"


def looks_remote(env=None):
    env = os.environ if env is None else env
    return any(env.get(name) for name in REMOTE_ENV_VARS)


def in_hub(env=None):
    env = os.environ if env is None else env
    return any(env.get(name) for name in HUB_ENV_VARS)


def in_colab():
    return _module_available("google.colab")


def colab_origin(port):
    """The public origin Colab will proxy `port` on, or None.

    None is a normal outcome, not just an error path: `eval_js` runs Javascript
    in the notebook FRONTEND and waits for an answer, so it needs a browser
    actually connected to this kernel. Under "Run all", a reconnect, or a
    headless execution there is nobody to ask, and it hangs or raises. The
    caller falls back to Colab's own iframe helper, which does not need a
    round trip.
    """
    try:
        from google.colab.output import eval_js
    except Exception:
        return None
    try:
        origin = eval_js(f"google.colab.kernel.proxyPort({int(port)})")
    except Exception:
        return None
    if not origin or not is_full_origin(origin):
        return None
    return str(origin).rstrip("/")


def discover_jupyter_prefix(env=None, echo=print):
    """The notebook server's base_url (e.g. `/user/me/`), or None.

    JUPYTERHUB_SERVICE_PREFIX is asked first because a hub sets it in the
    kernel's own environment and it is definitive. Failing that, ask the
    jupyter_server library which servers are running on this machine -- which
    is how Open OnDemand is found, since it configures a random prefix per job
    and advertises it nowhere else.
    """
    env = os.environ if env is None else env
    from_hub = env.get("JUPYTERHUB_SERVICE_PREFIX")
    if from_hub:
        return prefix_with_slash(from_hub)

    if not _module_available("jupyter_server"):
        return None
    try:
        from jupyter_server.serverapp import list_running_servers

        servers = [entry for entry in list_running_servers() if entry.get("base_url")]
    except Exception:
        return None
    if not servers:
        return None
    if len(servers) > 1:
        echo(
            "Plexora found several running Jupyter servers and is assuming "
            f"{servers[0]['base_url']!r}. Pass base_url=... to plexora.view() "
            "if that is the wrong one."
        )
    return prefix_with_slash(servers[0]["base_url"])


def proxy_hint_if_missing(echo=print):
    """Mention jupyter-server-proxy if it looks absent. Never raises.

    Only a hint, and only ever a hint: on several hubs the kernel runs in a
    different environment from the notebook server, so the package being
    missing HERE says nothing about whether the proxy will work THERE. Turning
    that guess into an exception would break setups that were fine.
    """
    if not _module_available("jupyter_server_proxy"):
        echo(
            "Note: jupyter-server-proxy does not appear to be installed in this "
            "environment. If the viewer below does not load, install it in the "
            "environment running your Jupyter server:\n"
            "    pip install jupyter-server-proxy"
        )


def resolve_display(proxy="auto", base_url=None, port=PORT_PLACEHOLDER, env=None,
                    echo=print):
    """`(server_base_url, display_base)` -- how to mount, and how to reach it.

    The two differ whenever a proxy is involved, and conflating them is the
    original bug: the sidecar has to generate links under the path the proxy
    exposes it at, while the notebook has to load that same path against the
    hub's origin.

    `port` may be `PORT_PLACEHOLDER`, and normally is: the caller wants this
    answer before it has picked a port, so that the answer can decide whether
    an already-running sidecar will do. Both returned strings then carry the
    placeholder through for the caller to substitute. `COLAB` is the one
    display base that cannot work that way -- see the constant.

    First match wins, and the order encodes what beats what:

    1. An explicit `base_url` is an instruction; nothing overrides it.
    2. `proxy=False` is also an instruction -- the pre-existing default, kept
       working verbatim for every notebook that passes it today.
    3. Colab, whose proxy is a whole origin rather than a path (returns the
       `COLAB` sentinel; ask `colab_origin(real_port)` once you have one).
    4. A discoverable notebook prefix, but only with a reason to use it:
       `proxy=True`, or evidence the kernel is not local.
    5. Direct localhost. Deliberately last and deliberately not an error --
       this is plain local Jupyter, and it is also VS Code Remote, which
       forwards the port itself and would be broken by "helpfully" proxying.
    """
    env = os.environ if env is None else env

    if base_url is not None:
        if is_full_origin(base_url):
            # A caller who already knows the public origin (a bespoke reverse
            # proxy, a tunnel they set up) -- the server still mounts at root.
            return "", str(base_url).rstrip("/")
        mounted = f"{prefix_with_slash(base_url)}proxy/{port}"
        return mounted, mounted

    if proxy is False:
        return "", f"http://127.0.0.1:{port}"

    if in_colab():
        # Colab maps the whole port onto a subdomain root, so there is no path
        # to mount under -- the server stays at "/" and only the display
        # changes.
        return "", COLAB

    prefix = discover_jupyter_prefix(env, echo=echo)
    if prefix and (proxy is True or looks_remote(env) or in_hub(env)):
        proxy_hint_if_missing(echo=echo)
        mounted = f"{prefix}proxy/{port}"
        return mounted, mounted

    return "", f"http://127.0.0.1:{port}"
