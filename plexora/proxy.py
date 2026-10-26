import os
import secrets
import sys


def _proxy_injects_headers():
    """Whether this jupyter-server-proxy adds `request_headers_override`
    headers to what it forwards (4.1.0 and later). An older one ignores the
    key, and a token it never forwards would refuse every request."""
    try:
        from importlib.metadata import version

        major, minor = (int(part) for part in version("jupyter-server-proxy").split(".")[:2])
    except Exception:
        return False
    return (major, minor) >= (4, 1)


def setup_plexora():
    data_dir = os.environ.get("PLEXORA_DATA_PATH", "")
    plugins = os.environ.get("PLEXORA_PLUGINS")
    base_url_template = "{base_url}plexora"
    command = [
        sys.executable,
        "-m",
        "plexora.server_cli",
        "--host",
        "127.0.0.1",
        "--port",
        "{port}",
        "--base-url",
        base_url_template,
        "--notebook-mode",
    ]
    # Omitted entirely when unset, exactly as the `environment` dict below
    # does. Passing `--plugins ""` here instead used to be a silent
    # catastrophe: server_cli.py writes the flag straight into PLEXORA_PLUGINS,
    # and "" is not "unset" -- it is the deliberate core-only build. Every
    # launch from the JupyterHub launcher tile came up with all plugins
    # disabled, and looked to the user like Plexora simply had no tools.
    if plugins is not None:
        command.extend(["--plugins", plugins])
    if data_dir:
        command.extend(["--data-dir", data_dir])

    # jupyter_server_proxy sets these as real OS env vars before the child
    # process starts (handlers.py: ensure_process's server_env.update(get_env())),
    # which is what actually reaches plexora/__init__.py's import-time
    # env snapshot -- the --base-url/--data-dir/--plugins CLI flags
    # above are consumed too late relative to that import. Keep the two in
    # sync if you change one.
    environment = {
        "PLEXORA_BASE_URL": base_url_template,
        "PLEXORA_NOTEBOOK_MODE": "1",
        **({"PLEXORA_PLUGINS": plugins} if plugins is not None else {}),
    }
    if data_dir:
        environment["PLEXORA_DATA_PATH"] = data_dir

    # The server listens on this node's loopback, which every other account
    # on a shared hub node can also reach. The proxy is the only way in that
    # the person who launched it uses, so the proxy carries a token on every
    # request it forwards and the browser never needs to hold one.
    headers = {}
    if _proxy_injects_headers():
        token = secrets.token_urlsafe(24)
        environment["PLEXORA_AUTH_TOKEN"] = token
        headers["X-Plexora-Token"] = token

    return {
        "command": command,
        "environment": environment,
        **({"request_headers_override": headers} if headers else {}),
        "absolute_url": False,
        "new_browser_tab": False,
        "timeout": 30,
        "launcher_entry": {
            "enabled": True,
            "title": "Plexora",
            "path_info": "",
        },
    }
