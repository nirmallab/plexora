"""Plexora 3D (`volume`): looking at a stack is Free, analysing it is Paid.

On Free a z-stack opens with its planes, projections, sections and volume
view; the controls that measure, track, review or segment cells in 3-D, run
Stack QC, make surfaces, carry a region through planes or make one from cells
are absent from the page, and their routes and capabilities refuse with the
`license` block. What those features made stays readable. With `volume` it is
all there.
"""

from __future__ import annotations

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.agent import AgentSession, registry
from plexora.datasource import register_image_datasource
from tests import volume_fixtures as vf

NAME = "zlic"


@pytest.fixture
def client():
    return plexora.app.test_client()


@pytest.fixture
def stack(tmp_path):
    """A two-channel z-stack with a 3-D mask."""
    path, _ = vf.write_ome_volume(tmp_path / f"{NAME}.ome.tif", axes="CZYX", channels=2,
                                  depth=6, height=64, width=64, names=["DNA", "CD8"],
                                  physical_z=2.0)
    labels = np.zeros((6, 64, 64), dtype=np.uint16)
    labels[1:4, 10:20, 10:20] = 5
    labels[3:6, 30:40, 30:46] = 9
    tifffile.imwrite(tmp_path / f"{NAME}_mask.tif", labels)
    register_image_datasource(NAME, path)
    datasets.add_layers(NAME, tmp_path / f"{NAME}_mask.tif", wait=True)
    registry.discover(["qc", "roi", "segmentation"])
    return NAME


def _license_block(response):
    assert response.status_code == 403, response.get_data(as_text=True)[:300]
    block = response.get_json()["license"]
    assert block["code"] == "license_required"
    return block


#: Present on a z-stack page only with Plexora 3D: the Surface Studio button
#: (index.html) and, with the Depth card open, its Measure / Link buttons
#: (depth_panel.html).
VIEWER_3D_IDS = ('id="layer_surface_button"',)
DEPTH_3D_IDS = ("data-depth-cells", 'id="depth_measure_button"', 'id="depth_track_button"')


# -- the page -------------------------------------------------------------------------


def test_a_free_stack_page_shows_viewing_and_nothing_paid(stack, client):
    html = client.get(f"/{stack}").get_data(as_text=True)
    for marker in VIEWER_3D_IDS:
        assert marker not in html, marker
    depth = client.get(f"/{stack}?tool=depth").get_data(as_text=True)
    assert 'id="depth_panel"' in depth or "depth-panel" in depth, "viewing a stack is Free"
    for marker in DEPTH_3D_IDS:
        assert marker not in depth, marker
    assert 'data-tool="segmentation"' not in html
    assert "/plugins/segmentation/static/" not in html
    assert "plx-paid-badge" not in html


def test_a_paid_stack_page_has_every_3d_control(stack, client, paid_license):
    html = client.get(f"/{stack}").get_data(as_text=True)
    for marker in VIEWER_3D_IDS:
        assert marker in html, marker
    assert 'data-tool="segmentation"' in html
    depth = client.get(f"/{stack}?tool=depth").get_data(as_text=True)
    for marker in DEPTH_3D_IDS:
        assert marker in depth, marker


@pytest.mark.parametrize("granted", [False, True])
def test_the_qc_and_roi_panels_leave_out_their_3d_sections(stack, client, license_issuer,
                                                          granted):
    if granted:
        license_issuer.install(license_issuer.issue(ent=["volume"]))
    qc = client.get(f"/{stack}?tool=qc").get_data(as_text=True)
    roi = client.get(f"/{stack}?tool=roi").get_data(as_text=True)
    assert ('id="qc_tool_stack"' in qc) is granted
    assert 'id="qc_tool_blur"' in qc, "2-D QC stays"
    assert ('id="roi_from_cells"' in roi) is granted


# -- the routes -----------------------------------------------------------------------


def test_measuring_and_tracking_refuse_on_free_and_reading_stays_open(stack, client):
    assert _license_block(client.post(f"/volume/{stack}/objects", json={}))["entitlement"] \
        == "volume:objects"
    _license_block(client.post(f"/volume/{stack}/tracks", json={}))
    assert client.get(f"/volume/{stack}/objects").status_code == 200
    assert client.get(f"/volume/{stack}/info").status_code == 200


def test_stack_qc_refuses_on_free_but_its_last_answer_reads(stack, client):
    block = _license_block(client.post("/plugins/qc/stack/run", json={"datasource": stack}))
    assert block["entitlement"] == "volume:analysis"
    _license_block(client.post("/plugins/qc/stack/plane", json={"datasource": stack}))
    assert client.get(f"/plugins/qc/stack?datasource={stack}").status_code == 200


