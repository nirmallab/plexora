"""A surface as a layer: licensed to make, free to read, built in the background.

The routes end to end through the app: Surface Studio is refused without the
`volume:surfaces` entitlement and the refusal carries what the Pro modal needs; with
it, an isosurface and a set of cell surfaces are registered pending, built,
listed, served (immutable mesh, objects table, manifest), reused for the same
request, restyled through the generic layer PATCH and removed with their files.
Everything a surface that exists needs stays open on Free.
"""

import math
import time
from pathlib import Path

import numpy as np
import pytest
import tifffile

import plexora
from plexora import datasets
from plexora.datasource import register_image_datasource
from plexora.server.models import layer_jobs
from plexora.server.models.project import Project
from plexora.server.surface import mesh as mesh_mod
from plexora.server.surface import store as surface_store

SX, SZ = 0.5, 2.0


@pytest.fixture()
def client():
    return plexora.app.test_client()


@pytest.fixture()
def surfaces_license(license_issuer):
    license_issuer.install(license_issuer.issue(ent=["volume"]))
    return license_issuer


def _write_stack(path, depth=16, size=64):
    """Two channels: DNA flat, CD31 a bright sphere of radius 8 µm."""
    z, y, x = np.mgrid[:depth, :size, :size].astype(np.float32)
    r = np.sqrt(((x - size / 2) * SX) ** 2 + ((y - size / 2) * SX) ** 2
                + ((z - depth / 2) * SZ) ** 2)
    sphere = (1000 * np.clip(1 - r / 16.0, 0, 1)).astype(np.uint16)   # 500 at r = 8 µm
    flat = np.full_like(sphere, 100)
    data = np.stack([flat, sphere])
    metadata = {"axes": "CZYX", "PhysicalSizeX": SX, "PhysicalSizeY": SX,
                "PhysicalSizeZ": SZ, "Channel": {"Name": ["DNA", "CD31"]}}
    with tifffile.TiffWriter(path, bigtiff=True, ome=True) as writer:
        writer.write(data, metadata=metadata)
    return data


def _project(tmp_path, name="surf", mask=False):
    _write_stack(tmp_path / f"{name}.ome.tif")
    register_image_datasource(name, tmp_path / f"{name}.ome.tif")
    if mask:
        labels = np.zeros((16, 64, 64), np.uint16)
        labels[4:9, 8:20, 8:20] = 5
        labels[6:12, 30:44, 30:40] = 9
        labels[0:3, 50:60, 50:60] = 12      # cut by the first plane
        tifffile.imwrite(tmp_path / f"{name}_mask.tif", labels)
        datasets.add_layers(name, tmp_path / f"{name}_mask.tif", wait=True)
    return Project.find(name)


