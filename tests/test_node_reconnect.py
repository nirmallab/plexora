"""What an already-loaded project does when its node comes back somewhere else.

A tunnel comes back on whatever local port was free, so reconnecting rewrites
`nodes.json` with a new endpoint and a new token. Nothing about that reaches
inside this process to find the work already holding the old ones: a provider
resolves its node once (providers/node.py explains why it must not read the
registry under the load lock), and reopening the project does not help, because
`load_datasource` returns early for a name it has already loaded.

Left alone, that produced the worst shape a failure can have. The node was
genuinely up. The connections panel resolved it freshly out of the registry,
probed it, and called it Healthy in single-digit milliseconds. And every tile,
stat and GMM in the open project was refused before a socket was opened,
against a port that had gone -- with no resource error recorded anywhere,
because the load that cached the old address had succeeded. Nothing on screen
was wrong except the pictures, and the one route that fixes it
(`/reload_datasource`) is offered by a banner that only renders when a resource
is recorded unavailable.

So two things are pinned here. A provider notices that its node's address moved
and re-resolves. And the health probe is about the address the open project is
actually using, so it can no longer report a machine as well while the viewer
is failing to read from it.
"""

import numpy as np
import pytest
import tifffile

import plexora
from plexora.server.models import data_model
from plexora.server.models import nodes as node_registry
from plexora.server.models import remotes as remote_store
from plexora.server.models.project import Project, ResourceBinding
from plexora.server.providers import http
from plexora.server.providers.node import NodeImageProvider
from tests.helpers import ALL_CONFIRMED, project

#: Two ports nothing is listening on. Which one a request went to is the whole
#: assertion in this file, so they only ever have to be told apart.
FIRST = "http://127.0.0.1:41200"
SECOND = "http://127.0.0.1:41201"


@pytest.fixture
def client():
    return plexora.app.test_client()


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


def map_a_node(name="o2", endpoint=FIRST, token="t"):
    # `managed_by` is what makes /remote_health speak for this entry at all --
    # a node somebody registered by hand is theirs, and no profile answers for
    # it. See `_registered_node_for`.
    return node_registry.save(node_registry.Node(
        name=name, endpoint=endpoint, token=token,
        extra={"managed_by": f"connect:{name}"}))


def an_image_provider(node="o2"):
    return NodeImageProvider(ResourceBinding(
        kind="image", provider="node", node=node, resource_id="slide"))


# -- a provider notices that its node moved ----------------------------------


def test_a_provider_picks_up_the_address_a_reconnect_gave_the_node(sockets):
    """The bug this file is named after. The provider resolved `o2` at one
    port, the tunnel came back on another, and every read after that has to go
    to the new one -- without the project being reloaded, because reopening it
    is a no-op and nothing else was going to say "again"."""
    map_a_node()
    provider = an_image_provider()
    http.json_request(provider.node, "GET", "/node/v1/health")

    map_a_node(endpoint=SECOND)
    http.json_request(provider.node, "GET", "/node/v1/health")

    assert sockets == [f"{FIRST}/node/v1/health", f"{SECOND}/node/v1/health"]


def test_a_rotated_token_counts_as_a_move(sockets):
    """The token is half of how to reach a node and it is reissued per session,
    so a reconnect that happened to land on the same port still hands the
    provider a credential the node will refuse."""
    map_a_node(token="old")
    provider = an_image_provider()
    assert provider.node.token == "old"

    map_a_node(token="new")

    assert provider.node.token == "new"


def test_a_provider_holding_a_retired_address_still_says_it_was_disconnected(
        sockets):
    """Re-resolving is for a node that MOVED. A node taken off the map was
    disconnected on purpose, the cached entry already reports that in its own
    words, and swapping it for "not connected to this Plexora. Connect it and
    reopen this project" would answer a question the user did not ask -- they
    closed the tunnel, they know."""
    map_a_node()
    provider = an_image_provider()
    assert provider.node.endpoint == FIRST

    node_registry.remove("o2")

    with pytest.raises(http.ResourceUnavailable) as caught:
        http.json_request(provider.node, "GET", "/node/v1/health")
    assert "was disconnected" in str(caught.value)
    assert sockets == []


