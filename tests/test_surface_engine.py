"""The surface engine against shapes whose answers are known.

Spheres and ellipsoids with anisotropic voxels: closed, the right volume and
area in microns, the same mesh whether marched whole or in strips, and
reproducible to the byte. Label surfaces: one closed surface per object, ids
kept, the clipped object flagged, the mask never touched.
"""

import hashlib
import math

import numpy as np
import pytest

from plexora.server.surface import isosurface, levels, marching, suggest
from plexora.server.surface import labels as label_surfaces
from plexora.server.surface import mesh as mesh_mod

SX, SZ = 0.5, 2.0   # µm per voxel in x/y and in z


def _sphere_volume(depth=20, size=64, radius_um=10.0):
    z, y, x = np.mgrid[:depth, :size, :size].astype(np.float32)
    centre = (depth / 2, size / 2, size / 2)
    r = np.sqrt(((x - centre[2]) * SX) ** 2 + ((y - centre[1]) * SX) ** 2
                + ((z - centre[0]) * SZ) ** 2)
    # A linear ramp: the 50 isovalue is the sphere of radius `radius_um`.
    return (100 * np.clip(1 - r / (2 * radius_um), 0, 1)).astype(np.float32)


def _iso(vol, level=50.0, **kwargs):
    grid = marching.Grid.of(vol.shape)
    kwargs.setdefault("scale", (SX, SX, SZ))
    return isosurface.extract(lambda a, b: vol[a:b + 1], grid, level, **kwargs)


def test_an_anisotropic_sphere_is_closed_and_measured_in_microns():
    surface, report = _iso(_sphere_volume())
    columns = mesh_mod.measure(surface, scale=(SX, SX, SZ), physical=True, dims=(64, 64, 20))
    assert report["pieces"] == 1 and surface.n_objects == 1
    assert bool(columns["closed"][0])
    assert columns["mesh_volume_um3"][0] == pytest.approx(4 / 3 * math.pi * 1000, rel=0.02)
    assert columns["mesh_area_um2"][0] == pytest.approx(4 * math.pi * 100, rel=0.03)
    assert columns["sphericity"][0] == pytest.approx(1.0, abs=0.02)
    assert columns["centroid_x"][0] == pytest.approx(32.0, abs=0.05)
    assert columns["centroid_z"][0] == pytest.approx(10.0, abs=0.05)
    assert not bool(columns["touches_bounds"][0])


def test_strips_weld_into_the_same_closed_mesh_as_one_pass():
    vol = _sphere_volume()
    whole, _ = _iso(vol, strip=64)
    strips, _ = _iso(vol, strip=3)
    assert strips.n_vertices == whole.n_vertices
    assert strips.n_triangles == whole.n_triangles
    assert len(mesh_mod.boundary_vertices(strips)) == 0
    # Euler characteristic of a sphere.
    edges = len(np.unique(np.sort(np.concatenate([strips.triangles[:, [0, 1]],
                                                  strips.triangles[:, [1, 2]],
                                                  strips.triangles[:, [2, 0]]]), axis=1),
                          axis=0))
    assert strips.n_vertices - edges + strips.n_triangles == 2


def test_the_same_request_makes_the_same_bytes():
    vol = _sphere_volume()
    a = mesh_mod.to_bytes(_iso(vol, smoothing=mesh_mod.Smoothing.preset("light"))[0])
    b = mesh_mod.to_bytes(_iso(vol, smoothing=mesh_mod.Smoothing.preset("light"))[0])
    assert hashlib.sha256(a).hexdigest() == hashlib.sha256(b).hexdigest()


def test_the_file_round_trips_and_aligns_its_arrays():
    surface, _ = _iso(_sphere_volume(), smoothing=mesh_mod.Smoothing.preset("medium"))
    data = mesh_mod.to_bytes(surface, {"level": 0})
    back = mesh_mod.from_bytes(data)
    assert np.array_equal(back.positions, surface.positions)
    assert np.array_equal(back.triangles, surface.triangles)
    assert np.array_equal(back.object_id, surface.object_id)
    assert back.positions_raw is not None
    assert back.header["level"] == 0 and back.header["vertices"] == surface.n_vertices
    for entry in back.header["arrays"]:
        assert entry["offset"] % 4 == 0, entry
    with pytest.raises(mesh_mod.MeshError):
        mesh_mod.from_bytes(b"nope" + data[4:])


def test_smoothing_is_capped_and_reported():
    surface, report = _iso(_sphere_volume(), smoothing=mesh_mod.Smoothing.preset("strong"))
    moved = np.abs(surface.positions - surface.positions_raw)
    # Half a voxel per axis, in voxels.
    assert moved.max() <= 0.5 + 1e-4
    assert report["smoothing"]["iterations"] == mesh_mod.Smoothing.PRESETS["strong"]
    assert report["smoothing"]["max_displacement"] > 0
    assert abs(report["volume_change_pct"]) < 3


