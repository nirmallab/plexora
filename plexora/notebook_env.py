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
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

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

#: What an Open OnDemand notebook's own prefix looks like: `/node/<host>/<port>/`.
#: Recognising it is what tells us we are on OOD -- the portal sets no
#: environment variable of its own, and this prefix is served to the kernel by
#: the very portal that would have to proxy us.
#:
#: OOD offers two proxy doors to a compute node, and which one an app needs is
#: decided by where the app serves from:
#:
#: - `/node/<host>/<port>/` forwards the request path UNSTRIPPED, so the app has
#:   to mount itself under that prefix. Jupyter does (it is started with a
#:   matching base_url), which is why the notebook itself arrives this way.
#: - `/rnode/<host>/<port>/` STRIPS the prefix before forwarding. That is
#:   Plexora: the Flask app always serves at root and uses its base URL only to
#:   generate links.
#:
#: Both are stock (`node_uri` / `rnode_uri` in `ood_portal.yml`), so a site that
#: serves Jupyter through `/node/` has the reverse proxy on and near-certainly
#: has `/rnode/` too. Note that jupyter-server-proxy is irrelevant on either
#: door -- see `resolve_display`.
OOD_NODE_RE = re.compile(r"^/node/(?P<host>[^/]+)/(?P<port>\d+)/$")


class Resolved(NamedTuple):
    """Everything the environment decides about one viewer's URL.

    `bind_host` travels with the URL rather than being worked out separately
    because the two answers have to agree: the OOD route is only reachable if
    the sidecar binds an address the portal's web host can connect to, while
    every other route depends on it staying on loopback. Two functions deciding
    that independently could disagree; one ladder cannot.

    `kind` names which rule matched, for callers that must treat the routes
    differently -- `verify_proxy_route` applies to "proxy" only, and the token
    that protects a non-loopback bind applies to "ood" only.
    """

    server_base: str
    display: str
    bind_host: str = "127.0.0.1"
    kind: str = "direct"


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


def verify_proxy_route(prefix, port, echo=print):
    """Ask the notebook SERVER whether it will really proxy `port`. Never raises.

    `proxy_hint_if_missing` can only look at the kernel's environment, which on
    a hub is routinely not the server's -- so it stays silent whenever the two
    differ, which is exactly when the proxied URL is about to 404. That silence
    is how a wrong URL got displayed with no explanation anywhere.

    This asks the only thing that can answer: the server itself, over its own
    loopback address, for the very path we are about to hand the browser. A 404
    is Jupyter's own error handler replying, meaning no proxy handler is
    registered there. Anything else -- a timeout, a refused connection, no
    matching server entry (the hub case, where `list_running_servers()` cannot
    see a server owned by another process) -- proves nothing, so it says
    nothing. A warning that fires when it does not know would be the old bug
    with the sign flipped.
    """
    if not _module_available("jupyter_server"):
        return
    try:
        from jupyter_server.serverapp import list_running_servers

        entry = next(
            (
                item
                for item in list_running_servers()
                if item.get("base_url")
                and prefix_with_slash(item["base_url"]) == prefix
                and item.get("url")
            ),
            None,
        )
    except Exception:
        return
    if entry is None:
        return

    # Not prefix_with_slash: `url` is a full origin plus the prefix
    # (`http://localhost:8888/user/me/`), which that helper rejects by design.
    url = f"{str(entry['url']).rstrip('/')}/proxy/{port}/config"
    token = entry.get("token")
    if token:
        url = f"{url}?token={urllib.parse.quote(str(token))}"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            status = getattr(response, "status", None)
    except urllib.error.HTTPError as exc:
        status = exc.code
    except Exception:
        return

    if status == 404:
        echo(
            "Note: your Jupyter server does not proxy arbitrary ports, so the "
            "URL below will not load. jupyter-server-proxy has to be installed "
            "in the environment running the JUPYTER SERVER -- not the one "
            "running this kernel -- and the session restarted afterwards. If "
            "that environment is not yours to change, pass proxy=False and "
            "reach the port another way (e.g. an SSH tunnel)."
        )


