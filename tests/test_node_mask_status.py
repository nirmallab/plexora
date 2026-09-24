"""What the viewer is told about a cell mask a data node serves.

A node converts a mask after it is shared, and that conversion can fail -- a
folder this account cannot write, a full disk. `/resource_status` reports how
it is going, and opening the project retries a failed one: once per load, so a
failure that recurs is not retried every two seconds for as long as the viewer
is open. The browser half is tests/js/resource_status_probe.mjs.
"""

from __future__ import annotations

import types

import pytest

from plexora.server.models import data_model
from plexora.server.routes import data_routes


def _project(node="o2", resource_id="cell-ome-1"):
    binding = types.SimpleNamespace(node=node, resource_id=resource_id)
    return types.SimpleNamespace(resources={"segmentation": binding})


@pytest.fixture
def node_api(monkeypatch):
    """A stand-in for `plexora.nodes` that records what was asked."""
    from plexora import nodes

    calls = {"status": [], "prepare": []}
    answers = {"status": {"state": "ready", "generation": 1,
                          "mask_mode": "filled"},
               "prepare": {"state": "preparing", "generation": 1,
                           "mask_mode": "filled",
                           "progress": {"stage": "checking", "done": 0,
                                        "total": 0}}}

    def status(node, resource_id, timeout=30.0):
        calls["status"].append((node, resource_id))
        answer = answers["status"]
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)

    def prepare(node, resource_id, timeout=30.0):
        calls["prepare"].append((node, resource_id))
        return dict(answers["prepare"])

    monkeypatch.setattr(nodes, "resource_status", status)
    monkeypatch.setattr(nodes, "prepare_again", prepare)
    monkeypatch.setattr(data_routes, "_mask_retries", set())
    return types.SimpleNamespace(calls=calls, answers=answers)


def test_a_failed_mask_is_retried_once_per_load(node_api, monkeypatch):
    node_api.answers["status"] = {"state": "error", "error": "disk full",
                                  "generation": 1, "mask_mode": "filled"}
    monkeypatch.setattr(data_model, "load_generation", 7)

    first = data_routes._node_mask_report(_project())
    assert node_api.calls["prepare"] == [("o2", "cell-ome-1")]
    assert first[0]["state"] == "preparing"

    # The next poll finds it failed again, and does not retry.
    again = data_routes._node_mask_report(_project())
    assert len(node_api.calls["prepare"]) == 1
    assert again[0]["state"] == "error"
    assert again[0]["error"] == "disk full"

    # Reopening the project is a new load, and tries again.
    monkeypatch.setattr(data_model, "load_generation", 8)
    data_routes._node_mask_report(_project())
    assert len(node_api.calls["prepare"]) == 2


def test_progress_and_the_read_only_note_reach_the_viewer(node_api):
    node_api.answers["status"] = {
        "state": "preparing", "generation": 1, "mask_mode": "filled",
        "progress": {"stage": "converting", "done": 3, "total": 12},
        "warning": "/n/data is read-only for this account, so ...",
    }
    [row] = data_routes._node_mask_report(_project())
    assert row["node"] == "o2"
    assert row["progress"] == {"stage": "converting", "done": 3, "total": 12}
    assert "read-only" in row["warning"]
    assert node_api.calls["prepare"] == []


def test_a_ready_mask_names_the_version_to_draw(node_api):
    [row] = data_routes._node_mask_report(_project())
    assert row["state"] == "ready"
    assert row["version"] == "1-filled"


def test_a_mask_the_node_no_longer_serves_is_a_sentence(node_api):
    from plexora.server.providers.base import ResourceError

    node_api.answers["status"] = ResourceError(
        "data node 'o2' does not serve that resource: unknown 'cell-ome-1'")
    [row] = data_routes._node_mask_report(_project())
    assert row["state"] == "error"
    assert "no longer serves this mask" in row["error"]


def test_an_unreachable_node_is_left_to_the_unavailable_report(node_api):
    from plexora.server.providers.base import ResourceUnavailable

    node_api.answers["status"] = ResourceUnavailable("asleep", node="o2")
    assert data_routes._node_mask_report(_project()) == []


def test_a_project_with_no_node_mask_asks_nothing(node_api):
    assert data_routes._node_mask_report(None) == []
    assert data_routes._node_mask_report(
        types.SimpleNamespace(resources={})) == []
    assert node_api.calls["status"] == []