def test_pieces_are_numbered_by_size_and_small_ones_dropped():
    vol = np.zeros((16, 48, 48), np.float32)
    vol[3:13, 5:25, 5:25] = 100     # big box
    vol[6:9, 34:38, 34:38] = 100    # small box
    big, report = _iso(vol, scale=(1, 1, 1))
    assert report["pieces"] == 2 and big.n_objects == 2
    columns = mesh_mod.measure(big)
    order = np.argsort(columns["object"])
    assert columns["mesh_volume_vox3"][order][0] > columns["mesh_volume_vox3"][order][1]
    kept, report = _iso(vol, scale=(1, 1, 1), min_size=200)
    assert report["dropped_small"] == 1 and kept.n_objects == 1


def test_a_structure_cut_by_the_edge_is_capped_and_flagged():
    vol = np.zeros((10, 32, 32), np.float32)
    vol[:, 8:20, 8:20] = 100        # runs through every plane
    surface, _ = _iso(vol, scale=(1, 1, 1))
    columns = mesh_mod.measure(surface, dims=(32, 32, 10))
    assert bool(columns["closed"][0]) and bool(columns["touches_bounds"][0])


def test_nothing_above_the_isovalue_says_so():
    with pytest.raises(marching.NoSurface):
        _iso(np.zeros((6, 16, 16), np.float32))


def test_preprocessing_runs_with_a_halo_so_strips_match_one_pass():
    rng = np.random.default_rng(1)
    vol = _sphere_volume() + rng.normal(0, 3, (20, 64, 64)).astype(np.float32)
    pre = isosurface.Preprocess(sigma_um=1.0)
    whole, _ = _iso(vol, pre=pre, voxel_um=(SX, SX, SZ), strip=64)
    strips, _ = _iso(vol, pre=pre, voxel_um=(SX, SX, SZ), strip=4)
    assert strips.n_triangles == whole.n_triangles
    np.testing.assert_allclose(np.sort(strips.positions, axis=0),
                               np.sort(whole.positions, axis=0), atol=1e-4)


def test_background_subtraction_lifts_a_dim_object_off_a_bright_bed():
    vol = np.full((12, 64, 64), 500.0, np.float32)
    vol += (400 * np.arange(64) / 63).astype(np.float32)   # uneven illumination
    vol[4:8, 10:20, 10:20] += 150    # dim object on the dim half
    vol[4:8, 10:20, 44:54] += 150    # same object on the bright half
    pre = isosurface.Preprocess(background_um=12.0)
    surface, report = _iso(vol, level=60.0, pre=pre, voxel_um=(1, 1, 1), scale=(1, 1, 1),
                           min_size=50)
    assert surface.n_objects == 2, report


def test_grids_map_coarse_voxels_onto_level_zero():
    grid = marching.Grid.of((4, 10, 10), factor_xy=(4, 4), z_stride=2, origin=(3, 5, 6))
    p = grid.to_level0(np.array([[0, 0, 0], [1, 1, 1]], np.float32))
    # Column 3 of a 4x level covers level-0 columns 12..15, centre 13.5.
    assert p[0].tolist() == pytest.approx([13.5, 21.5, 6.5])
    assert p[1].tolist() == pytest.approx([17.5, 25.5, 8.5])


# -- label surfaces --------------------------------------------------------------------


def _ellipsoid(labels, centre, radii, label):
    zz, yy, xx = np.ogrid[:labels.shape[0], :labels.shape[1], :labels.shape[2]]
    inside = (((zz - centre[0]) / radii[0]) ** 2 + ((yy - centre[1]) / radii[1]) ** 2
              + ((xx - centre[2]) / radii[2]) ** 2) <= 1.0
    labels[inside] = label
    return int(inside.sum())


class _Labels:
    def __init__(self, array):
        self.array = array
        self.depth, self.height, self.width = array.shape

    def level_shapes(self):
        return [[self.height, self.width]]

    def read(self, level, z0, z1, rows, cols):
        return self.array[z0:z1 + 1, rows, cols]


def _label_scene():
    labels = np.zeros((20, 128, 128), np.uint16)
    counts = {1: _ellipsoid(labels, (8, 40, 40), (5, 12, 12), 1),
              7: _ellipsoid(labels, (10, 90, 85), (6, 15, 10), 7),
              3: _ellipsoid(labels, (0, 100, 20), (3, 8, 8), 3)}   # clipped at z 0
    return labels, counts


