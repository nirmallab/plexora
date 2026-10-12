"""The agent's surface tools, driven through `invoke` as MCP and the harness reach them.

Licensed like the routes: making and analysing surfaces needs `surfaces`;
listing, measuring, restyling, exporting and deleting stay Free. Every result
says what an agent must not claim from a mesh, and no tool loads the project
into the viewer's slot.
"""

from pathlib import Path
import zipfile

import pytest

from plexora.agent import AgentSession, invoke, registry
from tests.test_surface_layer import _project


@pytest.fixture(autouse=True)
def _discovered():
    registry.discover([])
    yield


@pytest.fixture()
def surfaces_license(license_issuer):
    license_issuer.install(license_issuer.issue(ent=["surfaces"]))
    return license_issuer


def _ok(result):
    assert result["ok"], result
    return result["result"] if "result" in result else result


def _job(session, tool, arguments):
    """A job capability run to its end: `(ok, result or error)`."""
    started = invoke(session, tool, arguments)
    if not started["ok"]:
        return False, started["error"]
    done = _ok(invoke(session, "job_wait", {"job_id": started["result"]["job_id"],
                                            "timeout_s": 60}))["job"]
    if done["status"] != "done":
        return False, done.get("error")
    return True, done["result"]


CREATE = {"project": "surf", "type": "isosurface", "channel": "CD31", "iso": 500}


def test_making_a_surface_needs_surface_studio(tmp_path):
    _project(tmp_path)
    ok, error = _job(AgentSession(), "surface_create", CREATE)
    assert not ok and error["code"] == "license_required"
    assert error["detail"]["entitlement"] == "surfaces"
    assert _ok(invoke(AgentSession(), "surface_list", {"project": "surf"}))["surfaces"] == []


def test_a_surface_from_creation_to_deletion(tmp_path, surfaces_license):
    from plexora.server.models import data_model

    _project(tmp_path)
    session = AgentSession()
    ok, made = _job(session, "surface_create", CREATE)
    assert ok, made
    surface = made["surface"]
    assert made["status"] == "ready" and made["objects"] == 1 and made["triangles"] > 100
    assert made["units"] == "um" and "not a validated" in made["note"]
    assert made["receipt"]["persistent_state"] == "project_config"

    suggestion = _ok(invoke(session, "surface_suggest_threshold",
                            {"project": "surf", "channel": "CD31"}))
    assert suggestion["units"] == "raw" and "otsu" in suggestion["thresholds"]

    listed = _ok(invoke(session, "surface_list", {"project": "surf"}))["surfaces"]
    assert [entry["surface"] for entry in listed] == [surface]

    inspected = _ok(invoke(session, "surface_inspect", {"project": "surf", "surface": surface}))
    assert inspected["open_objects"] == 0 and inspected["touching_bounds"] == 0
    assert any("smoothing moved" in finding for finding in inspected["findings"])

    measured = _ok(invoke(session, "surface_measure", {"project": "surf", "surface": surface}))
    assert measured["objects"] == 1 and measured["counts"]["closed"] == 1
    assert measured["measurements"]["mesh_volume_um3"]["n"] == 1
    rows = invoke(session, "surface_objects", {"project": "surf", "surface": surface})
    assert not rows["ok"] and rows["error"]["code"] == "permission_required"

    smoothed = _ok(invoke(session, "surface_refine", {"project": "surf", "surface": surface,
                                                      "op": "smooth", "smoothing": "medium"}))
    assert smoothed["versions"][-1]["op"] == "smooth"
    undone = _ok(invoke(session, "surface_refine", {"project": "surf", "surface": surface,
                                                    "op": "undo"}))
    assert undone["versions"] == []

    restyled = _ok(invoke(session, "surface_update", {"project": "surf", "surface": surface,
                                                      "style": "glass", "opacity": 0.4}))
    assert restyled["render"]["style"] == "glass" and restyled["render"]["opacity"] == 0.4

    exported = _ok(invoke(session, "surface_export", {"project": "surf", "surface": surface,
                                                      "format": "obj"}))
    path = Path(exported["path"])
    assert path.exists() and path.suffix == ".zip"
    with zipfile.ZipFile(path) as archive:
        assert any(name.endswith(".obj") for name in archive.namelist())

    near = _ok(invoke(session, "surface_neighbours", {"project": "surf", "surface": surface,
                                                      "object": 1}))
    assert near["neighbours"] == [] and "not interaction" in near["note"]

    refused = invoke(session, "surface_delete", {"project": "surf", "surface": surface})
    assert not refused["ok"] and refused["error"]["code"] == "permission_required"
    from plexora.agent.policy import Policy

    gone = _ok(invoke(session, "surface_delete", {"project": "surf", "surface": surface,
                                                  "confirm": True},
                      policy=Policy(allow_destructive=True)))
    assert gone["deleted"] is True
    assert _ok(invoke(session, "surface_list", {"project": "surf"}))["surfaces"] == []
    # Nothing here put the project in the viewer's slot.
    assert data_model._loaded_source is None


def test_a_refused_surface_says_why(tmp_path, surfaces_license):
    _project(tmp_path)
    ok, bad = _job(AgentSession(), "surface_create", {**CREATE, "channel": "nope"})
    assert not ok and "no channel" in bad["message"]
    ok, nothing = _job(AgentSession(), "surface_create", {**CREATE, "iso": 60000})
    assert not ok and "crosses" in nothing["message"]


def test_the_volume_profile_offers_the_surface_tools():
    from plexora.mcp import profiles

    names = set(profiles.PROFILES["volume"])
    for tool in ("surface_create", "surface_list", "surface_inspect", "viewer_set_volume_mode",
                 "viewer_set_camera", "viewer_set_surface"):
        assert tool in names, tool
    tools = {cap.tool_name for cap in registry.all_capabilities()}
    assert names & {"surface_create", "viewer_set_surface"} <= tools
