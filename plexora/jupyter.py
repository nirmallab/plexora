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

from plexora.datasource import register_datasource, register_anndata_datasource


_SERVERS = {}


def _default_data_dir():
    return Path(__file__).resolve().parent / "data"


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _clean_base_url(base_url):
    if not base_url or base_url == "/":
        return "/"
    return "/" + str(base_url).strip("/") + "/"


def _jupyter_base_url():
    return _clean_base_url(os.environ.get("JUPYTERHUB_SERVICE_PREFIX", "/"))


def _wait_until_ready(port, timeout=30):
    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/config"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status < 500:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Plexora server did not become ready on port {port}")


def _server_key(data_dir, base_url, plugins):
    return (str(Path(data_dir).expanduser().resolve()), base_url, plugins)


def _start_server(data_dir, base_url, port=None, plugins=None):
    key = _server_key(data_dir, base_url, plugins)
    existing = _SERVERS.get(key)
    if existing and existing.poll() is None:
        return existing._plexora_port

    port = port or _free_port()
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
        command.extend(["--plugins", plugins])
    # Real OS env vars must be set before the child's first `import
    # plexora`, since __init__.py snapshots PLEXORA_DATA_PATH /
    # PLEXORA_BASE_URL / PLEXORA_PLUGINS at import time -- the CLI
    # flags above are consumed by server_cli.py too late relative to that
    # import (and, for PLEXORA_PLUGINS specifically, too late relative
    # to Blueprint registration, which happens inside that same import).
    env = os.environ.copy()
    env["PLEXORA_DATA_PATH"] = resolved_data_dir
    env["PLEXORA_BASE_URL"] = base_url
    env["PLEXORA_NOTEBOOK_MODE"] = "1"
    if plugins is not None:
        env["PLEXORA_PLUGINS"] = plugins
    repo_root = Path(__file__).resolve().parent.parent
    process = subprocess.Popen(cmd, cwd=repo_root, env=env)
    process._plexora_port = port
    _SERVERS[key] = process
    _wait_until_ready(port)
    return port


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
        proxy=False,
        height=850,
        width="100%",
        base_url=None,
        plugins=None,
        start=True,
    ):
        self.datasource = datasource
        self.data_dir = Path(data_dir or os.environ.get("PLEXORA_DATA_PATH", _default_data_dir())).expanduser().resolve()
        self.proxy = proxy
        self.height = height
        self.width = width
        self._jupyter_base_url = _clean_base_url(base_url) if base_url is not None else _jupyter_base_url()
        # Kept as-is (not truthy-or) so plugins="" -- explicitly core-only --
        # stays distinguishable from "not passed, use whatever is installed".
        self.plugins = plugins
        self._port = None
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
        if self._port is not None:
            return self._port
        port = _free_port()
        base_url = f"{self._jupyter_base_url}proxy/{port}" if self.proxy else ""
        self._port = _start_server(self.data_dir, base_url, port=port, plugins=self.plugins)
        return self._port

    @property
    def _proxied_base_url(self):
        if self._port is None:
            return ""
        return f"{self._jupyter_base_url}proxy/{self._port}"

    @property
    def url(self):
        self.start()
        if self.proxy:
            return f"{self._proxied_base_url}/{self.datasource}"
        return f"http://127.0.0.1:{self._port}/{self.datasource}"

    def iframe(self):
        try:
            from IPython.display import HTML
        except ImportError:
            return self._repr_html_()
        return HTML(self._repr_html_())

    def open(self):
        webbrowser.open(self.url)
        return self.url

    def _repr_html_(self):
        src = html.escape(self.url, quote=True)
        width = html.escape(str(self.width), quote=True)
        height = int(self.height)
        return (
            f'<iframe src="{src}" width="{width}" height="{height}" '
            'style="border: 0; width: 100%;" allowfullscreen></iframe>'
        )
