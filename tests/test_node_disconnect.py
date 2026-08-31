"""What stops happening when a data node is disconnected.

Disconnecting takes down a tunnel, and taking down a tunnel does not reach
inside this process to find the work that is still holding its address. A
provider resolves its node once and keeps it for its life; the background cache
warm-up walks every channel of a project on a thread that outlives the load
that started it; the browser can have tiles in flight. All of them go on
addressing a loopback port that nothing answers on any more.

Left to discover that for themselves they are slow and loud about it: two
connection attempts and a backoff each, a urllib3 warning per attempt about a
connection the user closed on purpose, and -- from the warm-up -- a line
reading `Background cache warmup failed`, which describes a deliberate action
as a fault.

So the registry records the address it was told to stop using, the HTTP client
refuses it before opening a socket, and the warm-up treats the node going away
as the end of its work rather than as an error. The one exception is a probe:
asking whether a machine is up is exactly what may be done to one that was
taken down, and it is how registering it again begins.
"""

import pytest

import plexora
from plexora.server.models import data_model
from plexora.server.models import nodes as node_registry
from plexora.server.models import remotes as remote_store
from plexora.server.providers import http


ENDPOINT = "http://127.0.0.1:41000"

#: The real warm-up, captured at import. conftest's `_no_background_cache_warmup`
#: replaces the module attribute for every test in the suite -- rightly, since a
#: thread that walks a project's channels must not outlive the tmp root it was
#: pointed at -- so the two tests below that are ABOUT the warm-up have to hold
#: their own reference to it.
_warm_up = data_model._warm_datasource_caches


@pytest.fixture
def client():
    return plexora.app.test_client()


def a_node(name="hpc-data", endpoint=ENDPOINT, **extra):
    """An entry on the map. No node is running behind it."""
    return node_registry.save(node_registry.Node(
        name=name, endpoint=endpoint, token="t", extra=extra))


class _Answer:
    """The shape `request()` reads out of urllib3: a status, headers, bytes."""

    status = 200
    headers: dict = {}
    data = b"{}"


@pytest.fixture
def sockets(monkeypatch):
    """Stand in for the connection pool, and record every call that reaches it.

    The assertion this file cares about is not "the call failed" -- an
    unreachable address fails either way -- but "no connection was attempted".
    """
    calls = []

    class _Pool:
        def request(self, method, url, **kwargs):
            calls.append(url)
            return _Answer()

    monkeypatch.setattr(http, "pool", lambda: _Pool())
    return calls


# -- the address a disconnect retires ---------------------------------------


def test_a_disconnected_node_is_refused_before_a_socket_is_opened(sockets):
    """The whole point. Everything still carrying the old node fails at once,
    and says which node and what to do, instead of rediscovering it one refused
    connection at a time."""
    node = a_node()
    node_registry.remove(node.name)

    with pytest.raises(http.ResourceUnavailable) as caught:
        http.json_request(node, "GET", "/node/v1/image/x/gmm")

    assert "was disconnected" in str(caught.value)
    assert "Connect it again" in str(caught.value)
    assert sockets == []


def test_registering_the_same_address_again_lifts_the_refusal(sockets):
    """A tunnel that comes back on the port the last one used is an ordinary
    thing, and the work still holding that address is right to use it again."""
    node = a_node()
    node_registry.remove(node.name)
    a_node()

    http.json_request(node, "GET", "/node/v1/health")

    assert sockets == [f"{ENDPOINT}/node/v1/health"]


def test_reconnecting_elsewhere_does_not_revive_the_old_port(sockets):
    """The endpoint is half the key, and this is why: the next session gets
    whatever loopback port was free, and the port the last one used is exactly
    what the machine may now be handing to something else."""
    stale = a_node()
    node_registry.remove(stale.name)
    a_node(endpoint="http://127.0.0.1:41001")

    with pytest.raises(http.ResourceUnavailable):
        http.json_request(stale, "GET", "/node/v1/health")

    assert sockets == []


def test_a_probe_may_still_ask_a_node_that_was_disconnected(sockets):
    """`register_node` verifies an address before recording it, so a refusal
    here would make reconnecting on the port the last session used impossible
    -- the node could never stop being disconnected."""
    node = a_node()
    node_registry.remove(node.name)

    http.hello(node)

    assert sockets == [f"{ENDPOINT}/node/v1/hello"]


def test_disconnecting_from_the_globe_retires_the_address(client, sockets):
    """The route already forgets the node entry; this is the other half of it.
    A provider resolved before the disconnect keeps the node object it
    resolved, and that object is what has to stop working."""
    remote_store.save(remote_store.Remote(
        name="hpc", target="me@login.cluster.edu", node_name="hpc-data"))
    resolved = a_node(managed_by="connect:hpc-data")

    client.post("/settings/remotes/hpc/disconnect", query_string={"kind": "node"})

    with pytest.raises(http.ResourceUnavailable):
        http.json_request(resolved, "GET", "/node/v1/health")
    assert sockets == []


# -- work nobody is waiting for ---------------------------------------------


def test_speculative_calls_do_not_retry(monkeypatch):
    """The pool retries idempotent GETs twice with a backoff. For the warm-up
    that buys nothing and costs two urllib3 warnings per call: nothing is
    blocked on the answer, and everything it precomputes is computed on demand
    later by a caller that does retry."""
    # What each call asked of the pool. `POOLS_OWN` is the sentinel for "said
    # nothing", which is how a caller leaves the pool's default policy alone.
    POOLS_OWN = object()
    asked = []

    class _Pool:
        def request(self, method, url, **kwargs):
            asked.append(kwargs.get("retries", POOLS_OWN))
            return _Answer()

    monkeypatch.setattr(http, "pool", lambda: _Pool())
    node = node_registry.Node(name="n", endpoint=ENDPOINT, token="t")

    http.json_request(node, "GET", "/node/v1/health")
    with http.speculative():
        http.json_request(node, "GET", "/node/v1/health")
    http.json_request(node, "GET", "/node/v1/health")

    assert asked == [POOLS_OWN, False, POOLS_OWN]


def test_speculation_does_not_leak_out_of_the_block():
    """It is thread state, and a warm-up thread is the only thing that sets it
    -- but a nested block must not turn retries off for the rest of the
    thread's life either."""
    with http.speculative():
        with http.speculative():
            pass
        assert http._speculation.on is True
    assert http._speculation.on is False


def test_a_node_going_away_ends_the_warm_up_without_calling_it_a_failure(
        capsys, monkeypatch):
    """`Background cache warmup failed for ...` is a bug report, and what
    happened is that the user clicked Disconnect. Nothing is waiting on this
    work and there is nothing to repair, so it stops where it is and says so
    plainly."""
    def gone(_name):
        raise data_model.providers.ResourceUnavailable(
            "data node 'HMS-O2' was disconnected from this server.")

    monkeypatch.setattr(data_model, "get_datasource_description", gone)
    _warm_up("nlu358")

    said = capsys.readouterr().out
    assert "Stopped warming nlu358" in said
    assert "failed" not in said


def test_the_warm_up_still_reports_a_real_fault(capsys, monkeypatch):
    """Only a node that went away is quiet. Anything else is a bug in the
    warm-up itself and has to keep saying so -- it runs on a thread whose
    exceptions nobody else will ever see."""
    def broken(_name):
        raise KeyError("imageData")

    monkeypatch.setattr(data_model, "get_datasource_description", broken)
    _warm_up("nlu358")

    assert "Background cache warmup failed for nlu358" in capsys.readouterr().out
