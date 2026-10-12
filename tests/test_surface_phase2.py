"""Surfaces after they are built: export and import, edits as versions,
neighbours and contact areas, and the cuts the 2-D views draw.

Formats round-trip through Plexora's own readers to the same triangles in
microns; an edit is a new version that undo removes, the mask untouched;
neighbours are geometry with the contact area read from the mask.
"""

import io
import json
import math
import zipfile

import numpy as np
import pytest

from plexora.server.surface import contacts, export, importers, marching, refine, sections
from plexora.server.surface import isosurface
from plexora.server.surface import mesh as mesh_mod

from tests.test_surface_layer import ISO, _project, _settle  # noqa: F401 -- fixtures below


@pytest.fixture()
def client():
    import plexora

    return plexora.app.test_client()


@pytest.fixture()
def surfaces_license(license_issuer):
    license_issuer.install(license_issuer.issue(ent=["surfaces"]))
    return license_issuer


def _two_boxes():
    vol = np.zeros((12, 40, 40), np.float32)
    vol[2:10, 4:16, 4:16] = 100
    vol[4:8, 24:30, 24:30] = 100
    surface, _ = isosurface.extract(lambda a, b: vol[a:b + 1], marching.Grid.of(vol.shape), 50.0)
    return surface


# -- formats --------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["ply", "obj", "stl", "glb"])
def test_every_format_reads_back_to_the_same_triangles(fmt):
    surface = _two_boxes()
    scale = (0.5, 0.5, 2.0)
    data, name, _ = export.export(surface, fmt, scale=scale, units="um", bundle=False,
                                  manifest={"label": "boxes", "type": "isosurface"})
    assert name == f"boxes.{fmt}"
    positions, triangles, ids, comments = importers.read(data, fmt)
    back = importers.place(positions, triangles, ids, units="um", spacing_um=scale)
    assert back.n_triangles == surface.n_triangles
    np.testing.assert_allclose(np.sort(back.positions, axis=0), np.sort(surface.positions, axis=0),
                               atol=1e-3)
    if fmt != "stl":
        assert back.ids().tolist() == surface.ids().tolist()
    if fmt in ("ply", "obj"):
        assert importers.units_from_comments(comments) == "um"


def test_a_bundle_carries_the_manifest_and_imports_whole():
    surface = _two_boxes()
    data, name, mimetype = export.export(surface, "ply", scale=(1, 1, 1), units="voxel",
                                         manifest={"label": "boxes", "cache_key": "abc"})
    assert name == "boxes.ply.zip" and mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        side = json.loads(archive.read("boxes.manifest.json"))
    assert side["cache_key"] == "abc" and side["export"]["units"] == "voxel"
    mesh_bytes, fmt, given = importers.from_bundle(data, name)
    assert fmt == "ply" and given["export"]["format"] == "ply"


def test_exporting_some_objects_exports_only_those():
    surface = _two_boxes()
    data, _, _ = export.export(surface, "obj", scale=(1, 1, 1), units="voxel", ids=[2],
                               bundle=False)
    assert data.count(b"\no object_") == 1 and b"o object_2" in data
    with pytest.raises(export.ExportRefused):
        export.export(surface, "obj", scale=(1, 1, 1), units="voxel", ids=[99], bundle=False)


# -- edits --------------------------------------------------------------------------------


def test_edits_keep_or_drop_objects_and_refuse_what_cannot_run():
    surface = _two_boxes()
    largest, note = refine.apply(surface, "keep_largest", {"n": 1})
    assert largest.ids().tolist() == [1] and "largest" in note
    small = refine.apply(surface, "remove_small", {"min_size": 500})[0]
    assert small.ids().tolist() == [1]
    assert refine.apply(surface, "delete", {"ids": [1]})[0].ids().tolist() == [2]
    with pytest.raises(refine.RefineRefused):
        refine.apply(surface, "delete", {"ids": [1, 2]})
    smoothed, _ = refine.apply(surface, "smooth", {"smoothing": "medium"})
    assert smoothed.positions_raw is not None


def test_decimating_without_the_extra_says_how_to_install_it(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_simplify(name, *args, **kwargs):
        if name == "fast_simplification":
            raise ImportError(name)
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_simplify)
    with pytest.raises(refine.RefineRefused) as refused:
        refine.apply(_two_boxes(), "decimate", {"ratio": 0.5})
    assert refused.value.install == refine.INSTALL


# -- geometry ------------------------------------------------------------------------------


def test_surface_neighbours_are_the_shortest_distance_between_meshes():
    surface = _two_boxes()
    pairs = contacts.surface_neighbours(surface, 1, 20.0)
    assert [o for o, _ in pairs] == [2]
    # Box 1 ends at x = 15 (voxel centres), box 2 starts at 24: about 8 voxels.
    assert pairs[0][1] == pytest.approx(math.dist((15, 15, 7), (24, 24, 7)), abs=1.5)
    assert contacts.surface_neighbours(surface, 1, 2.0) == []