def test_a_region_from_cells_and_carrying_refuse_on_free(stack, client):
    _license_block(client.post("/plugins/roi/api/from_cells",
                               json={"datasource": stack, "ids": [5]}))
    _license_block(client.post("/segment/v1/carry", json={"datasource": stack, "z": 2}))
    _license_block(client.post("/segment/v1/install3d", json={}))
    assert client.get("/segment/v1/status").status_code == 200


def test_the_segmentation_plugin_refuses_on_free(stack, client):
    routes = [rule.rule for rule in plexora.app.url_map.iter_rules()
              if rule.rule.startswith("/plugins/segmentation/") and "static" not in rule.rule
              and "POST" in rule.methods]
    assert routes
    _license_block(client.post(routes[0].replace("<", "").replace(">", ""), json={}))


def _flat_region(stack):
    answer = registry.invoke(AgentSession(), "create_roi", {
        "project": stack, "category": "Cells",
        "points": [[10, 10], [20, 10], [20, 20], [10, 20]]})
    assert answer["ok"], answer
    return answer["result"]["roi"]["id"], answer["result"]["receipt"]["revision_after"]


CONTOURS = {"kind": "contours", "keys": [2],
            "planes": {"1": {"type": "Polygon", "coordinates": [[[10, 10], [20, 10], [20, 20],
                                                               [10, 20], [10, 10]]]},
                       "2": {"type": "Polygon", "coordinates": [[[10, 10], [20, 10], [20, 20],
                                                               [10, 20], [10, 10]]]}}}


def _set_solid(client, stack, roi_id, revision, solid):
    return client.post("/plugins/roi/api/operations", json={
        "datasource": stack, "base_revision": revision,
        "operations": [{"op": "roi.set_solid", "id": roi_id, "solid": solid}]})


def test_making_a_flat_region_3d_is_paid_but_a_3d_one_stays_editable(stack, client,
                                                                     license_issuer):
    roi_id, revision = _flat_region(stack)
    block = _license_block(_set_solid(client, stack, roi_id, revision, CONTOURS))
    assert block["entitlement"] == "volume:objects"

    license_issuer.install(license_issuer.issue(ent=["volume"]))
    made = _set_solid(client, stack, roi_id, revision, CONTOURS)
    assert made.status_code == 200, made.get_json()
    revision = made.get_json()["revision"]

    # The licence lapses: the 3-D region it made is still edited and flattened.
    license_issuer.install(license_issuer.issue(ent=["ai"]))
    edited = _set_solid(client, stack, roi_id, revision, CONTOURS)
    assert edited.status_code == 200, edited.get_json()
    flat = _set_solid(client, stack, roi_id, edited.get_json()["revision"], None)
    assert flat.status_code == 200, flat.get_json()


# -- the agent ------------------------------------------------------------------------


@pytest.mark.parametrize("tool,arguments", [
    ("measure_objects", {}),
    ("run_qc_3d", {}),
    ("colocalize_3d", {"channels": ["DNA", "CD8"]}),
    ("object_review_start", {}),
])
def test_3d_analysis_capabilities_refuse_on_free(stack, tool, arguments):
    answer = registry.invoke(AgentSession(), tool, {"project": stack, **arguments})
    assert not answer["ok"]
    assert answer["error"]["code"] == "license_required", answer["error"]
    assert answer["error"]["detail"]["entitlement"].startswith("volume:")


def test_create_roi_from_cells_is_paid_but_a_flat_one_is_free(stack):
    refused = registry.invoke(AgentSession(), "create_roi", {
        "project": stack, "category": "Cells", "cells": [5]})
    assert not refused["ok"] and refused["error"]["code"] == "license_required"
    _flat_region(stack)


@pytest.mark.paid
def test_with_the_paid_default_the_licence_lets_them_through(stack, client):
    # Past the licence, each answers on its own merits (202, or 400 with a
    # reason when the mask has not become a label volume on this machine).
    assert client.post(f"/volume/{stack}/objects", json={"units": "voxel"}).status_code != 403
    assert client.post("/plugins/qc/stack/plane",
                       json={"datasource": stack}).status_code != 403
    answer = registry.invoke(AgentSession(), "create_roi", {
        "project": stack, "category": "Cells", "cells": [5]})
    assert answer["ok"] or answer["error"]["code"] != "license_required", answer
