"""A real Plexora data node, in a real second process, for tests.

Nothing here fakes the network. The node is started with `python -m plexora
node serve`, it is talked to over a socket, and the primary side is the app's
own Flask test client -- so what these tests exercise is the same handshake,
the same token check, the same gzip framing and the same Arrow buffers that a
laptop-and-cluster pair would exercise.

An in-process double would be cheaper and would prove almost nothing. The
failures this architecture actually has -- a header that is not exposed to a
browser, a body that is decoded twice, a float32 cast that eats a text column,
a lock held across a stream -- are all failures of the seam between two
processes, and a stub is a seam with nothing on the far side of it.

Two costs are accepted deliberately: a subprocess per test session (not per
test -- see the fixture's scope), and a few seconds of startup while the node
imports tifffile and the adapters.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

#: How long to wait for a node to answer /health before giving up. Generous:
#: the node imports anndata, tifffile, zarr and every plugin's server half, and
#: on a cold filesystem that is seconds.
STARTUP_TIMEOUT = 60.0


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class NodeProcess:
    """A running `plexora node serve`, and how to reach it."""

    def __init__(self, port, token, process, resources, root=None):
        self.port = port
        self.token = token
        self.process = process
        self.resources = resources
        self.root = root
        self.endpoint = f"http://127.0.0.1:{port}"

    def url(self, path):
        return f"{self.endpoint}/{path.lstrip('/')}"

    def get(self, path, *, raw=False, headers=None):
        """One authenticated GET, as the primary would make it."""
        request = urllib.request.Request(self.url(path))
        request.add_header("X-Plexora-Node-Token", self.token)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read()
        return body if raw else json.loads(body)

    def post(self, path, payload=None, *, raw=False):
        request = urllib.request.Request(
            self.url(path),
            data=json.dumps(payload or {}).encode("utf-8"),
            method="POST",
        )
        request.add_header("X-Plexora-Node-Token", self.token)
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=300) as response:
            body = response.read()
        return body if raw else json.loads(body)

    def post_bytes(self, path, payload, *, content_type="application/octet-stream"):
        """One authenticated POST whose body is the payload, not a document.

        What `/node/v1/write_file` takes: the file itself on the wire, with
        everything else in the query string.
        """
        request = urllib.request.Request(
            self.url(path), data=payload, method="POST")
        request.add_header("X-Plexora-Node-Token", self.token)
        request.add_header("Content-Type", content_type)
        with urllib.request.urlopen(request, timeout=300) as response:
            return json.loads(response.read())

    def delete(self, path):
        """One authenticated DELETE, as the primary's relay would make it."""
        request = urllib.request.Request(self.url(path), method="DELETE")
        request.add_header("X-Plexora-Node-Token", self.token)
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover - a wedged node
                self.process.kill()
                self.process.wait(timeout=15)
        if self.root:
            import shutil

            shutil.rmtree(self.root, ignore_errors=True)


def start_node(*serve, token=None, node_id=None, allow_origins=(), env=None,
               dynamic=False, manifest=None):
    """Start a node serving `serve` (each `kind:id=path`) and wait for it.

    The child gets a data root of its own -- a node has no config.json and must
    never be able to reach the primary's, and a test that shared one would let
    a bug that writes project state on the node pass unnoticed.
    """
    port = free_port()
    # Hex, not token_urlsafe: that alphabet includes '-', and argparse reads a
    # value starting with one as another flag. Real users hit this too, which
    # is why `plexora node serve` generates its own when none is given.
    token = token or secrets.token_hex(8)
    command = [
        sys.executable, "-m", "plexora", "node", "serve",
        "--port", str(port), "--token", token, "--host", "127.0.0.1",
    ]
    for argument in serve:
        command += ["--serve", argument]
    if node_id:
        command += ["--node-id", node_id]
    for origin in allow_origins:
        command += ["--allow-origin", origin]
    if dynamic:
        command += ["--dynamic"]
    if manifest:
        command += ["--manifest", str(manifest)]

    # A data root of the node's own, and deliberately NOT inside the test's
    # tmp_path. A node has no config.json and must never reach the primary's --
    # and on Windows a directory the child has touched makes pytest's tmp_path
    # cleanup fail in whichever test happens to run next.
    import tempfile

    root = tempfile.mkdtemp(prefix="plexora-node-")
    child_env = dict(os.environ)
    child_env["PLEXORA_DATA_PATH"] = root
    # Windows scanned inside the request, as in-process test nodes do via the
    # conftest fixture. The suite's assertions are byte-equality against local
    # reads, which needs the exact window on the first answer -- the
    # asynchrony is covered by its own tests, in process, where it can be
    # driven deterministically.
    child_env["PLEXORA_WINDOW_SCANS_INLINE"] = "1"
    child_env.update(env or {})
    process = subprocess.Popen(
        command, env=child_env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    node = NodeProcess(port, token, process, list(serve), root=root)
    _wait_until_ready(node)
    return node


def _wait_until_ready(node):
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if node.process.poll() is not None:
            output = node.process.stdout.read() if node.process.stdout else ""
            raise RuntimeError(
                f"plexora node exited with {node.process.returncode}:\n{output}")
        try:
            node.get("/node/v1/health")
            return
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.15)
    node.stop()
    raise RuntimeError(f"data node on port {node.port} never became ready")


@pytest.fixture
def node_process():
    """Start nodes for one test, stopping every one at the end.

    Function-scoped despite the startup cost, because the alternative is worse:
    a session-scoped node would outlive the `plexora_data_root` fixture that
    made the files it is serving, and would then be serving paths inside a
    deleted tmp_path for every later test.
    """
    started = []

    def start(*serve, **options):
        node = start_node(*serve, **options)
        started.append(node)
        return node

    yield start
    for node in started:
        node.stop()


def register(name, node, browser_endpoint=None):
    """Record a started node in this test's own nodes.json."""
    from plexora.nodes import register_node

    return register_node(name, node.endpoint, token=node.token,
                         browser_endpoint=browser_endpoint, verify=True)