def resolve_display(proxy="auto", base_url=None, port=PORT_PLACEHOLDER, env=None,
                    echo=print):
    """A `Resolved` -- how to mount, how to reach it, and what to bind.

    Mount and display differ whenever a proxy is involved, and conflating them
    is the original bug: the sidecar has to generate links under the path the
    proxy exposes it at, while the notebook has to load that same path against
    the hub's origin.

    `port` may be `PORT_PLACEHOLDER`, and normally is: the caller wants this
    answer before it has picked a port, so that the answer can decide whether
    an already-running sidecar will do. Both returned strings then carry the
    placeholder through for the caller to substitute. `COLAB` is the one
    display base that cannot work that way -- see the constant.

    First match wins, and the order encodes what beats what:

    1. An explicit `base_url` is an instruction; nothing overrides it. It means
       "this is my Jupyter prefix, proxy me under it", so it keeps the
       jupyter-server-proxy form even on OOD -- it is the escape hatch for a
       site where rule 4 guesses wrong.
    2. `proxy=False` is also an instruction -- the pre-existing default, kept
       working verbatim for every notebook that passes it today.
    3. Colab, whose proxy is a whole origin rather than a path (returns the
       `COLAB` sentinel; ask `colab_origin(real_port)` once you have one).
    4. Open OnDemand, recognised by the shape of its own prefix. We mount under
       the portal's OTHER door, `/rnode/<host>/<our port>`, because that one
       strips the prefix and this app serves at root; `/node/` would hand Flask
       a path it has no route for. Nothing needs to be installed for this: the
       portal proxies the node directly, so jupyter-server-proxy -- which would
       have to be in the Jupyter SERVER's environment, typically an
       admin-controlled module nobody here can change -- never enters into it.
       The host spelling is taken from the prefix rather than from the
       environment because that is precisely the spelling OOD itself routes.
       This binds 0.0.0.0: the portal's web host connects to the node over the
       network, so loopback is unreachable from it. The caller protects that
       with a token.
    5. A discoverable notebook prefix, but only with a reason to use it:
       `proxy=True`, or evidence the kernel is not local. This is the
       JupyterHub route, and it does need jupyter-server-proxy.
    6. Direct localhost. Deliberately last and deliberately not an error --
       this is plain local Jupyter, and it is also VS Code Remote, which
       forwards the port itself and would be broken by "helpfully" proxying.
    """
    env = os.environ if env is None else env
    direct = Resolved("", f"http://127.0.0.1:{port}", "127.0.0.1", "direct")

    if base_url is not None:
        if is_full_origin(base_url):
            # A caller who already knows the public origin (a bespoke reverse
            # proxy, a tunnel they set up) -- the server still mounts at root.
            return Resolved("", str(base_url).rstrip("/"), "127.0.0.1", "origin")
        mounted = f"{prefix_with_slash(base_url)}proxy/{port}"
        return Resolved(mounted, mounted, "127.0.0.1", "explicit")

    if proxy is False:
        return direct

    if in_colab():
        # Colab maps the whole port onto a subdomain root, so there is no path
        # to mount under -- the server stays at "/" and only the display
        # changes.
        return Resolved("", COLAB, "127.0.0.1", "colab")

    # Asked once and reused: discovery prints when it has to choose between
    # several running servers, and asking twice would print that twice.
    prefix = discover_jupyter_prefix(env, echo=echo)

    if prefix and proxy in ("auto", True):
        on_ood = OOD_NODE_RE.match(prefix)
        if on_ood:
            mounted = f"/rnode/{on_ood['host']}/{port}"
            return Resolved(mounted, mounted, "0.0.0.0", "ood")

    if prefix and (proxy is True or looks_remote(env) or in_hub(env)):
        proxy_hint_if_missing(echo=echo)
        mounted = f"{prefix}proxy/{port}"
        return Resolved(mounted, mounted, "127.0.0.1", "proxy")

    return direct