def test_keeping_last_seen_current_is_not_a_move():
    """`record_handshake` writes this file after every successful probe. If
    that counted as a change, every provider in the process would re-read
    nodes.json for bookkeeping that told it nothing."""
    map_a_node()
    before = node_registry.address_generation("o2")

    node_registry.record_handshake(
        "o2", {"plexora_version": "9.9.9"}, when="2026-08-30T10:00:00")

    assert node_registry.address_generation("o2") == before


def test_disconnecting_and_connecting_again_is_a_move(sockets):
    """The path that actually bites, and the one an earlier version of this got
    wrong. Disconnect REMOVES the entry, so reconnecting writes over an absence
    and looks exactly like a node being registered for the first time -- and
    exempting that case, on the reasoning that a first registration has no
    cached provider to invalidate, exempted the commonest reconnect there is.
    It is the moment a provider is most likely to be holding a retired
    address."""
    map_a_node()
    provider = an_image_provider()
    assert provider.node.endpoint == FIRST

    node_registry.remove("o2")
    map_a_node(endpoint=SECOND)

    http.json_request(provider.node, "GET", "/node/v1/health")
    assert sockets == [f"{SECOND}/node/v1/health"]


def test_a_first_registration_does_not_leave_a_provider_a_generation_behind():
    """The worry that motivated the exemption above, shown to be unfounded: a
    provider records the generation it saw at its OWN first resolve, so a
    counter that is already at 1 costs it nothing."""
    map_a_node()
    provider = an_image_provider()
    provider.node

    assert provider._generation == node_registry.address_generation("o2")


# -- and the panel stops disagreeing with the viewer -------------------------


def a_project(tmp_path, name="dark", node="o2"):
    """A project here whose IMAGE is served from a node."""
    path = tmp_path / f"{name}.ome.tif"
    tifffile.imwrite(path, np.zeros((2, 256, 256), dtype=np.uint16),
                     photometric="minisblack")
    project(name, channels=("A", "B"), confirmed=ALL_CONFIRMED,
            src=str(path), width=256, height=256).save()
    Project.load(name).with_resource("image", ResourceBinding(
        kind="image", provider="node", node=node, resource_id="slide")).save()
    return Project.load(name)


def a_profile(name="o2", node_name="o2"):
    remote_store.save(remote_store.Remote(
        name=name, target="me@login.cluster.edu", node_name=node_name))


def test_health_will_not_call_a_node_well_that_the_project_cannot_read(
        client, tmp_path, monkeypatch):
    """The symptom that started this: Healthy in the navbar, 503 on every
    channel. The probe used to resolve the node freshly out of the registry,
    which is not the node the open project is addressing -- so it answered a
    question nobody had asked, in green."""
    map_a_node()
    a_profile()
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")
    # The provider has now resolved and is holding FIRST.
    data_model.load_datasource("dark")
    data_model.get_current_providers().image.node

    map_a_node(endpoint=SECOND)
    # The registry's copy answers perfectly. That was always true, and is
    # exactly what made the old report so convincing.
    monkeypatch.setattr(http, "hello", lambda node, timeout=None: {})

    answer = client.get("/remote_health").get_json()["health"]["o2"]

    assert answer["state"] == "stale"
    assert "reload the project" in answer["detail"].lower()


def test_health_is_well_again_once_the_project_has_caught_up(
        client, tmp_path, monkeypatch, sockets):
    """`stale` is a statement about a disagreement, not a sticky flag. Once the
    provider has re-resolved -- which the next read does by itself -- there is
    nothing left to report."""
    map_a_node()
    a_profile()
    a_project(tmp_path)
    client.get("/init_database?datasource=dark")
    data_model.load_datasource("dark")
    provider = data_model.get_current_providers().image
    provider.node

    map_a_node(endpoint=SECOND)
    provider.node                                     # the next read re-resolves
    monkeypatch.setattr(http, "hello", lambda node, timeout=None: {})

    answer = client.get("/remote_health").get_json()["health"]["o2"]

    assert answer["state"] == "healthy"


def test_health_says_nothing_about_a_project_that_has_not_read_yet(
        client, tmp_path, monkeypatch):
    """A provider that has not been called holds no address, so there is no
    disagreement to report -- and inventing one would put a warning on every
    freshly opened project."""
    map_a_node()
    a_profile()
    a_project(tmp_path)
    monkeypatch.setattr(http, "hello", lambda node, timeout=None: {})

    answer = client.get("/remote_health").get_json()["health"]["o2"]

    assert answer["state"] == "healthy"
