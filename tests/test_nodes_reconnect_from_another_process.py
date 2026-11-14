"""A node reconnected by ANOTHER process has to reach this one's open project.

`plexora node connect user@hpc` runs in its own CLI process, and so does a
second Plexora sharing the data root: each writes this machine's nodes.json
with the tunnel's new local port and the node's new token. The running viewer
server finds out about a move only through `address_generation`, which only its
own `save()` bumps, so a provider it already resolved keeps the retired address
and token -- and reopening the page does not help, because `load_datasource`
returns early for a name already loaded.

These tests write nodes.json from a real second Python process, exactly as the
CLI does, and then ask this process's provider where it is reading from.
"""

import os
import subprocess
import sys
import textwrap

import pytest

from plexora.server.models import nodes as node_registry
from plexora.server.models.project import ResourceBinding
from plexora.server.providers import http
from plexora.server.providers.node import NodeImageProvider

#: Two ports nothing is listening on; which one a call went to is the assertion.
FIRST = "http://127.0.0.1:41210"
SECOND = "http://127.0.0.1:41211"


@pytest.fixture
def sockets(monkeypatch):
    """Stand in for the connection pool, recording the URL of every call."""
    calls = []

    class _Answer:
        status = 200
        headers: dict = {}
        data = b"{}"

    class _Pool:
        def request(self, method, url, **kwargs):
            calls.append(url)
            return _Answer()

    monkeypatch.setattr(http, "pool", lambda: _Pool())
    return calls


def map_a_node(endpoint=FIRST, token="old"):
    return node_registry.save(node_registry.Node(
        name="o2", endpoint=endpoint, token=token,
        extra={"managed_by": "connect:o2"}))


def another_process_registers(endpoint, token):
    """What `plexora node connect` does: save the node in its OWN process."""
    script = textwrap.dedent(f"""
        from plexora.server.models import nodes as node_registry
        node_registry.save(node_registry.Node(
            name="o2", endpoint={endpoint!r}, token={token!r},
            extra={{"managed_by": "connect:o2"}}))
    """)
    subprocess.run([sys.executable, "-c", script], check=True,
                   env=dict(os.environ), timeout=120)
    written = node_registry.get("o2")
    assert (written.endpoint, written.token) == (endpoint, token)


def an_image_provider():
    return NodeImageProvider(ResourceBinding(
        kind="image", provider="node", node="o2", resource_id="slide"))


def test_a_provider_follows_a_reconnect_written_by_another_process(sockets):
    """The tunnel came back on a new port with a new token, registered by the
    CLI. The next read of the already-open project has to go there, with the
    new token -- not to the port that has gone."""
    map_a_node()
    provider = an_image_provider()
    http.json_request(provider.node, "GET", "/node/v1/health")

    another_process_registers(SECOND, "new")

    assert (provider.node.endpoint, provider.node.token) == (SECOND, "new")
    http.json_request(provider.node, "GET", "/node/v1/health")
    assert sockets == [f"{FIRST}/node/v1/health", f"{SECOND}/node/v1/health"]


def test_a_node_disconnected_here_and_reconnected_elsewhere_is_readable_again(
        sockets):
    """Disconnect ran in this server; the CLI then connected the node again on
    the same --port with a new token. That address is current again, so a
    freshly resolved provider (what /reload_datasource builds) must be allowed
    to read from it instead of being refused as disconnected for the life of
    the process."""
    map_a_node()
    node_registry.remove("o2")

    another_process_registers(FIRST, "new")

    fresh = an_image_provider()
    http.json_request(fresh.node, "GET", "/node/v1/health")
    assert sockets == [f"{FIRST}/node/v1/health"]
