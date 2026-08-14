import os
import sys


def setup_plexora():
    data_dir = os.environ.get("PLEXORA_DATA_PATH", "")
    active_module = os.environ.get("PLEXORA_ACTIVE_MODULE", "gating")
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
        "--active-module",
        active_module,
    ]
    if data_dir:
        command.extend(["--data-dir", data_dir])

    # jupyter_server_proxy sets these as real OS env vars before the child
    # process starts (handlers.py: ensure_process's server_env.update(get_env())),
    # which is what actually reaches plexora/__init__.py's import-time
    # env snapshot -- the --base-url/--data-dir/--active-module CLI flags
    # above are consumed too late relative to that import. Keep the two in
    # sync if you change one.
    environment = {
        "PLEXORA_BASE_URL": base_url_template,
        "PLEXORA_NOTEBOOK_MODE": "1",
        "PLEXORA_ACTIVE_MODULE": active_module,
    }
    if data_dir:
        environment["PLEXORA_DATA_PATH"] = data_dir

    return {
        "command": command,
        "environment": environment,
        "absolute_url": False,
        "new_browser_tab": False,
        "timeout": 30,
        "launcher_entry": {
            "enabled": True,
            "title": "Plexora",
            "path_info": "",
        },
    }