def _settle(name, layer_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = layer_jobs.get(name, layer_id) or {}
        if job.get("status") in ("ready", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"{layer_id} did not finish")


ISO = {"type": "isosurface", "channel": "CD31", "iso": 500, "smoothing": "light"}


# -- the licence ----------------------------------------------------------------------


def test_making_a_surface_is_pro_and_says_so(tmp_path, client):
    _project(tmp_path)
    for method, url in (("post", "/volume/surf/surfaces"),
                        ("get", "/volume/surf/surfaces/preview?channel=CD31&iso=500"),
                        ("get", "/volume/surf/surfaces/suggest?channel=CD31"),
                        ("post", "/volume/surf/surfaces/estimate")):
        response = getattr(client, method)(url, json=ISO) if method == "post" \
            else client.get(url)
        assert response.status_code == 403, url
        body = response.get_json()
        assert body["license"]["code"] == "license_required"
        assert body["license"]["entitlement"] == "volume:surfaces"
        assert body["license"]["label"] == "Surface Studio"
    # Listing what exists is a read: open on Free, and it says the plan.
    listing = client.get("/volume/surf/surfaces").get_json()
    assert listing == {"surfaces": [], "licensed": False}


def test_an_ai_only_licence_does_not_unlock_it(tmp_path, client, license_issuer):
    license_issuer.install(license_issuer.issue(ent=["ai"]))
    _project(tmp_path)
    assert client.post("/volume/surf/surfaces", json=ISO).status_code == 403


# -- isosurfaces ----------------------------------------------------------------------


def test_an_isosurface_is_built_served_and_reused(tmp_path, client, surfaces_license):
    _project(tmp_path)
    response = client.post("/volume/surf/surfaces", json=ISO)
    assert response.status_code == 202, response.get_json()
    answer = response.get_json()
    layer_id = answer["layer"]
    assert layer_id.startswith("surf-") and answer["status"] == "pending"
    assert answer["plan"]["level"] == 0
    assert _settle("surf", layer_id)["status"] == "ready", layer_jobs.get("surf", layer_id)

    layer = Project.find("surf").layer(layer_id)
    assert layer.kind == "surface" and layer.modality == "surface"
    assert layer.label == "CD31 surface at 500"
    assert layer.extra["surface"]["iso"] == 500
    manifest = client.get(f"/volume/surf/surfaces/{layer_id}/manifest").get_json()
    assert manifest["type"] == "isosurface" and manifest["algorithm"] == "marching_cubes_lewiner"
    assert manifest["source"]["channel"] == "CD31" and manifest["params"]["iso"] == 500
    assert manifest["spacing"]["units"] == "um"
    assert manifest["spacing"]["scale"] == pytest.approx([SX, SX, SZ])
    assert manifest["objects"]["count"] == 1
    lod = manifest["lods"][-1]

    mesh = client.get(f"/volume/surf/surfaces/{layer_id}/mesh")
    assert mesh.status_code == 200 and mesh.headers["ETag"].strip('"') == lod["sha256"]
    surface = mesh_mod.from_bytes(mesh.data)
    assert surface.n_triangles == lod["triangles"]
    again = client.get(f"/volume/surf/surfaces/{layer_id}/mesh",
                       headers={"If-None-Match": mesh.headers["ETag"]})
    assert again.status_code == 304

    objects = client.get(f"/volume/surf/surfaces/{layer_id}/objects").get_json()
    row = dict(zip(objects["columns"], objects["rows"][0]))
    assert row["closed"] is True
    assert row["mesh_volume_um3"] == pytest.approx(4 / 3 * math.pi * 512, rel=0.05)

    # The same request is the same surface.
    repeat = client.post("/volume/surf/surfaces", json={**ISO, "label": "Vessels"})
    assert repeat.status_code == 200 and repeat.get_json()["layer"] == layer_id

    listing = client.get("/volume/surf/surfaces").get_json()
    assert listing["licensed"] is True
    entry = listing["surfaces"][0]
    assert entry["id"] == layer_id and entry["summary"]["triangles"] == lod["triangles"]


def test_reading_a_surface_stays_open_after_the_licence_goes(tmp_path, client, license_issuer):
    _project(tmp_path)
    license_issuer.install(license_issuer.issue(ent=["volume"]))
    layer_id = client.post("/volume/surf/surfaces", json=ISO).get_json()["layer"]
    assert _settle("surf", layer_id)["status"] == "ready"
    # A licence without Surface Studio replaces it.
    license_issuer.install(license_issuer.issue(ent=["ai"]))
    assert client.post("/volume/surf/surfaces", json=ISO).status_code == 403
    for path in ("manifest", "mesh", "objects"):
        assert client.get(f"/volume/surf/surfaces/{layer_id}/{path}").status_code == 200, path
    patched = client.patch(f"/project/surf/layers/{layer_id}",
                           json={"render": {"color": "#ff0000", "style": "wireframe"}})
    assert patched.status_code == 200, patched.get_json()
    assert Project.find("surf").layer(layer_id).render["style"] == "wireframe"


def test_removing_a_surface_deletes_its_folder(tmp_path, client, surfaces_license):
    _project(tmp_path)
    layer_id = client.post("/volume/surf/surfaces", json=ISO).get_json()["layer"]
    assert _settle("surf", layer_id)["status"] == "ready"
    folder = Path(Project.find("surf").layer(layer_id).src)
    assert (folder / "manifest.json").exists()
    assert client.delete(f"/project/surf/layers/{layer_id}").status_code == 200
    assert not folder.exists()
    assert Project.find("surf").layer(layer_id) is None


def test_what_cannot_be_built_is_refused_with_a_reason(tmp_path, client, surfaces_license):
    _project(tmp_path)
    for body, words in ((dict(ISO, channel="nope"), "no channel"),
                        ({"type": "isosurface", "channel": "CD31"}, "iso"),
                        (dict(ISO, smoothing="lots"), "smoothing"),
                        (dict(ISO, quality="ultra"), "quality"),
                        ({"type": "label_surfaces"}, "3-D mask")):
        response = client.post("/volume/surf/surfaces", json=body)
        assert response.status_code == 400, body
        assert words in response.get_json()["error"], (body, response.get_json())


def test_nothing_at_the_isovalue_fails_the_build_with_the_reason(tmp_path, client,
                                                                surfaces_license):
    _project(tmp_path)
    layer_id = client.post("/volume/surf/surfaces", json=dict(ISO, iso=60000)).get_json()["layer"]
    job = _settle("surf", layer_id)
    assert job["status"] == "failed" and "crosses" in job["error"]


# -- the slider -----------------------------------------------------------------------


def test_the_preview_answers_a_mesh_with_contours_and_suggestions(tmp_path, client,
                                                                   surfaces_license):
    _project(tmp_path)
    response = client.get("/volume/surf/surfaces/preview?channel=CD31&iso=500&z=8&x=32&y=32")
    assert response.status_code == 200, response.get_data(as_text=True)
    preview = mesh_mod.from_bytes(response.data)
    assert preview.header["preview"] is True and preview.n_objects == 1
    contours = preview.header["contours"]
    assert contours["xy"]["z"] == 8 and contours["xy"]["lines"]
    line = np.asarray(contours["xy"]["lines"][0]).reshape(-1, 2)
    radius = np.hypot(line[:, 0] - 32, line[:, 1] - 32)
    assert radius.mean() == pytest.approx(8 / SX, rel=0.1)   # 8 µm in pixels
    assert contours["xz"]["lines"] and contours["yz"]["lines"]
    empty = mesh_mod.from_bytes(client.get(
        "/volume/surf/surfaces/preview?channel=CD31&iso=60000").data)
    assert empty.n_triangles == 0 and "crosses" in empty.header["empty"]

    suggestion = client.get("/volume/surf/surfaces/suggest?channel=CD31").get_json()
    assert suggestion["units"] == "raw" and suggestion["channel"] == "CD31"
    assert 0 < suggestion["default"] < 1000
    assert len(suggestion["histogram"]["counts"]) == 256


def test_the_estimate_says_what_a_request_would_build(tmp_path, client, surfaces_license):
    _project(tmp_path)
    answer = client.post("/volume/surf/surfaces/estimate",
                         json=dict(ISO, region={"box": [16, 16, 32, 32], "z": [2, 13]})).get_json()
    assert answer["ok"] is True and answer["plan"]["level"] == 0
    assert answer["plan"]["grid"]["shape"] == [12, 32, 32]


# -- cell surfaces --------------------------------------------------------------------


def test_cell_surfaces_follow_the_mask_ids_and_join_the_voxel_table(tmp_path, client,
                                                                   surfaces_license):
    project = _project(tmp_path, name="cells", mask=True)
    from plexora.server.volume import objects as volume_objects

    volume_objects.measure_project_objects(project, units="physical", attach=False)
    response = client.post("/volume/cells/surfaces", json={"type": "label_surfaces"})
    assert response.status_code == 202, response.get_json()
    answer = response.get_json()
    assert answer["objects"] == 3
    layer_id = answer["layer"]
    assert _settle("cells", layer_id)["status"] == "ready", layer_jobs.get("cells", layer_id)
    manifest = client.get(f"/volume/cells/surfaces/{layer_id}/manifest").get_json()
    assert manifest["objects"]["count"] == 3 and manifest["objects"]["ids_from"] == "labels"
    assert manifest["objects"]["touching_bounds"] == 1
    surface = mesh_mod.from_bytes(client.get(f"/volume/cells/surfaces/{layer_id}/mesh").data)
    assert surface.ids().tolist() == [5, 9, 12]
    objects = client.get(f"/volume/cells/surfaces/{layer_id}/objects?ids=9").get_json()
    row = dict(zip(objects["columns"], objects["rows"][0]))
    assert row["object"] == 9 and row["voxel_count"] == 6 * 14 * 10
    assert row["voxel_volume_um3"] == pytest.approx(840 * SX * SX * SZ)
    assert row["mesh_volume_um3"] == pytest.approx(row["voxel_volume_um3"], rel=0.2)

    selected = client.post("/volume/cells/surfaces",
                           json={"type": "label_surfaces", "ids": [9]}).get_json()
    assert selected["objects"] == 1 and selected["layer"] != layer_id
    assert _settle("cells", selected["layer"])["status"] == "ready"
    assert Project.find("cells").layer(selected["layer"]).label == "Surfaces of 1 cell"


def test_a_preview_then_a_standard_build_are_two_layers_with_the_mask_role(tmp_path, client,
                                                                           surfaces_license):
    """What the viewer's Cells control asks for in 3-D: a quick preview, then
    the standard build, both marked as the mask's own -- with no object table,
    so the objects are counted on the mask's coarsest level."""
    _project(tmp_path, name="cells", mask=True)
    asked = {"type": "label_surfaces", "smoothing": "light", "role": "mask", "t": 0}
    preview = client.post("/volume/cells/surfaces",
                          json={**asked, "quality": "preview", "label": "Cells (preview)"})
    assert preview.status_code == 202, preview.get_json()
    standard = client.post("/volume/cells/surfaces",
                           json={**asked, "quality": "standard", "label": "Cells of the 3-D mask"})
    first, second = preview.get_json()["layer"], standard.get_json()["layer"]
    assert first != second
    for layer_id in (first, second):
        assert _settle("cells", layer_id)["status"] == "ready", layer_jobs.get("cells", layer_id)
        manifest = client.get(f"/volume/cells/surfaces/{layer_id}/manifest").get_json()
        assert manifest["algorithm"] == "surface_nets_multilabel"
        assert manifest["params"]["role"] == "mask"
        assert manifest["objects"]["count"] == 3
        assert manifest["build"]["report"]["engine"]["blocks"] >= 1
    listing = {entry["id"]: entry for entry in client.get("/volume/cells/surfaces")
               .get_json()["surfaces"]}
    assert listing[first]["role"] == "mask" and listing[first]["spec"]["quality"] == "preview"
    assert listing[second]["spec"]["quality"] == "standard" and not listing[second]["stale"]
    # A role is the mask's only, and only for cell surfaces.
    assert client.post("/volume/cells/surfaces", json={**ISO, "role": "mask"}).status_code == 400
    assert client.post("/volume/cells/surfaces",
                       json={**asked, "role": "other"}).status_code == 400
    # The same request again is the layer that exists.
    again = client.post("/volume/cells/surfaces", json={**asked, "quality": "preview"})
    assert again.status_code == 200 and again.get_json()["layer"] == first


def test_the_view_state_keeps_the_3d_camera_and_what_is_shown(tmp_path, client):
    _project(tmp_path)
    state = {"mode": "volume", "volume": {"camera": {"q": [0, 0, 0.6, 0.8], "dist": 2.5,
                                                     "pan": [0.1, -0.2]},
                                          "show": {"volume": False}}}
    stored = client.put("/view_state/surf", json=state).get_json()["state"]
    assert stored["volume"]["camera"]["q"] == pytest.approx([0, 0, 0.6, 0.8])
    assert stored["volume"]["camera"]["pan"] == pytest.approx([0.1, -0.2])
    assert stored["volume"]["show"] == {"volume": False, "surfaces": True}
    bad = client.put("/view_state/surf", json={"volume": {"camera": {"q": [1, "x", 0, 0]}}})
    assert "q" not in bad.get_json()["state"]["volume"]["camera"]


def test_a_surface_of_a_changed_source_says_it_is_stale(tmp_path, client, surfaces_license):
    import os

    _project(tmp_path)
    layer_id = client.post("/volume/surf/surfaces", json=ISO).get_json()["layer"]
    assert _settle("surf", layer_id)["status"] == "ready"
    entry = client.get("/volume/surf/surfaces").get_json()["surfaces"][0]
    assert entry["stale"] is False
    image = tmp_path / "surf.ome.tif"
    stat = image.stat()
    os.utime(image, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    entry = client.get("/volume/surf/surfaces").get_json()["surfaces"][0]
    assert entry["stale"] is True
