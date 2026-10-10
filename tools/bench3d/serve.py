"""Start a scratch Plexora server for the 3-D benchmark.

    python tools/bench3d/serve.py --data ~/plexora-bench/data [--port 0] [--env K=V ...]

The server runs THIS checkout (`PYTHONPATH` = the repository root, whatever
the interpreter's editable install points at), on loopback, with its data root
and HOME in the bench directory -- never the user's live install, never
Dropbox -- and telemetry off. Prints one JSON line `{port, pid, data, log}` once
the server answers, and leaves it running; `--stop PID` ends one.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def bench_home() -> Path:
    return Path(os.environ.get("PLEXORA_BENCH_HOME", Path.home() / "plexora-bench")).expanduser()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def server_env(data: Path, extra: dict | None = None, root: Path | None = None) -> dict:
    home = data.parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(root or ROOT),
        "PLEXORA_DATA_PATH": str(data),
        "HOME": str(home),
        "PLEXORA_TESTING": "1",
        "PLEXORA_TELEMETRY": "off",
        "DO_NOT_TRACK": "1",
        "PYTHONUNBUFFERED": "1",
    })
    env.update(extra or {})
    return env


def wait_up(port: int, timeout: float = 90.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as reply:
                if reply.status < 500:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def start(data: Path, port: int = 0, extra: dict | None = None, python: str | None = None,
          root: Path | None = None) -> dict:
    """Serve `data` from the checkout at `root` (default: this one)."""
    root = Path(root).expanduser().resolve() if root else ROOT
    data = Path(data).expanduser().resolve()
    data.mkdir(parents=True, exist_ok=True)
    port = port or free_port()
    log = data.parent / f"server-{port}.log"
    handle = open(log, "w")
    process = subprocess.Popen(
        [python or sys.executable, str(root / "run.py"), str(port)],
        cwd=str(root), env=server_env(data, extra, root), stdout=handle, stderr=subprocess.STDOUT,
        start_new_session=True)
    if not wait_up(port):
        process.kill()
        raise RuntimeError(f"the server did not start; see {log}")
    return {"port": port, "pid": process.pid, "data": str(data), "log": str(log)}


def stop(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=str(bench_home() / "data"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--env", action="append", default=[], help="K=V for the server")
    parser.add_argument("--stop", type=int, default=0, help="stop the server with this pid")
    parser.add_argument("--tree", default=None, help="serve this checkout instead of this one")
    args = parser.parse_args(argv)
    if args.stop:
        stop(args.stop)
        return
    extra = dict(item.split("=", 1) for item in args.env)
    print(json.dumps(start(Path(args.data), args.port, extra, root=args.tree)))


if __name__ == "__main__":
    main()
