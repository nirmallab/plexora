"""The node lists ask every registered node at once, not one after another.

Three routes answer "what is each node doing right now" by probing every node
on the map: Settings > Data nodes (`/settings/nodes`), the edit page's
Resources section (`/project/<name>/resources`) and the connections menu
(`/remote_health`). A node that does not answer costs its whole probe budget --
a refused loopback connect is about two seconds on Windows, a sleeping laptop
the full connect timeout -- and entries outlive the sessions that wrote them,
so dead tunnels pile up on the map. Probed one after another, five of them hold
the section empty (and one request thread busy) for five budgets.

Each test makes every probe wait until all of them have started. Probes that
run together all get through; probes that run one at a time leave the first
waiting for company that never arrives, and every node reads unreachable.
"""

from __future__ import annotations

import threading

import pytest

import plexora
from plexora.server.models import nodes as node_registry
from plexora.server.models import remote_sessions
from plexora.server.models import remotes as remote_store
from plexora.server.providers import http
from tests.helpers import ALL_CONFIRMED, csv_spec, project

NAMES = ("alpha", "beta", "gamma")

#: Only a deadline for the probes to meet each other by; nothing is timed.
MEET_WITHIN_S = 3.0


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture(autouse=True)
def _no_sessions_left_running():
    yield
    with remote_sessions._REGISTRY_LOCK:
        remote_sessions._SESSIONS.clear()


def _map_nodes():
    for port, name in enumerate(NAMES, start=41001):
        node_registry.save(node_registry.Node(
            name=name, endpoint=f"http://127.0.0.1:{port}", token="t"))


def _probes_that_wait_for_each_other(monkeypatch, count=len(NAMES)):
    """A `hello` that answers only once `count` probes are in flight together."""
    barrier = threading.Barrier(count, timeout=MEET_WITHIN_S)
    asked = []

    def hello(node, timeout=None):
        asked.append(node.name)
        barrier.wait()
        return {"api": 1, "resources": [], "capabilities": []}

    monkeypatch.setattr(http, "hello", hello)
    return asked


def test_the_settings_node_list_probes_every_node_at_once(
        client, plexora_data_root, monkeypatch):
    """Three nodes that each take a while to answer cost one wait, not three."""
    _map_nodes()
    asked = _probes_that_wait_for_each_other(monkeypatch)

    listed = client.get("/settings/nodes").get_json()["nodes"]

    assert sorted(asked) == sorted(NAMES)
    assert {entry["name"]: entry["reachable"] for entry in listed} == {
        name: True for name in NAMES}, listed


def test_the_resources_section_probes_every_node_at_once(
        client, plexora_data_root, monkeypatch, tmp_path):
    """The edit page's node list is the same question and the same cost."""
    import polars as pl

    table = tmp_path / "cells.csv"
    pl.DataFrame({"CellID": [1, 2], "X_centroid": [1.0, 2.0],
                  "Y_centroid": [1.0, 2.0], "CD3": [0.5, 1.5]}).write_csv(table)
    project("probes_parallel_demo",
            dataset=csv_spec(table, cell_id="CellID", x="X_centroid",
                             y="Y_centroid", markers=("CD3",),
                             metadata=("CellID", "X_centroid", "Y_centroid")),
            confirmed=ALL_CONFIRMED).save()
    _map_nodes()
    asked = _probes_that_wait_for_each_other(monkeypatch)

    answer = client.get("/project/probes_parallel_demo/resources").get_json()

    assert sorted(asked) == sorted(NAMES)
    assert {entry["name"]: entry["reachable"] for entry in answer["nodes"]} == {
        name: True for name in NAMES}, answer["nodes"]


def test_the_connections_menu_probes_every_open_node_at_once(
        client, plexora_data_root, monkeypatch):
    """The health probe's budget is per node; three dead machines must not make
    the menu wait three of them."""
    _map_nodes()
    for name in NAMES:
        remote = remote_store.save(remote_store.Remote(
            name=name, target=f"me@{name}.cluster.edu", local_node=False))
        session = remote_sessions.RemoteSession(
            remote, kind=remote_sessions.KIND_NODE)
        session.state = remote_sessions.STATE_CONNECTED
        with remote_sessions._REGISTRY_LOCK:
            remote_sessions._SESSIONS[
                remote_sessions._key(remote_sessions.KIND_NODE, name)] = session
    asked = _probes_that_wait_for_each_other(monkeypatch)

    health = client.get("/remote_health").get_json()["health"]

    assert sorted(asked) == sorted(NAMES)
    assert {name: entry["state"] for name, entry in health.items()} == {
        name: "healthy" for name in NAMES}, health
