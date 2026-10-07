"""A table changed underneath Plexora: the change becomes visible, and tabs hear of it.

SCIMAP Pro (or plain anndata) writes a column into an AnnData a Plexora project
reads. Before the bridge nothing noticed: the project's list of `obs` columns
was recorded at registration, the metadata caches held on, and an agent
session's held copy of a zarr store looked current because a directory's mtime
does not move when a column inside it is rewritten.
"""

from __future__ import annotations

import pytest

import plexora
from plexora.agent import AgentSession, registry
from plexora.server.models import data_model, dataset_events
from plexora.server.models import viewer_sessions as vs
from plexora.server.models.project import Project
from tests.scimappro_fixtures import add_obs_column, make_anndata_project


@pytest.fixture(autouse=True)
def _fresh_sessions():
    vs._reset_for_tests()
    yield
    vs._reset_for_tests()


@pytest.fixture(params=["zarr", "h5ad"])
def made(request, tmp_path):
    return make_anndata_project(tmp_path, fmt=request.param)


def _obs_columns(name):
    return list(Project.load(name).dataset.obs_columns)


def test_an_out_of_band_column_is_offered_after_the_event(made):
    name = made["name"]
    assert "ne_label" not in _obs_columns(name)
    add_obs_column(made["table"], "ne_label", ["a", "b"] * (len(made["cells"]) // 2))
    assert "ne_label" not in _obs_columns(name)   # nothing noticed on its own

    answer = dataset_events.on_dataset_changed(name, {"sections": ["obs:ne_label"]})
    assert answer["handled"] and answer["obs_columns_added"] == ["ne_label"]
    assert "ne_label" in _obs_columns(name)
    # How the table is READ is untouched: same roles, same subset.
    spec = Project.load(name).dataset
    assert spec.obs_id_field == "CellID" and spec.subset == {"column": "imageid",
                                                             "value": "slide_A"}


def test_the_session_identity_sees_a_write_inside_the_store(made):
    from plexora.agent.session import _identity

    name = made["name"]
    before = _identity(Project.load(name))
    add_obs_column(made["table"], "ne_label", ["a", "b"] * (len(made["cells"]) // 2))
    assert _identity(Project.load(name)) != before


def test_a_held_table_is_dropped_after_the_write(made):
    name = made["name"]
    session = AgentSession()
    first = session.data(name)
    add_obs_column(made["table"], "ne_label", ["a", "b"] * (len(made["cells"]) // 2))
    dataset_events.on_dataset_changed(name, {"sections": ["obs:ne_label"]})
    second = session.data(name)
    assert second.table._provider is not first.table._provider
    assert "ne_label" in second.table.metadata_columns


def test_the_metadata_caches_are_dropped(made):
    name = made["name"]
    data_model._metadata_column_cache[(name, "phenotype")] = object()
    data_model._metadata_column_cache[("other", "phenotype")] = object()
    data_model._gmm_cache[(name, ("cell_explorer", "variables"))] = ["stale"]
    data_model._gmm_cache[(name, ("gmm", "CD8"))] = "a fit, which obs does not change"
    data_model._description_cache[name] = {"stale": True}
    try:
        dropped = data_model.forget_metadata(name)
        assert dropped["metadata_columns"] == 1 and dropped["plugin_entries"] == 1
        assert dropped["description"] is True
        assert ("other", "phenotype") in data_model._metadata_column_cache
        assert (name, ("gmm", "CD8")) in data_model._gmm_cache
    finally:
        data_model._metadata_column_cache.pop(("other", "phenotype"), None)
        data_model._gmm_cache.pop((name, ("gmm", "CD8")), None)


def test_sections_say_what_kind_of_change_it_is():
    assert dataset_events.classify(["obs:phenotype"]) == {
        "metadata": True, "matrix": False, "gates": False, "rois": False}
    assert dataset_events.classify(["X"])["matrix"]
    assert dataset_events.classify(["layers:log"])["matrix"]
    assert dataset_events.classify(["uns:gates"])["gates"]
    assert dataset_events.classify(["obs:rois_tumour"])["rois"]
    assert dataset_events.classify([])["metadata"] and not dataset_events.classify([])["matrix"]


def test_the_event_route_refreshes_before_the_tabs_hear(made):
    name = made["name"]
    client = plexora.app.test_client()
    sid = client.post("/agent/v1/viewer/sessions",
                      json={"project": name}).get_json()["session"]["view_id"]
    add_obs_column(made["table"], "ne_label", ["a", "b"] * (len(made["cells"]) // 2))
    answer = client.post("/agent/v1/events", json={
        "project": name, "plugin": "core", "kind": "dataset.changed",
        "payload": {"sections": ["obs:ne_label"]}})
    assert answer.status_code == 200 and answer.get_json()["delivered"] == 1
    assert "ne_label" in _obs_columns(name)
    events = vs.wait_for_work(sid, wait_s=0)["events"]
    assert [(e["plugin"], e["kind"]) for e in events][-1] == ("core", "dataset.changed")


def test_notify_viewers_runs_the_same_hook(made):
    from plexora import api

    add_obs_column(made["table"], "ne_label", ["a", "b"] * (len(made["cells"]) // 2))
    api.notify_viewers(made["name"], "core", "dataset.changed", {"sections": ["obs:ne_label"]})
    assert "ne_label" in _obs_columns(made["name"])


def test_any_other_event_passes_through_untouched(made, monkeypatch):
    calls = []
    monkeypatch.setattr(dataset_events, "on_dataset_changed",
                        lambda *args, **kwargs: calls.append(args))
    from plexora import api

    api.notify_viewers(made["name"], "gating", "gating.changed", {})
    assert calls == []


def test_an_unknown_project_is_answered_not_raised():
    answer = dataset_events.on_dataset_changed("nope", {"sections": ["obs:x"]})
    assert answer == {"handled": False, "project": "nope", "reason": "no such project"}


def test_the_capability_works_without_the_bridge_package(made):
    registry.discover([])
    told = []
    add_obs_column(made["table"], "ne_label", ["a", "b"] * (len(made["cells"]) // 2))
    answer = registry.invoke(AgentSession(), "bridge_dataset_changed",
                             {"project": made["name"], "sections": ["obs:ne_label"],
                              "producer": "scimappro"},
                             notify=lambda *args: told.append(args) or 1)
    assert answer["ok"], answer
    assert answer["result"]["delivered"] == 1
    assert told[0][1:3] == ("core", "dataset.changed")
    assert told[0][3]["obs_columns_added"] == ["ne_label"]


def test_bridge_event_reaches_every_project_bound_to_the_workspace(made):
    from spatialbridge import workspace
    from spatialbridge.schema import ImageBinding
    from spatialbridge.tools import BridgeTools

    from plexora.agent.bridge_provider import PlexoraProvider

    registry.discover([])
    ws = workspace.ensure(made["table"], producer="scimappro")
    ws.bind_image(ImageBinding(image_id="slide_A", project=made["name"]))
    add_obs_column(made["table"], "ne_label", ["a", "b"] * (len(made["cells"]) // 2))
    revision = ws.commit(["obs:ne_label"], "scimappro")
    told = []
    tools = BridgeTools(PlexoraProvider(notify=lambda *args: told.append(args) or 1))
    answer = tools.call("bridge_event", {"event": {
        "kind": "dataset.changed", "workspace_id": ws.id, "revision": revision,
        "sections": ["obs:ne_label"], "producer": "scimappro"}})
    assert answer["handled"] and answer["projects"] == [made["name"]]
    assert "ne_label" in _obs_columns(made["name"])
    assert told and told[0][0] == made["name"]
