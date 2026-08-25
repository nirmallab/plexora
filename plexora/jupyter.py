import atexit
import html
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

from plexora._url import join_display
from plexora.datasource import register_datasource, register_anndata_datasource
from plexora.notebook_env import (
    COLAB,
    PORT_PLACEHOLDER,
    colab_origin,
    resolve_display,
    verify_proxy_route,
)


_SERVERS = {}


def _default_data_dir():
    """Where a notebook viewer keeps its data when the caller names nowhere.

    This used to be `Path(__file__).parent / "data"` -- i.e. inside the
    installed package. Under a real install that is site-packages: read-only
    for a system or conda install, destroyed by `pip install -U`, and invisible
    to `pip uninstall`. The one resolver decides now, exactly as it does for
    the CLI, so a notebook and a terminal see the same projects.
    """
    from plexora import paths

    return paths.data_root()


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ServerStartError(RuntimeError):
    """The sidecar process could not be started, with a reason worth reading."""


def _wait_until_ready(port, timeout=30, process=None, token=None):
    """Block until the sidecar answers, or explain why it never will.

    `process` is what makes this useful. Without it a child that died on its
    first line -- the overwhelmingly common case being a `plexora` that is not
    importable from the interpreter running the notebook -- was indistinguishable
    from one that was merely slow, so the notebook cell sat there for the full
    30 seconds and then reported a timeout, which is the one explanation that
    was not true.

    `token` is not optional politeness: a token-protected sidecar answers 403
    to an unauthenticated probe, urllib raises on that, and the loop would
    swallow it and keep retrying until the deadline -- reporting "did not
    become ready" about a server that was ready the whole time.
    """
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/config"
    if token:
        url = f"{url}?token={urllib.parse.quote(token)}"
    while time.time() < deadline:
        if process is not None and process.poll() is not None:
            raise ServerStartError(
                f"The Plexora server process exited with code "
                f"{process.returncode} before it was ready.\n"
                f"The usual cause is that `{sys.executable}` cannot import "
                f"plexora -- install it into the environment running this "
                f"kernel, e.g. `pip install -e .` from a source checkout."
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return
        except Exception:
            time.sleep(0.25)
    raise ServerStartError(f"Plexora server did not become ready on port {port}")


def _needs_token(host):
    """Whether a sidecar bound to `host` has to be token-protected.

    Loopback is reachable only from this machine, which is the situation every
    notebook viewer was built for and is left exactly as it was. Any other bind
    puts the viewer on a network -- on a cluster, one where every account
    holder can reach it -- so it does not go up unauthenticated.
    """
    return str(host) not in ("127.0.0.1", "localhost", "::1")


def _server_key(data_dir, base_url_template, plugins, host):
    """What makes two viewers able to share one sidecar.

    `base_url_template`, not the base URL: in proxy mode the URL contains the
    port, which is freshly chosen on every call, so keying on it meant the
    lookup could never hit and each `plexora.view()` in a notebook started
    another server. Harmless when proxying was opt-in and rare; not harmless
    now that hosted notebooks reach it by default.

    `host` is part of it because a loopback sidecar cannot serve a viewer that
    resolved to the Open OnDemand route, however identical the rest looks.
    """
    return (str(Path(data_dir).expanduser().resolve()), base_url_template, plugins, host)


#: Filename, under the data root, where running sidecars are recorded.
#:
#: `_SERVERS` above is a module-level dict, so it dies with the interpreter that
#: filled it -- and that is the whole problem this file exists to fix. A Jupyter
#: kernel that restarts without running its atexit handlers (an interrupt, a
#: hard restart, a crash) leaves its sidecar running and unreachable: the next
#: `view()` finds an empty `_SERVERS`, starts a SECOND server, and the two then
#: compete for the same cores while each re-reads every channel from cold. On a
#: two-core allocation that is the difference between a viewer and a 502.
#:
#: A file is the only thing that outlives a kernel, so this is where a new
#: kernel looks before spawning anything.
SIDECAR_REGISTRY_FILENAME = "sidecars.json"

#: Sidecars this process adopted rather than spawned, keyed like `_SERVERS`.
#: Kept separate because the values in `_SERVERS` are Popen handles: we have no
#: handle for a process another kernel started, and `_cleanup_servers` must
#: never try to terminate one it does not own.
_ADOPTED = {}


def _registry_path():
    from plexora import paths

    return paths.data_root() / SIDECAR_REGISTRY_FILENAME


def _registry_key(key):
    """`_server_key`'s tuple as something JSON can use as an object key."""
    return json.dumps([None if part is None else str(part) for part in key])


def _read_registry():
    """The registry, or {} for anything unreadable.

    A damaged or absent file means "nothing to reuse", never an error: the
    worst case is spawning a server we could have shared, which is exactly
    today's behaviour.
    """
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_registry(data):
    """Replace the registry in one step, or give up quietly.

    Same temp-file-and-rename as the settings file, for the same reason: a
    reader in another kernel sees the whole previous file or the whole new one,
    never the empty window `open(path, "w")` leaves. Failure is swallowed --
    a read-only or full data root must not stop a viewer opening.
    """
    path = _registry_path()
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        pass


def _process_alive(pid):
    """Whether `pid` still exists. True whenever that cannot be established.

    Only a pre-filter -- the HTTP probe below is what actually decides -- so
    the bias is deliberate: answering "yes" wrongly costs one wasted probe,
    while answering "no" wrongly discards a healthy server.

    Never `os.kill` on Windows. There, Python's os.kill ignores the signal for
    anything but the two console events and calls TerminateProcess, so the
    POSIX "send signal 0 to test liveness" idiom would kill the very process it
    is asking about.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _sidecar_alive(port, token, timeout=2):
    """Whether a sidecar we can use is answering on `port`.

    Probes /health rather than /config because /health is documented to do no
    work at all -- a server that is merely saturated still answers it, and
    must not be mistaken for a dead one and duplicated.

    A wrong token raises (403) and therefore reads as "not ours", which is the
    right answer: it means something else has the port.
    """
    url = f"http://127.0.0.1:{int(port)}/health"
    if token:
        url = f"{url}?token={urllib.parse.quote(token)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status < 400
    except Exception:
        return False


def _record_sidecar(key, port, base_url, token, pid):
    data = _read_registry()
    data[_registry_key(key)] = {
        "port": int(port),
        "base_url": base_url,
        "token": token,
        "pid": int(pid),
        # Kernels other than the one that spawned this server and are now
        # relying on it. See _cleanup_servers for what it is for.
        "adopters": [],
        "started": time.time(),
    }
    _write_registry(data)


def _adopt_sidecar(key):
    """`(port, base_url, token)` of a live sidecar from another kernel, or None.

    Prunes as it goes: an entry that fails its probe is dropped, so the file
    cannot accumulate one dead port per kernel restart.

    Registers this process as an adopter, which is what stops the kernel that
    spawned the server from terminating it out from under us on its way out.
    """
    registry_key = _registry_key(key)
    data = _read_registry()
    entry = data.get(registry_key)
    if not isinstance(entry, dict) or not entry.get("port"):
        return None

    port, token = entry["port"], entry.get("token")
    if not (_process_alive(entry.get("pid")) and _sidecar_alive(port, token)):
        data.pop(registry_key, None)
        _write_registry(data)
        return None

    adopters = [pid for pid in (entry.get("adopters") or []) if _process_alive(pid)]
    if os.getpid() not in adopters:
        adopters.append(os.getpid())
    entry["adopters"] = adopters
    data[registry_key] = entry
    _write_registry(data)
    return int(port), entry.get("base_url") or "", token


def _forget_sidecar(key):
    data = _read_registry()
    if data.pop(_registry_key(key), None) is not None:
        _write_registry(data)


def _has_live_adopter(key):
    """Whether another kernel is still relying on the sidecar under `key`."""
    entry = _read_registry().get(_registry_key(key))
    if not isinstance(entry, dict):
        return False
    return any(pid != os.getpid() and _process_alive(pid)
               for pid in (entry.get("adopters") or []))


def _start_server(data_dir, base_url_template, port=None, plugins=None,
                  host="127.0.0.1"):
    """Start (or reuse) a sidecar; returns `(port, base_url, token)`.

    `base_url_template` may contain `{port}`, which is filled in once the port
    is settled -- the proxied mount path has to name the port it is proxying,
    but the port is not known until after the cache has been consulted.

    `token` is None for a loopback sidecar and a fresh secret for any other,
    and it comes back out of the cache too: a second `view()` reusing a running
    sidecar has to present the token that one was started with.

    Three places are consulted before anything is spawned, cheapest first: this
    interpreter's own handles, sidecars it has already adopted, and finally the
    on-disk registry -- which is the only one that can see a server left behind
    by a PREVIOUS kernel. Without that last step a kernel restart silently
    doubles the number of Plexora servers on the node.
    """
    key = _server_key(data_dir, base_url_template, plugins, host)
    existing = _SERVERS.get(key)
    if existing and existing.poll() is None:
        return (
            existing._plexora_port,
            existing._plexora_base_url,
            existing._plexora_token,
        )

    adopted = _ADOPTED.get(key)
    if adopted and _sidecar_alive(adopted[0], adopted[2]):
        return adopted
    _ADOPTED.pop(key, None)

    # Only when the caller did not pin a port: an explicit `port=` is a
    # statement about which port to serve on, and quietly handing back a
    # different one that happens to be running would ignore it.
    if port is None:
        adopted = _adopt_sidecar(key)
        if adopted is not None:
            _ADOPTED[key] = adopted
            return adopted

    token = secrets.token_urlsafe(16) if _needs_token(host) else None

    # One retry, because the gap between _free_port() releasing a port and the
    # child binding it is a real window on a busy machine, and losing that race
    # is both plausible and entirely recoverable.
    attempts = 2 if port is None else 1
    last = None
    for attempt in range(attempts):
        chosen = port or _free_port()
        base_url = base_url_template.replace(PORT_PLACEHOLDER, str(chosen))
        try:
            process = _spawn_server(data_dir, base_url, chosen, plugins, host, token)
        except ServerStartError as exc:
            last = exc
            continue
        process._plexora_port = chosen
        process._plexora_base_url = base_url
        process._plexora_token = token
        _SERVERS[key] = process
        _record_sidecar(key, chosen, base_url, token, process.pid)
        return chosen, base_url, token
    raise last


def _spawn_server(data_dir, base_url, port, plugins, host="127.0.0.1", token=None):
    resolved_data_dir = str(Path(data_dir).expanduser().resolve())
    cmd = [
        sys.executable,
        "-m",
        "plexora.server_cli",
        "--host",
        str(host),
        "--port",
        str(port),
        "--data-dir",
        resolved_data_dir,
        "--base-url",
        base_url,
        "--notebook-mode",
    ]
    if plugins is not None:
        cmd.extend(["--plugins", plugins])
    # The FLAG is what actually decides the child's plugins: server_cli.main()
    # writes it into its own os.environ before `from plexora import app`, which
    # is in-process and therefore exact. The environment variable below is
    # belt-and-braces and cannot be the mechanism, because `plugins=""` -- a
    # deliberate core-only build -- does not survive a process boundary on
    # Windows at all: setting a variable to "" there deletes it, and the child
    # would read "unset", which means activate everything.
    #
    # The data path has no such constraint either way -- it resolves on demand
    # -- but is passed both ways so parent and child cannot disagree about
    # which directory this viewer is for.
    env = os.environ.copy()
    env["PLEXORA_DATA_PATH"] = resolved_data_dir
    env["PLEXORA_BASE_URL"] = base_url
    env["PLEXORA_NOTEBOOK_MODE"] = "1"
    if plugins is not None:
        env["PLEXORA_PLUGINS"] = plugins
    # Env only, never a flag: an argument would be visible in `ps` to every
    # other user on the node -- the exact people the token exists to keep out.
    # Safe across the Windows "empty value deletes the variable" trap, because
    # a token is never empty when there is one at all.
    if token:
        env["PLEXORA_AUTH_TOKEN"] = token
    # No cwd: this used to be the package's parent, which is the repository
    # root only when running from a checkout and site-packages otherwise.
    # Nothing the child does is relative to its working directory any more.
    process = subprocess.Popen(cmd, env=env)
    try:
        _wait_until_ready(port, process=process, token=token)
    except ServerStartError:
        if process.poll() is None:
            process.terminate()
        raise
    return process


def _cleanup_servers():
    """Terminate the sidecars this interpreter started, and only those.

    Two things this deliberately does not do:

    Adopted sidecars are never touched. We hold no Popen handle for them, and
    more importantly they belong to another kernel that may still be using one.

    A sidecar of ours that another live kernel has adopted is left running, and
    its registry entry left in place. Terminating it would be correct hygiene
    for a one-off script and a regression for the case this registry exists to
    serve: the other kernel's viewer would go dead mid-session, having done
    nothing wrong. It is reaped instead by whoever outlives it, or by the job
    ending -- an orphan is exactly what the next kernel now knows how to find
    and reuse.
    """
    for key, process in _SERVERS.items():
        if _has_live_adopter(key):
            continue
        if process.poll() is None:
            process.terminate()
        _forget_sidecar(key)


atexit.register(_cleanup_servers)


class PlexoraViewer:
    def __init__(
        self,
        datasource,
        data_dir=None,
        proxy="auto",
        height=850,
        width="100%",
        base_url=None,
        plugins=None,
        start=True,
    ):
        """`proxy` is one of:

        - "auto" (default) -- look at the environment and decide. Local Jupyter
          and VS Code Remote get a direct localhost URL exactly as before;
          JupyterHub, Open OnDemand and Colab get the proxied form they need.
        - True -- always proxy through the notebook server.
        - False -- always use a direct 127.0.0.1 URL.

        The default changed from False. That was only ever right when the
        browser and the kernel were the same machine; anywhere else it produced
        an iframe pointing at the user's own laptop and rendered blank.
        """
        self.datasource = datasource
        self.data_dir = Path(data_dir or os.environ.get("PLEXORA_DATA_PATH", _default_data_dir())).expanduser().resolve()
        self.proxy = proxy
        self.height = height
        self.width = width
        self._base_url = base_url
        # Kept as-is (not truthy-or) so plugins="" -- explicitly core-only --
        # stays distinguishable from "not passed, use whatever is installed".
        self.plugins = plugins
        self._port = None
        self._display_base = None
        self._token = None
        if start:
            self.start()

    @classmethod
    def from_files(
        cls,
        name,
        image,
        segmentation,
        features,
        x,
        y,
        id_column="CellID",
        celltype_column=None,
        channel_names=None,
        copy=False,
        data_dir=None,
        **viewer_kwargs,
    ):
        resolved_data_dir = Path(data_dir or os.environ.get("PLEXORA_DATA_PATH", _default_data_dir())).expanduser().resolve()
        register_datasource(
            name=name,
            image=image,
            segmentation=segmentation,
            features=features,
            x=x,
            y=y,
            id_column=id_column,
            celltype_column=celltype_column,
            channel_names=channel_names,
            copy=copy,
            data_dir=resolved_data_dir,
        )
        return cls(datasource=name, data_dir=resolved_data_dir, **viewer_kwargs)

    @classmethod
    def from_anndata(
        cls,
        name,
        image,
        features=None,
        adata=None,
        segmentation=None,
        coordinate_source=None,
        obsm_key=None,
        x=None,
        y=None,
        feature_source="X",
        layer=None,
        feature_obs_columns=None,
        obs_id_field=None,
        celltype_column=None,
        subset_by=None,
        subset_value=None,
        channel_names=None,
        copy=False,
        data_dir=None,
        **viewer_kwargs,
    ):
        resolved_data_dir = Path(data_dir or os.environ.get("PLEXORA_DATA_PATH", _default_data_dir())).expanduser().resolve()
        register_anndata_datasource(
            name=name,
            image=image,
            features=features,
            adata=adata,
            segmentation=segmentation,
            coordinate_source=coordinate_source,
            obsm_key=obsm_key,
            x=x,
            y=y,
            feature_source=feature_source,
            layer=layer,
            feature_obs_columns=feature_obs_columns,
            obs_id_field=obs_id_field,
            celltype_column=celltype_column,
            subset_by=subset_by,
            subset_value=subset_value,
            channel_names=channel_names,
            copy=copy,
            data_dir=resolved_data_dir,
        )
        return cls(datasource=name, data_dir=resolved_data_dir, **viewer_kwargs)

    def start(self):
        """Resolve where this viewer lives, then start or reuse a server.

        Resolution happens against the PORT PLACEHOLDER rather than a real
        port, so a second view() in the same notebook produces the same cache
        key and reuses the first sidecar instead of spawning another one.
        """
        if self._port is not None:
            return self._port
        resolved = resolve_display(self.proxy, self._base_url)
        was_running = _SERVERS.get(
            _server_key(self.data_dir, resolved.server_base, self.plugins,
                        resolved.bind_host)
        )
        if resolved.bind_host != "127.0.0.1" and not (
            was_running and was_running.poll() is None
        ):
            # Said once, when it actually happens -- not on every cell that
            # reuses the sidecar, and not as a warning about a hypothetical.
            print(
                f"Plexora is binding {resolved.bind_host}:<port> so Open OnDemand "
                "can reach it from the portal; while it runs it is reachable from "
                "the cluster network, protected by a token in the URL below."
            )
        self._port, _, self._token = _start_server(
            self.data_dir, resolved.server_base, plugins=self.plugins,
            host=resolved.bind_host,
        )
        if resolved.kind == "proxy":
            # Only this route depends on jupyter-server-proxy, and only the
            # notebook server can say whether it has it -- see
            # verify_proxy_route. The OOD route needs nothing installed.
            verify_proxy_route(resolved.server_base.rsplit("proxy/", 1)[0], self._port)
        # Substituted only now, against whichever port the sidecar actually
        # ended up on -- which is not necessarily the one this call would have
        # picked, since it may have reused a server started by an earlier cell.
        if resolved.display is COLAB:
            self._display_base = colab_origin(self._port)
        else:
            self._display_base = resolved.display.replace(
                PORT_PLACEHOLDER, str(self._port)
            )
        return self._port

    @property
    def url(self):
        """The address to load, token and all.

        Only this entry URL carries the token: the server trades it for a
        scoped cookie on the first request, so the iframe's own asset and API
        calls need nothing further. Everything user-facing -- `.open()`, the
        iframe `src`, the printed path -- comes through here, so there is one
        place where the token can be forgotten and it is not forgotten.
        """
        self.start()
        if self._display_base is None:
            raise RuntimeError(
                "Colab did not return a public URL for this port. That needs a "
                "connected notebook frontend, so it fails under 'Run all' or a "
                "reconnect. Use viewer.iframe(), which does not, or re-run this "
                "cell on its own."
            )
        url = join_display(self._display_base, self.datasource)
        if self._token:
            url = f"{url}?token={urllib.parse.quote(self._token)}"
        return url

    def _colab_iframe(self):
        """Let Colab's frontend work out the URL, since the kernel could not.

        This is the fallback for `colab_origin()` returning None, and it is not
        a lesser version of it -- it is the more reliable one. The helper emits
        Javascript that calls `proxyPort` in the notebook frontend, so it needs
        no kernel-to-frontend round trip and works under "Run all" and after a
        reconnect. It is only the fallback because it displays its own output
        rather than returning a URL, which `.url` and `.open()` need.
        """
        from google.colab.output import serve_kernel_port_as_iframe

        return serve_kernel_port_as_iframe(
            self._port,
            path=f"/{self.datasource}",
            width=str(self.width),
            height=str(self.height),
        )

    def iframe(self):
        self.start()
        if self._display_base is None:
            return self._colab_iframe()
        try:
            from IPython.display import HTML
        except ImportError:
            return self._repr_html_()
        return HTML(self._repr_html_())

    def _ipython_display_(self):
        """Render when the viewer is the last expression in a cell.

        Defined rather than leaving IPython to find `_repr_html_`, because the
        Colab fallback cannot be expressed as HTML at all: a <script> in cell
        output runs inside Colab's sandboxed output frame, where
        `google.colab.kernel` does not exist. Only frontend Javascript can
        resolve the port, and only this hook can emit it.
        """
        self.start()
        if self._display_base is None:
            self._colab_iframe()
            return
        from IPython.display import display, HTML

        display(HTML(self._repr_html_()))

    def open(self):
        """Open the viewer in a browser, where that means anything.

        A hosted notebook's URL is a path on the hub's origin, which this
        process cannot turn into something webbrowser could open -- and if it
        could, the browser here is on the wrong machine anyway. Printing it is
        the honest outcome.
        """
        url = self.url
        if not url.lower().startswith(("http://", "https://")):
            print(f"Open this under your Jupyter server's address: {url}")
            return url
        webbrowser.open(url)
        return url

    def _repr_html_(self):
        self.start()
        if self._display_base is None:
            # Reachable only when something bypassed _ipython_display_ -- a
            # message beats an exception raised from inside a repr, which
            # IPython would show as a traceback about formatting.
            return (
                "<pre>Plexora is running, but Colab did not return a public URL "
                "for it.\nCall viewer.iframe() to display it.</pre>"
            )
        src = html.escape(self.url, quote=True)
        width = html.escape(str(self.width), quote=True)
        height = int(self.height)
        return (
            f'<iframe src="{src}" width="{width}" height="{height}" '
            'style="border: 0; width: 100%;" allowfullscreen></iframe>'
        )
