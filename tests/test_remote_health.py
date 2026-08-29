"""Whether an open data node is actually answering, and how fast.

Session state and health are different claims and the difference is the whole
point of this route. `connected` is what Plexora *did*: it started an ssh, the
node announced itself, the tunnel came up. Nothing in that says the node
answers now -- and it stops answering for reasons no session state can notice.
A laptop sleeps, a job hits its walltime, a VPN drops, and the session goes on
reading `connected` because nothing has told it otherwise.

So this probes, and it is asked for rather than polled: the navbar's panel
calls it once when somebody opens it. A background health poll would be a
second opinion running against every connection forever, and the first thing it
would do is disagree with the session state at a moment nobody was watching.

The panel that reads it is tests/test_remote_globe.py.
"""

import pytest

import plexora
from plexora.server.models import remote_sessions
from plexora.server.models import remotes as remote_store
from plexora.server.providers import http


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture(autouse=True)
def _no_sessions_left_running():
    """The registry is module state, and a leaked session outlives its test."""
    yield
    with remote_sessions._REGISTRY_LOCK:
        remote_sessions._SESSIONS.clear()


def a_remote(name="hpc", **kwargs):
    fields = {"target": "me@login.cluster.edu", "local_node": False}
    fields.update(kwargs)
    return remote_store.Remote(name=name, **fields)


def open_a_node(remote):
    """A node session that has reached `connected`, without an ssh.

    The constructor spawns nothing -- `start()` is what does -- so this is the
    state the route reads without the machinery underneath it.
    """
    session = remote_sessions.RemoteSession(
        remote, kind=remote_sessions.KIND_NODE)
    session.state = remote_sessions.STATE_CONNECTED
    with remote_sessions._REGISTRY_LOCK:
        remote_sessions._SESSIONS[
            remote_sessions._key(remote_sessions.KIND_NODE, remote.name)
        ] = session
    return session


def map_a_node(name):
    from plexora.server.models import nodes as node_registry

    return node_registry.save(node_registry.Node(
        name=name, endpoint="http://127.0.0.1:41000", token="t"))


# -- what it probes, and what it leaves alone --------------------------------


def test_a_machine_with_nothing_open_is_not_contacted(
        client, plexora_data_root, monkeypatch):
    """There is nothing there to ask. A saved profile is an address, not a
    running process, and probing one would be a connection attempt dressed up
    as a status read -- on a cluster, a login somebody's site would see."""
    asked = []
    monkeypatch.setattr(http, "hello", lambda node, timeout=None: asked.append(node))
    remote_store.save(a_remote())

    answer = client.get("/remote_health").get_json()

    assert answer["health"] == {}
    assert asked == []


def test_an_open_node_is_asked_and_the_round_trip_is_reported(
        client, plexora_data_root, monkeypatch):
    """One authenticated GET -- `/node/v1/hello`, which is also the version
    handshake, so the probe and the compatibility check are the same request."""
    monkeypatch.setattr(http, "hello", lambda node, timeout=None: {"api": 1})
    remote = remote_store.save(a_remote())
    open_a_node(remote)
    map_a_node("hpc")

    answer = client.get("/remote_health").get_json()["health"]["hpc"]

    assert answer["state"] == "healthy"
    assert isinstance(answer["ms"], int) and answer["ms"] >= 0


def test_a_node_that_does_not_answer_says_why_rather_than_just_no(
        client, plexora_data_root, monkeypatch):
    """"Unreachable" and "refused this server's token" want completely
    different things done about them, and the sentence the provider already
    writes is the one that says which."""
    def refuse(node, timeout=None):
        raise RuntimeError("data node 'hpc' refused this server's token.")

    monkeypatch.setattr(http, "hello", refuse)
    remote = remote_store.save(a_remote())
    open_a_node(remote)
    map_a_node("hpc")

    answer = client.get("/remote_health").get_json()["health"]["hpc"]

    assert answer["state"] == "unreachable"
    assert answer["ms"] is None
    assert "refused this server's token" in answer["detail"]


def test_the_probe_is_bounded_so_a_dead_machine_answers_quickly(
        client, plexora_data_root, monkeypatch):
    """Somebody has just opened a menu. A probe that took the provider's
    ordinary timeout to say "no" would be replaced by their own conclusion long
    before it arrived."""
    from plexora.server.routes import settings_routes

    seen = {}
    monkeypatch.setattr(
        http, "hello",
        lambda node, timeout=None: seen.setdefault("timeout", timeout) or {})
    remote = remote_store.save(a_remote())
    open_a_node(remote)
    map_a_node("hpc")

    client.get("/remote_health")

    assert seen["timeout"] == settings_routes.HEALTH_TIMEOUT
    assert settings_routes.HEALTH_TIMEOUT <= 5


def test_a_node_the_map_has_lost_is_reported_rather_than_probed(
        client, plexora_data_root, monkeypatch):
    """A session saying it registered a node that is not on the map is a real
    state -- somebody forgot it in another tab -- and there is no address to
    probe. Saying so beats a made-up failure."""
    asked = []
    monkeypatch.setattr(http, "hello", lambda node, timeout=None: asked.append(node))
    remote = remote_store.save(a_remote())
    open_a_node(remote)

    answer = client.get("/remote_health").get_json()["health"]["hpc"]

    assert answer["state"] == "unknown"
    assert asked == []


def test_the_node_it_probes_is_the_one_the_profile_registered(
        client, plexora_data_root, monkeypatch):
    """A profile is called one thing and the node it opens may be called
    another. Probing the profile's name would report every renamed node as
    missing."""
    monkeypatch.setattr(http, "hello", lambda node, timeout=None: {"api": 1})
    remote = remote_store.save(a_remote(node_name="hpc-data"))
    open_a_node(remote)
    map_a_node("hpc-data")

    answer = client.get("/remote_health").get_json()["health"]

    # Keyed by the PROFILE, because that is what a row on the panel is; probed
    # by the NODE, because that is what is on the map.
    assert answer["hpc"]["state"] == "healthy"


# -- the short tail every card can draw before anyone focuses one -------------


def test_data_places_carries_the_last_thing_that_happened(
        client, plexora_data_root):
    """So a card has a terminal to draw the moment it appears, rather than an
    empty pane that fills in a second later. The deep 200-line tail is a
    separate request, for the one connection somebody is reading."""
    remote = remote_store.save(a_remote())
    session = open_a_node(remote)
    session.lines.extend(f"line {number}" for number in range(40))

    places = client.get("/data_places").get_json()["places"]
    entry = next(place for place in places if place["id"] == "hpc")

    assert entry["log"][-1] == "line 39"
    assert len(entry["log"]) == 8
