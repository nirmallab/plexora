"""Friendly command-line entry point for launching Plexora.

`plexora-server` remains the low-level sidecar command used by Jupyter proxy
integrations. This module backs the end-user `plexora` command: it starts the
same Waitress server, prints the URL, and opens a browser only when doing so
looks appropriate for the current environment.
"""

from __future__ import annotations

import argparse
import os
import platform
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import quote

from appdirs import user_data_dir


HEADLESS_ENV_VARS = (
    "CI",
    "SLURM_JOB_ID",
    "PBS_JOBID",
    "LSB_JOBID",
    "SSH_CONNECTION",
    "SSH_CLIENT",
)


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


def _wait_until_ready(url, timeout=30):
    health_url = url.rstrip("/") + "/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=1) as response:
                if response.status < 500:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _open_browser_when_ready(open_url, health_url, *, wait_fn=_wait_until_ready, open_fn=webbrowser.open):
    if wait_fn(health_url):
        open_fn(open_url)


def _schedule_browser_open(open_url, health_url):
    thread = threading.Thread(
        target=_open_browser_when_ready,
        args=(open_url, health_url),
        daemon=True,
    )
    thread.start()
    return thread


def build_parser():
    parser = argparse.ArgumentParser(
        prog="plexora",
        description="Start the Plexora local image viewer server.",
    )
    parser.add_argument(
        "datasource",
        nargs="?",
        help="Optional datasource/project name to open directly.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default="8000")
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


def _browser_preference(args):
    if args.browser:
        return "yes"
    if args.no_browser:
        return "no"
    return "auto"


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.data_dir:
        os.environ["PLEXORA_DATA_PATH"] = str(Path(args.data_dir).expanduser())
    elif "PLEXORA_DATA_PATH" not in os.environ:
        os.environ["PLEXORA_DATA_PATH"] = user_data_dir("plexora")
    if args.base_url is not None:
        os.environ["PLEXORA_BASE_URL"] = args.base_url
    if args.plugins is not None:
        os.environ["PLEXORA_PLUGINS"] = args.plugins

    from waitress import serve
    from plexora import app, _clean_base_url as app_clean_base_url

    if args.base_url is not None:
        app.config["PLEXORA_BASE_URL"] = app_clean_base_url(args.base_url)

    health_url = browser_url(args.host, args.port, args.base_url)
    url = browser_url(args.host, args.port, args.base_url, args.datasource)
    print(f"Serving Plexora at {url}")

    preference = _browser_preference(args)
    if should_open_browser(preference=preference):
        print("Opening browser...")
        _schedule_browser_open(url, health_url)
    elif preference == "auto":
        print("Browser auto-open skipped: headless environment detected.")

    serve(
        app,
        host=args.host,
        port=int(args.port),
        max_request_body_size=1073741824000000,
        max_request_header_size=85899345920000,
        threads=8,
    )


if __name__ == "__main__":
    main()