def test_contact_area_is_the_mask_faces_the_cells_share():
    labels = np.zeros((6, 20, 20), np.int64)
    labels[1:5, 4:10, 4:10] = 3
    labels[1:5, 4:10, 10:16] = 8         # shares a 4 x 6 wall across x

    class Volume:
        depth, height, width = labels.shape

        def read(self, level, z0, z1, rows, cols):
            return labels[z0:z1 + 1, rows, cols]

    areas = contacts.contact_areas(Volume(), 3, (1, 4, 4, 9, 4, 9), spacing=(0.5, 0.5, 2.0))
    assert areas == {8: pytest.approx(4 * 6 * 0.5 * 2.0)}


def test_a_section_through_a_sphere_is_a_circle_of_its_radius():
    z, y, x = np.mgrid[:24, :48, :48].astype(np.float32)
    vol = (20 - np.sqrt((x - 24) ** 2 + (y - 24) ** 2 + (z - 12) ** 2)).astype(np.float32)
    surface, _ = isosurface.extract(lambda a, b: vol[a:b + 1], marching.Grid.of(vol.shape), 10.0)
    cut = sections.section(surface, "z", 12)
    segments = np.asarray(cut[1]).reshape(-1, 2)
    radius = np.hypot(segments[:, 0] - 24, segments[:, 1] - 24)
    assert radius.mean() == pytest.approx(10.0, abs=0.15)
    side = np.asarray(sections.section(surface, "y", 24)[1]).reshape(-1, 2)
    assert np.hypot(side[:, 0] - 24, side[:, 1] - 12).mean() == pytest.approx(10.0, abs=0.15)
    assert sections.section(surface, "z", 40) == {}


# -- through the app -------------------------------------------------------------------------


def test_routes_export_refine_and_undo_a_built_surface(tmp_path, client, surfaces_license):
    _project(tmp_path)
    layer_id = client.post("/volume/surf/surfaces", json=ISO).get_json()["layer"]
    assert _settle("surf", layer_id)["status"] == "ready"
    base = f"/volume/surf/surfaces/{layer_id}"
    exported = client.get(f"{base}/export?format=glb&bundle=0")
    assert exported.status_code == 200 and exported.data[:4] == b"glTF"
    assert "attachment" in exported.headers["Content-Disposition"]
    cut = client.get(f"{base}/sections?z=8&x=32").get_json()
    assert cut["xy"]["z"] == 8 and cut["xy"]["objects"]["1"]
    assert "yz" in cut and "xz" not in cut

    smoothed = client.post(f"{base}/refine", json={"op": "smooth", "smoothing": "strong"})
    assert smoothed.status_code == 200, smoothed.get_json()
    body = smoothed.get_json()
    assert body["versions"][-1]["op"] == "smooth" and len(body["summary"]["lods"]) == 2
    manifest = client.get(f"{base}/manifest").get_json()
    assert manifest["lods"][-1]["path"].endswith("-v1.bin")
    undone = client.post(f"{base}/refine", json={"op": "undo"}).get_json()
    assert undone["versions"] == [] and len(undone["summary"]["lods"]) == 1
    assert client.post(f"{base}/refine", json={"op": "undo"}).status_code == 400


def test_neighbours_and_import_are_paid_and_work(tmp_path, client, surfaces_license):
    _project(tmp_path)
    layer_id = client.post("/volume/surf/surfaces", json=ISO).get_json()["layer"]
    assert _settle("surf", layer_id)["status"] == "ready"
    near = client.get(f"/volume/surf/surfaces/{layer_id}/neighbours?object=1&within=5")
    assert near.status_code == 200 and near.get_json()["neighbours"] == []
    assert "not interaction" in near.get_json()["note"]

    data, _, _ = export.export(_two_boxes(), "ply", scale=(1, 1, 1), units="voxel", bundle=False)
    answer = client.post("/volume/surf/surfaces/import",
                         data={"file": (io.BytesIO(data), "boxes.ply"), "units": "voxel",
                               "label": "Imported boxes"},
                         content_type="multipart/form-data")
    assert answer.status_code == 201, answer.get_json()
    imported = answer.get_json()
    assert imported["objects"] == 2
    manifest = client.get(f"/volume/surf/surfaces/{imported['layer']}/manifest").get_json()
    assert manifest["type"] == "imported" and manifest["source"]["units"] == "voxel"
    mesh = mesh_mod.from_bytes(client.get(f"/volume/surf/surfaces/{imported['layer']}/mesh").data)
    assert mesh.ids().tolist() == [1, 2]

    surfaces_license.install(surfaces_license.issue(ent=["ai"]))
    assert client.get(f"/volume/surf/surfaces/{layer_id}/neighbours?object=1").status_code == 403
    assert client.post(f"/volume/surf/surfaces/{layer_id}/refine",
                       json={"op": "undo"}).status_code == 403
    # Export and sections are reads: still open.
    assert client.get(f"/volume/surf/surfaces/{layer_id}/export?format=stl").status_code == 200
    assert client.get(f"/volume/surf/surfaces/{layer_id}/sections?z=8").status_code == 200