def test_every_object_gets_its_own_closed_surface_under_its_id():
    labels, counts = _label_scene()
    before = labels.copy()
    volume = _Labels(labels)
    ids, boxes, voxels = label_surfaces.object_boxes(volume, 0)
    assert ids.tolist() == [1, 3, 7]
    assert dict(zip(ids.tolist(), voxels.tolist())) == counts
    surface, report = label_surfaces.extract(
        volume, 0, marching.Grid.of(labels.shape), ids, boxes,
        smoothing=mesh_mod.Smoothing.preset("medium"))
    assert surface.ids().tolist() == [1, 3, 7] and report["missing_count"] == 0
    assert np.array_equal(labels, before)
    columns = mesh_mod.measure(surface, dims=(128, 128, 20))
    assert columns["closed"].all()
    for label, volume_mesh, touches in zip(columns["object"], columns["mesh_volume_vox3"],
                                           columns["touches_bounds"]):
        # The surface of the voxels, a little inside them (smoothing, inset).
        assert volume_mesh == pytest.approx(counts[int(label)], rel=0.15), label
        assert bool(touches) == (int(label) == 3)


def test_a_selection_makes_only_those_surfaces_and_the_inset_is_applied():
    labels, _ = _label_scene()
    volume = _Labels(labels)
    ids, boxes, _ = label_surfaces.object_boxes(volume, 0, ids=[7])
    plain, _ = label_surfaces.extract(volume, 0, marching.Grid.of(labels.shape), ids, boxes,
                                      inset=0.0)
    inset, report = label_surfaces.extract(volume, 0, marching.Grid.of(labels.shape), ids,
                                           boxes, inset=0.25)
    assert plain.ids().tolist() == [7] and report["inset_vox"] == 0.25
    assert mesh_mod.signed_volume(inset.positions, inset.triangles) < \
        mesh_mod.signed_volume(plain.positions, plain.triangles)


def test_neighbours_that_touch_stay_two_surfaces():
    labels = np.zeros((8, 32, 32), np.uint16)
    labels[2:6, 8:16, 8:16] = 4
    labels[2:6, 8:16, 16:24] = 5     # shares a face with 4
    volume = _Labels(labels)
    ids, boxes, _ = label_surfaces.object_boxes(volume, 0)
    surface, _ = label_surfaces.extract(volume, 0, marching.Grid.of(labels.shape), ids, boxes)
    assert surface.ids().tolist() == [4, 5]
    columns = mesh_mod.measure(surface)
    assert columns["closed"].all()
    a = surface.positions[surface.object_id == 4][:, 0].max()
    b = surface.positions[surface.object_id == 5][:, 0].min()
    assert a < b                     # the inset parts them


# -- levels and suggestions ------------------------------------------------------------


def test_the_label_triangle_estimate_is_within_a_quarter():
    labels, counts = _label_scene()
    volume = _Labels(labels)
    ids, boxes, voxels = label_surfaces.object_boxes(volume, 0)
    surface, _ = label_surfaces.extract(volume, 0, marching.Grid.of(labels.shape), ids, boxes)
    estimate = levels.label_triangles(voxels)
    assert estimate == pytest.approx(surface.n_triangles, rel=0.25)


def test_too_many_triangles_is_refused_with_the_estimate():
    class Big(_Labels):
        def level_shapes(self):
            return [[4096, 4096]]

    with pytest.raises(levels.SurfaceRefused) as refused:
        levels.plan_labels(Big(np.zeros((2, 2, 2), np.uint16)), np.full(2_000_000, 4000),
                           quality="standard")
    assert refused.value.estimate["triangles"] > levels.RESIDENT_TRIANGLES


def test_regions_are_clamped_and_whole_extents_drop_out():
    region = levels.Region.from_dict({"box": [-10, 5, 50, 40], "z": [3, 99]},
                                     width=40, height=60, depth=10)
    assert region.box == (0, 5, 40, 45) and region.planes == (3, 9)
    assert levels.Region.from_dict({"box": [0, 0, 40, 60]}, width=40, height=60,
                                   depth=10).to_dict() is None
    with pytest.raises(levels.SurfaceRefused):
        levels.Region.from_dict({"box": [100, 100, 5, 5]}, width=40, height=60, depth=10)


def test_suggestions_split_two_populations_in_raw_units():
    rng = np.random.default_rng(0)
    block = np.concatenate([rng.normal(200, 20, 90_000), rng.normal(1500, 100, 10_000)])
    answer = suggest.suggest(block.astype(np.float32))
    for name in ("otsu", "triangle", "gmm"):
        assert 250 < answer["thresholds"][name] < 1400, (name, answer["thresholds"])
    assert answer["default"] == answer["thresholds"]["otsu"]
    assert len(answer["histogram"]["counts"]) == suggest.BINS
