import atexit
import html
import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from plexora._url import join_display
from plexora.datasource import register_datasource, register_anndata_datasource
from plexora.notebook_env import COLAB, PORT_PLACEHOLDER, colab_origin, resolve_display


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


def _wait_until_ready(port, timeout=30, process=None):
    """Block until the sidecar answers, or explain why it never will.

    `process` is what makes this useful. Without it a child that died on its
    first line -- the overwhelmingly common case being a `plexora` that is not
    importable from the interpreter running the notebook -- was indistinguishable
    from one that was merely slow, so the notebook cell sat there for the full
    30 seconds and then reported a timeout, which is the one explanation that
    was not true.
    """
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/config"
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


def _server_key(data_dir, base_url_template, plugins):
    """What makes two viewers able to share one sidecar.

    `base_url_template`, not the base URL: in proxy mode the URL contains the
    port, which is freshly chosen on every call, so keying on it meant the
    lookup could never hit and each `plexora.view()` in a notebook started
    another server. Harmless when proxying was opt-in and rare; not harmless
    now that hosted notebooks reach it by default.
    """
    return (str(Path(data_dir).expanduser().resolve()), base_url_template, plugins)


def _start_server(data_dir, base_url_template, port=None, plugins=None):
    """Start (or reuse) a sidecar; returns `(port, base_url)`.

    `base_url_template` may contain `{port}`, which is filled in once the port
    is settled -- the proxied mount path has to name the port it is proxying,
    but the port is not known until after the cache has been consulted.
    """
    key = _server_key(data_dir, base_url_template, plugins)
    existing = _SERVERS.get(key)
    if existing and existing.poll() is None:
        return existing._plexora_port, existing._plexora_base_url

    # One retry, because the gap between _free_port() releasing a port and the
    # child binding it is a real window on a busy machine, and losing that race
    # is both plausible and entirely recoverable.
    attempts = 2 if port is None else 1
    last = None
    for attempt in range(attempts):
        chosen = port or _free_port()
        base_url = base_url_template.replace(PORT_PLACEHOLDER, str(chosen))
        try:
            process = _spawn_server(data_dir, base_url, chosen, plugins)
        except ServerStartError as exc:
            last = exc
            continue
        process._plexora_port = chosen
        process._plexora_base_url = base_url
        _SERVERS[key] = process
        return chosen, base_url
    raise last


def _spawn_server(data_dir, base_url, port, plugins):
    resolved_data_dir = str(Path(data_dir).expanduser().resolve())
    cmd = [
        sys.executable,
        "-m",
        "plexora.server_cli",
        "--host",
        "127.0.0.1",
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
    # No cwd: this used to be the package's parent, which is the repository
    # root only when running from a checkout and site-packages otherwise.
    # Nothing the child does is relative to its working directory any more.
    process = subprocess.Popen(cmd, env=env)
    try:
        _wait_until_ready(port, process=process)
    except ServerStartError:
        if process.poll() is None:
            process.terminate()
        raise
    return process


def _cleanup_servers():
    for process in _SERVERS.values():
        if process.poll() is None:
            process.terminate()


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
        server_base, display_base = resolve_display(self.proxy, self._base_url)
        self._port, _ = _start_server(
            self.data_dir, server_base, plugins=self.plugins
        )
        # Substituted only now, against whichever port the sidecar actually
        # ended up on -- which is not necessarily the one this call would have
        # picked, since it may have reused a server started by an earlier cell.
        if display_base is COLAB:
            self._display_base = colab_origin(self._port)
        else:
            self._display_base = display_base.replace(
                PORT_PLACEHOLDER, str(self._port)
            )
        return self._port

    @property
    def url(self):
        self.start()
        if self._display_base is None:
            raise RuntimeError(
                "Colab did not return a public URL for this port. That needs a "
                "connected notebook frontend, so it fails under 'Run all' or a "
                "reconnect. Use viewer.iframe(), which does not, or re-run this "
                "cell on its own."
            )
        return join_display(self._display_base, self.datasource)

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
